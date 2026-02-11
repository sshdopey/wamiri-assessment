# Review Queue Design

## Overview

The review queue manages the human-in-the-loop workflow where reviewers verify, correct, or approve AI-extracted invoice data. It implements atomic claiming, field-level locking, priority-based ordering, and SLA tracking — all backed by PostgreSQL.

## Priority Algorithm

Items are ranked by a weighted composite score:

```python
priority = (
    (100 - confidence_avg) * 0.4 +   # Lower confidence → higher priority
    (hours_until_sla / 24) * 0.3 +    # Closer to SLA → higher priority
    (num_line_items / 100) * 0.2 +    # More items → higher priority
    (total_amount / 10000) * 0.1      # Higher value → higher priority
)
```

### Weight Rationale

| Factor | Weight | Why |
|--------|--------|-----|
| Confidence | 40% | Low-confidence items need human attention most urgently |
| SLA proximity | 30% | Approaching deadlines must be prioritized |
| Complexity | 20% | Multi-item invoices take longer, should start sooner |
| Value | 10% | Higher-value invoices warrant more careful review |

### Priority Bands

| Score | Band | UI Badge Color |
|-------|------|----------------|
| 70–100 | High | Red (destructive) |
| 40–69 | Medium | Yellow |
| 0–39 | Low | Gray (secondary) |

## SLA Calculation

Each item receives a deadline upon creation:

```python
sla_deadline = created_at + timedelta(hours=settings.sla_default_hours)  # Default: 24h
```

The dashboard displays a real-time countdown with color coding:

| Time Remaining | Color | Urgency |
|---------------|-------|---------|
| < 2 hours | Red | Critical |
| 2–6 hours | Yellow | Warning |
| > 6 hours | Green | Normal |
| Overdue | Red, "OVERDUE" | Immediate |

## Assignment Logic

### Claim Flow

```
Reviewer clicks "Claim"
    │
    ├── 1. Query: SELECT ... WHERE id=$1 AND status='pending'
    │       └── If status ≠ 'pending' → HTTP 409 Conflict
    ├── 2. UPDATE status='in_review', assigned_to=$2, claimed_at=NOW()
    ├── 3. Return updated item with all extracted fields
    └── 4. Second concurrent claim → 409 Conflict (atomic check)
```

The claim is **atomic**: if two reviewers click simultaneously, only one succeeds. The second receives a 409 Conflict error.

### Review Submission

```
Reviewer clicks "Approve" / "Correct" / "Reject"
    │
    ├── Approve: status → 'approved', no field changes
    ├── Correct: status → 'corrected', apply corrections to unlocked fields
    │       └── Each corrected field: locked=True, corrected_by=reviewer
    └── Reject: status → 'rejected', reason recorded
```

### Field Locking on Correction

When a reviewer corrects a field:

1. Field value is updated
2. `manually_corrected = TRUE`
3. `locked = TRUE`
4. `corrected_at = NOW()`
5. `corrected_by = reviewer_id`
6. Audit log entry created

On re-processing (e.g., document re-upload):
- **Locked fields**: Skipped entirely, human value preserved
- **Unlocked fields**: Overwritten with new AI extraction

## Feedback Loop

```
  ┌──────────┐     Corrections     ┌──────────────┐
  │  AI      │◀────────────────────│  Reviewer    │
  │  Output  │                     │  Corrections │
  └────┬─────┘                     └──────────────┘
       │                                  │
       ▼                                  ▼
  ┌──────────┐                     ┌──────────────┐
  │  Parquet/ │                     │  Audit Log   │
  │  JSON    │                     │  (all edits) │
  └──────────┘                     └──────────────┘
```

The audit trail records every action:

```sql
INSERT INTO audit_log (item_id, action, field_name, old_value, new_value, actor)
VALUES ($1, $2, $3, $4, $5, $6);
```

This enables:
- Tracking correction patterns (which fields does AI get wrong most?)
- Reviewer performance analysis
- Compliance reporting
- Model improvement data collection

## Database Schema (PostgreSQL)

```sql
CREATE TABLE IF NOT EXISTS processed_documents (
    content_hash TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL,
    filename     TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS review_items (
    id           TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL,
    filename     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','in_review','approved','corrected','rejected')),
    priority     DOUBLE PRECISION DEFAULT 0,
    sla_deadline TIMESTAMPTZ,
    assigned_to  TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    claimed_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS extracted_fields (
    id                TEXT PRIMARY KEY,
    review_item_id    TEXT NOT NULL REFERENCES review_items(id),
    field_name        TEXT NOT NULL,
    value             TEXT,
    confidence        DOUBLE PRECISION DEFAULT 0,
    manually_corrected BOOLEAN DEFAULT FALSE,
    corrected_at      TIMESTAMPTZ,
    corrected_by      TEXT,
    locked            BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         SERIAL PRIMARY KEY,
    item_id    TEXT NOT NULL,
    action     TEXT NOT NULL,
    field_name TEXT,
    old_value  TEXT,
    new_value  TEXT,
    actor      TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Performance indexes
CREATE INDEX idx_review_items_status   ON review_items(status);
CREATE INDEX idx_review_items_priority ON review_items(priority DESC);
CREATE INDEX idx_extracted_fields_item ON extracted_fields(review_item_id);
```

## Concurrency Considerations

| Scenario | Protection |
|----------|-----------|
| Two reviewers claim same item | Atomic UPDATE with status check (only `pending` → `in_review`) |
| Simultaneous field corrections | Row-level locking via PostgreSQL |
| Queue read during write | PostgreSQL MVCC allows concurrent readers/writers |
| Celery worker creates item during API read | Separate asyncpg connections from the pool |
| Re-upload of same PDF | Idempotency cache returns extraction, new review item still created |
