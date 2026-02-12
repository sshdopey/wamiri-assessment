# Monitoring Runbook

> Prometheus metrics, SLA definitions, alert thresholds, and troubleshooting procedures for production operation.

---

## Metrics

All metrics are exposed at `GET /api/metrics` in Prometheus text format. Metrics come from **two sources**:

1. **In-memory** (Prometheus client) — counters, histograms collected inside Celery workers
2. **DB-backed gauges** — refreshed every `/api/metrics` call by querying PostgreSQL (cross-process safe)

### Available Metrics

| Metric | Type | Source | What It Measures |
|--------|------|--------|-----------------|
| `documents_processed_total` | Counter | In-memory | Total documents processed (labeled `status=success\|failed`) |
| `document_processing_seconds` | Histogram | In-memory | Extraction duration — use for P50/P95/P99 |
| `extraction_confidence_score` | Histogram | In-memory | AI confidence distribution |
| `review_queue_depth` | Gauge | DB-backed | Current items per queue status (pending, in_review, etc.) |
| `sla_breaches_total` | Counter | In-memory | SLA violations, labeled by severity |
| `review_duration_seconds` | Histogram | In-memory | Time from claim to review submission |
| `documents_per_hour` | Gauge | DB-backed | Current throughput rate |
| `documents_total` | Gauge | DB-backed | Total document counts by status |
| `extraction_latency_p95` | Gauge | DB-backed | P95 extraction latency (SQL `percentile_cont`) |
| `extraction_confidence_avg` | Gauge | DB-backed | Average extraction confidence |
| `sla_compliance_rate` | Gauge | DB-backed | Percentage of items within SLA deadline |

### Prometheus Scrape Configuration

```yaml
scrape_configs:
  - job_name: 'wamiri-invoices'
    scrape_interval: 15s
    static_configs:
      - targets: ['api:8000']
    metrics_path: '/api/metrics'
```

---

## SLA Definitions

Five SLAs define "healthy" operation:

| SLA | Metric | Threshold | Window | Severity | Action |
|-----|--------|-----------|--------|----------|--------|
| **Latency** | P95 extraction time | < 30 seconds | 5 min | Critical | Scale workers |
| **Throughput** | Docs processed/hour | > 4,500 | 15 min | Warning | Check Redis + workers |
| **Error Rate** | Failed / total | < 1% | 5 min | Critical | Check Gemini API |
| **Queue Depth** | Pending review items | < 500 | 5 min | Warning | Add reviewers |
| **SLA Breach** | % items past deadline | < 0.1% | 1 hour | Critical | Escalate immediately |

---

## Troubleshooting

### 1. High Extraction Latency (P95 > 30s)

**You'll see:** `document_processing_seconds` P95 above 30s.

**Check:**
```bash
# Worker status
celery -A src.tasks.celery_app inspect active

# Redis queue depth (backlog)
redis-cli LLEN celery

# Worker load
celery -A src.tasks.celery_app inspect stats | grep concurrency
```

**Fix:**
1. Increase concurrency: `--concurrency=8` (if CPU allows)
2. Add worker nodes (horizontal scale)
3. Check Gemini API quotas — may need a quota increase
4. Large multi-page PDFs take longer — consider splitting

---

### 2. High Error Rate (> 1%)

**You'll see:** `documents_processed_total{status="failed"}` climbing.

**Check:**
```bash
# Recent task errors
celery -A src.tasks.celery_app inspect reserved

# Gemini API availability
curl -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview" \
  -H "x-goog-api-key: $GEMINI_API_KEY" | head
```

**Fix:**
1. Verify Gemini API key is valid and not expired
2. Check if specific file formats are causing failures
3. Review task error logs for patterns
4. Check for corrupt or malformed uploads

---

### 3. Queue Depth > 500

**You'll see:** `review_queue_depth{status="pending"}` above 500.

**Fix:**
1. Assign more reviewers to the queue
2. Consider auto-approval for items with confidence > 95%
3. Check if workers are stuck (tasks not completing)
4. Implement batch approval for high-confidence items

---

### 4. SLA Breaches

**You'll see:** `sla_breaches_total` counter increasing.

**Check:**
```sql
-- Items closest to SLA breach
SELECT id, filename, sla_deadline,
       ROUND(EXTRACT(EPOCH FROM (sla_deadline - NOW())) / 3600, 1) AS hours_left
FROM review_items
WHERE status IN ('pending', 'in_review')
ORDER BY sla_deadline ASC
LIMIT 20;
```

**Fix:**
1. Prioritize items nearest to deadline (the queue already does this)
2. Escalate overdue items to senior reviewers
3. Temporarily extend SLA for batch uploads if needed
4. Check reviewer workload balance

---

### 5. Redis Issues

**You'll see:** Workers disconnecting, tasks not queuing.

**Check:**
```bash
redis-cli ping                              # Connectivity
redis-cli INFO memory | grep used_memory    # Memory usage
redis-cli CLIENT LIST | wc -l              # Connected clients
```

**Fix:**
1. Restart: `docker compose restart redis`
2. Check `maxmemory` — increase if needed
3. Clear stale connections: `redis-cli CLIENT KILL`
4. Enable persistence (AOF/RDB) for durability

---

### 6. PostgreSQL Connection Exhaustion

**You'll see:** `asyncpg.TooManyConnectionsError` in API logs.

**Check:**
```sql
SELECT count(*) FROM pg_stat_activity WHERE datname = 'document_processing';

SELECT state, count(*) FROM pg_stat_activity
WHERE datname = 'document_processing' GROUP BY state;
```

**Fix:**
1. Increase pool `max_size` in `database.py`
2. Check for leaked connections (long-running queries)
3. Restart: `docker compose restart api`
4. Consider PgBouncer for connection pooling at scale

---

## Scaling Guide

### Grafana Dashboard (12 Panels)

The auto-provisioned Grafana dashboard (`Wamiri Invoices`) refreshes every 15 seconds and provides comprehensive visibility across five rows:

| Row | Panels | Source | Purpose |
|-----|--------|--------|---------|
| **Overview** | Documents Processed (stat), P95 Latency (gauge), Queue Depth (stat), Error Rate (gauge) | DB gauges | At-a-glance KPIs |
| **Timeseries** | Processing Latency P95, Throughput (docs/min) | Dual: DB gauge + `histogram_quantile` | Trend analysis with two independent data sources |
| **Quality** | Extraction Confidence, SLA Compliance (with breach overlay) | Dual: DB avg + histogram median | Quality trends + breach rate on right Y-axis |
| **Distributions** | Confidence Score (heatmap), Processing Duration (heatmap) | Prometheus histograms | Distribution shape analysis for 5K+ doc runs |
| **Rates** | Documents Processed Rate (success/failure), Queue Depth over time (stacked) | `rate()` + DB gauges | Live rate visibility + queue pressure |

**Dual-source strategy:** DB-backed gauges are always available (even with 1 document). Prometheus histograms populate as more data accumulates. Both are shown together — you always have at least one working source.

**Dashboard spec:** `backend/configs/dashboard_spec.yaml` (v2.0.0) documents all panel configurations.

### When to Scale

| Signal | Threshold | Action |
|--------|-----------|--------|
| Worker CPU > 80% for 5 min | Sustained | Add worker node |
| Queue depth > 1000 and growing | Trend | Add workers + reviewers |
| API P99 > 500ms | Sustained | Add API instance |
| Redis memory > 75% | Growing | Increase `maxmemory` |
| PostgreSQL connections > 80% | Sustained | Increase pool or add read replicas |

### Horizontal Scaling Architecture

```
                     ┌─── Celery Worker 1 (4 tasks) ──┐
  Load Balancer ────┤                                   ├── Gemini API
  (nginx)           ├─── Celery Worker 2 (4 tasks) ──┤
       │            └─── Celery Worker 3 (4 tasks) ──┘
       ▼
  ┌─────────┐  ┌─────────┐
  │ API #1  │  │ API #2  │   (behind load balancer)
  └────┬────┘  └────┬────┘
       └──────┬─────┘
              ▼
        ┌──────────┐     ┌──────────────┐
        │  Redis   │     │  PostgreSQL  │
        └──────────┘     └──────────────┘
```

### Capacity Estimates

| Config | Throughput | Notes |
|--------|-----------|-------|
| 1 worker × 4 concurrency | ~4,800 docs/hr | Single node, 3s avg extraction |
| 3 workers × 4 concurrency | ~14,000 docs/hr | Three nodes, linear scaling |
| + 2 API instances | Same throughput | Better request latency under load |

---

## Metric Snapshots

Metrics are automatically saved hourly to `data/metrics/`:

```
data/metrics/
├── snapshot_2026-02-11_14-00.json
├── snapshot_2026-02-11_15-00.json
└── ...
```

Each snapshot captures all current Prometheus metric values for historical trend analysis, even without a full Prometheus/Grafana setup.
