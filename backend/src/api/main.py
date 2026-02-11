"""FastAPI application entry point."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import router
from src.config import settings
from src.services.database import close_pool, init_db

_start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle hook."""
    await init_db()
    yield
    await close_pool()


app = FastAPI(
    title="Document Processing API",
    description="AI-powered invoice extraction & human review platform",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(router, prefix="/api")


@app.get("/health")
async def health():
    """Basic health-check."""
    return {
        "status": "ok",
        "version": "1.0.0",
        "uptime_seconds": round(time.time() - _start_time, 2),
    }
