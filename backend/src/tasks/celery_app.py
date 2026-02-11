"""Celery application and task definitions for distributed document processing.

Configuration
─────────────
- Broker:          Redis (redis://localhost:6379/0)
- Result backend:  Redis (redis://localhost:6379/1)
- Serializer:      JSON
- Hard time limit: 300 s (5 min SLA)
- Soft time limit: 270 s (4.5 min)
"""

from __future__ import annotations

import logging
import time

from celery import Celery, group, chord
from celery.exceptions import SoftTimeLimitExceeded

from src.config import settings

logger = logging.getLogger(__name__)

# ── Celery app ────────────────────────────────────────────────────────────────

app = Celery("document_processing")

app.conf.update(
    broker_url=settings.redis_url,
    result_backend=settings.redis_result_backend,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=settings.task_time_limit,
    task_soft_time_limit=settings.task_soft_time_limit,
    worker_concurrency=settings.max_concurrent_tasks,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)


# ── Tasks ─────────────────────────────────────────────────────────────────────


@app.task(
    bind=True,
    name="tasks.process_document",
    max_retries=settings.max_retries,
    default_retry_delay=settings.retry_backoff_base,
    acks_late=True,
)
def process_document_task(self, document_id: str, file_path: str, stored_filename: str | None = None) -> dict:
    """Extract structured data from a single document (PDF or image).

    Steps
    ─────
    1. Update document status to 'processing'.
    2. Check idempotency cache (skip if already processed).
    3. Extract with Gemini via ExtractionService.
    4. Save dual-format output (Parquet + JSON).
    5. Create / update review-queue item.
    6. Update document status to 'completed'.

    Retries with exponential back-off (2^retry × base) and jitter.
    """
    from src.services.extraction_service import ExtractionService
    from src.services.storage_service import StorageService
    from src.models.schemas import ExtractionResult
    import psycopg2 as _pg

    t0 = time.time()
    logger.info("[task] Processing document %s — attempt %s", document_id, self.request.retries + 1)

    # Helper to update document status synchronously
    def _update_doc_status(status: str, error_message: str | None = None) -> None:
        try:
            conn = _pg.connect(settings.database_url)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET status = %s, error_message = %s, updated_at = NOW() WHERE id = %s",
                    (status, error_message, document_id),
                )
            conn.close()
        except Exception as exc:
            logger.warning("[task] Failed to update doc status for %s: %s", document_id, exc)

    try:
        # 1. Mark as processing
        _update_doc_status("processing")

        # 2. Idempotency check
        storage = StorageService()
        cached = storage.get_cached_result(file_path)
        if cached is not None:
            logger.info("[task] Cache hit (duplicate) for %s", document_id)
            # Duplicate upload — mark as duplicate, do NOT create another
            # review-queue item (the original is already queued/reviewed).
            _update_doc_status("duplicate")
            filename = stored_filename or f"{document_id}.pdf"
            cached_result = ExtractionResult(**{
                **cached,
                "document_id": document_id,
                "filename": filename,
            })
            return cached_result.model_dump(mode="json")

        # 3. Extract
        extractor = ExtractionService()
        result = extractor.extract(file_path=file_path, document_id=document_id)

        # 4. Save dual format
        storage.save_result(result)

        # 5. Enqueue for review (runs sync helper)
        storage.create_review_item(result)

        # 6. Mark completed
        _update_doc_status("completed")

        elapsed = round(time.time() - t0, 2)
        logger.info("[task] Completed %s in %.1fs", document_id, elapsed)

        return result.model_dump(mode="json")

    except SoftTimeLimitExceeded:
        logger.error("[task] Soft time limit hit for %s", document_id)
        _update_doc_status("failed", "Processing timed out")
        raise

    except Exception as exc:
        retry_delay = (2 ** self.request.retries) * settings.retry_backoff_base
        logger.warning(
            "[task] Retrying %s in %ds (attempt %d): %s",
            document_id,
            retry_delay,
            self.request.retries + 1,
            exc,
        )
        # On final retry failure, mark as failed
        if self.request.retries >= settings.max_retries - 1:
            _update_doc_status("failed", str(exc)[:500])
        raise self.retry(exc=exc, countdown=retry_delay)


@app.task(
    name="tasks.batch_process",
    time_limit=600,  # 10 min hard limit for batch
    soft_time_limit=540,
)
def batch_process_task(document_ids_and_paths: list[list[str]]) -> dict:
    """Process up to 100 documents in parallel using Celery group + chord.

    Parameters
    ──────────
    document_ids_and_paths : list of [document_id, file_path, stored_filename] triples
                             (stored_filename is optional, defaults to None)

    Returns
    ───────
    dict with ``completed``, ``failed``, ``total``, ``elapsed_seconds``.
    """
    t0 = time.time()
    tasks = []
    for entry in document_ids_and_paths:
        doc_id = entry[0]
        path = entry[1]
        stored_fn = entry[2] if len(entry) > 2 else None
        tasks.append(process_document_task.s(doc_id, path, stored_fn))

    # Fan out all tasks then collect results
    job = group(tasks)
    result = job.apply_async()
    result.get(timeout=settings.task_time_limit, propagate=False)

    completed = sum(1 for r in result.results if r.successful())
    failed = sum(1 for r in result.results if r.failed())

    elapsed = round(time.time() - t0, 2)
    logger.info("[batch] %d/%d completed in %.1fs", completed, len(tasks), elapsed)

    return {
        "total": len(tasks),
        "completed": completed,
        "failed": failed,
        "elapsed_seconds": elapsed,
    }


@app.task(name="tasks.aggregate_batch_results")
def aggregate_batch_results(results: list) -> dict:
    """Chord callback — aggregate individual task results."""
    completed = sum(1 for r in results if r is not None)
    return {"completed": completed, "total": len(results)}
