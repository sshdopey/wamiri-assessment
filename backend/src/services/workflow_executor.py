"""General-purpose DAG-based workflow executor with concurrency control.

This module provides a workflow engine that:
- Represents processing pipelines as directed acyclic graphs (DAGs)
- Validates DAG structure (cycle detection, schema validation)
- Executes steps in topological order with asyncio-based parallelism
- Enforces concurrency limits via semaphores
- Applies token-bucket rate limiting per resource
- Supports conditional routing (branch on runtime predicates)
- Handles retries with exponential backoff and jitter

Example DAG for document processing::

    upload -> extract -> save_parquet -> create_review
                |                            ^
                +-> save_json ---------------+

Usage::

    executor = WorkflowExecutor(max_concurrency=4, rate_limit_per_sec=10.0)
    dag = WorkflowDAG()
    dag.add_step("extract", extract_fn, depends_on=[])
    dag.add_step("save_parquet", save_parquet_fn, depends_on=["extract"])
    dag.add_step("save_json", save_json_fn, depends_on=["extract"])
    dag.add_step("review", create_review_fn, depends_on=["save_parquet", "save_json"])
    result = await executor.execute(dag, context={"doc_id": "abc"})
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Data Structures


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StepResult:
    """Result of executing a single workflow step."""

    step_id: str
    status: StepStatus
    output: Any = None
    error: Optional[str] = None
    duration_seconds: float = 0.0
    retries_used: int = 0


@dataclass
class WorkflowStep:
    """A single node in the workflow DAG."""

    id: str
    fn: Callable[..., Awaitable[Any]]
    depends_on: list[str] = field(default_factory=list)
    max_retries: int = 3
    retry_backoff_base: float = 1.0
    condition: Optional[Callable[[dict[str, Any]], bool]] = None
    """Optional predicate — step is skipped if condition returns False."""
    resource_tag: Optional[str] = None
    """Tag for rate-limiting (e.g. 'gemini_api', 'database')."""
    timeout_seconds: Optional[float] = None


@dataclass
class WorkflowResult:
    """Aggregate result of a full workflow execution."""

    success: bool
    steps: dict[str, StepResult]
    total_duration_seconds: float = 0.0
    completed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0


# DAG


class WorkflowDAG:
    """Directed Acyclic Graph of processing steps.

    Provides structural validation (cycle detection, missing dependencies)
    and topological sorting for execution ordering.
    """

    def __init__(self) -> None:
        self._steps: dict[str, WorkflowStep] = {}
        self._adjacency: dict[str, list[str]] = defaultdict(list)
        self._reverse: dict[str, list[str]] = defaultdict(list)

    @property
    def steps(self) -> dict[str, WorkflowStep]:
        return self._steps

    # Building

    def add_step(
        self,
        step_id: str,
        fn: Callable[..., Awaitable[Any]],
        depends_on: list[str] | None = None,
        max_retries: int = 3,
        retry_backoff_base: float = 1.0,
        condition: Optional[Callable[[dict[str, Any]], bool]] = None,
        resource_tag: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> "WorkflowDAG":
        """Add a step to the DAG. Returns self for chaining."""
        if step_id in self._steps:
            raise ValueError(f"Duplicate step ID: {step_id}")

        deps = depends_on or []
        step = WorkflowStep(
            id=step_id,
            fn=fn,
            depends_on=deps,
            max_retries=max_retries,
            retry_backoff_base=retry_backoff_base,
            condition=condition,
            resource_tag=resource_tag,
            timeout_seconds=timeout_seconds,
        )
        self._steps[step_id] = step

        for dep in deps:
            self._adjacency[dep].append(step_id)
            self._reverse[step_id].append(dep)

        # Ensure nodes with no outgoing edges still appear
        if step_id not in self._adjacency:
            self._adjacency[step_id] = []

        return self

    # Validation

    def validate(self) -> list[str]:
        """Validate the DAG structure. Returns a list of error messages (empty = valid).

        Checks:
        1. No missing dependencies (edges point to existing steps)
        2. No cycles (Kahn's algorithm)
        3. At least one step exists
        """
        errors: list[str] = []

        if not self._steps:
            errors.append("DAG has no steps")
            return errors

        # Check for missing dependencies
        for step_id, step in self._steps.items():
            for dep in step.depends_on:
                if dep not in self._steps:
                    errors.append(
                        f"Step '{step_id}' depends on '{dep}' which does not exist"
                    )

        if errors:
            return errors

        # Cycle detection via Kahn's algorithm
        in_degree: dict[str, int] = {sid: 0 for sid in self._steps}
        for step in self._steps.values():
            for dep in step.depends_on:
                # dep → step (dep must finish before step)
                pass
            # Count in-degree from adjacency
        for parent, children in self._adjacency.items():
            for child in children:
                if child in in_degree:
                    in_degree[child] += 1

        # Recalculate properly
        in_degree = {sid: 0 for sid in self._steps}
        for step in self._steps.values():
            for dep in step.depends_on:
                if dep in self._steps:
                    in_degree[step.id] += 1

        queue: deque[str] = deque()
        for sid, deg in in_degree.items():
            if deg == 0:
                queue.append(sid)

        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for child in self._adjacency.get(node, []):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if visited != len(self._steps):
            errors.append(
                f"DAG contains a cycle (visited {visited}/{len(self._steps)} nodes)"
            )

        return errors

    def has_cycle(self) -> bool:
        """Quick check: does the DAG contain a cycle?"""
        return any("cycle" in e.lower() for e in self.validate())

    # Topological Sort

    def topological_sort(self) -> list[str]:
        """Return step IDs in topological order (Kahn's algorithm).

        Steps with no dependencies come first. Independent steps at the
        same level can be executed in parallel.

        Raises ValueError if the graph contains a cycle.
        """
        errors = self.validate()
        if errors:
            raise ValueError(f"Invalid DAG: {'; '.join(errors)}")

        in_degree: dict[str, int] = {sid: 0 for sid in self._steps}
        for step in self._steps.values():
            for dep in step.depends_on:
                if dep in self._steps:
                    in_degree[step.id] += 1

        queue: deque[str] = deque()
        for sid, deg in in_degree.items():
            if deg == 0:
                queue.append(sid)

        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for child in self._adjacency.get(node, []):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        return order

    def get_execution_layers(self) -> list[list[str]]:
        """Return steps grouped by execution level (parallelizable layers).

        Layer 0 = steps with no deps (can all run in parallel).
        Layer 1 = steps whose deps are all in layer 0, etc.
        """
        errors = self.validate()
        if errors:
            raise ValueError(f"Invalid DAG: {'; '.join(errors)}")

        in_degree: dict[str, int] = {sid: 0 for sid in self._steps}
        for step in self._steps.values():
            for dep in step.depends_on:
                if dep in self._steps:
                    in_degree[step.id] += 1

        current_layer: list[str] = [sid for sid, deg in in_degree.items() if deg == 0]
        layers: list[list[str]] = []

        while current_layer:
            layers.append(current_layer)
            next_layer: list[str] = []
            for node in current_layer:
                for child in self._adjacency.get(node, []):
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        next_layer.append(child)
            current_layer = next_layer

        return layers

    def __repr__(self) -> str:
        return f"WorkflowDAG(steps={list(self._steps.keys())})"


# Token Bucket Rate Limiter


class TokenBucketRateLimiter:
    """Async token-bucket rate limiter for resource-tagged steps.

    Ensures that steps tagged with the same resource don't exceed
    a given rate (e.g. 10 Gemini API calls per second).
    """

    def __init__(self, rate_per_second: float, burst: int = 1) -> None:
        self._rate = rate_per_second
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._burst,
                    self._tokens + elapsed * self._rate,
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

            # No token available — wait and retry
            await asyncio.sleep(1.0 / self._rate)


# Workflow Executor


class WorkflowExecutor:
    """Executes a WorkflowDAG with concurrency control, rate limiting, and retries.

    Parameters
    ----------
    max_concurrency : int
        Maximum number of steps running simultaneously (enforced by semaphore).
    rate_limiters : dict
        Mapping of resource_tag → TokenBucketRateLimiter.
    default_timeout : float
        Default per-step timeout in seconds (overridden by step.timeout_seconds).
    """

    def __init__(
        self,
        max_concurrency: int = 4,
        rate_limiters: dict[str, TokenBucketRateLimiter] | None = None,
        default_timeout: float = 300.0,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_limiters = rate_limiters or {}
        self._default_timeout = default_timeout

    async def execute(
        self,
        dag: WorkflowDAG,
        context: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """Execute all steps in the DAG respecting dependencies and concurrency.

        1. Validate the DAG (raises on cycle or missing deps).
        2. Compute execution layers (parallelizable groups).
        3. For each layer, launch all steps concurrently (bounded by semaphore).
        4. Within each step: evaluate condition, apply rate limiting, retry on failure.
        5. Downstream steps are skipped if any dependency failed.
        """
        context = context or {}
        t0 = time.monotonic()

        # Validate
        errors = dag.validate()
        if errors:
            raise ValueError(f"Cannot execute invalid DAG: {'; '.join(errors)}")

        layers = dag.get_execution_layers()
        results: dict[str, StepResult] = {}
        step_outputs: dict[str, Any] = {}

        for layer in layers:
            # Launch all steps in this layer concurrently
            tasks = []
            for step_id in layer:
                step = dag.steps[step_id]

                # Skip if any dependency failed
                dep_failed = any(
                    results.get(d, StepResult(d, StepStatus.PENDING)).status
                    in (StepStatus.FAILED,)
                    for d in step.depends_on
                )
                dep_skipped = any(
                    results.get(d, StepResult(d, StepStatus.PENDING)).status
                    == StepStatus.SKIPPED
                    for d in step.depends_on
                )

                if dep_failed:
                    results[step_id] = StepResult(
                        step_id=step_id,
                        status=StepStatus.SKIPPED,
                        error="Dependency failed",
                    )
                    continue

                tasks.append(self._execute_step(step, context, step_outputs, results))

            if tasks:
                await asyncio.gather(*tasks)

        elapsed = time.monotonic() - t0
        completed = sum(1 for r in results.values() if r.status == StepStatus.COMPLETED)
        failed = sum(1 for r in results.values() if r.status == StepStatus.FAILED)
        skipped = sum(1 for r in results.values() if r.status == StepStatus.SKIPPED)

        return WorkflowResult(
            success=failed == 0,
            steps=results,
            total_duration_seconds=round(elapsed, 3),
            completed_count=completed,
            failed_count=failed,
            skipped_count=skipped,
        )

    async def _execute_step(
        self,
        step: WorkflowStep,
        context: dict[str, Any],
        step_outputs: dict[str, Any],
        results: dict[str, StepResult],
    ) -> None:
        """Execute a single step with semaphore, rate limiting, condition, and retries."""
        step_id = step.id
        t0 = time.monotonic()

        # Conditional routing — skip if condition returns False
        if step.condition is not None:
            try:
                merged_ctx = {**context, "step_outputs": step_outputs}
                if not step.condition(merged_ctx):
                    results[step_id] = StepResult(
                        step_id=step_id,
                        status=StepStatus.SKIPPED,
                        duration_seconds=0.0,
                    )
                    logger.info("Step '%s' skipped (condition=False)", step_id)
                    return
            except Exception as exc:
                results[step_id] = StepResult(
                    step_id=step_id,
                    status=StepStatus.FAILED,
                    error=f"Condition evaluation failed: {exc}",
                )
                return

        # Acquire semaphore (concurrency control)
        async with self._semaphore:
            # Rate limiting
            if step.resource_tag and step.resource_tag in self._rate_limiters:
                await self._rate_limiters[step.resource_tag].acquire()

            # Retry loop
            last_error: Optional[str] = None
            retries = 0
            timeout = step.timeout_seconds or self._default_timeout

            for attempt in range(step.max_retries + 1):
                try:
                    merged_ctx = {**context, "step_outputs": step_outputs}
                    output = await asyncio.wait_for(
                        step.fn(merged_ctx),
                        timeout=timeout,
                    )
                    step_outputs[step_id] = output
                    results[step_id] = StepResult(
                        step_id=step_id,
                        status=StepStatus.COMPLETED,
                        output=output,
                        duration_seconds=round(time.monotonic() - t0, 3),
                        retries_used=retries,
                    )
                    logger.info(
                        "Step '%s' completed (%.2fs, %d retries)",
                        step_id,
                        time.monotonic() - t0,
                        retries,
                    )
                    return

                except asyncio.TimeoutError:
                    last_error = f"Step timed out after {timeout}s"
                    retries = attempt
                    logger.warning(
                        "Step '%s' timed out (attempt %d)", step_id, attempt + 1
                    )

                except Exception as exc:
                    last_error = str(exc)
                    retries = attempt
                    logger.warning(
                        "Step '%s' failed (attempt %d): %s",
                        step_id,
                        attempt + 1,
                        exc,
                    )

                # Exponential backoff with jitter before next retry
                if attempt < step.max_retries:
                    base_delay = step.retry_backoff_base * (2**attempt)
                    jitter = random.uniform(0, base_delay * 0.5)
                    delay = base_delay + jitter
                    logger.info(
                        "Step '%s' retrying in %.1fs (backoff=%.1f + jitter=%.1f)",
                        step_id,
                        delay,
                        base_delay,
                        jitter,
                    )
                    await asyncio.sleep(delay)

            # All retries exhausted
            results[step_id] = StepResult(
                step_id=step_id,
                status=StepStatus.FAILED,
                error=last_error,
                duration_seconds=round(time.monotonic() - t0, 3),
                retries_used=retries,
            )
            logger.error(
                "Step '%s' failed after %d attempts: %s",
                step_id,
                retries + 1,
                last_error,
            )


# Document Processing DAG Factory


def build_document_processing_dag(
    document_id: str,
    file_path: str,
    stored_filename: str | None = None,
) -> WorkflowDAG:
    """Build the standard document-processing DAG.

    Graph structure::

        extract -> save_parquet -> create_review
           |                            ^
           +-> save_json ---------------+
           |
           +-> record_metrics

    Steps ``save_parquet`` and ``save_json`` run in parallel (same layer).
    ``create_review`` waits for both saves to complete (fan-in / join).
    ``record_metrics`` runs independently after extraction.
    """
    from src.services.extraction_service import ExtractionService
    from src.services.storage_service import StorageService

    dag = WorkflowDAG()

    # Step 1: Extract invoice data via Gemini
    async def extract(ctx: dict) -> dict:
        svc = ExtractionService()
        result = svc.extract(
            file_path=ctx.get("file_path", file_path), document_id=document_id
        )
        return result.model_dump(mode="json")

    dag.add_step(
        "extract",
        extract,
        depends_on=[],
        max_retries=3,
        retry_backoff_base=10.0,
        resource_tag="gemini_api",
        timeout_seconds=120.0,
    )

    # Step 2a: Save Parquet (parallel with JSON)
    async def save_parquet(ctx: dict) -> str:
        from src.models.schemas import ExtractionResult

        result = ExtractionResult(**ctx["step_outputs"]["extract"])
        svc = StorageService()
        parquet_path, _ = svc.save_result(result)
        return str(parquet_path)

    dag.add_step(
        "save_parquet",
        save_parquet,
        depends_on=["extract"],
        max_retries=2,
        timeout_seconds=30.0,
    )

    # Step 2b: Save JSON (parallel with Parquet)
    async def save_json(ctx: dict) -> str:
        # save_result already writes both, but conceptually these are parallel
        # In practice, save_parquet already saved both. This step is a no-op
        # but demonstrates the DAG fan-out pattern.
        return ctx["step_outputs"].get("save_parquet", "")

    dag.add_step(
        "save_json",
        save_json,
        depends_on=["extract"],
        max_retries=1,
        timeout_seconds=30.0,
    )

    # Step 3: Create review queue item (fan-in: waits for both saves)
    async def create_review(ctx: dict) -> str:
        from src.models.schemas import ExtractionResult

        result = ExtractionResult(**ctx["step_outputs"]["extract"])
        svc = StorageService()
        svc.create_review_item(result)
        return result.document_id

    dag.add_step(
        "create_review",
        create_review,
        depends_on=["save_parquet", "save_json"],
        max_retries=2,
        timeout_seconds=30.0,
    )

    # Step 4: Record monitoring metrics (conditional — only on success)
    async def record_metrics(ctx: dict) -> None:
        from src.services.monitoring_service import monitoring

        extract_output = ctx["step_outputs"].get("extract", {})
        monitoring.record_processing(
            document_id=document_id,
            duration_seconds=extract_output.get("processing_time_seconds", 0),
            confidence=extract_output.get("overall_confidence", 0),
            success=True,
        )

    dag.add_step(
        "record_metrics",
        record_metrics,
        depends_on=["extract"],
        max_retries=1,
        condition=lambda ctx: ctx.get("step_outputs", {}).get("extract") is not None,
    )

    return dag
