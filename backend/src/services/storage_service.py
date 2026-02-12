"""Storage service — idempotency cache, dual-format output (Parquet + JSON).

Idempotency strategy
1. Hash PDF bytes with SHA-256.
2. Look up hash in ``processed_documents`` table.
3. If found → return cached result (skip extraction).
4. If new   → extract, store hash + result, write Parquet + JSON.

Output guarantees
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

# Module-level connection pool (thread-safe)

import psycopg2.pool as _pg_pool

_storage_pool: _pg_pool.ThreadedConnectionPool | None = None
_pool_lock = __import__("threading").Lock()


def _get_storage_pool() -> _pg_pool.ThreadedConnectionPool:
    """Lazy-initialise and return a thread-safe psycopg2 connection pool."""
    global _storage_pool
    if _storage_pool is None or _storage_pool.closed:
        with _pool_lock:
            if _storage_pool is None or _storage_pool.closed:
                _storage_pool = _pg_pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=10,
                    dsn=settings.database_url,
                )
    return _storage_pool


class StorageService:
    """Handles idempotency cache and dual-format result persistence.

    Uses *synchronous* psycopg2 via a thread-safe connection pool so it can
    be called inside Celery tasks that run in a thread-pool worker.
    """

    def __init__(self) -> None:
        self._pool = _get_storage_pool()
        self._db: psycopg2.extensions.connection | None = None
        self._ensure_connection()
        self._ensure_tables()

    def _ensure_connection(self) -> None:
        """Get a connection from the pool (lazy, reusable)."""
        if self._db is None or self._db.closed:
            self._db = self._pool.getconn()
            self._db.autocommit = False

    def close(self) -> None:
        """Return connection to pool."""
        if self._db is not None and self._pool:
            try:
                self._pool.putconn(self._db)
            except Exception:
                pass
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self) -> None:
        self.close()

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

    # Idempotency

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

    # Dual format save

    def save_result(self, result: ExtractionResult) -> tuple[Path, Path]:
        """Write extraction result to both Parquet and JSON.

        Returns (parquet_path, json_path).
        """
        now = datetime.now(timezone.utc)
        date_parts = now.strftime("%Y/%m/%d")

        # JSON
        json_dir = Path(settings.json_dir) / date_parts
        json_dir.mkdir(parents=True, exist_ok=True)
        json_path = json_dir / f"{result.document_id}.json"
        self._atomic_write_json(json_path, result)

        # Parquet
        parquet_dir = Path(settings.parquet_dir) / date_parts
        parquet_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = parquet_dir / f"{result.document_id}.parquet"
        self._atomic_write_parquet(parquet_path, result)

        # Cache for idempotency
        self.cache_result(result)

        logger.info(
            "Saved dual output for %s → %s, %s",
            result.document_id,
            json_path,
            parquet_path,
        )
        return parquet_path, json_path

    # Review item creation (fully synchronous — psycopg2)

    def create_review_item(self, result: ExtractionResult) -> None:
        """Create a review-queue row and extracted fields using psycopg2.

        This is a pure-sync implementation so it works safely inside Celery
        prefork workers without any asyncio / asyncpg event-loop issues.
        """
        from src.services.review_queue_service import calculate_priority

        item_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        # SLA deadline is NOT set at creation — it starts when
        # the reviewer clicks "Start Review" (claim_item).
        sla_deadline = None

        priority = calculate_priority(
            confidence_avg=result.overall_confidence,
            sla_deadline=sla_deadline,
            num_line_items=len(result.invoice_data.line_items),
            total_amount=result.invoice_data.total or 0,
        )

        try:
            with self._db.cursor() as cur:
                # Upsert review item — on conflict, just update priority/SLA
                cur.execute(
                    """INSERT INTO review_items
                       (id, document_id, filename, status, priority,
                        sla_deadline, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (document_id)
                       DO UPDATE SET priority = EXCLUDED.priority,
                                     sla_deadline = EXCLUDED.sla_deadline
                       RETURNING id""",
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
                actual_item_id = cur.fetchone()[0]

                # Delete old non-locked fields to avoid orphans on re-processing
                cur.execute(
                    """DELETE FROM extracted_fields
                       WHERE review_item_id = %s AND locked = FALSE""",
                    (actual_item_id,),
                )

                for fc in result.field_confidences:
                    field_id = str(uuid.uuid4())
                    value_str = (
                        fc.value
                        if isinstance(fc.value, str)
                        else str(fc.value)
                        if fc.value is not None
                        else None
                    )
                    # Skip if a locked field with this name already exists
                    cur.execute(
                        """INSERT INTO extracted_fields
                           (id, review_item_id, field_name, value, confidence)
                           SELECT %s, %s, %s, %s, %s
                           WHERE NOT EXISTS (
                               SELECT 1 FROM extracted_fields
                               WHERE review_item_id = %s AND field_name = %s AND locked = TRUE
                           )""",
                        (
                            field_id,
                            actual_item_id,
                            fc.field_name,
                            value_str,
                            fc.confidence,
                            actual_item_id,
                            fc.field_name,
                        ),
                    )
            self._db.commit()

            # Auto-assign via least-loaded immediately on creation
            self._auto_assign_least_loaded(actual_item_id)

            logger.info(
                "Created review item %s for doc %s (priority=%.1f)",
                item_id,
                result.document_id,
                priority,
            )
        except Exception:
            self._db.rollback()
            raise

    # Least-loaded auto-assign (sync, for Celery workers)

    def _auto_assign_least_loaded(self, item_id: str) -> None:
        """Atomically assign a newly created review item to the least-loaded reviewer.

        Strategy: query the DB for each roster reviewer's current active
        (in_review) count and pick the one with the fewest.  Ties are broken
        by Redis INCR to maintain round-robin fairness among equal loads.

        This is much fairer than pure round-robin because it accounts for
        reviewers who finish reviews faster than others.
        """
        roster = settings.reviewer_roster
        if not roster:
            return

        try:
            # Count assigned items (pending + in_review) per reviewer
            with self._db.cursor() as cur:
                cur.execute(
                    """SELECT assigned_to, COUNT(*) AS cnt
                       FROM review_items
                       WHERE status IN ('pending', 'in_review')
                         AND assigned_to IS NOT NULL
                       GROUP BY assigned_to"""
                )
                workload: dict[str, int] = {row[0]: row[1] for row in cur.fetchall()}

            # Build list: (active_count, reviewer_id)
            # Reviewers not in workload dict have 0 active items
            candidates = [(workload.get(r, 0), r) for r in roster]
            min_load = min(c[0] for c in candidates)
            tied = [r for load, r in candidates if load == min_load]

            if len(tied) == 1:
                reviewer = tied[0]
            else:
                # Break ties with Redis INCR (true round-robin among equals)
                try:
                    import redis

                    r = redis.from_url(settings.redis_url)
                    idx = r.incr("wamiri:rr_index") - 1
                except Exception:
                    idx = 0
                reviewer = tied[idx % len(tied)]

        except Exception as exc:
            # Ultimate fallback: first reviewer
            logger.warning("Least-loaded query failed, using fallback: %s", exc)
            reviewer = roster[0]

        now = datetime.now(timezone.utc)
        try:
            with self._db.cursor() as cur:
                cur.execute(
                    """UPDATE review_items
                       SET assigned_to = %s
                       WHERE id = %s AND status = 'pending'""",
                    (reviewer, item_id),
                )
                # Audit log entry
                cur.execute(
                    """INSERT INTO audit_log (item_id, action, actor, created_at)
                       VALUES (%s, 'auto_assign', %s, %s)""",
                    (item_id, reviewer, now),
                )
            self._db.commit()
            logger.info(
                "Auto-assigned %s → %s (least-loaded, assigned=%d)",
                item_id,
                reviewer,
                workload.get(reviewer, 0) if "workload" in dir() else 0,
            )
        except Exception as exc:
            self._db.rollback()
            logger.warning("Auto-assign failed for %s: %s", item_id, exc)

    # Internal helpers

    @staticmethod
    def _atomic_write_json(path: Path, result: ExtractionResult) -> None:
        """Write JSON via temp file + rename for atomicity."""
        data = result.model_dump(mode="json")
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".json.tmp")
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
            "schema_version": result.schema_version,
        }

        df = pd.DataFrame([row])

        schema = pa.schema(
            [
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
                ("schema_version", pa.string()),
            ]
        )

        table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)

        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".parquet.tmp")
        os.close(tmp_fd)
        try:
            pq.write_table(table, tmp_path, compression="snappy")
            os.rename(tmp_path, str(path))
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
