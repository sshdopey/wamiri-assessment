"""Database initialization & session helpers (async PostgreSQL via asyncpg)."""

from __future__ import annotations

import logging

import asyncpg

from src.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id               TEXT PRIMARY KEY,
    filename         TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    mime_type        TEXT NOT NULL DEFAULT 'application/pdf',
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK(status IN ('queued','processing','completed','failed','duplicate','review_pending')),
    task_id          TEXT,
    error_message    TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS processed_documents (
    content_hash TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL,
    filename     TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS review_items (
    id           TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL UNIQUE,
    filename     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','in_review','approved','corrected','rejected')),
    priority     DOUBLE PRECISION DEFAULT 0,
    sla_deadline TIMESTAMPTZ,
    assigned_to  TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    claimed_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS extracted_fields (
    id                TEXT PRIMARY KEY,
    review_item_id    TEXT NOT NULL,
    field_name        TEXT NOT NULL,
    value             TEXT,
    confidence        DOUBLE PRECISION DEFAULT 0,
    manually_corrected BOOLEAN DEFAULT FALSE,
    corrected_at      TIMESTAMPTZ,
    corrected_by      TEXT,
    locked            BOOLEAN DEFAULT FALSE,
    FOREIGN KEY(review_item_id) REFERENCES review_items(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         SERIAL PRIMARY KEY,
    item_id    TEXT NOT NULL,
    action     TEXT NOT NULL,
    field_name TEXT,
    old_value  TEXT,
    new_value  TEXT,
    actor      TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_documents_status') THEN
        CREATE INDEX idx_documents_status ON documents(status);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_documents_created') THEN
        CREATE INDEX idx_documents_created ON documents(created_at DESC);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_review_items_status') THEN
        CREATE INDEX idx_review_items_status ON review_items(status);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_review_items_priority') THEN
        CREATE INDEX idx_review_items_priority ON review_items(priority DESC);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_extracted_fields_item') THEN
        CREATE INDEX idx_extracted_fields_item ON extracted_fields(review_item_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'review_items_document_id_key') THEN
        BEGIN
            ALTER TABLE review_items ADD CONSTRAINT review_items_document_id_key UNIQUE (document_id);
        EXCEPTION WHEN duplicate_table THEN NULL;
        END;
    END IF;

    -- Migrate CHECK constraint to allow 'duplicate' status
    IF EXISTS (
        SELECT 1 FROM information_schema.check_constraints
        WHERE constraint_name = 'documents_status_check'
    ) THEN
        BEGIN
            ALTER TABLE documents DROP CONSTRAINT documents_status_check;
            ALTER TABLE documents ADD CONSTRAINT documents_status_check
                CHECK(status IN ('queued','processing','completed','failed','duplicate','review_pending'));
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
    END IF;
END $$;
"""


def _dsn() -> str:
    """Build a PostgreSQL DSN from settings."""
    return settings.database_url


async def init_db() -> None:
    """Create tables if they don't exist and initialise the connection pool."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=10)
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)
    logger.info("Database initialised (PostgreSQL)")


async def get_pool() -> asyncpg.Pool:
    """Return the shared connection pool, creating it if needed."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=_dsn(), min_size=2, max_size=10)
    return _pool


async def get_db() -> asyncpg.Connection:
    """Acquire a connection from the pool (caller must release via pool.release)."""
    pool = await get_pool()
    return await pool.acquire()


async def release_db(conn: asyncpg.Connection) -> None:
    """Release a connection back to the pool."""
    global _pool
    if _pool is not None:
        try:
            await _pool.release(conn)
        except Exception:
            pass  # pool may have been closed or conn may not belong to it


async def close_pool() -> None:
    """Close the pool (call on shutdown or between tests)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def reset_pool() -> None:
    """Reset the pool reference without closing (for event loop changes in tests).

    Call this when the event loop has changed and the old pool is no longer valid.
    """
    global _pool
    _pool = None
