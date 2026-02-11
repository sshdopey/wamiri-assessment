"""Performance tests – latency, throughput, memory."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestLatency:
    """Individual endpoint latency must stay within SLA bounds."""

    @pytest.mark.asyncio
    async def test_queue_list_p95_under_200ms(self, client: AsyncClient):
        """GET /api/queue should respond within 200ms at p95."""
        times: list[float] = []
        for _ in range(20):
            start = time.perf_counter()
            resp = await client.get("/api/queue", params={"page": 1, "per_page": 10})
            elapsed = time.perf_counter() - start
            assert resp.status_code == 200
            times.append(elapsed)

        times.sort()
        p95 = times[int(len(times) * 0.95)]
        assert p95 < 0.2, f"p95 latency {p95:.3f}s exceeds 200ms SLA"

    @pytest.mark.asyncio
    async def test_stats_endpoint_under_100ms(self, client: AsyncClient):
        """GET /api/stats should respond within 100ms."""
        start = time.perf_counter()
        resp = await client.get("/api/stats")
        elapsed = time.perf_counter() - start
        assert resp.status_code == 200
        assert elapsed < 0.1, f"Stats latency {elapsed:.3f}s exceeds 100ms"

    @pytest.mark.asyncio
    async def test_health_under_50ms(self, client: AsyncClient):
        """Health endpoint must respond within 50ms."""
        start = time.perf_counter()
        resp = await client.get("/health")
        elapsed = time.perf_counter() - start
        assert resp.status_code == 200
        assert elapsed < 0.05, f"Health latency {elapsed:.3f}s exceeds 50ms"


class TestThroughput:
    """System must be able to handle concurrent review operations."""

    @pytest.mark.asyncio
    async def test_concurrent_queue_reads(self, client: AsyncClient):
        """50 concurrent GET /queue should all succeed."""

        async def fetch():
            resp = await client.get("/api/queue", params={"page": 1, "per_page": 5})
            return resp.status_code

        results = await asyncio.gather(*(fetch() for _ in range(50)))
        assert all(r == 200 for r in results), "Some concurrent reads failed"

    @pytest.mark.asyncio
    async def test_review_throughput_estimate(self, client: AsyncClient):
        """Measure operations/sec for queue listing."""
        count = 100
        start = time.perf_counter()
        for _ in range(count):
            await client.get("/api/queue", params={"page": 1, "per_page": 10})
        elapsed = time.perf_counter() - start

        ops_per_sec = count / elapsed
        # At minimum 50 ops/sec for lightweight list endpoint
        assert ops_per_sec > 50, (
            f"Throughput {ops_per_sec:.1f} ops/s too low (need >50)"
        )


class TestMemory:
    """Basic memory sanity checks."""

    def test_extraction_result_serialisation_size(self, sample_extraction_result):
        """Serialised result should be < 50KB for a single invoice."""
        data = sample_extraction_result.model_dump_json()
        size_kb = len(data) / 1024
        assert size_kb < 50, f"Serialised result {size_kb:.1f}KB exceeds 50KB"

    def test_large_batch_payload_bounded(self):
        """Creating many extraction results should not explode memory."""
        from src.models.schemas import (
            ExtractionResult,
            InvoiceData,
            LineItem,
        )

        items = []
        for i in range(100):
            result = ExtractionResult(
                document_id=f"doc-{i}",
                filename=f"test_{i}.pdf",
                invoice_data=InvoiceData(
                    vendor=f"Vendor {i}",
                    invoice_number=f"INV-{i:05d}",
                    date="2025-01-01",
                    total=100.0 + i,
                    currency="KES",
                    line_items=[
                        LineItem(
                            item="Item",
                            quantity=1,
                            unit_price=100.0,
                            total=100.0,
                        )
                    ],
                ),
                confidence_score=0.95,
                content_hash=f"hash-{i}",
            )
            items.append(result.model_dump_json())

        total_kb = sum(len(s) for s in items) / 1024
        # 100 results should be < 500KB total
        assert total_kb < 500, f"100 results = {total_kb:.0f}KB, too large"
