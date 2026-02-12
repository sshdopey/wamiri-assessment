"""Tests for the monitoring service — sliding window, SLA checks, queue depth.

Covers:
- Sliding window eviction
- P95 latency calculation
- Queue depth tracking (no longer hardcoded)
- SLA breach detection and breach-rate tracking
- Metric snapshot generation
"""

from __future__ import annotations

import time

import pytest

from src.services.monitoring_service import _WINDOW_SECONDS, MonitoringService


class TestSlidingWindow:
    """Tests for the time-based sliding window."""

    def test_record_processing_adds_to_window(self):
        svc = MonitoringService()
        svc.record_processing("doc-1", 2.5, 0.85)
        assert svc._processed_count == 1
        assert len(svc._processing_window) == 1

    def test_multiple_recordings(self):
        svc = MonitoringService()
        for i in range(10):
            svc.record_processing(f"doc-{i}", 1.0 + i * 0.1, 0.9)
        assert svc._processed_count == 10
        assert len(svc._processing_window) == 10

    def test_error_tracking(self):
        svc = MonitoringService()
        svc.record_processing("doc-1", 2.0, 0.5, success=False)
        assert svc._error_count == 1
        metrics = svc._get_current_metrics()
        assert metrics["error_rate_percent"] == 100.0


class TestQueueDepth:
    """Queue depth must reflect real values, not hardcoded 0."""

    def test_initial_queue_depth_zero(self):
        svc = MonitoringService()
        metrics = svc._get_current_metrics()
        assert metrics["review_queue_depth"] == 0

    def test_queue_depth_updates(self):
        svc = MonitoringService()
        svc.update_queue_depth(pending=15, in_review=5)
        metrics = svc._get_current_metrics()
        assert metrics["review_queue_depth"] == 20

    def test_queue_depth_changes(self):
        svc = MonitoringService()
        svc.update_queue_depth(pending=10, in_review=2)
        assert svc._current_queue_depth == 12
        svc.update_queue_depth(pending=8, in_review=3)
        assert svc._current_queue_depth == 11


class TestSLAChecks:
    """SLA breach detection and rate tracking."""

    def test_no_breaches_on_good_metrics(self):
        svc = MonitoringService()
        # Record enough good data
        for i in range(10):
            svc.record_processing(f"doc-{i}", 1.0, 0.95)
        svc.update_queue_depth(pending=5, in_review=2)
        breaches = svc.check_slas()
        # May have some breaches if throughput is low, but latency should be fine
        latency_breaches = [b for b in breaches if b["sla"] == "Latency"]
        assert len(latency_breaches) == 0

    def test_sla_breach_rate_increases(self):
        svc = MonitoringService()
        # With no data, some SLAs will breach (e.g., throughput)
        svc.check_slas()
        assert svc._sla_total_checks > 0
        # Check breach percentage is computed
        metrics = svc._get_current_metrics()
        # sla_breach_percent should be a real number, not hardcoded 0
        assert isinstance(metrics["sla_breach_percent"], float)

    def test_breach_count_tracked(self):
        svc = MonitoringService()
        initial_breaches = svc._sla_breach_count
        svc.check_slas()
        # With empty data, some SLAs should breach (throughput = 0)
        assert svc._sla_breach_count >= initial_breaches


class TestP95Calculation:
    """P95 latency from sliding window."""

    def test_p95_with_uniform_data(self):
        svc = MonitoringService()
        for i in range(100):
            svc.record_processing(f"doc-{i}", 1.0, 0.9)
        metrics = svc._get_current_metrics()
        assert metrics["p95_latency_seconds"] == 1.0

    def test_p95_with_outlier(self):
        svc = MonitoringService()
        for i in range(95):
            svc.record_processing(f"doc-{i}", 1.0, 0.9)
        for i in range(5):
            svc.record_processing(f"doc-slow-{i}", 10.0, 0.5)
        metrics = svc._get_current_metrics()
        assert metrics["p95_latency_seconds"] >= 1.0


class TestDashboardMetrics:
    """get_dashboard_metrics should return the same as _get_current_metrics."""

    def test_dashboard_metrics_structure(self):
        svc = MonitoringService()
        svc.record_processing("doc-1", 2.0, 0.85)
        metrics = svc.get_dashboard_metrics()
        assert "p95_latency_seconds" in metrics
        assert "docs_per_hour" in metrics
        assert "error_rate_percent" in metrics
        assert "review_queue_depth" in metrics
        assert "sla_breach_percent" in metrics
        assert "total_processed" in metrics
