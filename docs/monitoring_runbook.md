# Monitoring Runbook

## Metrics Overview

All metrics are exposed via Prometheus at `GET /api/metrics`.

### Key Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `documents_processed_total` | Counter | Documents processed, labeled by status |
| `document_processing_seconds` | Histogram | Extraction duration (P50, P95, P99) |
| `extraction_confidence_score` | Histogram | Confidence distribution |
| `review_queue_depth` | Gauge | Current items per status |
| `sla_breaches_total` | Counter | SLA violations by severity |
| `review_duration_seconds` | Histogram | Human review time |
| `documents_per_hour` | Gauge | Current throughput rate |

### Prometheus Scrape Config

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'document-processor'
    scrape_interval: 15s
    static_configs:
      - targets: ['api:8000']
    metrics_path: '/api/metrics'
```

## Alert Thresholds

| Alert | Condition | Severity | Action |
|-------|-----------|----------|--------|
| High Latency | P95 > 30s | Warning | Scale workers |
| Error Rate | > 1% | Critical | Check Gemini API |
| Queue Depth | > 500 | Warning | Add reviewers |
| SLA Breach | > 0.1% | Critical | Escalate + investigate |
| Throughput Drop | < 4,500/hr | Warning | Check Redis/workers |

## Troubleshooting Guide

### 1. High Extraction Latency (P95 > 30s)

**Symptoms**: `document_processing_seconds` P95 exceeds 30 seconds.

**Diagnostic Steps**:
```bash
# Check Celery worker status
celery -A src.tasks.celery_app inspect active

# Check Redis queue depth
redis-cli LLEN celery

# Check worker concurrency
celery -A src.tasks.celery_app inspect stats | grep concurrency
```

**Resolution**:
1. Increase Celery concurrency: `--concurrency=8`
2. Add more worker nodes
3. Check Gemini API rate limits (may need quota increase)
4. Review PDF sizes — large multi-page documents take longer

### 2. High Error Rate (> 1%)

**Symptoms**: `documents_processed_total{status="failed"}` increasing.

**Diagnostic Steps**:
```bash
# Check recent errors
celery -A src.tasks.celery_app inspect reserved

# Check Gemini API status
curl -s https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview \
  -H "x-goog-api-key: $GEMINI_API_KEY" | head
```

**Resolution**:
1. Check Gemini API key validity
2. Verify document format compatibility (PDF, PNG, JPEG, WebP, etc.)
3. Review retry logs for patterns
4. Check for malformed documents in upload queue

### 3. Queue Depth > 500

**Symptoms**: `review_queue_depth{status="pending"}` exceeds 500.

**Resolution**:
1. Assign more reviewers
2. Increase auto-approval threshold for high-confidence items
3. Check if workers are stuck (inspect active tasks)
4. Consider batch approval for items with confidence > 95%

### 4. SLA Breaches

**Symptoms**: `sla_breaches_total` counter increasing.

**Diagnostic Steps**:
```sql
-- Check items near SLA breach (PostgreSQL)
SELECT id, filename, sla_deadline,
       ROUND(EXTRACT(EPOCH FROM (sla_deadline - NOW())) / 3600, 1) AS hours_remaining
FROM review_items
WHERE status IN ('pending', 'in_review')
ORDER BY sla_deadline ASC
LIMIT 20;
```

**Resolution**:
1. Prioritize items closest to SLA deadline
2. Escalate overdue items to senior reviewers
3. Consider extending SLA for batch uploads
4. Review assignment balance across reviewers

### 5. Redis Connection Issues

**Symptoms**: Celery workers disconnecting, tasks not queuing.

**Diagnostic Steps**:
```bash
# Test Redis connectivity
redis-cli ping

# Check Redis memory
redis-cli INFO memory | grep used_memory_human

# Check connected clients
redis-cli CLIENT LIST | wc -l
```

**Resolution**:
1. Restart Redis: `docker compose restart redis`
2. Check memory limits: `maxmemory` configuration
3. Clear stale connections: `CLIENT KILL`
4. Enable Redis persistence for durability

### 6. PostgreSQL Connection Pool Exhaustion

**Symptoms**: `asyncpg.TooManyConnectionsError` in API logs.

**Diagnostic Steps**:
```sql
-- Check active connections
SELECT count(*) FROM pg_stat_activity
WHERE datname = 'document_processing';

-- Check connection states
SELECT state, count(*)
FROM pg_stat_activity
WHERE datname = 'document_processing'
GROUP BY state;
```

**Resolution**:
1. Increase pool size in `database.py` (`max_size` parameter)
2. Ensure connections are released after use
3. Check for long-running queries holding connections
4. Restart the API service: `docker compose restart api`

## Scaling Recommendations

### Vertical Scaling

| Component | Current | Recommended for 10K docs/hr |
|-----------|---------|---------------------------|
| Celery workers | 1 × 4 concurrency | 3 × 4 concurrency |
| Redis memory | 256MB | 1GB |
| API workers | 1 uvicorn | 4 gunicorn workers |
| PostgreSQL | Default | Tune `shared_buffers`, `work_mem` |
| Disk I/O | Standard SSD | NVMe for Parquet writes |

### Horizontal Scaling

```
                    ┌─── Worker 1 (4 tasks)
Load Balancer ────┤
(nginx)           ├─── Worker 2 (4 tasks)
    │             └─── Worker 3 (4 tasks)
    ▼
┌────────┐      ┌────────┐
│ API 1  │      │ API 2  │    (behind LB)
└────────┘      └────────┘
    │               │
    └───────┬───────┘
            ▼
      ┌──────────┐       ┌──────────────┐
      │  Redis   │       │  PostgreSQL  │
      └──────────┘       └──────────────┘
```

### When to Scale

| Metric | Threshold | Scale Action |
|--------|-----------|-------------|
| Worker CPU > 80% | Sustained 5 min | Add worker node |
| Queue depth > 1000 | Growing trend | Add workers + reviewers |
| API latency P99 > 500ms | Sustained | Add API instance |
| Redis memory > 75% | Growing | Increase maxmemory |
| PostgreSQL connections > 80% | Sustained | Increase pool / add read replicas |

## Metric Snapshots

Metrics are automatically saved to `data/metrics/` every hour:

```
data/metrics/
├── snapshot_2025-01-15_14-00.json
├── snapshot_2025-01-15_15-00.json
└── ...
```

Each snapshot contains all current Prometheus metric values for historical analysis and trend detection.
