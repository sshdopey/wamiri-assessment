"""Celery application and task definitions for distributed document processing.

Configuration
- Broker:          Redis (redis://localhost:6379/0)
- Result backend:  Redis (redis://localhost:6379/1)
- Serializer:      JSON
- Hard time limit: 300 s (5 min SLA)
- Soft time limit: 270 s (4.5 min)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from celery import Celery, chord, group
from celery.exceptions import SoftTimeLimitExceeded

from src.config import settings

logger = logging.getLogger(__name__)

# Celery app

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
    # Celery Beat schedule — periodic tasks
    beat_schedule={
        "release-expired-claims": {
            "task": "tasks.release_expired_claims",
            "schedule": 300.0,  # every 5 minutes
        },
        "update-queue-metrics": {
            "task": "tasks.update_queue_metrics",
            "schedule": 15.0,  # every 15 seconds — matches Prometheus scrape interval
        },
    },
)

# Helpers


def _update_queue_depth_metric() -> None:
    """Query review_items to update queue-depth Prometheus gauge."""
    import psycopg2 as _pg

    try:
        conn = _pg.connect(settings.database_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) FROM review_items "
                "WHERE status IN ('pending','in_review') GROUP BY status"
            )
            counts = dict(cur.fetchall())
        conn.close()
        from src.services.monitoring_service import monitoring

        monitoring.update_queue_depth(
            pending=counts.get("pending", 0),
            in_review=counts.get("in_review", 0),
        )
    except Exception as exc:
        logger.warning("[task] Queue depth metric update failed: %s", exc)


# Tasks


@app.task(
    bind=True,
    name="tasks.process_document",
    max_retries=settings.max_retries,
    default_retry_delay=settings.retry_backoff_base,
    acks_late=True,
)
def process_document_task(
    self, document_id: str, file_path: str, stored_filename: str | None = None
) -> dict:
    """Extract structured data from a single document (PDF or image).

    Steps
    1. Update document status to 'processing'.
    2. Check idempotency cache (skip if already processed).
    3. Extract with Gemini via ExtractionService.
    4. Save dual-format output (Parquet + JSON).
    5. Create / update review-queue item.
    6. Update document status to 'completed'.

    Retries with exponential back-off (2^retry × base) and jitter.
    """
    import psycopg2 as _pg

    from src.models.schemas import ExtractionResult
    from src.services.extraction_service import ExtractionService
    from src.services.storage_service import StorageService

    t0 = time.time()
    logger.info(
        "[task] Processing document %s — attempt %s",
        document_id,
        self.request.retries + 1,
    )

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
            logger.warning(
                "[task] Failed to update doc status for %s: %s", document_id, exc
            )

    try:
        # 1. Mark as processing
        _update_doc_status("processing")

        # 2. Idempotency check
        with StorageService() as storage:
            cached = storage.get_cached_result(file_path)
            if cached is not None:
                logger.info("[task] Cache hit (duplicate) for %s", document_id)
                # Duplicate upload — mark as duplicate, do NOT create another
                # review-queue item (the original is already queued/reviewed).
                _update_doc_status("duplicate")
                filename = stored_filename or f"{document_id}.pdf"
                cached_result = ExtractionResult(
                    **{
                        **cached,
                        "document_id": document_id,
                        "filename": filename,
                    }
                )
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

        # 7. Record metrics for Prometheus / Grafana
        try:
            from src.services.monitoring_service import monitoring

            avg_conf = (
                sum(f.confidence for f in result.field_confidences)
                / len(result.field_confidences)
                if result.field_confidences
                else result.overall_confidence
            )
            monitoring.record_processing(document_id, elapsed, avg_conf, success=True)
            # Update queue depth from DB
            _update_queue_depth_metric()
        except Exception as m_exc:
            logger.warning("[task] Metrics recording failed (non-fatal): %s", m_exc)

        return result.model_dump(mode="json")

    except SoftTimeLimitExceeded:
        logger.error("[task] Soft time limit hit for %s", document_id)
        _update_doc_status("failed", "Processing timed out")
        try:
            from src.services.monitoring_service import monitoring

            monitoring.record_processing(
                document_id, time.time() - t0, 0.0, success=False
            )
        except Exception:
            pass
        raise

    except Exception as exc:
        # Record failure metric before retry
        try:
            from src.services.monitoring_service import monitoring

            monitoring.record_processing(
                document_id, time.time() - t0, 0.0, success=False
            )
        except Exception:
            pass
        base_delay = (2**self.request.retries) * settings.retry_backoff_base
        jitter = random.uniform(0, base_delay * 0.5)
        retry_delay = base_delay + jitter
        logger.warning(
            "[task] Retrying %s in %.1fs (attempt %d, backoff=%.0f+jitter=%.1f): %s",
            document_id,
            retry_delay,
            self.request.retries + 1,
            base_delay,
            jitter,
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
    document_ids_and_paths : list of [document_id, file_path, stored_filename] triples
                             (stored_filename is optional, defaults to None)

    Returns
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


# WorkflowExecutor-based processing


@app.task(
    bind=True,
    name="tasks.process_document_dag",
    max_retries=settings.max_retries,
    acks_late=True,
)
def process_document_dag_task(
    self, document_id: str, file_path: str, stored_filename: str | None = None
) -> dict:
    """Process a document using the DAG-based WorkflowExecutor.

    Routes the document through the full DAG pipeline:
        extract → save_parquet (parallel) → create_review
                → save_json    (parallel) ↗
                → record_metrics

    Uses ``asyncio.run()`` to bridge from synchronous Celery to the async
    WorkflowExecutor.
    """
    import psycopg2 as _pg

    from src.services.workflow_executor import (
        TokenBucketRateLimiter,
        WorkflowExecutor,
        build_document_processing_dag,
    )

    t0 = time.time()
    logger.info("[dag-task] Processing document %s via WorkflowExecutor", document_id)

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
            logger.warning(
                "[dag-task] Failed to update doc status for %s: %s", document_id, exc
            )

    try:
        _update_doc_status("processing")

        # Idempotency check — skip entire DAG if content already processed
        from src.models.schemas import ExtractionResult
        from src.services.storage_service import StorageService

        with StorageService() as storage:
            cached = storage.get_cached_result(file_path)
            if cached is not None:
                logger.info("[dag-task] Cache hit (duplicate) for %s", document_id)
                _update_doc_status("duplicate")
                filename = stored_filename or f"{document_id}.pdf"
                cached_result = ExtractionResult(
                    **{
                        **cached,
                        "document_id": document_id,
                        "filename": filename,
                    }
                )
                try:
                    from src.services.monitoring_service import monitoring

                    monitoring.record_processing(
                        document_id, 0.0, cached_result.overall_confidence, success=True
                    )
                except Exception:
                    pass
                return cached_result.model_dump(mode="json")

        dag = build_document_processing_dag(document_id, file_path, stored_filename)
        executor = WorkflowExecutor(
            max_concurrency=4,
            rate_limiters={
                "gemini_api": TokenBucketRateLimiter(rate_per_second=10.0, burst=5),
            },
            default_timeout=120.0,
        )

        result = asyncio.run(executor.execute(dag, context={"file_path": file_path}))

        if result.success:
            _update_doc_status("completed")
            elapsed = round(time.time() - t0, 2)
            logger.info(
                "[dag-task] Completed %s in %.1fs (%d steps, %d skipped)",
                document_id,
                elapsed,
                result.completed_count,
                result.skipped_count,
            )

            # Record metrics for Prometheus / Grafana
            try:
                from src.services.monitoring_service import monitoring

                # Get real confidence from the extract step output
                extract_output = result.steps.get("extract")
                real_confidence = 0.0
                if extract_output and extract_output.output:
                    real_confidence = extract_output.output.get(
                        "overall_confidence", 0.0
                    )
                monitoring.record_processing(
                    document_id, elapsed, real_confidence, success=True
                )
                _update_queue_depth_metric()
            except Exception:
                pass

            # Return extract step output if available
            extract_result = result.steps.get("extract")
            if extract_result and extract_result.output:
                return extract_result.output
            return {"document_id": document_id, "status": "completed"}
        else:
            failed_steps = [
                f"{sid}: {sr.error}"
                for sid, sr in result.steps.items()
                if sr.status.value == "failed"
            ]
            error_msg = "; ".join(failed_steps)[:500]
            _update_doc_status("failed", error_msg)
            logger.error("[dag-task] Failed %s: %s", document_id, error_msg)

            try:
                from src.services.monitoring_service import monitoring

                monitoring.record_processing(
                    document_id, time.time() - t0, 0.0, success=False
                )
            except Exception:
                pass

            return {"document_id": document_id, "status": "failed", "error": error_msg}

    except Exception as exc:
        _update_doc_status("failed", str(exc)[:500])
        logger.error("[dag-task] Exception processing %s: %s", document_id, exc)
        raise self.retry(exc=exc, countdown=10)


# Periodic: Release expired claims


@app.task(name="tasks.release_expired_claims")
def release_expired_claims_task() -> dict:
    """Periodic beat task: release review items stuck in 'in_review' past the expiry window.

    Uses sync psycopg2 — asyncpg connections cannot be shared across
    event loops, which causes 'Future attached to a different loop' errors
    inside Celery prefork workers.
    """
    from datetime import datetime, timedelta, timezone

    import psycopg2 as _pg

    expiry_minutes = settings.claim_expiry_minutes
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=expiry_minutes)

    released = 0
    try:
        conn = _pg.connect(settings.database_url)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE review_items
                   SET status = 'pending', assigned_to = NULL,
                       claimed_at = NULL, sla_deadline = NULL
                   WHERE status = 'in_review' AND claimed_at < %s""",
                (cutoff,),
            )
            released = cur.rowcount
        conn.commit()
        conn.close()
        if released > 0:
            logger.info(
                "Released %d expired claims (older than %d min)",
                released,
                expiry_minutes,
            )
    except Exception as exc:
        logger.error("release_expired_claims failed: %s", exc)

    return {"released": released}


# Periodic: Update queue metrics for Prometheus


@app.task(name="tasks.update_queue_metrics")
def update_queue_metrics_task() -> dict:
    """Periodic beat task: refresh queue-depth gauges for Prometheus/Grafana."""
    _update_queue_depth_metric()
    return {"status": "ok"}
