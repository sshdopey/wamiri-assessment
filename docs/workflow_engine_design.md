# Workflow Engine Design

## Celery DAG Architecture

The document processing pipeline uses Celery with Redis as both broker and result backend. Tasks are organized as a directed acyclic graph (DAG) with two primary execution paths:

### Single Document Processing

```
process_document_task(document_id, file_path)
    │
    ├── 1. Idempotency Check (SHA-256 hash lookup in PostgreSQL)
    │       └── Cache hit → Skip extraction (steps 2-3), but still run step 4
    │
    ├── 2. Send file bytes inline to Gemini (PDF or image)
    │       └── Returns InvoiceData Pydantic model
    │
    ├── 3. Dual Format Save (Parquet + JSON)
    │       ├── Atomic writes via temp file + rename
    │       └── Cache result in processed_documents table
    │
    └── 4. Create Review Queue Item ← ALWAYS runs (even on cache hit)
            └── Priority calculated from confidence + SLA + complexity + value
```

> **Key design decision**: On cache hit, the cached extraction result is re-used but rebuilt with the current `document_id`. A new review queue item is always created so that every upload appears in the review queue, even re-uploads of the same PDF content.

### Batch Processing

```
batch_process_task(document_ids)
    │
    └── Celery group() ─┬── process_document_task(doc_1)
                        ├── process_document_task(doc_2)
                        ├── process_document_task(doc_3)
                        └── ... (up to max_concurrent_tasks)
```

The `group()` primitive runs tasks in parallel across available workers. Results are collected and aggregated after all tasks complete.

## Parallelism Strategy

| Layer | Mechanism | Limit |
|-------|-----------|-------|
| Task distribution | Celery workers (prefork pool) | `--concurrency=4` per worker |
| Batch processing | `celery.group()` | `max_concurrent_tasks=10` per batch |
| API requests | FastAPI async (uvicorn) | Event loop per worker |
| Gemini API calls | Sequential per task | Rate limited by Google |
| PostgreSQL | asyncpg connection pool | min=2, max=10 |

### Worker Configuration

```python
app = Celery("document_processor")
app.conf.update(
    broker_url=settings.redis_url,
    result_backend=settings.redis_result_backend,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,       # 5 min hard kill
    task_soft_time_limit=270,  # 4.5 min graceful
    worker_prefetch_multiplier=1,
    task_acks_late=True,       # Re-queue on crash
)
```

## Rate Limiting

| Resource | Strategy |
|----------|----------|
| Gemini API | Sequential per-task (1 call per document) |
| Redis broker | Connection pooling, prefetch=1 |
| PostgreSQL | asyncpg pool (min=2, max=10), psycopg2 for sync Celery tasks |
| File I/O | Temp-file + atomic rename prevents corruption |

## Failure Recovery

### Retry Policy

```python
@app.task(bind=True, max_retries=settings.max_retries)
def process_document_task(self, document_id, file_path):
    try:
        ...
    except Exception as exc:
        delay = (2 ** self.request.retries) * settings.retry_backoff_base
        raise self.retry(exc=exc, countdown=delay)
```

### Retry Timeline

| Attempt | Delay | Formula | Total Elapsed |
|---------|-------|---------|---------------|
| 1st retry | 10s | 2⁰ × 10 | 10s |
| 2nd retry | 20s | 2¹ × 10 | 30s |
| 3rd retry | 40s | 2² × 10 | 70s |
| Final failure | — | — | Task marked FAILED |

### Failure Scenarios

| Failure | Recovery |
|---------|----------|
| Gemini API timeout | Retry with exponential backoff |
| Gemini rate limit (429) | Retry with exponential backoff |
| Invalid PDF | Fail immediately (no retry) |
| Redis connection lost | Celery reconnects automatically |
| Worker crash | `task_acks_late=True` re-queues task |
| Disk full | Atomic write fails cleanly, no corruption |
| PostgreSQL unavailable | Retry with backoff |

### Idempotency on Retry

Re-processing the same document is safe because:
1. SHA-256 hash check prevents duplicate Gemini API calls (uses cached extraction)
2. Review queue items are always created with the current `document_id`
3. Locked fields are never overwritten by re-extraction
4. Parquet/JSON writes are atomic (temp file + rename)

## Monitoring Integration

Each task emits Prometheus metrics:

```python
documents_processed.labels(status="success").inc()
processing_duration.observe(elapsed_seconds)
extraction_confidence.observe(result.overall_confidence)
```

The monitoring service checks SLA compliance every 5 minutes and writes metric snapshots hourly to `data/metrics/`.
