# Workflow Engine Design

> How documents are modeled as a DAG, executed with concurrency control, and kept reliable under load.

---

## Architecture Overview

The workflow engine implements a **DAG-based (Directed Acyclic Graph)** execution model where each document-processing pipeline is represented as a graph of steps. Steps that don't depend on each other run in parallel; fan-in joins wait for all parents to complete.

Two layers work together:

| Layer | Responsibility | Technology |
|-------|---------------|------------|
| **DAG Executor** | Step ordering, concurrency, retries, conditional routing | `WorkflowExecutor` (asyncio) |
| **Task Distribution** | Cross-worker fan-out, message brokering, result collection | Celery + Redis |

```
                        ┌───────────────────────────────────────────┐
                        │           WorkflowExecutor                │
                        │  ┌──────┐   ┌──────────┐   ┌──────────┐  │
                        │  │Topo  │──▶│Semaphore  │──▶│Rate      │  │
                        │  │Sort  │   │Gate (N=4) │   │Limiter   │  │
                        │  └──────┘   └──────────┘   └──────────┘  │
                        └───────────────────────────────────────────┘
                                          │
                        ┌─────────────────┼──────────────────┐
                        ▼                 ▼                  ▼
                  ┌──────────┐     ┌──────────┐       ┌──────────┐
                  │  Celery  │     │  Celery  │       │  Celery  │
                  │ Worker 1 │     │ Worker 2 │  ...  │ Worker N │
                  └──────────┘     └──────────┘       └──────────┘
```

---

## DAG Data Structure

### Formal Representation

A workflow is a DAG G = (V, E) where:
- V = set of processing steps (nodes)
- E ⊆ V × V = dependency edges (if (u, v) ∈ E, step u must complete before v starts)

Each step v ∈ V has properties:

| Property | Type | Purpose |
|----------|------|---------|
| `id` | string | Unique step identifier |
| `fn` | async callable | The work to perform |
| `depends_on` | list[string] | Parent step IDs (incoming edges) |
| `condition` | predicate | Conditional routing — skip if false |
| `resource_tag` | string | Rate-limiter group (e.g. `"gemini_api"`) |
| `max_retries` | int | Per-step retry limit |
| `timeout_seconds` | float | Per-step deadline |

### Document Processing DAG

```
    extract ──► save_parquet ──► create_review
       │                              ▲
       └──► save_json ────────────────┘
       │
       └──► record_metrics
```

**Execution layers** (steps within a layer run in parallel):

| Layer | Steps | Notes |
|-------|-------|-------|
| 0 | `extract` | Single Gemini API call |
| 1 | `save_parquet`, `save_json`, `record_metrics` | Fan-out — all three run concurrently |
| 2 | `create_review` | Fan-in — waits for both saves to complete |

---

## Cycle Detection & Validation

Before execution, `WorkflowDAG.validate()` runs three checks:

### 1. Missing Dependencies
Every `depends_on` reference must point to an existing step ID.

### 2. Cycle Detection (Kahn's Algorithm)

```
function has_cycle(dag):
    in_degree = {v: 0 for v in dag.vertices}
    for each edge (u, v) in dag.edges:
        in_degree[v] += 1

    queue = [v for v in dag.vertices if in_degree[v] == 0]
    visited = 0

    while queue is not empty:
        node = queue.pop_front()
        visited += 1
        for each child of node:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    return visited != |V|   // True → cycle exists
```

**Complexity:** O(|V| + |E|) — linear in graph size.

If a cycle is detected, the DAG is rejected with an error listing the cycle size. This prevents deadlocks where step A waits for step B which waits for step A.

### 3. Non-Empty Graph
An empty DAG (no steps) is rejected.

---

## Topological Sort

Execution order is determined by **Kahn's algorithm** (BFS-based topological sort):

1. Compute in-degree for each node
2. Enqueue all zero-in-degree nodes (root steps)
3. For each dequeued node, reduce children's in-degree
4. When a child reaches zero, enqueue it

The result is a linear ordering where all parents come before their children.

For **parallelism**, `get_execution_layers()` groups steps by "distance from roots":

```python
layers = dag.get_execution_layers()
# [
#   ["extract"],                                      # Layer 0
#   ["save_parquet", "save_json", "record_metrics"],  # Layer 1 (parallel)
#   ["create_review"],                                # Layer 2 (fan-in)
# ]
```

---

## Concurrency Control

### Semaphore-Based Gate

An `asyncio.Semaphore(max_concurrency)` limits how many steps execute simultaneously:

```python
executor = WorkflowExecutor(max_concurrency=4)
# At most 4 steps run at the same time, regardless of layer size
```

This prevents resource exhaustion when processing large batches. The semaphore is acquired before step execution and released after (including on failure).

### Why Not Threads?

| Approach | Problem |
|----------|---------|
| Threads | GIL contention, complex synchronization |
| Processes | High memory overhead per step |
| **asyncio + Semaphore** | Cooperative scheduling, bounded concurrency, low overhead |

Since steps are I/O-bound (Gemini API calls, database writes, filesystem I/O), asyncio is the optimal choice.

---

## Rate Limiting

### Token Bucket Algorithm

Resource-tagged steps (e.g. `resource_tag="gemini_api"`) are rate-limited using a **token bucket**:

```
class TokenBucketRateLimiter:
    rate_per_second: float = 10.0
    burst: int = 1
    tokens: float = burst

    async acquire():
        while True:
            refill tokens based on elapsed time
            if tokens >= 1.0:
                consume one token
                return
            else:
                await sleep(1 / rate)
```

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `rate_per_second` | 10.0 | Sustained throughput cap |
| `burst` | 1 | Maximum instantaneous burst |

Multiple rate limiters can coexist (one per resource type):

```python
executor = WorkflowExecutor(
    rate_limiters={
        "gemini_api": TokenBucketRateLimiter(rate_per_second=10, burst=1),
        "database": TokenBucketRateLimiter(rate_per_second=50, burst=5),
    }
)
```

---

## Conditional Routing

Steps can have a `condition` predicate that receives the merged execution context. If the condition returns `False`, the step is **skipped** (status = `SKIPPED`).

```python
dag.add_step(
    "record_metrics",
    record_metrics_fn,
    depends_on=["extract"],
    condition=lambda ctx: ctx["step_outputs"].get("extract") is not None,
)
```

This enables:
- **Branching by document type** (e.g. skip line-item extraction for expense receipts)
- **Confidence-gated routing** (e.g. skip auto-approval if confidence < 0.8)
- **Error-aware skipping** (e.g. skip metrics if extraction failed)

Skipped steps don't block downstream steps that have other satisfied dependencies.

---

## Retry Strategy

Each step retries independently with **exponential backoff + jitter**:

```
delay = base × 2^attempt + jitter
where jitter ~ Uniform(0, base_delay × 0.5)
```

| Attempt | Base Delay | Jitter Range | Typical Delay |
|---------|-----------|--------------|---------------|
| 0 (1st try) | — | — | Immediate |
| 1 | 10s | 0–5s | ~12s |
| 2 | 20s | 0–10s | ~25s |
| 3 | 40s | 0–20s | ~50s |

**Why jitter?** Without jitter, retries from multiple failed tasks would all fire at the same time, creating a "thundering herd" that overwhelms the API. Jitter spreads retries across a random window.

### Per-Step Timeout

Each step has an individual timeout (default 300s). If exceeded, `asyncio.TimeoutError` is caught and the step enters its retry loop.

---

## Failure Propagation

When a step fails (all retries exhausted):

1. The step is marked `FAILED` with error details
2. All **direct and transitive dependents** are marked `SKIPPED` with error "Dependency failed"
3. **Independent branches** continue executing normally

```
extract ──► save_parquet ──► create_review
   │                              ▲
   └──► save_json ────────────────┘
   │
   └──► record_metrics

If save_json FAILS:
  • create_review → SKIPPED (dependency failed)
  • save_parquet → still runs (independent)
  • record_metrics → still runs (independent)
```

---

## Celery Integration

The DAG executor runs **within** Celery tasks. Each Celery worker runs an event loop for the async DAG execution:

### Single Document (DAG Execution)

```python
@app.task(bind=True, max_retries=3)
def process_document_task(self, document_id, file_path):
    dag = build_document_processing_dag(document_id, file_path)
    executor = WorkflowExecutor(
        max_concurrency=4,
        rate_limiters={"gemini_api": TokenBucketRateLimiter(10, 1)},
    )
    result = asyncio.run(executor.execute(dag, context={"doc_id": document_id}))
```

### Batch Processing (Celery group)

```
batch_process_task([doc1, doc2, doc3, …])
    │
    └── celery.group() ─┬── process_document_task(doc1)  ─┐
                        ├── process_document_task(doc2)   │ parallel
                        ├── process_document_task(doc3)   │ workers
                        └── …                            ─┘
                                    │
                                    ▼
                             Aggregate results
```

Each task in the group runs its own DAG independently. Celery handles cross-worker distribution.

---

## Worker Configuration

```python
app.conf.update(
    task_time_limit      = 300,   # 5 min hard kill
    task_soft_time_limit = 270,   # 4.5 min — raises SoftTimeLimitExceeded
    task_acks_late       = True,  # re-queues on crash
    worker_prefetch_multiplier = 1,  # fair distribution
    task_track_started   = True,  # enables "processing" status
)
```

| Setting | Value | Why |
|---------|-------|-----|
| `task_acks_late` | `True` | If worker crashes, task returns to queue |
| `worker_prefetch_multiplier` | `1` | Prevents one slow task from blocking others |
| `task_time_limit` | `300s` | Hard ceiling prevents stuck workers |

---

## Scalability Analysis

### Target: 5,000 documents/hour

| Config | Throughput |
|--------|-----------|
| 1 worker × 4 concurrency × ~3s/doc | ~4,800 docs/hr |
| 2 workers × 4 concurrency × ~3s/doc | ~9,600 docs/hr |
| **Recommended: 2 workers** | **> 5,000 docs/hr** |

Horizontal scaling is linear: adding workers doubles throughput with no code changes. The rate limiter prevents Gemini API overload regardless of worker count.

### Bottleneck Analysis

| Resource | Bottleneck Point | Mitigation |
|----------|-----------------|------------|
| Gemini API | ~10 req/s rate limit | Token bucket rate limiter |
| PostgreSQL | ~50 concurrent connections | asyncpg pool (max=10 per worker) |
| Filesystem | ~1000 writes/s (SSD) | Not a bottleneck |
| Redis | ~100K ops/s | Not a bottleneck |

---

## Monitoring Integration

Every step execution emits Prometheus metrics:

```python
documents_processed.labels(status="success").inc()
processing_duration.observe(elapsed_seconds)
extraction_confidence.observe(overall_confidence)
```

The `WorkflowResult` captures:
- Per-step status, duration, retry count
- Total workflow duration
- Completed / failed / skipped counts

These feed into the monitoring service's SLA compliance checks and dashboard metrics.

---

## Idempotency on Retry

Re-processing the same document is **always safe**:

1. **SHA-256 cache** — duplicate content returns cached result
2. **Review items** — `ON CONFLICT (document_id) DO UPDATE` prevents duplicates
3. **Field locking** — corrected fields are never overwritten
4. **Atomic file writes** — temp + rename means partial files never exist
5. **DAG re-execution** — completed steps are tracked; only failed steps re-run
