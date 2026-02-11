# Module Architecture

## System Overview

The Document Processing System follows a modular, event-driven architecture with clear separation of concerns across five core services. All services communicate through PostgreSQL (state), Redis (task queue), and the filesystem (extraction outputs).

## Processing Pipeline

```
                                  ┌──────────────────────┐
                                  │   Google Gemini       │
                                  │   (gemini-3-flash-   │
                                  │    preview)           │
                                  └──────────┬───────────┘
                                             │
  ┌──────────┐    ┌───────────┐    ┌─────────▼──────────┐
  │  FastAPI  │───▶│  Celery   │───▶│ ExtractionService  │
  │  Upload   │    │  Task     │    │  • Inline Bytes    │
  │  Endpoint │    │  Queue    │    │  • Gemini Prompt   │
  └──────────┘    └───────────┘    │  • Confidence Calc │
                       │            └─────────┬──────────┘
                       │                      │
                  ┌────▼────┐        ┌────────▼─────────┐
                  │  Redis  │        │  StorageService   │
                  │  Broker │        │  • SHA-256 Hash   │
                  └─────────┘        │  • Parquet Write  │
                                     │  • JSON Write     │
                                     │  • Atomic Ops     │
                                     └────────┬─────────┘
                                              │
                                     ┌────────▼─────────┐
                                     │ ReviewQueueSvc   │
                                     │  • Priority Calc │
                                     │  • Field Locking │
                                     │  • SLA Tracking  │
                                     └────────┬─────────┘
                                              │
                                     ┌────────▼─────────┐
                                     │ MonitoringService │
                                     │  • Prometheus     │
                                     │  • SLA Checks     │
                                     │  • Snapshots      │
                                     └──────────────────┘
```

## Document Status State Machine

```
  ┌──────────┐   Upload    ┌────────────┐   Celery     ┌──────────────┐
  │  UNKNOWN │────────────▶│   QUEUED    │─────────────▶│  PROCESSING  │
  └──────────┘             └────────────┘              └──────┬───────┘
                                                              │
                                          Gemini Fail ┌───────┤ Success
                                                      ▼       ▼
                                               ┌──────────┐ ┌──────────┐
                                               │  FAILED   │ │ EXTRACTED│
                                               └──────────┘ └────┬─────┘
                                                                  │
                                               ┌──────────────────┤
                                               ▼                  ▼
                                         ┌──────────┐      ┌───────────┐
                                         │ PENDING  │      │ IN_REVIEW │
                                         │ (Queue)  │─────▶│ (Claimed) │
                                         └──────────┘      └─────┬─────┘
                                                                  │
                                         ┌────────────┬───────────┤
                                         ▼            ▼           ▼
                                   ┌──────────┐ ┌──────────┐ ┌──────────┐
                                   │ APPROVED │ │CORRECTED │ │ REJECTED │
                                   └──────────┘ └──────────┘ └──────────┘
```

## Idempotency Mechanism

The system prevents duplicate extraction through SHA-256 content hashing while ensuring every upload gets a review queue item:

1. **Upload**: PDF bytes are hashed with SHA-256
2. **Lookup**: Hash checked against `processed_documents` table in PostgreSQL
3. **Cache Hit**: Cached extraction result is retrieved, but a **new review queue item is created** for the current `document_id` — this ensures re-uploads always appear in the review queue
4. **Cache Miss**: Full extraction pipeline runs, result is cached, and a review queue item is created

```python
# Simplified flow (actual code in celery_app.py)
content_hash = sha256(pdf_bytes)
cached = db.query("SELECT * FROM processed_documents WHERE content_hash = $1", content_hash)

if cached:
    # Re-use extraction, but create a review item for THIS upload
    result = rebuild_extraction(cached, current_document_id)
    create_review_item(result)
    return result

# Full pipeline
result = gemini.extract(pdf_bytes)
storage.save_dual_format(result)       # Parquet + JSON
storage.cache_result(result)           # Store in processed_documents
create_review_item(result)
return result
```

## Field Locking Mechanism

Human corrections are protected from being overwritten during re-processing:

| Scenario | Field Locked? | Re-processing Behavior |
|----------|---------------|------------------------|
| AI-only extraction | No | Full overwrite |
| Human corrected once | Yes | Skip this field |
| Human corrected, new doc version | Yes | Keep human value |
| Admin unlock (manual) | Configurable | Depends on policy |

The `extracted_fields` table tracks:
- `manually_corrected`: Boolean flag set on first correction
- `corrected_at`: Timestamp of correction
- `corrected_by`: Reviewer ID
- `locked`: Whether the field is protected from overwrites

When submitting corrections via `PUT /api/queue/{id}/submit`, only unlocked fields are updated. Previously corrected fields retain their human-verified values.

## Module Dependencies

```
schemas.py ← config.py
    ↑            ↑
    │            │
extraction_service.py ← storage_service.py
                              ↑
                    review_queue_service.py ← monitoring_service.py
                              ↑
                         database.py
                              ↑
                        routes.py → main.py
```

## Service Summary

| Service | File | Responsibility |
|---------|------|----------------|
| ExtractionService | `extraction_service.py` | PDF → Gemini → InvoiceData + confidence scores |
| StorageService | `storage_service.py` | SHA-256 cache, Parquet/JSON write, atomic file ops |
| ReviewQueueService | `review_queue_service.py` | Queue CRUD, atomic claiming, field locking, audit log |
| MonitoringService | `monitoring_service.py` | Prometheus counters/histograms, SLA checks, snapshots |
| Database | `database.py` | PostgreSQL DDL, asyncpg connection pool (min=2, max=10) |
