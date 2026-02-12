# Backend — Wamiri Invoices API

> Python 3.13 backend powering the AI extraction pipeline. FastAPI serves the REST API, Celery distributes extraction tasks, and Google Gemini performs the actual document understanding.

---

## How It Works

```
Upload arrives (PDF/PNG/JPEG/…)
    │
    ▼
┌─────────────┐    Celery task dispatched
│  FastAPI     │─────────────────────────────────▶ Redis broker
│  (routes.py) │    document status = "queued"      │
└─────────────┘                                     ▼
                                            ┌───────────────┐
                                            │ Celery Worker  │
                                            │ (4 concurrent) │
                                            └───────┬───────┘
                                                    │
                        ┌───────────────────────────┼───────────────────┐
                        ▼                           ▼                   ▼
                 SHA-256 hash check          Gemini extraction    Dual-format save
                 (idempotency cache)         (inline base64)     (Parquet + JSON)
                        │                           │                   │
                        ▼                           ▼                   ▼
                 Cache hit? → "duplicate"    InvoiceData model   Atomic temp+rename
                 Cache miss? → continue      with confidence     Date-partitioned
                                                    │
                                                    ▼
                                            Review queue item
                                            (priority calculated)
```

---

## Tech Stack

| Component | Technology | Why |
|-----------|------------|-----|
| **API** | FastAPI + uvicorn | Async performance, auto-generated OpenAPI docs |
| **Task Queue** | Celery + Redis | Proven distributed task processing, retry built-in |
| **LLM** | Google Gemini (`gemini-3-flash-preview`) | Multimodal — reads PDFs and images natively |
| **Database** | PostgreSQL 18 | ACID transactions for review queue atomicity |
| **API DB driver** | asyncpg | Non-blocking queries in async FastAPI handlers |
| **Worker DB driver** | psycopg2 | Synchronous — avoids event-loop conflicts in Celery prefork |
| **Schemas** | Pydantic v2 | Request/response validation, Gemini structured output |
| **Output** | PyArrow + JSON | Dual-format: columnar analytics + API consumption |
| **Metrics** | prometheus-client | Standard observability, Grafana-ready |
| **Package Manager** | uv | 10-50x faster than pip |

---

## Services

### ExtractionService (`extraction_service.py`)

Sends documents **directly** to Gemini as inline base64 bytes — no PDF-to-image conversion, no external rendering libraries. The prompt instructs Gemini to return a structured `GeminiInvoiceSchema` (Pydantic model), which is mapped to `InvoiceData` with per-field confidence scores.

**Supported formats:** PDF, PNG, JPEG, WebP, GIF, TIFF, BMP

**Key implementation detail:** Uses `Part.from_bytes(data=file_bytes, mime_type=mime)` from the `google-genai` SDK. The MIME type is auto-detected from the file extension.

### StorageService (`storage_service.py`)

Handles three responsibilities:

1. **Idempotency cache** — SHA-256 hash of file content checked against `processed_documents` table. Cache hits skip the entire Gemini call.
2. **Dual-format output** — Every extraction writes both `{doc_id}.parquet` and `{doc_id}.json`, date-partitioned under `data/parquet/YYYY/MM/DD/` and `data/json/YYYY/MM/DD/`.
3. **Review item creation** — Creates review queue entries with calculated priority. Uses synchronous psycopg2 (not asyncpg) because this runs inside Celery workers.

**Atomic writes:** Every file write goes to a temp file first, then `os.rename()` into place. A crash mid-write leaves zero corruption.

### ReviewQueueService (`review_queue_service.py`)

Manages the human review workflow:

- **Priority calculation:** weighted composite of confidence (40%), SLA proximity (30%), line item count (20%), invoice total (10%)
- **Atomic claiming:** `UPDATE ... WHERE status IN ('pending', 'in_review')` — supports both initial claims and manual re-assignment
- **Auto-assignment:** Least-loaded strategy — picks reviewer with fewest active items, Redis INCR for tie-breaking
- **Reviewer filter:** Queue is filterable by `assigned_to` for workload visibility
- **Field locking:** Corrected fields are marked `locked=True` and preserved across re-processing
- **Audit trail:** Every action (claim, approve, correct, reject) is logged to `audit_log`

### MonitoringService (`monitoring_service.py`)

Exposes Prometheus metrics at `/api/metrics`:

- `documents_processed_total` (Counter) — by status label
- `document_processing_seconds` (Histogram) — P50/P95/P99
- `extraction_confidence_score` (Histogram) — confidence distribution
- `review_queue_depth` (Gauge) — by queue status
- `sla_breaches_total` (Counter) — by severity
- `documents_per_hour` (Gauge) — throughput rate

Metric snapshots are also saved hourly to `data/metrics/` for historical analysis.

---

## Database Schema

Five PostgreSQL tables, auto-created on startup:

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| **documents** | Upload tracking (lifecycle) | `id`, `status` (queued/processing/completed/failed/duplicate), `filename` |
| **processed_documents** | Idempotency cache | `content_hash` (SHA-256, PK), `result_json` |
| **review_items** | Review queue | `status`, `priority`, `sla_deadline`, `assigned_to`, UNIQUE on `document_id` |
| **extracted_fields** | Per-field extraction results | `field_name`, `value`, `confidence`, `locked`, `manually_corrected` |
| **audit_log** | Immutable action history | `item_id`, `action`, `field_name`, `old_value`, `new_value`, `actor` |

**Performance indexes:** `review_items(status)`, `review_items(priority DESC)`, `extracted_fields(review_item_id)`

---

## Celery Task Configuration

```python
task_time_limit     = 300        # 5 min hard kill
task_soft_time_limit = 270       # 4.5 min graceful shutdown
task_acks_late       = True      # re-queues on worker crash
worker_prefetch_multiplier = 1   # fair task distribution
max_retries          = 3         # exponential backoff: 10s → 20s → 40s
```

**Duplicate handling:** On idempotency cache hit, the document is marked `"duplicate"` and **no review item is created** (the original upload's review item already exists).

---

## Project Structure

```
backend/
├── main.py                         # Entrypoint
├── pyproject.toml                  # Python 3.13, uv managed
├── requirements.txt                # pip fallback
├── Dockerfile                      # Production image
├── src/
│   ├── config.py                   # Settings (env vars, .env file)
│   ├── api/
│   │   ├── main.py                 # FastAPI app, CORS, lifespan
│   │   └── routes.py               # All REST endpoints
│   ├── models/
│   │   └── schemas.py              # Pydantic models (InvoiceData, ReviewItem, etc.)
│   ├── services/
│   │   ├── database.py             # PostgreSQL DDL + asyncpg pool
│   │   ├── extraction_service.py   # Gemini API integration
│   │   ├── storage_service.py      # Idempotency + Parquet/JSON writes
│   │   ├── review_queue_service.py # Queue CRUD, claiming, field locking
│   │   └── monitoring_service.py   # Prometheus metric collectors
│   └── tasks/
│       └── celery_app.py           # Celery task definitions
├── configs/
│   ├── extraction_module_schema.yaml
│   ├── sla_definitions.yaml
│   └── dashboard_spec.yaml
├── tests/
│   ├── conftest.py                 # Shared fixtures
│   ├── unit/test_extraction.py     # Idempotency, locking, validation, priority
│   ├── integration/test_api.py     # API endpoints, review workflow
│   ├── quality/test_data_quality.py # Parquet/JSON consistency
│   └── performance/test_performance.py # Latency, throughput, concurrency
├── data/                           # Extraction outputs (date-partitioned)
│   ├── parquet/
│   ├── json/
│   └── metrics/
└── uploads/                        # Uploaded files
```

---

## Testing

```bash
uv run pytest -v --cov=src
```

| Suite | File | What It Tests |
|-------|------|---------------|
| **Unit** | `test_extraction.py` | Idempotency cache logic, field locking, validation rules, priority scoring |
| **Integration** | `test_api.py` | Full API request/response cycles, review workflow end-to-end, SLA ordering |
| **Data Quality** | `test_data_quality.py` | Parquet ↔ JSON consistency, schema compliance, null field handling |
| **Performance** | `test_performance.py` | P95 latency < 30s, 5000 docs/hr throughput, concurrent load, memory |

---

## Environment Variables

See the root [README.md](../README.md#configuration) for the full configuration table.