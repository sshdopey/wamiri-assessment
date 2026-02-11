"""Shared test fixtures and helpers."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Point DB to test database (override before importing app code)
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://wamiri:wamiri_secret@localhost:5432/document_processing",
)


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
async def _reset_db_pool():
    """Reset the asyncpg pool before each test to match the current event loop."""
    from src.services.database import close_pool, reset_pool
    reset_pool()
    yield
    try:
        await close_pool()
    except Exception:
        reset_pool()


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def sample_pdf(tmp_dir: Path) -> Path:
    """Create a minimal valid-ish PDF file for testing."""
    pdf = tmp_dir / "test_invoice.pdf"
    # Minimal PDF (1 blank page)
    pdf.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
        b"0000000058 00000 n \n0000000115 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF"
    )
    return pdf


@pytest.fixture()
def sample_extraction_result():
    """Return a mock ExtractionResult for testing."""
    from src.models.schemas import (
        ExtractionResult,
        FieldConfidence,
        InvoiceData,
        LineItem,
    )

    return ExtractionResult(
        document_id=str(uuid.uuid4()),
        filename="test_invoice.pdf",
        invoice_data=InvoiceData(
            vendor="Test Vendor Inc.",
            invoice_number="INV-2024-001",
            date="2024-01-15",
            due_date="2024-02-15",
            subtotal=1000.0,
            tax_rate=20.0,
            tax_amount=200.0,
            total=1200.0,
            currency="USD",
            line_items=[
                LineItem(item="Widget A", quantity=10, unit_price=50.0, total=500.0),
                LineItem(item="Widget B", quantity=5, unit_price=100.0, total=500.0),
            ],
        ),
        field_confidences=[
            FieldConfidence(field_name="vendor", value="Test Vendor Inc.", confidence=0.95),
            FieldConfidence(field_name="invoice_number", value="INV-2024-001", confidence=0.92),
            FieldConfidence(field_name="date", value="2024-01-15", confidence=0.90),
            FieldConfidence(field_name="total", value=1200.0, confidence=0.97),
            FieldConfidence(field_name="line_items", value=[], confidence=0.88),
        ],
        overall_confidence=0.92,
        processing_time_seconds=2.5,
        content_hash="abc123def456",
    )
