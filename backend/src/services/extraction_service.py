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


def get_field_config(field_name: str) -> dict | None:
    """Look up config for a specific field from the YAML."""
    for field_def in _CONFIG.get("fields", []):
        if field_def.get("name") == field_name:
            return field_def
    return None


def get_confidence_threshold(field_name: str) -> float:
    """Get the confidence threshold for a field (from YAML or default 0.70)."""
    cfg = get_field_config(field_name)
    if cfg:
        return cfg.get("confidence_threshold", 0.70)
    return 0.70


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


class GeminiInvoiceSchema(BaseModel):
    """Invoice schema sent to Gemini for structured extraction."""

    vendor: str = Field(description="Vendor / supplier company name")
    invoice_number: str = Field(description="Invoice number or ID")
    date: str = Field(description="Invoice date in YYYY-MM-DD format")
    due_date: str = Field(
        description="Payment due date in YYYY-MM-DD format, or empty string if not found"
    )
    subtotal: float = Field(description="Subtotal before tax (0 if not found)")
    tax_rate: float = Field(
        description="Tax/VAT rate as percentage, e.g. 20.0 for 20%  (0 if not found)"
    )
    tax_amount: float = Field(description="Tax amount (0 if no tax)")
    total: float = Field(description="Grand total including tax")
    currency: str = Field(description="ISO 4217 currency code, e.g. USD, EUR, CHF")
    line_items: list[GeminiLineItem] = Field(
        description="All line items on the invoice"
    )


# Extraction prompt

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

# Helpers


def compute_file_hash(path: str | Path) -> str:
    """SHA-256 hash of file contents (used for idempotency)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# Confidence estimation


def _estimate_confidence(invoice: GeminiInvoiceSchema) -> list[FieldConfidence]:
    """Heuristic confidence scoring based on field completeness & validity.

    Uses YAML-defined confidence thresholds as base scores. Penalises empty,
    zero, or suspiciously short values. Applies cross-field validation from
    the YAML config (e.g. total ≈ subtotal + tax_amount).
    """
    scores: list[FieldConfidence] = []

    def _score(name: str, value: Any) -> FieldConfidence:
        """Give a score based on YAML threshold, penalise empty / zero / suspicious."""
        base = get_confidence_threshold(name)
        conf = base
        if value is None or value == "" or value == 0:
            conf = 0.40
        elif isinstance(value, str) and len(value) < 2:
            conf = 0.60
        # Validate against YAML rules
        field_cfg = get_field_config(name)
        if field_cfg and value and conf > 0.50:
            validation = field_cfg.get("validation", {})
            # Pattern check
            pattern = validation.get("pattern")
            if pattern and isinstance(value, str):
                if not re.match(pattern, value):
                    conf = min(conf, 0.55)
            # Enum check
            enum_vals = validation.get("enum")
            if enum_vals and isinstance(value, str) and value not in enum_vals:
                conf = min(conf, 0.50)
            # Min/max for numeric
            if isinstance(value, (int, float)):
                min_val = validation.get("min")
                max_val = validation.get("max")
                if min_val is not None and value < min_val:
                    conf = min(conf, 0.45)
                if max_val is not None and value > max_val:
                    conf = min(conf, 0.45)
            # String length check
            if isinstance(value, str):
                min_len = validation.get("min_length")
                max_len = validation.get("max_length")
                if min_len is not None and len(value) < min_len:
                    conf = min(conf, 0.50)
                if max_len is not None and len(value) > max_len:
                    conf = min(conf, 0.60)

        return FieldConfidence(field_name=name, value=value, confidence=round(conf, 2))

    scores.append(_score("vendor", invoice.vendor))
    scores.append(_score("invoice_number", invoice.invoice_number))
    scores.append(_score("date", invoice.date))
    scores.append(_score("due_date", invoice.due_date))
    scores.append(_score("subtotal", invoice.subtotal))
    scores.append(_score("tax_rate", invoice.tax_rate))
    scores.append(_score("tax_amount", invoice.tax_amount))
    scores.append(_score("total", invoice.total))
    scores.append(_score("currency", invoice.currency))

    # Cross-field validation from YAML config
    cross_rules = _CONFIG.get("validation", {}).get("cross_field", [])
    for rule in cross_rules:
        tolerance = rule.get("tolerance", 0.02)
        rule_str = rule.get("rule", "")

        # total ≈ subtotal + tax_amount
        if "total" in rule_str and "subtotal" in rule_str and "tax_amount" in rule_str:
            if invoice.subtotal and invoice.tax_amount and invoice.total:
                expected = invoice.subtotal + invoice.tax_amount
                if abs(expected - invoice.total) / max(invoice.total, 1) < tolerance:
                    for s in scores:
                        if s.field_name in ("total", "subtotal", "tax_amount"):
                            s.confidence = min(1.0, s.confidence + 0.05)
                else:
                    for s in scores:
                        if s.field_name in ("total", "subtotal", "tax_amount"):
                            s.confidence = max(0.40, s.confidence - 0.10)

        # sum(line_items.total) ≈ subtotal
        if "line_items" in rule_str and "subtotal" in rule_str:
            if invoice.line_items and invoice.subtotal:
                li_sum = sum(li.total for li in invoice.line_items)
                if (
                    abs(li_sum - invoice.subtotal) / max(invoice.subtotal, 1)
                    < tolerance
                ):
                    pass  # Line items handled below with boost
                else:
                    # Penalise slightly
                    pass

    # Line-item consistency
    if invoice.line_items:
        li_total = sum(li.total for li in invoice.line_items)
        cross_tolerance = 0.05
        for rule in cross_rules:
            if "line_items" in rule.get("rule", ""):
                cross_tolerance = rule.get("tolerance", 0.05)
                break

        if (
            invoice.subtotal
            and abs(li_total - invoice.subtotal) / max(invoice.subtotal, 1)
            < cross_tolerance
        ):
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
        scores.append(
            FieldConfidence(field_name="line_items", value=[], confidence=0.50)
        )

    return scores


# Post-extraction validation


def _validate_extraction(invoice: GeminiInvoiceSchema) -> list[str]:
    """Validate extracted data against YAML-defined rules.

    Returns a list of validation warnings (empty = clean).
    """
    warnings: list[str] = []

    # Check required fields
    required_for_approval = _CONFIG.get("validation", {}).get(
        "required_fields_for_approval", []
    )
    for field_name in required_for_approval:
        value = getattr(invoice, field_name, None)
        if not value or value == "" or value == 0:
            warnings.append(f"Required field '{field_name}' is empty or zero")

    # Cross-field checks
    cross_rules = _CONFIG.get("validation", {}).get("cross_field", [])
    for rule in cross_rules:
        tolerance = rule.get("tolerance", 0.02)
        rule_str = rule.get("rule", "")

        if "total" in rule_str and "subtotal" in rule_str and "tax_amount" in rule_str:
            if invoice.subtotal and invoice.tax_amount and invoice.total:
                expected = invoice.subtotal + invoice.tax_amount
                diff = abs(expected - invoice.total) / max(invoice.total, 1)
                if diff > tolerance:
                    warnings.append(
                        f"Total ({invoice.total}) ≠ subtotal ({invoice.subtotal}) + "
                        f"tax ({invoice.tax_amount}) — diff {diff:.1%}"
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

        # 6. Post-extraction validation against YAML rules
        validation_warnings = _validate_extraction(gemini_invoice)
        if validation_warnings:
            logger.warning(
                "Validation warnings for %s: %s",
                document_id,
                "; ".join(validation_warnings),
            )
            # Penalise overall confidence if there are warnings
            penalty = min(len(validation_warnings) * 0.03, 0.15)
            overall = max(0.0, overall - penalty)

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
