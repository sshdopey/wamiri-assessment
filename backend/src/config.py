"""Application configuration using pydantic-settings."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central application configuration.

    Values are read from environment variables first, then from a `.env` file
    if present.  All variable names are prefixed with nothing (flat).
    """

    # ── Gemini / LLM ──────────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3-flash-preview"

    # ── Redis ──────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_result_backend: str = "redis://localhost:6379/1"

    # ── Database ───────────────────────────────────────────────────────────
    database_url: str = "postgresql://wamiri:wamiri_secret@localhost:5432/document_processing"
    postgres_user: str = "wamiri"
    postgres_password: str = "wamiri_secret"
    postgres_db: str = "document_processing"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # ── Storage paths ──────────────────────────────────────────────────────
    base_data_dir: Path = Path("./data")
    upload_dir: Path = Path("./uploads")
    parquet_dir: Path = Path("./data/parquet")
    json_dir: Path = Path("./data/json")
    metrics_dir: Path = Path("./data/metrics")
    documents_dir: Path = Path("../documents")

    # ── Processing ─────────────────────────────────────────────────────────
    max_concurrent_tasks: int = 10
    task_time_limit: int = 300  # 5 min hard limit
    task_soft_time_limit: int = 270  # 4.5 min soft limit
    max_retries: int = 3
    retry_backoff_base: int = 10  # seconds

    # ── SLA thresholds ─────────────────────────────────────────────────────
    sla_p95_latency_seconds: float = 30.0
    sla_throughput_docs_per_hour: int = 4500
    sla_error_rate_percent: float = 1.0
    sla_queue_depth_warning: int = 500
    sla_breach_percent: float = 0.1
    sla_default_hours: int = 24  # default review SLA

    # ── Confidence ─────────────────────────────────────────────────────────
    confidence_threshold_low: float = 0.70
    confidence_threshold_high: float = 0.90

    # ── API ────────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    # ── Feature flags ──────────────────────────────────────────────────────
    enable_prometheus: bool = True
    debug: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# Singleton
settings = Settings()
