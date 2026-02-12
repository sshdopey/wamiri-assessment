# Review Queue Design

> How the human-in-the-loop workflow manages review priority, prevents conflicts, protects corrections, and maintains a full audit trail.

---

## Overview

After AI extraction, every document enters a **review queue** where human reviewers verify the results. The queue is designed for:

- **Priority ordering** — low-confidence, near-SLA items surface first
- **Atomic claiming** — two reviewers can't accidentally claim the same item
- **Field locking** — human corrections are never overwritten by re-extraction
- **Full audit trail** — every action is permanently logged

---

## Priority Algorithm

Each review item gets a priority score when created:

```
priority = confidence_weight + sla_weight + complexity_weight + value_weight
```

| Factor | Weight | Logic | Why |
|--------|--------|-------|-----|
| **Confidence** | 40% | `(100 - avg_confidence) × 0.4` | Low-confidence items need human attention most |
| **SLA proximity** | 30% | `(hours_until_deadline / 24) × 0.3` | Approaching deadlines must be prioritized |
| **Complexity** | 20% | `(line_item_count / 100) × 0.2` | Multi-item invoices take longer — start them sooner |
| **Invoice value** | 10% | `(total_amount / 10000) × 0.1` | Higher-value invoices warrant more careful review |

### Priority Bands (UI display)

| Score Range | Band | Badge Color |
|-------------|------|-------------|
| 70–100 | High priority | Red |
| 40–69 | Medium priority | Yellow |
| 0–39 | Low priority | Gray |

---

## SLA Tracking

Each item receives a deadline when created:

```python
sla_deadline = created_at + timedelta(hours=24)    # configurable via SLA_DEFAULT_HOURS
```

The dashboard shows a real-time countdown with color coding:

| Time Remaining | Color | Meaning |
|---------------|-------|---------|
| > 6 hours | Green | On track |
| 2–6 hours | Yellow | Needs attention soon |
| < 2 hours | Red | Urgent |
| Past deadline | Red + "OVERDUE" | Requires immediate action |

---

## Claim Flow (Atomic)

When a reviewer clicks "Claim":

```
Reviewer A clicks "Claim"          Reviewer B clicks "Claim" (same item)
        │                                    │
        ▼                                    ▼
  SELECT ... WHERE id=$1              SELECT ... WHERE id=$1
  AND status IN ('pending',           AND status IN ('pending',
     'in_review')                        'in_review')
        │                                    │
        ▼                                    ▼
  ✅ Row found                         ❌ Row already claimed by A
  UPDATE status='in_review'           → HTTP 409 Conflict
  SET assigned_to=A, claimed_at=NOW()
        │
        ▼
  Return item with all extracted fields
```

PostgreSQL's row-level locking ensures **exactly one reviewer** can claim an item. Items already `in_review` can be **re-assigned** to a different reviewer (supporting manual workload rebalancing).

---

## Auto-Assignment (Least-Loaded)

Documents are **automatically assigned** to a reviewer the moment they enter the review queue. The system uses a **least-loaded strategy**:

```
New review item created
        │
        ▼
  Query DB: count active items per reviewer
  (SELECT assigned_to, COUNT(*) WHERE status='in_review' GROUP BY assigned_to)
        │
        ▼
  Pick reviewer with minimum active count
        │
        ├── Tie? → Break with Redis INCR round-robin index
        │
        ▼
  UPDATE review_items
  SET status='in_review', assigned_to=<chosen>, claimed_at=NOW()
  WHERE id=<item_id>
```

**Why least-loaded > round-robin?**
- Round-robin assigns evenly **regardless of completion rate**
- If reviewer-1 is slow and reviewer-2 is fast, round-robin still gives both the same number
- Least-loaded naturally directs work to the reviewer who finishes faster
- Redis INCR for tie-breaking ensures fairness when all reviewers have equal load

**Roster:** Three built-in reviewer accounts (`reviewer-1`, `reviewer-2`, `reviewer-3`) are configured in `config.py`. The roster is returned by the `/queue/reviewer-workload` endpoint.

**Consequence:** The `pending` status is effectively transient — items are assigned within milliseconds of creation. The default queue view shows all statuses.

---

## Review Submission

After claiming, the reviewer has three options:

| Action | What Happens |
|--------|-------------|
| **Approve** | `status → 'approved'`, no field changes |
| **Correct** | `status → 'corrected'`, updated field values saved, corrections locked |
| **Reject** | `status → 'rejected'`, rejection reason recorded |

### What Happens on Correction

For each corrected field:
1. Field `value` is updated with the reviewer's input
2. `manually_corrected` = `TRUE`
3. `locked` = `TRUE`
4. `corrected_at` = current timestamp
5. `corrected_by` = reviewer identifier
6. An entry is added to `audit_log` with old and new values

---

## Field Locking

This is the **feedback loop** between AI and humans:

```
  AI extracts vendor = "Acne Corp" (confidence: 67%)
       │
       ▼
  Reviewer corrects to "Acme Corp"
       │
       ▼
  Field marked: locked=TRUE, manually_corrected=TRUE
       │
       ▼
  Same document re-uploaded later
       │
       ▼
  AI extracts vendor = "Acne Corp" again
       │
       ▼
  System checks: field is LOCKED → keeps "Acme Corp" ✓
```

**Unlocked fields** (never corrected) get overwritten on re-processing.
**Locked fields** (previously corrected) are preserved — the human's value wins.

---

## Audit Trail

Every action writes to the `audit_log` table:

```sql
INSERT INTO audit_log (item_id, action, field_name, old_value, new_value, actor)
VALUES ($1, $2, $3, $4, $5, $6);
```

This enables:
- **Correction pattern analysis** — which fields does the AI get wrong most often?
- **Reviewer performance tracking** — how many reviews per day, average time?
- **Compliance reporting** — full, immutable history of who changed what and when
- **Model improvement** — corrections become training signal for future AI tuning

---

## Database Schema

```sql
-- Idempotency cache
CREATE TABLE processed_documents (
    content_hash  TEXT PRIMARY KEY,             -- SHA-256 of file bytes
    document_id   TEXT NOT NULL,
    filename      TEXT NOT NULL,
    result_json   TEXT NOT NULL,                -- Full extraction result
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Review queue
CREATE TABLE review_items (
    id            TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL UNIQUE,         -- One review item per document
    filename      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','in_review','approved','corrected','rejected')),
    priority      DOUBLE PRECISION DEFAULT 0,
    sla_deadline  TIMESTAMPTZ,
    assigned_to   TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    claimed_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ
);

-- Per-field extraction results
CREATE TABLE extracted_fields (
    id                 TEXT PRIMARY KEY,
    review_item_id     TEXT NOT NULL REFERENCES review_items(id),
    field_name         TEXT NOT NULL,
    value              TEXT,
    confidence         DOUBLE PRECISION DEFAULT 0,
    manually_corrected BOOLEAN DEFAULT FALSE,
    corrected_at       TIMESTAMPTZ,
    corrected_by       TEXT,
    locked             BOOLEAN DEFAULT FALSE
);

-- Immutable action history
CREATE TABLE audit_log (
    id          SERIAL PRIMARY KEY,
    item_id     TEXT NOT NULL,
    action      TEXT NOT NULL,
    field_name  TEXT,
    old_value   TEXT,
    new_value   TEXT,
    actor       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Performance indexes
CREATE INDEX idx_review_items_status   ON review_items(status);
CREATE INDEX idx_review_items_priority ON review_items(priority DESC);
CREATE INDEX idx_extracted_fields_item ON extracted_fields(review_item_id);
```

---

## Concurrency Safeguards

| Scenario | Protection |
|----------|-----------|
| Two reviewers claim same item | Atomic `UPDATE WHERE status IN ('pending','in_review')` — one wins, one gets 409 |
| Simultaneous field corrections | PostgreSQL row-level locking |
| Queue reads during writes | PostgreSQL MVCC (readers never block writers) |
| Celery creates item while API reads | Separate connection pools (asyncpg for API, psycopg2 for workers) |
| Re-upload of processed document | Idempotency cache returns cached result, marks as "duplicate" |
