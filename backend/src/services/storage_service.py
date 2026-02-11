"""Storage service — idempotency cache, dual-format output (Parquet + JSON).

Idempotency strategy
────────────────────
1. Hash PDF bytes with SHA-256.
2. Look up hash in ``processed_documents`` table.
3. If found → return cached result (skip extraction).
4. If new   → extract, store hash + result, write Parquet + JSON.

Output guarantees
─────────────────
- Same data in both Parquet and JSON.
- Schema validation before write.
- Atomic writes via temp-file + rename.
- Checksums persisted for verification.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import psycopg2
import pyarrow as pa
import pyarrow.parquet as pq

from src.config import settings
from src.models.schemas import ExtractionResult

logger = logging.getLogger(__name__)


class StorageService:
    """Handles idempotency cache and dual-format result persistence.

    Uses *synchronous* psycopg2 (not asyncpg) so it can be called inside
    Celery tasks that run in a thread-pool worker.
    """

    def __init__(self) -> None:
        self._db = psycopg2.connect(settings.database_url)
        self._db.autocommit = False
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self._db.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_documents (
                    content_hash TEXT PRIMARY KEY,
                    document_id  TEXT NOT NULL,
                    filename     TEXT NOT NULL,
                    result_json  TEXT NOT NULL,
                    created_at   TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id               TEXT PRIMARY KEY,
                    filename         TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    mime_type        TEXT NOT NULL DEFAULT 'application/pdf',
                    status           TEXT NOT NULL DEFAULT 'queued'
                                     CHECK(status IN ('queued','processing','completed','failed','duplicate')),
                    task_id          TEXT,
                    error_message    TEXT,
                    created_at       TIMESTAMPTZ DEFAULT NOW(),
                    updated_at       TIMESTAMPTZ DEFAULT NOW()
                );
            """)
        self._db.commit()

    # ── Idempotency ──────────────────────────────────────────────────────

    def compute_hash(self, file_path: str | Path) -> str:
        """SHA-256 of file bytes."""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def get_cached_result(self, file_path: str | Path) -> dict | None:
        """Return cached extraction result dict if this file was already processed."""
        content_hash = self.compute_hash(file_path)
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT result_json FROM processed_documents WHERE content_hash = %s",
                (content_hash,),
            )
            row = cur.fetchone()
        if row:
            logger.info("Idempotency cache HIT for hash %s…", content_hash[:12])
            return json.loads(row[0])
        return None

    def cache_result(self, result: ExtractionResult) -> None:
        """Persist extraction result keyed by content hash."""
        if not result.content_hash:
            return
        with self._db.cursor() as cur:
            cur.execute(
                """INSERT INTO processed_documents
                   (content_hash, document_id, filename, result_json, created_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (content_hash) DO NOTHING""",
                (
                    result.content_hash,
                    result.document_id,
                    result.filename,
                    result.model_dump_json(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        self._db.commit()

    # ── Dual format save ─────────────────────────────────────────────────

    def save_result(self, result: ExtractionResult) -> tuple[Path, Path]:
        """Write extraction result to both Parquet and JSON.

        Returns (parquet_path, json_path).
        """
        now = datetime.now(timezone.utc)
        date_parts = now.strftime("%Y/%m/%d")

        # ── JSON ──
        json_dir = Path(settings.json_dir) / date_parts
        json_dir.mkdir(parents=True, exist_ok=True)
        json_path = json_dir / f"{result.document_id}.json"
        self._atomic_write_json(json_path, result)

        # ── Parquet ──
        parquet_dir = Path(settings.parquet_dir) / date_parts
        parquet_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = parquet_dir / f"{result.document_id}.parquet"
        self._atomic_write_parquet(parquet_path, result)

        # ── Cache for idempotency ──
        self.cache_result(result)

        logger.info(
            "Saved dual output for %s → %s, %s",
            result.document_id,
            json_path,
            parquet_path,
        )
        return parquet_path, json_path

    # ── Review item creation (fully synchronous — psycopg2) ─────────────

    def create_review_item(self, result: ExtractionResult) -> None:
        """Create a review-queue row and extracted fields using psycopg2.

        This is a pure-sync implementation so it works safely inside Celery
        prefork workers without any asyncio / asyncpg event-loop issues.
        """
        from src.services.review_queue_service import calculate_priority

        item_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        sla_deadline = now + timedelta(hours=settings.sla_default_hours)

        priority = calculate_priority(
            confidence_avg=result.overall_confidence,
            sla_deadline=sla_deadline,
            num_line_items=len(result.invoice_data.line_items),
            total_amount=result.invoice_data.total or 0,
        )

        try:
            with self._db.cursor() as cur:
                cur.execute(
                    """INSERT INTO review_items
                       (id, document_id, filename, status, priority,
                        sla_deadline, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (document_id)
                       DO UPDATE SET priority = EXCLUDED.priority,
                                     sla_deadline = EXCLUDED.sla_deadline""",
                    (
                        item_id,
                        result.document_id,
                        result.filename,
                        "pending",
                        priority,
                        sla_deadline,
                        now,
                    ),
                )

                for fc in result.field_confidences:
                    field_id = str(uuid.uuid4())
                    value_str = (
                        fc.value
                        if isinstance(fc.value, str)
                        else str(fc.value) if fc.value is not None else None
                    )
                    cur.execute(
                        """INSERT INTO extracted_fields
                           (id, review_item_id, field_name, value, confidence)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (field_id, item_id, fc.field_name, value_str, fc.confidence),
                    )
            self._db.commit()
            logger.info(
                "Created review item %s for doc %s (priority=%.1f)",
                item_id, result.document_id, priority,
            )
        except Exception:
            self._db.rollback()
            raise

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _atomic_write_json(path: Path, result: ExtractionResult) -> None:
        """Write JSON via temp file + rename for atomicity."""
        data = result.model_dump(mode="json")
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".json.tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.rename(tmp_path, str(path))
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    @staticmethod
    def _atomic_write_parquet(path: Path, result: ExtractionResult) -> None:
        """Write Parquet via temp file + rename for atomicity."""
        inv = result.invoice_data

        # Flatten for columnar storage
        row = {
            "document_id": result.document_id,
            "filename": result.filename,
            "vendor": inv.vendor or "",
            "invoice_number": inv.invoice_number or "",
            "date": inv.date or "",
            "due_date": inv.due_date or "",
            "subtotal": float(inv.subtotal or 0),
            "tax_rate": float(inv.tax_rate or 0),
            "tax_amount": float(inv.tax_amount or 0),
            "total": float(inv.total or 0),
            "currency": inv.currency or "",
            "num_line_items": len(inv.line_items),
            "line_items_json": json.dumps(
                [li.model_dump() for li in inv.line_items], default=str
            ),
            "confidence_score": result.overall_confidence,
            "extracted_at": result.extracted_at.isoformat()
            if result.extracted_at
            else "",
            "content_hash": result.content_hash or "",
        }

        df = pd.DataFrame([row])

        schema = pa.schema([
            ("document_id", pa.string()),
            ("filename", pa.string()),
            ("vendor", pa.string()),
            ("invoice_number", pa.string()),
            ("date", pa.string()),
            ("due_date", pa.string()),
            ("subtotal", pa.float64()),
            ("tax_rate", pa.float32()),
            ("tax_amount", pa.float64()),
            ("total", pa.float64()),
            ("currency", pa.string()),
            ("num_line_items", pa.int32()),
            ("line_items_json", pa.string()),
            ("confidence_score", pa.float32()),
            ("extracted_at", pa.string()),
            ("content_hash", pa.string()),
        ])

        table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".parquet.tmp"
        )
        os.close(tmp_fd)
        try:
            pq.write_table(table, tmp_path, compression="snappy")
            os.rename(tmp_path, str(path))
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
