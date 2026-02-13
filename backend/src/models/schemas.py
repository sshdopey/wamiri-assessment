"""Pydantic models for the document processing system."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class DocumentStatus(str, enum.Enum):
    """Processing status for documents."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    REVIEW_PENDING = "review_pending"


class ReviewStatus(str, enum.Enum):
    """Status for review queue items."""

    PENDING = "pending"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class ReviewAction(str, enum.Enum):
    """Actions a reviewer can take."""

    APPROVE = "approve"
    CORRECT = "correct"
    REJECT = "reject"


# Line Item


class LineItem(BaseModel):
    """A single line item on an invoice."""

    item: str = Field(..., description="Item description")
    quantity: int = Field(..., ge=0, description="Quantity")
    unit_price: float = Field(..., description="Unit price")
    total: float = Field(..., description="Line total")


# Invoice Data (Gemini output schema)


class InvoiceData(BaseModel):
    """Structured invoice data extracted by Gemini."""

    vendor: Optional[str] = None
    invoice_number: Optional[str] = None
    date: Optional[str] = None
    due_date: Optional[str] = None
    subtotal: Optional[float] = None
    tax_rate: Optional[float] = None
    tax_amount: Optional[float] = None
    total: Optional[float] = None
    currency: Optional[str] = None
    line_items: list[LineItem] = Field(default_factory=list)


# Extraction Result


class FieldConfidence(BaseModel):
    """Confidence score for a single extracted field."""

    field_name: str
    value: Optional[str | float | int | list] = None
    confidence: float = Field(..., ge=0, le=1, description="0-1 confidence")


class ExtractionResult(BaseModel):
    """Full result of extracting data from a document."""

    document_id: str
    filename: str
    invoice_data: InvoiceData
    field_confidences: list[FieldConfidence] = Field(default_factory=list)
    overall_confidence: float = Field(0.0, ge=0, le=1)
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    processing_time_seconds: float = 0.0
    content_hash: Optional[str] = None
    schema_version: str = Field(
        default="1.0.0",
        description="Schema version for backward compatibility",
    )


# Review Queue Models


class ExtractedField(BaseModel):
    """An extracted field stored in the review system."""

    id: str
    review_item_id: str
    field_name: str
    value: Optional[str] = None
    confidence: float = 0.0
    manually_corrected: bool = False
    corrected_at: Optional[datetime] = None
    corrected_by: Optional[str] = None
    locked: bool = False


class ReviewItem(BaseModel):
    """A single item in the human-review queue."""

    id: str
    document_id: str
    filename: str
    status: ReviewStatus = ReviewStatus.PENDING
    priority: float = 0.0
    sla_deadline: Optional[datetime] = None
    assigned_to: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    claimed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    fields: list[ExtractedField] = Field(default_factory=list)


class ReviewSubmission(BaseModel):
    """Payload when a reviewer submits a decision."""

    action: ReviewAction
    corrections: dict[str, str] = Field(default_factory=dict)
    reason: Optional[str] = None


class ClaimRequest(BaseModel):
    """Request body for claiming a review item."""

    reviewer_id: str


# API Response Models


class Document(BaseModel):
    """A tracked document (persisted from the moment of upload)."""

    id: str
    filename: str
    original_filename: str
    mime_type: str = "application/pdf"
    status: DocumentStatus = DocumentStatus.QUEUED
    task_id: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DocumentListResponse(BaseModel):
    """Paginated list of tracked documents."""

    items: list[Document]
    total: int


class PaginatedResponse(BaseModel):
    """Paginated list response."""

    items: list[ReviewItem]
    total: int
    limit: int
    offset: int


class QueueStats(BaseModel):
    """Dashboard statistics."""

    queue_depth: int = 0
    items_reviewed_today: int = 0
    avg_review_time_seconds: float = 0.0
    sla_compliance_percent: float = 100.0


class UploadResponse(BaseModel):
    """Response after uploading a document."""

    document_id: str
    task_id: str
    filename: str
    mime_type: str = "application/pdf"
    status: DocumentStatus = DocumentStatus.QUEUED


class HealthResponse(BaseModel):
    """Health-check response."""

    status: str = "ok"
    version: str = "1.0.0"
    uptime_seconds: float = 0.0
