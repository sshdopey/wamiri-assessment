"""Integration tests – API endpoints, workflow execution, review queue."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.main import app
from src.services.database import init_db


@pytest.fixture(autouse=True)
async def _setup_db():
    """Ensure DB is initialised before every test."""
    await init_db()


@pytest.fixture()
async def client():
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# Queue Endpoints


class TestQueueAPI:
    @pytest.mark.asyncio
    async def test_get_queue_empty(self, client: AsyncClient):
        """GET /api/queue returns empty list when no items."""
        resp = await client.get("/api/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert isinstance(data["items"], list)

    @pytest.mark.asyncio
    async def test_get_queue_pagination(self, client: AsyncClient):
        """Pagination params are honoured."""
        resp = await client.get("/api/queue", params={"limit": 5, "offset": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 5
        assert data["offset"] == 0

    @pytest.mark.asyncio
    async def test_get_nonexistent_item(self, client: AsyncClient):
        """GET /api/queue/{bad_id} returns 404."""
        resp = await client.get("/api/queue/nonexistent-id")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_claim_nonexistent_item(self, client: AsyncClient):
        """POST /api/queue/{bad_id}/claim returns 409."""
        resp = await client.post(
            "/api/queue/nonexistent/claim",
            json={"reviewer_id": "user-1"},
        )
        assert resp.status_code == 409


# Stats Endpoint


class TestStatsAPI:
    @pytest.mark.asyncio
    async def test_stats_returns_valid_shape(self, client: AsyncClient):
        """GET /api/stats returns expected keys."""
        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "queue_depth" in data
        assert "items_reviewed_today" in data
        assert "avg_review_time_seconds" in data
        assert "sla_compliance_percent" in data


# Health Endpoint


class TestHealth:
    @pytest.mark.asyncio
    async def test_health(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# Upload Endpoint


class TestUpload:
    @pytest.mark.asyncio
    async def test_upload_rejects_unsupported_type(self, client: AsyncClient):
        """Only PDF and image files should be accepted."""
        resp = await client.post(
            "/api/documents/upload",
            files={"file": ("test.txt", b"not a pdf", "text/plain")},
        )
        assert resp.status_code == 400


# Review Workflow


class TestReviewWorkflow:
    @pytest.mark.asyncio
    async def test_full_review_cycle(
        self, client: AsyncClient, sample_extraction_result
    ):
        """Create item → claim → approve → verify status."""
        from src.services.review_queue_service import ReviewQueueService

        svc = ReviewQueueService()
        item = await svc.create_item(sample_extraction_result)

        # Claim
        resp = await client.post(
            f"/api/queue/{item.id}/claim",
            json={"reviewer_id": "tester"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "in_review"

        # Approve
        resp = await client.put(
            f"/api/queue/{item.id}/submit",
            json={"action": "approve", "corrections": {}},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    @pytest.mark.asyncio
    async def test_double_claim_rejected(
        self, client: AsyncClient, sample_extraction_result
    ):
        """Second claim on same item returns 409."""
        from src.services.review_queue_service import ReviewQueueService

        svc = ReviewQueueService()
        item = await svc.create_item(sample_extraction_result)

        # First claim succeeds
        resp1 = await client.post(
            f"/api/queue/{item.id}/claim",
            json={"reviewer_id": "user-a"},
        )
        assert resp1.status_code == 200

        # Second claim fails
        resp2 = await client.post(
            f"/api/queue/{item.id}/claim",
            json={"reviewer_id": "user-b"},
        )
        assert resp2.status_code == 409

    @pytest.mark.asyncio
    async def test_sla_ordering(self, client: AsyncClient, sample_extraction_result):
        """Items closer to SLA deadline appear first when sorted by SLA."""
        from src.services.review_queue_service import ReviewQueueService

        svc = ReviewQueueService()
        # Create multiple items (they'll have different priorities)
        await svc.create_item(sample_extraction_result)
        await svc.create_item(sample_extraction_result)

        resp = await client.get("/api/queue", params={"sort_by": "sla"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        if len(items) >= 2:
            # SLA ordering: earlier deadline first
            assert items[0]["sla_deadline"] <= items[1]["sla_deadline"]
