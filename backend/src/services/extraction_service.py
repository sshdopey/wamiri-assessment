"""Invoice extraction service using Google Gemini with structured output.

This module sends uploaded documents (PDFs or images) directly to Gemini
as inline base64 content for structured data extraction, validates the
results against the YAML-defined schema, and returns typed InvoiceData
with per-field confidence scores.

Configuration is loaded from ``configs/extraction_module_schema.yaml``
which defines field-level validation rules, confidence thresholds,
cross-field checks, and processing parameters.

Supported formats: PDF, PNG, JPEG, WebP, GIF, TIFF, BMP.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

import yaml
from google import genai
from pydantic import BaseModel, Field

from src.config import settings
from src.models.schemas import (
    ExtractionResult,
    FieldConfidence,
    InvoiceData,
    LineItem,
)
from src.services.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)

# Circuit breaker for Gemini API calls — opens after 5 consecutive failures,
# recovers after 60s, probes with 2 calls in HALF_OPEN before closing.
_gemini_breaker = CircuitBreaker(
    name="gemini_api",
    failure_threshold=5,
    recovery_timeout_seconds=60.0,
    half_open_max_calls=2,
)

# Load YAML configuration

_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "configs"
    / "extraction_module_schema.yaml"
)


def _load_extraction_config() -> dict:
    """Load and cache the extraction module YAML configuration."""
    try:
        with open(_CONFIG_PATH) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning(
            "Extraction config not found at %s — using defaults", _CONFIG_PATH
        )
        return {}


_CONFIG: dict = _load_extraction_config()


# Supported file types

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


# Gemini structured-output schema
# We mirror InvoiceData but define a separate model so Gemini sees clean JSON
# Schema without optional wrappers that confuse it.


class GeminiLineItem(BaseModel):
    """Line item schema for Gemini structured output."""

    item: str = Field(description="Item/product description")
    quantity: int = Field(description="Quantity ordered")
    unit_price: float = Field(description="Price per unit")
    total: float = Field(description="Line item total")


class GeminiFieldWithConfidence(BaseModel):
    """A single extracted field with AI-assessed confidence."""

    value: str = Field(description="The extracted value as a string")
    confidence: float = Field(
        description=(
            "Your confidence that this value is correct, from 0.0 to 1.0. "
            "1.0 means you are certain. 0.5 means you are guessing. "
            "Consider: is the text clearly legible? Could you misread a digit? "
            "Is this field actually present on the invoice or are you inferring it?"
        )
    )


class GeminiInvoiceSchema(BaseModel):
    """Invoice schema sent to Gemini for structured extraction."""

    vendor: GeminiFieldWithConfidence = Field(
        description="Vendor / supplier company name"
    )
    invoice_number: GeminiFieldWithConfidence = Field(
        description="Invoice number or ID"
    )
    date: GeminiFieldWithConfidence = Field(
        description="Invoice date in YYYY-MM-DD format"
    )
    due_date: GeminiFieldWithConfidence = Field(
        description="Payment due date in YYYY-MM-DD format, or empty string if not found"
    )
    subtotal: GeminiFieldWithConfidence = Field(
        description="Subtotal before tax as a number (0 if not found)"
    )
    tax_rate: GeminiFieldWithConfidence = Field(
        description="Tax/VAT rate as percentage number, e.g. 20.0 for 20% (0 if not found)"
    )
    tax_amount: GeminiFieldWithConfidence = Field(
        description="Tax amount as a number (0 if no tax)"
    )
    total: GeminiFieldWithConfidence = Field(
        description="Grand total including tax as a number"
    )
    currency: GeminiFieldWithConfidence = Field(
        description="ISO 4217 currency code, e.g. USD, EUR, CHF"
    )
    line_items: list[GeminiLineItem] = Field(
        description="All line items on the invoice"
    )
    line_items_confidence: float = Field(
        description=(
            "Your overall confidence in the line items extraction from 0.0 to 1.0. "
            "Consider: did you capture every line item? Are quantities and prices correct?"
        )
    )


# Extraction prompt

_EXTRACTION_PROMPT = """You are an expert invoice data-extraction assistant.

Carefully examine this invoice document (it may be a PDF or an image) and
extract ALL of the following fields.  If a field is not present on the
document, use a sensible default (empty string for text, 0 for numbers).

For EACH field, you must also provide a confidence score between 0.0 and 1.0
reflecting how certain you are about that extraction:
- 0.95-1.0: The value is clearly printed and unambiguous.
- 0.80-0.94: High confidence but minor ambiguity (e.g. slightly blurry text).
- 0.60-0.79: Moderate confidence — the value is partially visible or inferred.
- 0.30-0.59: Low confidence — you are guessing or the field is not on the document.
- 0.0-0.29: Very low confidence — the field is missing and you used a default.

Rules:
- Dates must be in YYYY-MM-DD format.
- Currency must be an ISO 4217 code (USD, EUR, GBP, CHF, etc.).
- tax_rate is a percentage (e.g. 20.0 means 20%).
- Include every single line item — do not truncate.
- For multi-page invoices, merge all line items into one list.
- Quantity should be an integer; round if necessary.
- If a field (like tax or due_date) genuinely does not exist on the invoice,
  set its value to default (0 or empty string) and give it a HIGH confidence
  (0.85+) since you are confident it is absent — low confidence means you
  think the field IS there but you cannot read it.

Return a JSON object matching the provided schema exactly.
"""

# Helpers


def compute_file_hash(path: str | Path) -> str:
    """SHA-256 hash of file contents (used for idempotency)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _to_float(val: Any) -> float:
    """Safely convert a string or numeric value to float."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            # Strip currency symbols, commas, whitespace
            cleaned = re.sub(r"[^\d.\-]", "", val.strip())
            return float(cleaned) if cleaned else 0.0
        except (ValueError, TypeError):
            return 0.0
    return 0.0


# Confidence estimation


def _build_field_confidences(invoice: GeminiInvoiceSchema) -> list[FieldConfidence]:
    """Build FieldConfidence list directly from AI-provided confidence scores."""
    header_fields = [
        "vendor",
        "invoice_number",
        "date",
        "due_date",
        "subtotal",
        "tax_rate",
        "tax_amount",
        "total",
        "currency",
    ]
    scores: list[FieldConfidence] = []
    for name in header_fields:
        field_obj: GeminiFieldWithConfidence = getattr(invoice, name)
        # Clamp confidence to [0, 1]
        conf = max(0.0, min(1.0, field_obj.confidence))
        scores.append(
            FieldConfidence(
                field_name=name,
                value=field_obj.value,
                confidence=round(conf, 2),
            )
        )

    # Line items — use the AI's overall line_items_confidence
    li_conf = max(0.0, min(1.0, invoice.line_items_confidence))
    scores.append(
        FieldConfidence(
            field_name="line_items",
            value=[li.model_dump() for li in invoice.line_items]
            if invoice.line_items
            else [],
            confidence=round(li_conf, 2),
        )
    )

    return scores


# Post-extraction validation


def _validate_extraction(invoice: GeminiInvoiceSchema) -> list[str]:
    """Validate extracted data against YAML-defined rules."""
    warnings: list[str] = []

    # Check required fields
    required_for_approval = _CONFIG.get("validation", {}).get(
        "required_fields_for_approval", []
    )
    for field_name in required_for_approval:
        field_obj = getattr(invoice, field_name, None)
        if field_obj is None:
            warnings.append(f"Required field '{field_name}' is missing")
            continue
        val = (
            field_obj.value
            if isinstance(field_obj, GeminiFieldWithConfidence)
            else field_obj
        )
        if not val or val == "" or val == "0" or val == "0.0":
            warnings.append(f"Required field '{field_name}' is empty or zero")

    # Cross-field checks
    cross_rules = _CONFIG.get("validation", {}).get("cross_field", [])
    for rule in cross_rules:
        tolerance = rule.get("tolerance", 0.02)
        rule_str = rule.get("rule", "")

        if "total" in rule_str and "subtotal" in rule_str and "tax_amount" in rule_str:
            subtotal = _to_float(invoice.subtotal.value)
            tax_amount = _to_float(invoice.tax_amount.value)
            total = _to_float(invoice.total.value)
            if subtotal and tax_amount and total:
                expected = subtotal + tax_amount
                diff = abs(expected - total) / max(total, 1)
                if diff > tolerance:
                    warnings.append(
                        f"Total ({total}) != subtotal ({subtotal}) + "
                        f"tax ({tax_amount}) — diff {diff:.1%}"
                    )

    return warnings


# Main extraction service


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

            # Circuit breaker protects against cascading failures
            with _gemini_breaker:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=contents,
                    config={
                        "response_mime_type": "application/json",
                        "response_json_schema": GeminiInvoiceSchema.model_json_schema(),
                        "temperature": 0.0,  # deterministic for idempotency
                    },
                )
        except CircuitOpenError:
            logger.error(
                "Circuit breaker OPEN for Gemini API — skipping %s", document_id
            )
            raise
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
            vendor=gemini_invoice.vendor.value,
            invoice_number=gemini_invoice.invoice_number.value,
            date=gemini_invoice.date.value,
            due_date=gemini_invoice.due_date.value or None,
            subtotal=_to_float(gemini_invoice.subtotal.value),
            tax_rate=_to_float(gemini_invoice.tax_rate.value),
            tax_amount=_to_float(gemini_invoice.tax_amount.value),
            total=_to_float(gemini_invoice.total.value),
            currency=gemini_invoice.currency.value,
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

        # 5. Build confidence scores from AI response
        field_confidences = _build_field_confidences(gemini_invoice)
        overall = (
            sum(fc.confidence for fc in field_confidences) / len(field_confidences)
            if field_confidences
            else 0.0
        )

        # 6. Post-extraction validation
        validation_warnings = _validate_extraction(gemini_invoice)
        if validation_warnings:
            logger.warning(
                "Validation warnings for %s: %s",
                document_id,
                "; ".join(validation_warnings),
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
