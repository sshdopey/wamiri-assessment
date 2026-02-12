# Wamiri Invoices

**AI-powered invoice processing with human-in-the-loop review.**

> Built for the Wamiri Data & AI Engineer Assessment — a production-ready system that extracts structured data from invoices using Google Gemini, orchestrates processing through a distributed task queue, and provides a polished review dashboard for human verification.

---

## What This System Does

1. **Upload** a PDF or image invoice through the React dashboard
2. **AI Extraction** — Gemini reads the document and outputs structured fields (vendor, amounts, line items, dates)
3. **Quality Review** — items enter a priority-ranked queue where reviewers verify, correct, or approve the AI output
4. **Dual Output** — approved data is persisted in both Parquet (analytics) and JSON (API consumption) formats

Every step is observable: real-time status tracking, SLA countdowns, confidence scores, and Prometheus metrics.

---

## Architecture

```
  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
  │   React UI   │────▶│   FastAPI    │────▶│    Celery    │────▶│   Google     │
  │  (Vite/TS)   │     │   REST API   │     │   Workers    │     │   Gemini     │
  │  port 5173   │     │  port 8000   │     │  (4 threads) │     │   (LLM)      │
  └──────────────┘     └──────┬───────┘     └──────┬───────┘     └──────────────┘
                              │                    │
                    ┌─────────┴────────────────────┴─────────────────┐
                    │                                                │
             ┌──────▼──────┐    ┌───────────────┐    ┌──────────────▼──────────┐
             │    Redis    │    │  PostgreSQL   │    │     Filesystem         │
             │   (broker)  │    │  (state, SLA, │    │  Parquet + JSON        │
             │             │    │   audit log)  │    │  (date-partitioned)    │
             └─────────────┘    └───────────────┘    └────────────────────────┘
```

**Eight Docker services** — one `docker compose up` starts everything:

| Service | Image | Role |
|---------|-------|------|
| **redis** | redis:8-alpine | Message broker + Celery result backend |
| **postgres** | postgres:18-alpine | Review queue, idempotency cache, audit log |
| **api** | Python 3.13 / FastAPI | REST API (async, uvicorn) |
| **celery** | Python 3.13 / Celery | Background extraction workers (concurrency=4) |
| **celery-beat** | Python 3.13 / Celery | Periodic task scheduler (claim expiry) |
| **ui** | React 19 / nginx | Single-page dashboard |
| **prometheus** | prom/prometheus | Metrics collection & alerting |
| **grafana** | grafana/grafana | 12-panel monitoring dashboard |

---

## Quick Start

```bash
# 1. Clone and configure
cp backend/.env.example backend/.env    # ← add your GEMINI_API_KEY

# 2. Launch (Docker)
docker compose up -d

# 3. Open the dashboard
open http://localhost:5173
```

| URL | Service |
|-----|---------|
| http://localhost:5173 | Dashboard UI |
| http://localhost:8000 | API Server |
| http://localhost:8000/health | Health Check |
| http://localhost:8000/api/metrics | Prometheus Metrics |
| http://localhost:9090 | Prometheus UI |
| http://localhost:3000 | Grafana Dashboard (admin/wamiri) |

> **Requirements:** Docker & Docker Compose, a Google Gemini API key.

<details>
<summary><strong>Local development (without Docker)</strong></summary>

**Prerequisites:** Python 3.13+ with [uv](https://docs.astral.sh/uv/), Node.js 20+, Redis 7+, PostgreSQL 16+

```bash
# Backend
cd backend
cp .env.example .env              # set GEMINI_API_KEY
uv sync
redis-server &
uv run celery -A src.tasks.celery_app worker --loglevel=info --concurrency=4 &
uv run uvicorn src.api.main:app --reload --port 8000

# Frontend (separate terminal)
cd ui
npm install
npm run dev                       # → http://localhost:5173
```
</details>

---

## Key Design Decisions

### 1. Inline Gemini Extraction (No PDF Conversion)

Documents are sent directly to Gemini as **inline base64 bytes** using `Part.from_bytes()`. This eliminates the need for PDF-to-image libraries (no fitz, no PyMuPDF, no Poppler), reducing dependencies and container size while supporting **7 formats**: PDF, PNG, JPEG, WebP, GIF, TIFF, BMP.

### 2. SHA-256 Idempotency with Duplicate Detection

Every uploaded file is hashed. If the same content was already processed, the system skips the Gemini API call entirely, marks the upload as **"duplicate"**, and returns the cached result. This saves money and prevents unnecessary work.

### 3. Dual-Format Output (Parquet + JSON)

Extraction results are atomically written to **both** Parquet (for analytics/columnar queries via PyArrow) and JSON (for API consumption). Atomic writes use temp-file + rename — no corruption on failure.

### 4. Priority-Based Review Queue

Items are ranked by a weighted composite: **confidence** (40%) + **SLA proximity** (30%) + **complexity** (20%) + **invoice value** (10%). Low-confidence items near their SLA deadline surface first.

### 5. Least-Loaded Reviewer Assignment

Documents are **automatically assigned** to reviewers on creation. The system picks the reviewer with the fewest active (`in_review`) items, breaking ties with a rotating index stored in Redis. This replaces naive round-robin and prevents uneven workload distribution. Three built-in reviewer accounts (`reviewer-1`, `reviewer-2`, `reviewer-3`) rotate fairly under load.

### 6. Field Locking

When a reviewer corrects a field, it becomes **locked**. If the same document is re-processed, locked fields are preserved — the AI cannot overwrite human corrections.

### 7. Synchronous Celery Workers

Celery tasks use **psycopg2** (synchronous) instead of asyncpg to avoid event-loop conflicts in prefork workers. The API layer uses asyncpg for async performance. Each layer uses the right tool.

---

## Assessment Coverage

This project addresses all four parts of the Wamiri assessment:

### Part A — Processing Module Design (25%)

| Requirement | Implementation |
|-------------|----------------|
| LLM integration | Gemini `gemini-3-flash-preview` with structured Pydantic output schema |
| Idempotency | SHA-256 content hashing → `processed_documents` table in PostgreSQL |
| Field preservation | `extracted_fields` table with `locked` and `manually_corrected` flags |
| Dual output format | Parquet (PyArrow) + JSON, date-partitioned, atomic writes |
| Confidence scoring | Per-field confidence computed during extraction |
| Error handling | Retry with exponential backoff (10s → 20s → 40s), circuit breaking on final failure |

→ See [Module Architecture](docs/module_architecture.md)

### Part B — Workflow Orchestration & Parallelism (25%)

| Requirement | Implementation |
|-------------|----------------|
| DAG representation | Celery task chain: upload → extract → save → queue |
| Parallelism | `celery.group()` for batch processing, 4 concurrent workers |
| Rate limiting | Sequential Gemini calls per task, prefetch=1, connection pooling |
| Failure handling | `max_retries=3`, `task_acks_late=True` (re-queues on crash), exponential backoff with jitter |
| Execution semantics | Fan-out via `group()`, hard timeout 5min, soft timeout 4.5min |

→ See [Workflow Engine Design](docs/workflow_engine_design.md)

### Part C — Human Review + Dashboard UI (25%)

| Requirement | Implementation |
|-------------|----------------|
| Queue view | Priority-ranked cards with SLA countdowns, filter by status/reviewer, confidence badges |
| Document review | Split-pane: pdf.js canvas preview (left) + editable fields (right) |
| Review actions | Approve, Correct (inline editing), Reject with reason |
| Field editing | Type-aware inputs with confidence indicators and lock status |
| Auto-assignment | Least-loaded strategy — picks reviewer with fewest active items |
| Stats panel | KPI cards: queue depth, reviewed today, SLA compliance %, throughput |
| Dashboard | Full 5-page React SPA with real-time upload tracking |

→ See [Review Queue Design](docs/review_queue_design.md), [UI README](ui/README.md)

### Part D — Testing, Monitoring & SLAs (25%)

| Requirement | Implementation |
|-------------|----------------|
| Unit tests | Idempotency, field locking, validation, priority calculation |
| Integration tests | API endpoints, review workflow, SLA ordering |
| Data quality tests | Parquet/JSON consistency, schema compliance, null checks |
| Performance tests | P95 latency < 30s, throughput 5000/hr, concurrent load |
| UI tests | 11 Vitest tests: Dashboard, Queue, Upload, error handling |
| Monitoring | Prometheus metrics at `/api/metrics` with histograms and gauges |
| Grafana | 12-panel dashboard: stats, timeseries, heatmaps, dual-source (DB + Prometheus) |
| SLA tracking | 5 SLA thresholds with alerting (latency, throughput, error rate, queue depth, breach %) |

→ See [Monitoring Runbook](docs/monitoring_runbook.md)

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/documents/upload` | Upload PDF/image for AI extraction |
| `GET` | `/api/documents` | List all uploads with processing status |
| `GET` | `/api/documents/{id}/status` | Single document status |
| `GET` | `/api/documents/{id}/preview` | Inline preview (PDF or image) |
| `GET` | `/api/documents/{id}/download/{fmt}` | Download as Parquet or JSON |
| `GET` | `/api/queue` | List review queue (filterable by status, assigned_to, sortable) |
| `GET` | `/api/queue/{id}` | Single review item with extracted fields |
| `POST` | `/api/queue/{id}/claim` | Manually claim / re-assign for review |
| `POST` | `/api/queue/{id}/auto-assign` | Least-loaded auto-assignment |
| `GET` | `/api/queue/{id}/audit` | Full audit trail for a document |
| `PUT` | `/api/queue/{id}/submit` | Submit decision (approve/correct/reject) |
| `POST` | `/api/queue/expire-claims` | Release expired review claims |
| `GET` | `/api/queue/reviewer-workload` | Current workload per reviewer |
| `GET` | `/api/stats` | Dashboard KPIs |
| `GET` | `/api/metrics` | Prometheus metrics |
| `GET` | `/health` | Health check |

---

## SLA Targets

| Metric | Target | Window |
|--------|--------|--------|
| P95 extraction latency | < 30 seconds | 5 min |
| Throughput | > 4,500 docs/hour | 15 min |
| Error rate | < 1% | 5 min |
| Review queue depth | < 500 items | 5 min |
| SLA breach rate | < 0.1% | 1 hour |

---

## Testing

```bash
# Backend — 4 test suites via pytest
cd backend && uv run pytest -v --cov=src

# Frontend — 11 tests via Vitest
cd ui && npm test
```

| Suite | What It Covers |
|-------|----------------|
| **Unit** | Idempotency, field locking, validation logic, priority calculation |
| **Integration** | API endpoints, full review workflow, SLA ordering |
| **Data Quality** | Parquet/JSON consistency, schema compliance, null checks |
| **Performance** | P95 latency, throughput benchmarks, concurrent load, memory usage |
| **UI** | Dashboard rendering, queue display, upload flow, API error handling |

---

## Configuration

All settings via environment variables (`backend/.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | **Required.** Google Gemini API key |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | LLM model |
| `DATABASE_URL` | `postgresql://wamiri:…` | PostgreSQL connection |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker |
| `MAX_RETRIES` | `3` | Retry limit per document |
| `TASK_TIME_LIMIT` | `300` | Hard timeout (seconds) |
| `SLA_DEFAULT_HOURS` | `24` | Default review SLA |

---

## Scaling to 5,000+ Documents

The system is designed for **linear horizontal scaling** with zero code changes.

### Quick Scaling Guide

| Bottleneck | Signal | Fix |
|-----------|--------|-----|
| Extraction throughput | Queue depth growing | Add Celery workers: `docker compose up --scale celery=3` |
| API latency | P99 > 500ms | Add API instances behind nginx |
| Database connections | Pool exhaustion errors | Increase `max_size` or add PgBouncer |
| Gemini rate limit | 429 errors | Token bucket already handles this; request quota increase |

### Capacity Planning

| Configuration | Throughput | Cost |
|--------------|-----------|------|
| 1 worker × 4 concurrency | ~4,800 docs/hr | Baseline |
| 2 workers × 4 concurrency | ~9,600 docs/hr | 2× workers |
| 3 workers × 4 concurrency | ~14,400 docs/hr | 3× workers |

### Production Recommendations

1. **Workers**: Start with 2 Celery workers for 5K+ docs/hr headroom
2. **PostgreSQL**: Enable `pg_stat_statements` for slow query monitoring
3. **Redis**: Enable AOF persistence for broker durability
4. **Monitoring**: Watch Grafana dashboard — all 12 panels auto-populate within 15s
5. **Backups**: Schedule `pg_dump` for review queue state

---

## Project Structure

```
wamiri-assessment/
├── backend/
│   ├── src/
│   │   ├── api/                    # FastAPI app + REST routes
│   │   ├── models/                 # Pydantic schemas
│   │   ├── services/
│   │   │   ├── extraction_service  # Gemini inline extraction
│   │   │   ├── storage_service     # Idempotency + Parquet/JSON writes
│   │   │   ├── review_queue_service# Priority queue + field locking
│   │   │   ├── monitoring_service  # Prometheus metrics
│   │   │   └── database            # PostgreSQL DDL + async pool
│   │   └── tasks/                  # Celery task definitions
│   ├── configs/                    # YAML: dashboard, extraction schema, SLA
│   └── tests/                      # unit / integration / quality / performance
├── ui/
│   ├── src/
│   │   ├── pages/                  # Dashboard, Upload, Documents, Queue, Review
│   │   ├── components/             # shadcn/ui + PdfViewer + AppLayout
│   │   ├── lib/                    # API client, Zustand store, types
│   │   └── tests/                  # Vitest + Testing Library
│   └── package.json
├── docs/                           # Design documentation (4 docs)
├── docker-compose.yml              # Full-stack orchestration
└── README.md                       # ← you are here
```

---

## Documentation Index

| Document | What It Covers |
|----------|----------------|
| **[Module Architecture](docs/module_architecture.md)** | Processing pipeline, state machine, idempotency, field locking |
| **[Workflow Engine Design](docs/workflow_engine_design.md)** | Celery DAG, parallelism, retry policy, failure recovery |
| **[Review Queue Design](docs/review_queue_design.md)** | Priority algorithm, SLA, atomic claiming, database schema |
| **[Monitoring Runbook](docs/monitoring_runbook.md)** | Prometheus metrics, alerts, troubleshooting, scaling |
| **[Backend README](backend/README.md)** | Backend architecture, services, database schema |
| **[UI README](ui/README.md)** | Frontend pages, components, UX decisions |

---

## Tech Stack

| Layer | Technologies |
|-------|-------------|
| **Frontend** | React 19, TypeScript 5.9, Vite 7, TailwindCSS v4, shadcn/ui, Zustand 5, pdfjs-dist |
| **API** | Python 3.13, FastAPI, Pydantic v2, asyncpg, uvicorn |
| **Workers** | Celery, Redis 7, psycopg2, google-genai |
| **AI/LLM** | Google Gemini (`gemini-3-flash-preview`), structured output, inline base64 |
| **Database** | PostgreSQL 18 (5 tables: documents, processed_documents, review_items, extracted_fields, audit_log) |
| **Output** | PyArrow (Parquet) + JSON, date-partitioned |
| **Observability** | Prometheus + Grafana (12-panel dashboard with heatmaps), SLA monitoring, metric snapshots |
| **Testing** | pytest + coverage (backend), Vitest + Testing Library (frontend) |
| **DevOps** | Docker Compose (8 services), uv (Python), nginx (UI) |
