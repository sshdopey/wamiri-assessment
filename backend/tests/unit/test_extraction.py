"""Unit tests – extraction, idempotency, field locking, validation."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from src.models.schemas import (
    ExtractionResult,
    FieldConfidence,
    InvoiceData,
    LineItem,
    ReviewAction,
    ReviewSubmission,
)


# ── Idempotency ──────────────────────────────────────────────────────────────


class TestIdempotency:
    """Re-processing the same document must produce identical results."""

    def test_same_hash_returns_cached(self, tmp_dir: Path, sample_extraction_result):
        """StorageService should return cached result for same content hash."""
        from src.services.storage_service import StorageService

        # Write a dummy file with unique content to avoid collisions
        unique_content = f"identical content {uuid.uuid4()}".encode()
        pdf = tmp_dir / "dup.pdf"
        pdf.write_bytes(unique_content)

        svc = StorageService()

        # First time: no cache
        assert svc.get_cached_result(pdf) is None

        # Save result
        sample_extraction_result.content_hash = svc.compute_hash(pdf)
        svc.cache_result(sample_extraction_result)

        # Second time: cache hit
        cached = svc.get_cached_result(pdf)
        assert cached is not None
        assert cached["document_id"] == sample_extraction_result.document_id

    def test_different_content_no_cache(self, tmp_dir: Path, sample_extraction_result):
        """Different file content should NOT hit cache."""
        from src.services.storage_service import StorageService

        tag = uuid.uuid4().hex
        pdf1 = tmp_dir / "a.pdf"
        pdf2 = tmp_dir / "b.pdf"
        pdf1.write_bytes(f"content A {tag}".encode())
        pdf2.write_bytes(f"content B {tag}".encode())

        svc = StorageService()
        sample_extraction_result.content_hash = svc.compute_hash(pdf1)
        svc.cache_result(sample_extraction_result)

        assert svc.get_cached_result(pdf1) is not None
        assert svc.get_cached_result(pdf2) is None

    def test_hash_deterministic(self, tmp_dir: Path):
        """SHA-256 hash of same bytes is always the same."""
        from src.services.storage_service import StorageService

        pdf = tmp_dir / "det.pdf"
        pdf.write_bytes(b"deterministic")

        svc = StorageService()
        h1 = svc.compute_hash(pdf)
        h2 = svc.compute_hash(pdf)
        assert h1 == h2


# ── Field Preservation / Locking ─────────────────────────────────────────────


class TestFieldLocking:
    """Locked (manually corrected) fields must never be overwritten."""

    @pytest.mark.asyncio
    async def test_locked_field_not_overwritten(self, sample_extraction_result):
        """Submitting a correction on a locked field is silently skipped."""
        from src.services.database import init_db
        from src.services.review_queue_service import ReviewQueueService

        await init_db()
        svc = ReviewQueueService()

        # Create item
        item = await svc.create_item(sample_extraction_result)
        assert item is not None

        # First correction → locks the field
        sub1 = ReviewSubmission(
            action=ReviewAction.CORRECT,
            corrections={"vendor": "Corrected Vendor"},
        )
        updated = await svc.submit_review(item.id, sub1, reviewer_id="user-1")
        vendor_field = next(f for f in updated.fields if f.field_name == "vendor")
        assert vendor_field.value == "Corrected Vendor"
        assert vendor_field.locked is True

        # Re-create & try to overwrite (simulating re-extraction)
        item2 = await svc.create_item(sample_extraction_result)
        sub2 = ReviewSubmission(
            action=ReviewAction.CORRECT,
            corrections={"vendor": "Should Not Appear"},
        )
        # First we need to claim the item before we can submit
        # Actually submit_review works on any item
        result = await svc.submit_review(item.id, sub2, reviewer_id="user-2")
        vendor_after = next(f for f in result.fields if f.field_name == "vendor")
        # Should still be the first correction
        assert vendor_after.value == "Corrected Vendor"

    @pytest.mark.asyncio
    async def test_correction_creates_audit_trail(self, sample_extraction_result):
        """Every correction must be recorded in the audit_log table."""
        from src.services.database import init_db, get_db, release_db
        from src.services.review_queue_service import ReviewQueueService

        await init_db()
        svc = ReviewQueueService()

        item = await svc.create_item(sample_extraction_result)
        sub = ReviewSubmission(
            action=ReviewAction.CORRECT,
            corrections={"total": "1500.00"},
        )
        await svc.submit_review(item.id, sub, reviewer_id="auditor")

        db = await get_db()
        try:
            rows = await db.fetch(
                "SELECT * FROM audit_log WHERE item_id = $1 AND action = 'correction'",
                item.id,
            )
            assert len(rows) >= 1
            assert rows[0]["field_name"] == "total"
            assert rows[0]["new_value"] == "1500.00"
        finally:
            await release_db(db)


# ── Validation Logic ─────────────────────────────────────────────────────────


class TestValidation:
    """Schema and cross-field validation tests."""

    def test_invoice_data_model_valid(self):
        """InvoiceData model accepts valid input."""
        inv = InvoiceData(
            vendor="Acme",
            invoice_number="INV-1",
            date="2024-01-01",
            total=100.0,
            line_items=[
                LineItem(item="A", quantity=1, unit_price=100, total=100),
            ],
        )
        assert inv.total == 100.0

    def test_line_item_negative_quantity_rejected(self):
        """LineItem with quantity < 0 should be rejected."""
        with pytest.raises(Exception):
            LineItem(item="Bad", quantity=-1, unit_price=10, total=-10)

    def test_extraction_result_confidence_clamped(self):
        """Confidence must be between 0 and 1."""
        with pytest.raises(Exception):
            FieldConfidence(field_name="x", confidence=1.5)

    def test_extraction_result_serialisation_roundtrip(
        self, sample_extraction_result
    ):
        """Serialise to JSON and back without data loss."""
        json_str = sample_extraction_result.model_dump_json()
        restored = ExtractionResult.model_validate_json(json_str)
        assert restored.document_id == sample_extraction_result.document_id
        assert restored.overall_confidence == sample_extraction_result.overall_confidence


# ── Priority Calculation ─────────────────────────────────────────────────────


class TestPriorityCalculation:
    """Priority formula produces expected rankings."""

    def test_low_confidence_gets_high_priority(self):
        from src.services.review_queue_service import calculate_priority

        high_conf = calculate_priority(0.95, None, 1, 100)
        low_conf = calculate_priority(0.50, None, 1, 100)
        assert low_conf > high_conf

    def test_urgent_sla_gets_high_priority(self):
        from datetime import datetime, timedelta, timezone
        from src.services.review_queue_service import calculate_priority

        far_sla = datetime.now(timezone.utc) + timedelta(hours=20)
        near_sla = datetime.now(timezone.utc) + timedelta(hours=1)

        p_far = calculate_priority(0.80, far_sla, 1, 100)
        p_near = calculate_priority(0.80, near_sla, 1, 100)
        assert p_near > p_far
