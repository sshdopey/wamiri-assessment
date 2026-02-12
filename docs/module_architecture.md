# Module Architecture

> How documents flow through the system — from upload to reviewed output.

---

## The Big Picture

The backend is split into **five services**, each with a single responsibility. They communicate through PostgreSQL (state), Redis (task queue), and the filesystem (outputs).

```
  Upload (PDF/image)
       │
       ▼
  ┌──────────┐    dispatch    ┌───────────┐    extract    ┌──────────────┐
  │ FastAPI   │──────────────▶│  Celery    │─────────────▶│  Gemini LLM  │
  │ routes.py │               │  Worker    │              │  (inline b64)│
  └──────────┘               └─────┬─────┘              └──────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
             ┌───────────┐  ┌──────────┐  ┌──────────────┐
             │ Storage   │  │ Review   │  │ Monitoring   │
             │ Service   │  │ Queue    │  │ Service      │
             │           │  │ Service  │  │              │
             │ • SHA-256 │  │ • Priority│ │ • Prometheus │
             │ • Parquet │  │ • Locking│  │ • SLA checks │
             │ • JSON    │  │ • Audit  │  │ • Snapshots  │
             └───────────┘  └──────────┘  └──────────────┘
                    │              │              │
                    └──────────────┼──────────────┘
                                   ▼
                            ┌──────────────┐
                            │  PostgreSQL  │
                            │  (5 tables)  │
                            └──────────────┘
```

---

## Document Lifecycle

A document passes through two distinct status tracks:

### Upload Status (tracked in `documents` table)

```
  queued ──▶ processing ──▶ completed
                   │
                   ├──▶ failed
                   └──▶ duplicate  (cache hit — same content already processed)
```

### Review Status (tracked in `review_items` table)

```
  pending ──▶ in_review ──▶ approved
                    │
                    ├──▶ corrected  (fields edited by reviewer)
                    └──▶ rejected   (with reason)
```

These are **separate concerns**: upload tracking shows "did the AI finish?", while review tracking shows "did a human verify it?".

---

## Service Breakdown

### 1. ExtractionService

**File:** `extraction_service.py`
**Job:** Send document bytes to Gemini, get back structured invoice data.

- Documents are sent as **inline base64** via `Part.from_bytes()` — no PDF-to-image conversion needed
- Gemini returns a `GeminiInvoiceSchema` (Pydantic model) with vendor, dates, amounts, line items
- Per-field confidence scores are computed and attached to the result
- **7 supported formats:** PDF, PNG, JPEG, WebP, GIF, TIFF, BMP

### 2. StorageService

**File:** `storage_service.py`
**Job:** Idempotency cache + dual-format file output + review item creation.

| Responsibility | How |
|----------------|-----|
| **Idempotency** | SHA-256 hash of file bytes → lookup in `processed_documents` table |
| **Parquet output** | PyArrow table → `data/parquet/YYYY/MM/DD/{doc_id}.parquet` |
| **JSON output** | Pydantic `.model_dump_json()` → `data/json/YYYY/MM/DD/{doc_id}.json` |
| **Atomic writes** | Write to temp file first, then `os.rename()` into place |
| **Review creation** | Insert into `review_items` + `extracted_fields` with calculated priority |
| **Auto-assignment** | Least-loaded strategy — picks reviewer with fewest active items, Redis INCR for tie-breaking |

Uses **psycopg2** (synchronous) because it runs inside Celery prefork workers where asyncpg would cause event-loop errors.

### 3. ReviewQueueService

**File:** `review_queue_service.py`
**Job:** Queue operations, atomic claiming, field locking, audit trail.

- Priority is a weighted composite: confidence (40%) + SLA proximity (30%) + complexity (20%) + value (10%)
- Claims use `UPDATE ... WHERE status IN ('pending', 'in_review')` — supports both initial claims and manual re-assignment
- Auto-assignment uses **least-loaded** strategy: queries each reviewer's active item count, picks the minimum
- Corrected fields are marked `locked=True` and preserved across re-processing
- Every action writes to the `audit_log` table

### 4. MonitoringService

**File:** `monitoring_service.py`
**Job:** Prometheus metrics, SLA compliance checks, hourly snapshots.

Exposes counters, histograms, and gauges at `GET /api/metrics` — ready for Grafana dashboards.

### 5. Database

**File:** `database.py`
**Job:** PostgreSQL schema DDL, asyncpg connection pool management.

- Pool: min=2, max=10 connections
- Tables are auto-created on app startup
- Used by the API layer (async); Celery workers use their own psycopg2 connections

---

## Idempotency Mechanism

Preventing duplicate Gemini API calls (which cost money and time):

```
Upload arrives
     │
     ▼
SHA-256(file bytes)
     │
     ▼
Look up hash in `processed_documents` table
     │
     ├── CACHE HIT:  Return cached result, mark document as "duplicate"
     │                (no review item created — original's already exists)
     │
     └── CACHE MISS: Run full extraction pipeline
                     Save to Parquet + JSON
                     Cache result in `processed_documents`
                     Create review queue item
```

**Why this matters:** If someone uploads the same invoice twice, the system skips the ~5-second Gemini call, saves API costs, and correctly identifies it as a duplicate in the UI.

---

## Field Locking

Human corrections are protected from AI overwrite:

| Scenario | What Happens |
|----------|-------------|
| First extraction (AI-only) | Fields are unlocked, AI values stored |
| Human corrects a field | Field marked `locked=True`, `manually_corrected=True`, correction logged |
| Same document re-processed | Locked fields are **skipped** — human value preserved |
| New document (different content) | Full extraction, no locks carry over |

This ensures that once a human reviewer has corrected a value, the AI cannot accidentally overwrite it — even if the document is re-uploaded or re-processed.

---

## Module Dependencies

```
config.py ──────────────────────────────────────────────┐
     │                                                   │
     ▼                                                   ▼
schemas.py ◀── extraction_service.py ◀── storage_service.py
                        │                      │
                        ▼                      ▼
               workflow_executor.py    review_queue_service.py
                                              │
                                              ▼
                                    monitoring_service.py
                                              │
                                              ▼
                                         database.py
                                              │
                                              ▼
                                    routes.py → main.py
```

All services depend on `config.py` (settings) and `schemas.py` (shared Pydantic models). The dependency flow is strictly top-down — no circular imports.

---

## Retry Strategy & Backoff

All retries use **exponential backoff with jitter** to avoid thundering herds:

```
delay = base × 2^attempt + uniform(0, base × 2^attempt × 0.5)
```

| Layer | Max Retries | Base Delay | Backoff Sequence |
|-------|-------------|-----------|------------------|
| **Celery task** | 3 | 10s | 10s → 20s → 40s (+ jitter) |
| **DAG step** | 3 | configurable | Per-step, with per-step timeout |
| **Database reconnect** | automatic | — | asyncpg pool auto-reconnects |

### Why Jitter?

Without jitter, if 100 tasks fail simultaneously (e.g. Gemini outage), they all retry at exactly the same time — creating a "retry storm" that re-overloads the API. Adding `uniform(0, delay × 0.5)` spreads retries across a random window.

### Failure States

```
  attempt 1 ──► fail ──► wait 10s + jitter
  attempt 2 ──► fail ──► wait 20s + jitter
  attempt 3 ──► fail ──► wait 40s + jitter
  attempt 4 ──► PERMANENT FAILURE ──► mark "failed", log error, notify
```

---

## Circuit Breaker Pattern

The extraction pipeline implements a **circuit breaker** to prevent cascading failures when the Gemini API is down or rate-limited.

### State Machine

```
    ┌─────────┐         failure_count > 5         ┌─────────┐
    │ CLOSED  │ ──────────────────────────────── ▶ │  OPEN   │
    │ (normal)│                                    │ (reject)│
    └─────────┘                                    └────┬────┘
         ▲                                              │
         │   success                      recovery_timeout (60s)
         │                                              │
    ┌────┴──────┐                                       │
    │ HALF-OPEN │ ◀ ────────────────────────────────────┘
    │ (probe)   │
    └───────────┘
```

| State | Behavior |
|-------|----------|
| **CLOSED** | All requests pass through normally. Track failure count. |
| **OPEN** | All requests are immediately rejected (no API call). Entered when failures exceed threshold (5). |
| **HALF-OPEN** | After recovery timeout (60s), allow ONE probe request. If it succeeds → CLOSED. If it fails → OPEN. |

Configuration (from `extraction_module_schema.yaml`):
```yaml
circuit_breaker:
  failure_threshold: 5
  recovery_timeout_seconds: 60
```

This prevents wasting resources on a known-broken API and gives the external service time to recover.

---

## Scalability Analysis

### Target: 5,000 documents per hour

**Calculation:**

| Variable | Value |
|----------|-------|
| Avg Gemini extraction time | ~3 seconds |
| Worker concurrency (prefork) | 4 processes |
| Docs per worker per hour | 4 × (3600 / 3) = 4,800 |
| **1 worker node** | **~4,800 docs/hr** |
| **2 worker nodes** | **~9,600 docs/hr** |

A single worker node nearly meets the 5,000/hr target. Two nodes exceed it comfortably.

### Horizontal Scaling

```
                     ┌── Worker A (4 processes) ──┐
  Redis Queue ──────┤── Worker B (4 processes) ──┤── PostgreSQL
                     └── Worker C (4 processes) ──┘
```

Scaling is **linear**: adding a Celery worker doubles throughput with zero code changes. Workers are stateless and share only the Redis queue and PostgreSQL database.

### Bottleneck Analysis

| Resource | Capacity | Mitigation |
|----------|----------|------------|
| Gemini API | ~10 req/s (rate limited) | Token-bucket rate limiter in WorkflowExecutor |
| PostgreSQL | ~10K writes/s | asyncpg pool (2–10 connections per worker) |
| Redis | ~100K ops/s | Not a bottleneck |
| Filesystem | ~1K writes/s (SSD) | Not a bottleneck |
| Memory | ~50 MB per worker | Low — no document caching |

### Vertical Scaling

For very high throughput, increase `worker_concurrency` up to the number of CPU cores. Beyond that, add worker nodes horizontally.

---

## Configuration Schema (YAML)

The extraction pipeline is configurable via `configs/extraction_module_schema.yaml`:

| Section | Purpose |
|---------|---------|
| `fields` | Per-field validation rules and confidence thresholds |
| `idempotency` | Hash algorithm, cache TTL, field preservation |
| `output` | Parquet/JSON path templates, compression, atomic writes |
| `processing` | Concurrency limits, retry policy, circuit breaker |
| `validation` | Cross-field rules (e.g. total ≈ subtotal + tax) |

The YAML config is loaded at service startup and drives:
- Field-level confidence thresholds (used instead of hardcoded values)
- Validation rules for pattern matching, enum checks, and range validation
- Cross-field consistency checks with configurable tolerance
- Required fields for review approval
