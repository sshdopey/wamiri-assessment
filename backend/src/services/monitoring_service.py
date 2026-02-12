"""Monitoring service with Prometheus metrics and SLA tracking.

Collects processing metrics, queue health, and SLA compliance data.
Exposes a /metrics endpoint for Prometheus scraping and periodically
snapshots metrics to disk.

Uses a sliding time-window (default 1 hour) for derived metrics so that
old data points are automatically evicted instead of accumulating without
bound.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import yaml
from prometheus_client import Counter, Gauge, Histogram

from src.config import settings

logger = logging.getLogger(__name__)

# Sliding window size in seconds (1 hour)
_WINDOW_SECONDS = 3600

# Load SLA definitions from YAML

_SLA_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "configs" / "sla_definitions.yaml"
)


def _load_sla_config() -> list[dict]:
    """Load SLA definitions from YAML config. Falls back to empty list."""
    try:
        with open(_SLA_CONFIG_PATH) as f:
            data = yaml.safe_load(f)
            return data.get("slas", [])
    except FileNotFoundError:
        logger.warning(
            "SLA config not found at %s — using built-in defaults", _SLA_CONFIG_PATH
        )
        return []


# Prometheus metrics

# Processing
documents_processed = Counter(
    "documents_processed_total",
    "Total documents processed",
    ["status"],
)
processing_duration = Histogram(
    "document_processing_seconds",
    "Processing time in seconds",
    buckets=[1, 5, 10, 30, 60, 120, 300],
)
extraction_confidence = Histogram(
    "extraction_confidence_score",
    "Confidence scores distribution",
    buckets=[0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99],
)

# Queue
queue_depth = Gauge(
    "review_queue_depth",
    "Items currently in review queue",
    ["status"],
)
sla_breaches = Counter(
    "sla_breaches_total",
    "Total SLA violations",
    ["severity"],
)
review_duration = Histogram(
    "review_duration_seconds",
    "Human review time in seconds",
    buckets=[10, 30, 60, 120, 300, 600],
)

# Throughput
documents_per_hour = Gauge(
    "documents_per_hour",
    "Current processing rate",
)

# System
active_tasks = Gauge(
    "active_celery_tasks",
    "Currently running Celery tasks",
)
error_rate = Gauge(
    "error_rate_percent",
    "Error rate percentage over sliding window",
)
p95_latency = Gauge(
    "p95_latency_seconds",
    "P95 processing latency",
)
sla_breach_percent = Gauge(
    "sla_breach_percent",
    "Percentage of SLA breaches",
)

# SLA Definitions


class SLADefinition:
    """A single SLA rule."""

    def __init__(
        self,
        name: str,
        metric_name: str,
        threshold: float,
        comparison: str,  # "lt" or "gt"
        window_minutes: int,
        severity: str,
    ):
        self.name = name
        self.metric_name = metric_name
        self.threshold = threshold
        self.comparison = comparison
        self.window_minutes = window_minutes
        self.severity = severity

    def is_breached(self, current_value: float) -> bool:
        if self.comparison == "lt":
            return current_value >= self.threshold
        else:  # gt
            return current_value < self.threshold


# Load from YAML, fall back to built-in defaults
_yaml_slas = _load_sla_config()

if _yaml_slas:
    DEFAULT_SLAS = [
        SLADefinition(
            name=s["name"],
            metric_name=s["metric"],
            threshold=float(s["threshold"]),
            comparison=s["comparison"],
            window_minutes=int(s["window_minutes"]),
            severity=s["severity"],
        )
        for s in _yaml_slas
    ]
    logger.info("Loaded %d SLA definitions from YAML config", len(DEFAULT_SLAS))
else:
    DEFAULT_SLAS = [
        SLADefinition("Latency", "p95_latency_seconds", 30.0, "lt", 5, "critical"),
        SLADefinition("Throughput", "docs_per_hour", 4500, "gt", 15, "warning"),
        SLADefinition("Error Rate", "error_rate_percent", 1.0, "lt", 5, "critical"),
        SLADefinition("Queue Depth", "review_queue_depth", 500, "lt", 5, "warning"),
        SLADefinition("SLA Breach", "sla_breach_percent", 0.1, "lt", 60, "critical"),
    ]

# Monitoring Service


class MonitoringService:
    """Collects, evaluates, and persists metrics and SLA checks.

    Uses a sliding time-window (deque) for processing times so that
    metrics are always computed over the most recent hour instead of
    accumulating unbounded data.
    """

    def __init__(self) -> None:
        # Sliding window: deque of (timestamp, duration) tuples
        self._processing_window: deque[tuple[float, float]] = deque()
        self._window_start = time.time()
        self._processed_count = 0
        self._error_count = 0
        # Track actual queue depth (updated by update_queue_depth)
        self._current_queue_depth: int = 0
        # Track SLA breach counts
        self._sla_total_checks: int = 0
        self._sla_breach_count: int = 0
        self.sla_definitions = DEFAULT_SLAS

    # Record events

    def record_processing(
        self,
        document_id: str,
        duration_seconds: float,
        confidence: float,
        success: bool = True,
    ) -> None:
        """Record a document processing event."""
        status = "success" if success else "failure"
        documents_processed.labels(status=status).inc()
        processing_duration.observe(duration_seconds)
        extraction_confidence.observe(confidence)

        now = time.time()
        self._processing_window.append((now, duration_seconds))
        self._evict_old_entries(now)

        self._processed_count += 1
        if not success:
            self._error_count += 1

        # Update derived gauges
        self._update_derived_metrics()

        logger.debug(
            "Recorded processing: doc=%s duration=%.1fs confidence=%.2f status=%s",
            document_id,
            duration_seconds,
            confidence,
            status,
        )

    def record_review(self, duration_seconds: float) -> None:
        """Record a human review completion."""
        review_duration.observe(duration_seconds)

    def update_queue_depth(self, pending: int, in_review: int) -> None:
        """Update queue depth gauges and internal tracking."""
        queue_depth.labels(status="pending").set(pending)
        queue_depth.labels(status="in_review").set(in_review)
        self._current_queue_depth = pending + in_review

    # SLA evaluation

    def check_slas(self) -> list[dict]:
        """Evaluate all SLA definitions and return breaches."""
        current_metrics = self._get_current_metrics()
        breaches = []

        for sla in self.sla_definitions:
            value = current_metrics.get(sla.metric_name, 0)
            self._sla_total_checks += 1
            if sla.is_breached(value):
                self._sla_breach_count += 1
                breach = {
                    "sla": sla.name,
                    "metric": sla.metric_name,
                    "threshold": sla.threshold,
                    "current_value": value,
                    "severity": sla.severity,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                breaches.append(breach)
                sla_breaches.labels(severity=sla.severity).inc()
                logger.warning(
                    "SLA BREACH: %s — %s=%.2f (threshold=%.2f) [%s]",
                    sla.name,
                    sla.metric_name,
                    value,
                    sla.threshold,
                    sla.severity,
                )

        # Update breach-rate gauge
        if self._sla_total_checks > 0:
            rate = (self._sla_breach_count / self._sla_total_checks) * 100
            sla_breach_percent.set(round(rate, 2))

        return breaches

    # Metrics snapshot

    def save_snapshot(self) -> Path:
        """Persist current metrics to a JSON file in the metrics directory."""
        metrics_dir = Path(settings.metrics_dir)
        metrics_dir.mkdir(parents=True, exist_ok=True)

        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": self._get_current_metrics(),
            "sla_breaches": self.check_slas(),
        }

        filename = (
            f"metrics_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        )
        path = metrics_dir / filename
        with open(path, "w") as f:
            json.dump(snapshot, f, indent=2)

        logger.info("Saved metrics snapshot to %s", path)
        return path

    def get_dashboard_metrics(self) -> dict:
        """Return metrics suitable for the dashboard API."""
        return self._get_current_metrics()

    # Internal

    def _update_derived_metrics(self) -> None:
        """Recompute derived gauges (P95, throughput, error rate) from sliding window."""
        now = time.time()
        self._evict_old_entries(now)

        durations = [d for _, d in self._processing_window]

        # P95 latency
        if durations:
            sorted_times = sorted(durations)
            idx = int(len(sorted_times) * 0.95)
            p95 = sorted_times[min(idx, len(sorted_times) - 1)]
            p95_latency.set(p95)

        # Throughput (docs/hour) — based on window size
        window_hours = max(_WINDOW_SECONDS / 3600, 0.001)
        rate = len(durations) / window_hours
        documents_per_hour.set(round(rate, 1))

        # Error rate
        if self._processed_count > 0:
            err = (self._error_count / self._processed_count) * 100
            error_rate.set(round(err, 2))

    def _evict_old_entries(self, now: float) -> None:
        """Remove entries older than the sliding window."""
        cutoff = now - _WINDOW_SECONDS
        while self._processing_window and self._processing_window[0][0] < cutoff:
            self._processing_window.popleft()

    def _get_current_metrics(self) -> dict:
        """Build a dict of current metric values."""
        now = time.time()
        self._evict_old_entries(now)

        elapsed_hours = max((now - self._window_start) / 3600, 0.001)
        durations = [d for _, d in self._processing_window]

        # P95 from sliding window
        p95 = 0.0
        if durations:
            s = sorted(durations)
            p95 = s[int(len(s) * 0.95)] if len(s) > 1 else s[0]

        # SLA breach rate (real, not hardcoded)
        breach_pct = 0.0
        if self._sla_total_checks > 0:
            breach_pct = (self._sla_breach_count / self._sla_total_checks) * 100

        return {
            "p95_latency_seconds": round(p95, 2),
            "docs_per_hour": round(self._processed_count / elapsed_hours, 1),
            "error_rate_percent": round(
                (self._error_count / max(self._processed_count, 1)) * 100, 2
            ),
            "review_queue_depth": self._current_queue_depth,
            "sla_breach_percent": round(breach_pct, 2),
            "total_processed": self._processed_count,
            "total_errors": self._error_count,
            "uptime_hours": round(elapsed_hours, 2),
        }


# Module-level singleton
monitoring = MonitoringService()
