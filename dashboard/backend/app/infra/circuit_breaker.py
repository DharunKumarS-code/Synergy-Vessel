"""Circuit breaker for calls to an external service (used around the AI condition-analysis provider).

    CLOSED     normal: calls go through; consecutive failures are counted.
    OPEN       after ``failure_threshold`` consecutive failures: calls are refused immediately (no request is
               sent) until ``recovery_seconds`` have passed, or longer if the service asked us to wait
               (e.g. a quota error's retry-after).
    HALF_OPEN  after the wait: exactly one trial call is let through. Success closes the circuit;
               failure opens it again for another full wait.

The breaker never produces a result itself: a refused call is reported to the user as an error they can
retry later. Timeouts are enforced by the provider's own HTTP client (Gemini: request_timeout_seconds in
automation/config/settings.yaml; Groq: 60 s) and count as failures here.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"
HALF_OPEN_BUSY_RETRY_SECONDS = 10.0


class CircuitOpen(Exception):
    def __init__(self, retry_after: float):
        super().__init__("circuit open")
        self.retry_after = max(1.0, retry_after)


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_seconds = max(1.0, float(recovery_seconds))
        self.clock = clock
        self._state = CLOSED
        self._failures = 0
        self._open_until = 0.0
        self._trial_running = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._current_state(self.clock())

    def _current_state(self, now: float) -> str:
        if self._state == OPEN and now >= self._open_until:
            return HALF_OPEN  # reported as half-open once the wait is over; the next call is the trial
        return self._state

    def before_call(self) -> None:
        """Raise CircuitOpen if the call must not be made now; otherwise reserve the call."""
        now = self.clock()
        with self._lock:
            state = self._current_state(now)
            if state == OPEN:
                raise CircuitOpen(self._open_until - now)
            if state == HALF_OPEN:
                if self._trial_running:
                    raise CircuitOpen(HALF_OPEN_BUSY_RETRY_SECONDS)
                self._state = HALF_OPEN
                self._trial_running = True

    def record_success(self) -> None:
        with self._lock:
            self._state, self._failures, self._trial_running = CLOSED, 0, False

    def record_failure(self, retry_after: Optional[float] = None) -> None:
        now = self.clock()
        with self._lock:
            self._failures += 1
            if self._state == HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = OPEN
                self._open_until = now + max(self.recovery_seconds, float(retry_after or 0))
            self._trial_running = False

    def snapshot(self) -> dict:
        now = self.clock()
        with self._lock:
            state = self._current_state(now)
            return {
                "state": state,
                "consecutive_failures": self._failures,
                "failure_threshold": self.failure_threshold,
                "retry_after_seconds": int(max(0.0, self._open_until - now)) if state == OPEN else 0,
            }
