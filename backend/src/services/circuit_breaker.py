"""Circuit breaker pattern for protecting external service calls.

Implements the classic three-state circuit breaker:

    CLOSED -> OPEN            (when failures >= threshold)
    OPEN -> HALF_OPEN         (after recovery timeout expires)
    HALF_OPEN -> CLOSED       (probe call succeeds)
    HALF_OPEN -> OPEN         (probe call fails)

Usage::

    breaker = CircuitBreaker(
        name="gemini_api",
        failure_threshold=5,
        recovery_timeout_seconds=60.0,
        half_open_max_calls=2,
    )

    with breaker:
        result = call_gemini_api(...)

The breaker raises ``CircuitOpenError`` when the circuit is open, preventing
thundering-herd retries against a failing downstream.  After the recovery
timeout, it transitions to HALF_OPEN and allows a limited number of probe
calls.  If those succeed, the circuit closes again.

Thread-safe via ``threading.Lock`` (works in both async and sync contexts
since state transitions are fast in-memory operations).
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class CircuitState(str, enum.Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is attempted while the circuit is open."""

    def __init__(self, breaker_name: str, remaining_seconds: float) -> None:
        self.breaker_name = breaker_name
        self.remaining_seconds = remaining_seconds
        super().__init__(
            f"Circuit '{breaker_name}' is OPEN — retry in {remaining_seconds:.0f}s"
        )


class CircuitBreaker:
    """Thread-safe circuit breaker for external service protection.

    Parameters
    ----------
    name : str
        Human-readable name for logging (e.g. "gemini_api").
    failure_threshold : int
        Number of consecutive failures before opening the circuit.
    recovery_timeout_seconds : float
        Seconds to wait in OPEN state before transitioning to HALF_OPEN.
    half_open_max_calls : int
        Maximum probe calls allowed in HALF_OPEN state.
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 60.0,
        half_open_max_calls: int = 2,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_seconds
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._last_failure_time: float = 0.0
        self._lock = threading.Lock()

    # Public API

    @property
    def state(self) -> CircuitState:
        """Current circuit state (may transition OPEN → HALF_OPEN on read)."""
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def record_success(self) -> None:
        """Record a successful call."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.half_open_max_calls:
                    self._transition(CircuitState.CLOSED)
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0  # Reset consecutive failure counter

    def record_failure(self) -> None:
        """Record a failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                # Probe failed — back to OPEN
                self._transition(CircuitState.OPEN)
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._transition(CircuitState.OPEN)

    def allow_request(self) -> bool:
        """Check whether a request should be allowed through."""
        with self._lock:
            self._maybe_transition_to_half_open()

            if self._state == CircuitState.CLOSED:
                return True
            elif self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls < self.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False
            else:
                # OPEN
                return False

    def reset(self) -> None:
        """Manually reset the circuit to CLOSED."""
        with self._lock:
            self._transition(CircuitState.CLOSED)

    # Context manager

    def __enter__(self) -> "CircuitBreaker":
        if not self.allow_request():
            remaining = self.recovery_timeout - (
                time.monotonic() - self._last_failure_time
            )
            raise CircuitOpenError(self.name, max(remaining, 0))
        return self

    def __exit__(self, exc_type: type | None, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is None:
            self.record_success()
        else:
            self.record_failure()
        return False  # Don't suppress exceptions

    # Internals

    def _maybe_transition_to_half_open(self) -> None:
        """Transition OPEN → HALF_OPEN if recovery timeout has elapsed."""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                self._transition(CircuitState.HALF_OPEN)

    def _transition(self, new_state: CircuitState) -> None:
        """Perform a state transition."""
        old = self._state
        self._state = new_state

        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._half_open_calls = 0
            self._success_count = 0

        logger.info(
            "Circuit '%s' transitioned %s → %s (failures=%d)",
            self.name,
            old.value,
            new_state.value,
            self._failure_count,
        )

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, state={self._state.value}, "
            f"failures={self._failure_count}/{self.failure_threshold})"
        )
