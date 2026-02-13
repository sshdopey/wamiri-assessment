"""FastAPI application entry point."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from src.api.routes import router
from src.config import settings
from src.services.database import close_pool, init_db

_start_time = time.time()

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle hook."""
    await init_db()
    yield
    await close_pool()


TAG_METADATA = [
    {
        "name": "Documents",
        "description": "Upload, list, preview, and download processed documents.",
    },
    {
        "name": "Review Queue",
        "description": "Browse, claim, and submit human-review decisions on extracted invoices.",
    },
    {
        "name": "Dashboard",
        "description": "Aggregated statistics for the processing dashboard.",
    },
    {
        "name": "Monitoring",
        "description": "Prometheus-compatible metrics for operational observability.",
    },
]

app = FastAPI(
    title="Wamiri Invoices API",
    description="AI-powered invoice extraction & human review platform",
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=TAG_METADATA,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.get("/health")
async def health():
    """Basic health-check."""
    return {
        "status": "ok",
        "version": "1.0.0",
        "uptime_seconds": round(time.time() - _start_time, 2),
    }
