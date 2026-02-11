# Backend — Document Processing API

Python 3.13 backend powering the AI invoice extraction pipeline. Built with FastAPI, Celery, Google Gemini, and PostgreSQL.

## Tech Stack

| Component | Technology | Purpose |
|-----------|------------|---------|
| API | FastAPI + Uvicorn | Async REST endpoints |
| Task Queue | Celery + Redis | Background document processing |
| LLM | Google Gemini (`google-genai`) | PDF → structured invoice data |
| Database | PostgreSQL 16 (`asyncpg`) | Review queue, audit log, cache |
| Sync DB | `psycopg2` | PostgreSQL access from Celery tasks |
| Schemas | Pydantic v2 | Request/response validation |
| Config | `pydantic-settings` | Environment variable management |
| Metrics | `prometheus-client` | Observability |
| Output | PyArrow + JSON | Dual-format extraction results |
| Package Manager | `uv` | Fast dependency resolution |

## Project Structure

```
backend/
├── main.py                     # Entrypoint (imports FastAPI app)
├── pyproject.toml              # Python 3.13 dependencies (uv managed)
├── requirements.txt            # Pip-compatible fallback
├── Dockerfile                  # Production container image
├── src/
│   ├── config.py               # Settings class (env vars + .env file)
│   ├── api/
│   │   ├── main.py             # FastAPI app, CORS, lifespan, health check
│   │   └── routes.py           # All REST endpoints
│   ├── models/
│   │   └── schemas.py          # Pydantic models (InvoiceData, ReviewItem, etc.)
│   ├── services/
│   │   ├── database.py         # PostgreSQL DDL, asyncpg pool management
│   │   ├── extraction_service.py   # Gemini API integration
│   │   ├── storage_service.py      # Dual-format save, SHA-256 cache
│   │   ├── review_queue_service.py # Queue CRUD, claiming, field locking
│   │   └── monitoring_service.py   # Prometheus metric collectors
│   └── tasks/
│       └── celery_app.py       # Celery task definitions
├── configs/
│   ├── dashboard_spec.yaml
│   ├── extraction_module_schema.yaml
│   └── sla_definitions.yaml
├── tests/
│   ├── conftest.py             # Shared fixtures (sample PDFs, extraction results)
│   ├── unit/
│   │   └── test_extraction.py  # Idempotency, field locking, validation, priority
│   ├── integration/
│   │   └── test_api.py         # API endpoints, review workflow, SLA ordering
│   ├── quality/
│   │   └── test_data_quality.py # Parquet/JSON consistency, schema compliance
│   └── performance/
│       └── test_performance.py # Latency (P95), throughput, concurrency, memory
├── data/                       # Extraction outputs (date-partitioned)
│   ├── parquet/
│   ├── json/
│   └── metrics/
└── uploads/                    # Uploaded PDF files
```

## Setup

### Local Development

```bash
cd backend
cp .env.example .env              # Set GEMINI_API_KEY and database credentials
uv sync                           # Install all dependencies

# Start dependencies
redis-server &
# Ensure PostgreSQL is running with the configured database

# Start Celery worker
uv run celery -A src.tasks.celery_app worker --loglevel=info --concurrency=4 &

# Start API server
uv run uvicorn src.api.main:app --reload --port 8000
```

### Docker

The backend runs as two containers (`api` and `celery`) via the root `docker-compose.yml`. Both share the same image and `uploads` volume.

## Services

### ExtractionService

Sends uploaded documents (PDFs and images) directly to Google Gemini as inline bytes for structured extraction. The prompt asks Gemini to return an `InvoiceData` Pydantic model with vendor, invoice number, dates, line items, totals, and currency. Confidence scores are computed per field. Supported formats: PDF, PNG, JPEG, WebP, GIF, TIFF, BMP.

### StorageService

- **Idempotency cache**: SHA-256 hash of PDF content stored in `processed_documents` table. On cache hit, the cached extraction is returned but a new review queue item is still created for the current document ID.
- **Dual-format output**: Every extraction is saved as both Parquet (via PyArrow) and JSON, date-partitioned under `data/parquet/` and `data/json/`.
- **Atomic writes**: Uses temp-file + rename to prevent corruption on failure.

### ReviewQueueService

- **Priority calculation**: Weighted composite of confidence (40%), SLA proximity (30%), line item count (20%), and invoice total (10%).
- **Atomic claiming**: `SELECT ... WHERE status='pending'` → `UPDATE status='in_review'` prevents double-claiming.
- **Field locking**: Corrected fields are locked and preserved across re-processing.
- **Audit trail**: Every correction is logged to the `audit_log` table.

### MonitoringService

Exposes Prometheus metrics at `/api/metrics`: extraction counts, processing duration histograms, confidence distributions, queue depth gauges, and SLA breach counters.

## Database Schema

Four PostgreSQL tables (auto-created on startup):

| Table | Purpose |
|-------|---------|
| `documents` | Upload tracking from moment of upload (status: queued/processing/completed/failed) |
| `processed_documents` | SHA-256 content hash → cached extraction result |
| `review_items` | Review queue with status, priority, SLA deadline |
| `extracted_fields` | Per-field values, confidence, locking state |
| `audit_log` | Immutable record of all review actions |

## Testing

```bash
uv run pytest -v --cov=src
```

| Suite | File | Tests |
|-------|------|-------|
| Unit | `tests/unit/test_extraction.py` | Idempotency, field locking, validation, priority |
| Integration | `tests/integration/test_api.py` | API endpoints, review workflow, SLA ordering |
| Data Quality | `tests/quality/test_data_quality.py` | Parquet/JSON consistency, schema compliance |
| Performance | `tests/performance/test_performance.py` | P95 latency, throughput, concurrency, memory |

## Environment Variables

See root [README.md](../README.md#configuration) for the full configuration table.