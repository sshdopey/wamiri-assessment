"""Invoice extraction service using Google Gemini with structured output.

This module sends uploaded documents (PDFs or images) directly to Gemini
as inline base64 content for structured data extraction, validates the
results, and returns typed InvoiceData with per-field confidence scores.

Supported formats: PDF, PNG, JPEG, WebP, GIF, TIFF, BMP.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from google import genai
from pydantic import BaseModel, Field

from src.config import settings
from src.models.schemas import (
    ExtractionResult,
    FieldConfidence,
    InvoiceData,
    LineItem,
)

logger = logging.getLogger(__name__)

# ── Supported file types ─────────────────────────────────────────────────────

SUPPORTED_MIME_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".bmp": "image/bmp",
}


def get_mime_type(file_path: Path) -> str:
    """Determine MIME type from file extension."""
    ext = file_path.suffix.lower()
    mime = SUPPORTED_MIME_TYPES.get(ext)
    if mime:
        return mime
    # Fallback to mimetypes stdlib
    guessed, _ = mimetypes.guess_type(str(file_path))
    if guessed:
        return guessed
    raise ValueError(f"Unsupported file type: {ext}")


# ── Gemini structured-output schema ──────────────────────────────────────────
# We mirror InvoiceData but define a separate model so Gemini sees clean JSON
# Schema without optional wrappers that confuse it.


class GeminiLineItem(BaseModel):
    """Line item schema for Gemini structured output."""

    item: str = Field(description="Item/product description")
    quantity: int = Field(description="Quantity ordered")
    unit_price: float = Field(description="Price per unit")
    total: float = Field(description="Line item total")


class GeminiInvoiceSchema(BaseModel):
    """Invoice schema sent to Gemini for structured extraction."""

    vendor: str = Field(description="Vendor / supplier company name")
    invoice_number: str = Field(description="Invoice number or ID")
    date: str = Field(description="Invoice date in YYYY-MM-DD format")
    due_date: str = Field(description="Payment due date in YYYY-MM-DD format, or empty string if not found")
    subtotal: float = Field(description="Subtotal before tax (0 if not found)")
    tax_rate: float = Field(description="Tax/VAT rate as percentage, e.g. 20.0 for 20%  (0 if not found)")
    tax_amount: float = Field(description="Tax amount (0 if no tax)")
    total: float = Field(description="Grand total including tax")
    currency: str = Field(description="ISO 4217 currency code, e.g. USD, EUR, CHF")
    line_items: list[GeminiLineItem] = Field(description="All line items on the invoice")


# ── Extraction prompt ────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """You are an expert invoice data-extraction assistant.

Carefully examine this invoice document (it may be a PDF or an image) and
extract ALL of the following fields.  If a field is not present, use a
sensible default (empty string for text, 0 for numbers).

Rules:
- Dates must be in YYYY-MM-DD format.
- Currency must be an ISO 4217 code (USD, EUR, GBP, CHF, etc.).
- tax_rate is a percentage (e.g. 20.0 means 20 %).
- Include every single line item — do not truncate.
- For multi-page invoices, merge all line items into one list.
- Quantity should be an integer; round if necessary.

Return a JSON object matching the provided schema exactly.
"""

# ── Helpers ──────────────────────────────────────────────────────────────────


def compute_file_hash(path: str | Path) -> str:
    """SHA-256 hash of file contents (used for idempotency)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Confidence estimation ────────────────────────────────────────────────────


def _estimate_confidence(invoice: GeminiInvoiceSchema) -> list[FieldConfidence]:
    """Heuristic confidence scoring based on field completeness & validity.

    Since Gemini structured output doesn't provide per-field confidence
    natively, we derive scores from data quality signals.
    """
    scores: list[FieldConfidence] = []

    def _score(name: str, value: Any, base: float = 0.90) -> FieldConfidence:
        """Give a base score, penalise empty / zero / suspicious values."""
        conf = base
        if value is None or value == "" or value == 0:
            conf = 0.40
        elif isinstance(value, str) and len(value) < 2:
            conf = 0.60
        return FieldConfidence(field_name=name, value=value, confidence=round(conf, 2))

    scores.append(_score("vendor", invoice.vendor, 0.92))
    scores.append(_score("invoice_number", invoice.invoice_number, 0.93))
    scores.append(_score("date", invoice.date, 0.90))
    scores.append(_score("due_date", invoice.due_date, 0.80))
    scores.append(_score("subtotal", invoice.subtotal, 0.85))
    scores.append(_score("tax_rate", invoice.tax_rate, 0.80))
    scores.append(_score("tax_amount", invoice.tax_amount, 0.82))
    scores.append(_score("total", invoice.total, 0.95))
    scores.append(_score("currency", invoice.currency, 0.88))

    # Cross-field validation bumps / penalties
    if invoice.subtotal and invoice.tax_amount:
        expected_total = invoice.subtotal + invoice.tax_amount
        if invoice.total and abs(expected_total - invoice.total) / max(invoice.total, 1) < 0.02:
            # total matches subtotal+tax → boost
            for s in scores:
                if s.field_name in ("total", "subtotal", "tax_amount"):
                    s.confidence = min(1.0, s.confidence + 0.05)

    # Line-item consistency
    if invoice.line_items:
        li_total = sum(li.total for li in invoice.line_items)
        if invoice.subtotal and abs(li_total - invoice.subtotal) / max(invoice.subtotal, 1) < 0.05:
            li_conf = 0.90
        else:
            li_conf = 0.70
        scores.append(
            FieldConfidence(
                field_name="line_items",
                value=[li.model_dump() for li in invoice.line_items],
                confidence=round(li_conf, 2),
            )
        )
    else:
        scores.append(FieldConfidence(field_name="line_items", value=[], confidence=0.50))

    return scores


# ── Main extraction service ──────────────────────────────────────────────────


class ExtractionService:
    """Stateless service that extracts structured invoice data from documents.

    Supports both PDF and image files.  Files are sent directly to Gemini
    as inline base64 content — no intermediate conversion is performed.
    """

    def __init__(self) -> None:
        api_key = settings.gemini_api_key
        if not api_key:
            logger.warning("GEMINI_API_KEY not set — extraction will fail at runtime")
        self._client = genai.Client(api_key=api_key) if api_key else None
        self._model = settings.gemini_model

    # ──────────────────────────────────────────────────────────────────────

    def extract(
        self,
        file_path: str | Path,
        document_id: str,
    ) -> ExtractionResult:
        """Extract invoice data from *file_path* and return an ExtractionResult.

        The file can be a PDF or an image.  It is sent to Gemini as inline
        base64 bytes using ``Part.from_bytes`` — no conversion is performed.

        Steps:
        1. Read file bytes and detect MIME type.
        2. Send file bytes + prompt to Gemini with structured-output schema.
        3. Parse & validate the response.
        4. Compute per-field confidence scores.
        """
        file_path = Path(file_path)
        t0 = time.time()

        # 1. Read file bytes and detect MIME type
        if not file_path.exists():
            raise FileNotFoundError(f"Document not found: {file_path}")

        try:
            file_bytes = file_path.read_bytes()
            mime_type = get_mime_type(file_path)
        except Exception as exc:
            logger.error("Failed to read document %s: %s", file_path, exc)
            raise RuntimeError(f"Failed to read document: {exc}") from exc

        logger.info(
            "Sending %s (%s, %.1f KB) to Gemini inline",
            file_path.name,
            mime_type,
            len(file_bytes) / 1024,
        )

        # 2. Call Gemini with inline base64
        if self._client is None:
            raise RuntimeError("Gemini API key is not configured")

        try:
            contents: list[Any] = [
                genai.types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                _EXTRACTION_PROMPT,
            ]

            response = self._client.models.generate_content(
                model=self._model,
                contents=contents,
                config={
                    "response_mime_type": "application/json",
                    "response_json_schema": GeminiInvoiceSchema.model_json_schema(),
                    "temperature": 0.0,  # deterministic for idempotency
                },
            )
        except Exception as exc:
            logger.error("Gemini API error for %s: %s", document_id, exc)
            raise RuntimeError(f"Gemini extraction failed: {exc}") from exc

        # 3. Parse structured response
        try:
            raw_text = response.text
            gemini_invoice = GeminiInvoiceSchema.model_validate_json(raw_text)
        except Exception as exc:
            logger.error("Invalid Gemini response for %s: %s", document_id, exc)
            raise RuntimeError(f"Failed to parse Gemini response: {exc}") from exc

        # 4. Convert to domain model
        invoice_data = InvoiceData(
            vendor=gemini_invoice.vendor,
            invoice_number=gemini_invoice.invoice_number,
            date=gemini_invoice.date,
            due_date=gemini_invoice.due_date or None,
            subtotal=gemini_invoice.subtotal,
            tax_rate=gemini_invoice.tax_rate,
            tax_amount=gemini_invoice.tax_amount,
            total=gemini_invoice.total,
            currency=gemini_invoice.currency,
            line_items=[
                LineItem(
                    item=li.item,
                    quantity=li.quantity,
                    unit_price=li.unit_price,
                    total=li.total,
                )
                for li in gemini_invoice.line_items
            ],
        )

        # 5. Confidence scores
        field_confidences = _estimate_confidence(gemini_invoice)
        overall = (
            sum(fc.confidence for fc in field_confidences) / len(field_confidences)
            if field_confidences
            else 0.0
        )

        elapsed = time.time() - t0
        content_hash = compute_file_hash(file_path)

        result = ExtractionResult(
            document_id=document_id,
            filename=file_path.name,
            invoice_data=invoice_data,
            field_confidences=field_confidences,
            overall_confidence=round(overall, 3),
            processing_time_seconds=round(elapsed, 2),
            content_hash=content_hash,
        )

        logger.info(
            "Extracted %s in %.1fs (confidence=%.2f)",
            document_id,
            elapsed,
            overall,
        )
        return result
