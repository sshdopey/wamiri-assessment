"""Tests for WorkflowExecutor — DAG validation, topological sort, execution.

Covers:
- DAG construction and validation
- Cycle detection
- Topological sort
- Parallel execution with semaphore
- Conditional routing (step skipping)
- Retry with backoff and jitter
- Failure propagation to dependents
- Rate limiting
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.services.workflow_executor import (
    StepStatus,
    TokenBucketRateLimiter,
    WorkflowDAG,
    WorkflowExecutor,
    WorkflowResult,
)

# DAG construction


class TestWorkflowDAG:
    """Tests for the WorkflowDAG data structure."""

    def test_add_step(self):
        dag = WorkflowDAG()
        dag.add_step("a", _noop)
        assert "a" in dag.steps
        assert len(dag.steps) == 1

    def test_add_duplicate_step_raises(self):
        dag = WorkflowDAG()
        dag.add_step("a", _noop)
        with pytest.raises(ValueError, match="Duplicate step"):
            dag.add_step("a", _noop)

    def test_chaining(self):
        dag = WorkflowDAG()
        result = dag.add_step("a", _noop).add_step("b", _noop, depends_on=["a"])
        assert result is dag
        assert len(dag.steps) == 2

    def test_repr(self):
        dag = WorkflowDAG()
        dag.add_step("a", _noop)
        assert "a" in repr(dag)


# Validation


class TestDAGValidation:
    """Tests for DAG validation (cycle detection, missing deps)."""

    def test_valid_linear_dag(self):
        dag = _build_linear_dag()
        errors = dag.validate()
        assert errors == []

    def test_valid_diamond_dag(self):
        dag = _build_diamond_dag()
        errors = dag.validate()
        assert errors == []

    def test_empty_dag(self):
        dag = WorkflowDAG()
        errors = dag.validate()
        assert any("no steps" in e.lower() for e in errors)

    def test_missing_dependency(self):
        dag = WorkflowDAG()
        dag.add_step("a", _noop, depends_on=["nonexistent"])
        errors = dag.validate()
        assert any("nonexistent" in e for e in errors)

    def test_cycle_detection_simple(self):
        """A → B → A is a cycle."""
        dag = WorkflowDAG()
        dag.add_step("a", _noop, depends_on=["b"])
        dag.add_step("b", _noop, depends_on=["a"])
        errors = dag.validate()
        assert any("cycle" in e.lower() for e in errors)

    def test_cycle_detection_complex(self):
        """A → B → C → A is a cycle."""
        dag = WorkflowDAG()
        dag.add_step("a", _noop, depends_on=["c"])
        dag.add_step("b", _noop, depends_on=["a"])
        dag.add_step("c", _noop, depends_on=["b"])
        assert dag.has_cycle()

    def test_no_cycle(self):
        dag = _build_diamond_dag()
        assert not dag.has_cycle()


# Topological sort


class TestTopologicalSort:
    """Tests for topological ordering."""

    def test_linear_order(self):
        dag = _build_linear_dag()
        order = dag.topological_sort()
        assert order.index("a") < order.index("b")
        assert order.index("b") < order.index("c")

    def test_diamond_order(self):
        dag = _build_diamond_dag()
        order = dag.topological_sort()
        assert order.index("root") < order.index("left")
        assert order.index("root") < order.index("right")
        assert order.index("left") < order.index("join")
        assert order.index("right") < order.index("join")

    def test_cycle_raises(self):
        dag = WorkflowDAG()
        dag.add_step("a", _noop, depends_on=["b"])
        dag.add_step("b", _noop, depends_on=["a"])
        with pytest.raises(ValueError, match="Invalid DAG"):
            dag.topological_sort()

    def test_execution_layers_linear(self):
        dag = _build_linear_dag()
        layers = dag.get_execution_layers()
        assert layers == [["a"], ["b"], ["c"]]

    def test_execution_layers_diamond(self):
        dag = _build_diamond_dag()
        layers = dag.get_execution_layers()
        assert layers[0] == ["root"]
        assert set(layers[1]) == {"left", "right"}
        assert layers[2] == ["join"]


# Execution


class TestWorkflowExecution:
    """Tests for the WorkflowExecutor."""

    @pytest.mark.asyncio
    async def test_simple_execution(self):
        dag = WorkflowDAG()
        dag.add_step("step1", _noop)
        executor = WorkflowExecutor(max_concurrency=2)
        result = await executor.execute(dag)
        assert result.success
        assert result.completed_count == 1
        assert result.failed_count == 0

    @pytest.mark.asyncio
    async def test_linear_execution_order(self):
        """Steps execute in dependency order."""
        execution_log: list[str] = []

        async def make_step(name: str):
            async def step(ctx):
                execution_log.append(name)
                return name

            return step

        dag = WorkflowDAG()
        dag.add_step("a", await make_step("a"))
        dag.add_step("b", await make_step("b"), depends_on=["a"])
        dag.add_step("c", await make_step("c"), depends_on=["b"])

        executor = WorkflowExecutor(max_concurrency=4)
        result = await executor.execute(dag)

        assert result.success
        assert execution_log == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_parallel_execution(self):
        """Independent steps in the same layer run concurrently."""
        start_times: dict[str, float] = {}

        async def timed_step(ctx):
            name = ctx.get("_step_name", "?")
            start_times[name] = time.monotonic()
            await asyncio.sleep(0.1)
            return name

        async def step_left(ctx):
            start_times["left"] = time.monotonic()
            await asyncio.sleep(0.1)
            return "left"

        async def step_right(ctx):
            start_times["right"] = time.monotonic()
            await asyncio.sleep(0.1)
            return "right"

        dag = WorkflowDAG()
        dag.add_step("root", _noop)
        dag.add_step("left", step_left, depends_on=["root"])
        dag.add_step("right", step_right, depends_on=["root"])

        executor = WorkflowExecutor(max_concurrency=4)
        result = await executor.execute(dag)

        assert result.success
        # Left and right should start at approximately the same time
        assert abs(start_times["left"] - start_times["right"]) < 0.05

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """Semaphore limits concurrent execution."""
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def tracked_step(ctx):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.05)
            async with lock:
                current_concurrent -= 1

        dag = WorkflowDAG()
        # Create 5 independent steps
        for i in range(5):
            dag.add_step(f"step{i}", tracked_step)

        # Allow only 2 concurrent
        executor = WorkflowExecutor(max_concurrency=2)
        result = await executor.execute(dag)

        assert result.success
        assert max_concurrent <= 2

    @pytest.mark.asyncio
    async def test_conditional_skip(self):
        """Steps with false conditions are skipped."""
        dag = WorkflowDAG()
        dag.add_step("always", _noop)
        dag.add_step(
            "never",
            _noop,
            depends_on=["always"],
            condition=lambda ctx: False,
        )

        executor = WorkflowExecutor(max_concurrency=2)
        result = await executor.execute(dag)

        assert result.success
        assert result.steps["never"].status == StepStatus.SKIPPED
        assert result.skipped_count == 1

    @pytest.mark.asyncio
    async def test_conditional_execute(self):
        """Steps with true conditions execute normally."""
        dag = WorkflowDAG()
        dag.add_step("always", _noop)
        dag.add_step(
            "conditional",
            _noop,
            depends_on=["always"],
            condition=lambda ctx: True,
        )

        executor = WorkflowExecutor(max_concurrency=2)
        result = await executor.execute(dag)

        assert result.success
        assert result.steps["conditional"].status == StepStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_failure_propagation(self):
        """If a step fails, its dependents are skipped."""

        async def failing_step(ctx):
            raise RuntimeError("boom")

        dag = WorkflowDAG()
        dag.add_step("fail", failing_step, max_retries=0)
        dag.add_step("child", _noop, depends_on=["fail"])
        dag.add_step("independent", _noop)

        executor = WorkflowExecutor(max_concurrency=4)
        result = await executor.execute(dag)

        assert not result.success
        assert result.steps["fail"].status == StepStatus.FAILED
        assert result.steps["child"].status == StepStatus.SKIPPED
        assert result.steps["independent"].status == StepStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """Steps retry up to max_retries times."""
        attempt_count = 0

        async def flaky_step(ctx):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise RuntimeError("temporary failure")
            return "success"

        dag = WorkflowDAG()
        dag.add_step("flaky", flaky_step, max_retries=3, retry_backoff_base=0.01)

        executor = WorkflowExecutor(max_concurrency=2)
        result = await executor.execute(dag)

        assert result.success
        assert attempt_count == 3
        assert result.steps["flaky"].retries_used == 2

    @pytest.mark.asyncio
    async def test_timeout(self):
        """Steps that exceed timeout are treated as failures."""

        async def slow_step(ctx):
            await asyncio.sleep(10)

        dag = WorkflowDAG()
        dag.add_step("slow", slow_step, max_retries=0, timeout_seconds=0.1)

        executor = WorkflowExecutor(max_concurrency=2)
        result = await executor.execute(dag)

        assert not result.success
        assert result.steps["slow"].status == StepStatus.FAILED
        assert "timed out" in result.steps["slow"].error

    @pytest.mark.asyncio
    async def test_step_output_passed_to_dependents(self):
        """Step outputs are available to downstream steps via context."""

        async def producer(ctx):
            return {"value": 42}

        async def consumer(ctx):
            output = ctx["step_outputs"]["producer"]
            assert output["value"] == 42
            return "consumed"

        dag = WorkflowDAG()
        dag.add_step("producer", producer)
        dag.add_step("consumer", consumer, depends_on=["producer"])

        executor = WorkflowExecutor(max_concurrency=2)
        result = await executor.execute(dag)

        assert result.success
        assert result.steps["consumer"].output == "consumed"

    @pytest.mark.asyncio
    async def test_context_passed_to_steps(self):
        """External context is available to all steps."""

        async def check_context(ctx):
            return ctx.get("doc_id")

        dag = WorkflowDAG()
        dag.add_step("check", check_context)

        executor = WorkflowExecutor(max_concurrency=2)
        result = await executor.execute(dag, context={"doc_id": "abc123"})

        assert result.success
        assert result.steps["check"].output == "abc123"

    @pytest.mark.asyncio
    async def test_invalid_dag_raises(self):
        """Executing an invalid DAG raises ValueError."""
        dag = WorkflowDAG()  # empty
        executor = WorkflowExecutor()
        with pytest.raises(ValueError, match="invalid DAG"):
            await executor.execute(dag)

    @pytest.mark.asyncio
    async def test_workflow_result_metrics(self):
        """WorkflowResult captures timing and counts."""
        dag = _build_linear_dag()
        executor = WorkflowExecutor(max_concurrency=4)
        result = await executor.execute(dag)

        assert result.total_duration_seconds >= 0
        assert result.completed_count == 3
        assert result.failed_count == 0
        assert result.skipped_count == 0


# Rate Limiter


class TestTokenBucketRateLimiter:
    """Tests for the token bucket rate limiter."""

    @pytest.mark.asyncio
    async def test_acquire_within_rate(self):
        limiter = TokenBucketRateLimiter(rate_per_second=100, burst=5)
        t0 = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        elapsed = time.monotonic() - t0
        # 5 tokens available in burst — should be nearly instant
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_acquire_exceeding_burst_waits(self):
        limiter = TokenBucketRateLimiter(rate_per_second=10, burst=1)
        t0 = time.monotonic()
        await limiter.acquire()  # instant (1 token available)
        await limiter.acquire()  # must wait ~0.1s
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.08  # ~100ms for second token


# Helpers


async def _noop(ctx: dict = {}) -> str:
    return "ok"


def _build_linear_dag() -> WorkflowDAG:
    """a → b → c"""
    dag = WorkflowDAG()
    dag.add_step("a", _noop)
    dag.add_step("b", _noop, depends_on=["a"])
    dag.add_step("c", _noop, depends_on=["b"])
    return dag


def _build_diamond_dag() -> WorkflowDAG:
    """root → left, right → join (diamond/fan-out + fan-in)"""
    dag = WorkflowDAG()
    dag.add_step("root", _noop)
    dag.add_step("left", _noop, depends_on=["root"])
    dag.add_step("right", _noop, depends_on=["root"])
    dag.add_step("join", _noop, depends_on=["left", "right"])
    return dag
