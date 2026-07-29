from __future__ import annotations

import time
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-provider circuit breaker: open after N failures, probe after reset window."""

    def __init__(self, *, failure_threshold: int, reset_ms: int) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.reset_ms = max(1, reset_ms)
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at_ms: float | None = None

    @property
    def state(self) -> CircuitState:
        self._maybe_half_open()
        return self._state

    def allow(self) -> bool:
        self._maybe_half_open()
        return self._state in {CircuitState.CLOSED, CircuitState.HALF_OPEN}

    def record_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at_ms = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._state == CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at_ms = time.monotonic() * 1000

    def _maybe_half_open(self) -> None:
        if self._state != CircuitState.OPEN or self._opened_at_ms is None:
            return
        elapsed = (time.monotonic() * 1000) - self._opened_at_ms
        if elapsed >= self.reset_ms:
            self._state = CircuitState.HALF_OPEN
