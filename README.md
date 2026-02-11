# Document Processing System

> AI-powered invoice extraction with human-in-the-loop review, built for the Wamiri Data & AI Engineer Assessment.

## Architecture

```
  ┌──────────┐      ┌──────────┐      ┌──────────────┐      ┌──────────────┐
  │  React   │      │  FastAPI  │      │   Celery     │      │   Gemini     │
  │  UI      │─────▶│  API      │─────▶│   Worker     │─────▶│   LLM        │
  │  :5173   │      │  :8000    │      │  (4 threads) │      │  (extraction)│
  └──────────┘      └──────────┘      └──────┬───────┘      └──────────────┘
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    ▼                         ▼                         ▼
             ┌──────────┐             ┌──────────────┐          ┌──────────────┐
             │  Redis   │             │  PostgreSQL  │          │  Dual Output │
             │  Broker  │             │  (Review DB) │          │ Parquet/JSON │
             └──────────┘             └──────────────┘          └──────────────┘
```

## Quick Start

### One-command setup (Docker)

```bash
cp backend/.env.example backend/.env   # Add your GEMINI_API_KEY
docker compose up -d
```

Open **http://localhost:5173** — the full application is ready.

| Service    | URL                              |
|------------|----------------------------------|
| UI         | http://localhost:5173             |
| API        | http://localhost:8000             |
| Health     | http://localhost:8000/health      |
| Metrics    | http://localhost:8000/api/metrics |

### Prerequisites (local development)

- Python 3.13+ with [uv](https://docs.astral.sh/uv/)
- Node.js 20+
- Redis 7+
- PostgreSQL 16+
- Google Gemini API key

### Backend (local)

```bash
cd backend
cp .env.example .env                # Add your GEMINI_API_KEY
uv sync                             # Install dependencies

redis-server &                       # Start Redis
uv run celery -A src.tasks.celery_app worker --loglevel=info --concurrency=4 &
uv run uvicorn src.api.main:app --reload --port 8000
```

### Frontend (local)

```bash
cd ui
npm install
npm run dev                          # http://localhost:5173
```

## Configuration

All settings are managed via environment variables in `backend/.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | Google Gemini API key (**required**) |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Gemini model name |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker URL |
| `REDIS_RESULT_BACKEND` | `redis://localhost:6379/1` | Celery result backend |
| `DATABASE_URL` | `postgresql://wamiri:wamiri_secret@localhost:5432/document_processing` | PostgreSQL connection string |
| `POSTGRES_USER` | `wamiri` | PostgreSQL user |
| `POSTGRES_PASSWORD` | `wamiri_secret` | PostgreSQL password |
| `POSTGRES_DB` | `document_processing` | PostgreSQL database name |
| `MAX_RETRIES` | `3` | Celery task retry limit |
| `TASK_TIME_LIMIT` | `300` | Hard task timeout (seconds) |
| `SLA_DEFAULT_HOURS` | `24` | Default SLA deadline (hours) |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/documents/upload` | Upload a PDF or image for AI extraction |
| `GET` | `/api/documents` | List all tracked documents with processing status |
| `GET` | `/api/documents/{id}/status` | Get a single document's processing status |
| `GET` | `/api/queue` | List review queue (paginated, filterable) |
| `GET` | `/api/queue/{id}` | Get a single review item with extracted fields |
| `POST` | `/api/queue/{id}/claim` | Atomically claim an item for review |
| `PUT` | `/api/queue/{id}/submit` | Submit a review decision (approve / correct / reject) |
| `GET` | `/api/stats` | Dashboard KPIs (queue depth, SLA, throughput) |
| `GET` | `/api/documents/{id}/preview` | Inline document preview (PDF or image) |
| `GET` | `/api/documents/{id}/download/{fmt}` | Download result as Parquet or JSON |
| `GET` | `/api/metrics` | Prometheus-compatible metrics |
| `GET` | `/health` | Health check |

## SLA Targets

| Metric | Target |
|--------|--------|
| P95 extraction latency | < 30 seconds |
| Batch throughput (100 docs) | < 5 minutes |
| Sustained throughput | 5,000 docs/hour |
| Error rate | < 1% |
| Review queue depth alert | 500 items |

## Testing

```bash
# Backend (pytest — unit, integration, quality, performance)
cd backend
uv run pytest -v --cov=src

# Frontend (Vitest — 11 tests)
cd ui
npm test
```

## Docker Services

| Service | Image | Purpose |
|---------|-------|---------|
| `redis` | `redis:7-alpine` | Celery broker & result backend |
| `postgres` | `postgres:16-alpine` | Review queue, audit log, idempotency cache |
| `api` | Built from `backend/Dockerfile` | FastAPI server (uvicorn) |
| `celery` | Built from `backend/Dockerfile` | Celery worker (concurrency=4) |
| `ui` | Built from `ui/Dockerfile` | React app served via nginx |

## Project Structure

```
├── backend/
│   ├── src/
│   │   ├── api/              # FastAPI app & REST routes
│   │   ├── tasks/            # Celery task definitions
│   │   ├── models/           # Pydantic schemas (InvoiceData, ReviewItem, etc.)
│   │   └── services/
│   │       ├── extraction_service.py    # Gemini PDF → structured data
│   │       ├── storage_service.py       # Dual-format save + idempotency cache
│   │       ├── review_queue_service.py  # Queue, claiming, field locking, SLA
│   │       ├── monitoring_service.py    # Prometheus metrics
│   │       └── database.py             # PostgreSQL schema & asyncpg pool
│   ├── configs/              # YAML specs (dashboard, extraction, SLA)
│   ├── tests/                # pytest suite (unit / integration / quality / perf)
│   └── pyproject.toml        # Python 3.13, uv managed
├── ui/
│   ├── src/
│   │   ├── pages/            # Dashboard, Upload, Documents, Queue, Review
│   │   ├── components/       # shadcn/ui + layout components
│   │   ├── lib/              # API client, Zustand store, types, upload tracking
│   │   └── tests/            # Vitest + Testing Library
│   └── package.json          # React 19, Vite 7, TailwindCSS v4
├── docs/                     # Architecture & design documentation
├── docker-compose.yml        # Full-stack orchestration (5 services)
└── README.md
```

## Documentation

| Document | Description |
|----------|-------------|
| [Module Architecture](docs/module_architecture.md) | Service breakdown, state machine, idempotency, field locking |
| [Workflow Engine Design](docs/workflow_engine_design.md) | Celery DAG, parallelism, retry policy, failure recovery |
| [Review Queue Design](docs/review_queue_design.md) | Priority algorithm, SLA, atomic claiming, database schema |
| [Monitoring Runbook](docs/monitoring_runbook.md) | Prometheus metrics, alerts, troubleshooting, scaling |
| [Backend README](backend/README.md) | Backend-specific setup and service details |
| [UI README](ui/README.md) | Frontend architecture, pages, and component library |

## License

Private — Wamiri Assessment Submission
