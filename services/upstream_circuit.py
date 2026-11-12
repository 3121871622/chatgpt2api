"""Small rolling circuit breaker for the shared ChatGPT upstream.

The account pool can survive one bad account by switching, but it cannot
survive an upstream-wide 5xx/429 storm: trying eight accounts only multiplies
the outage and marks healthy accounts as suspect.  When the recent image error
rate crosses the threshold the selector fails fast instead of probing the pool.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

UPSTREAM_FAILURE_CODES = {
    "upstream_unavailable",
    "upstream_connection_failed",
    "upstream_connection_timeout",
    "upstream_rate_limited",
    "image_stream_timeout",
    "image_poll_timeout",
    "image_stream_interrupted",
}


class UpstreamCircuitBreaker:
    def __init__(
        self,
        *,
        window_seconds: float = 30.0,
        min_samples: int = 20,
        error_rate: float = 0.5,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_seconds = max(1.0, float(window_seconds))
        self.min_samples = max(1, int(min_samples))
        self.error_rate = min(0.99, max(0.01, float(error_rate)))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._events: deque[tuple[float, bool]] = deque()
        self._open_until = 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def record(self, success: bool) -> None:
        now = self._clock()
        with self._lock:
            if self._open_until and now < self._open_until:
                return
            self._events.append((now, bool(success)))
            self._prune(now)

    def is_open(self) -> bool:
        now = self._clock()
        with self._lock:
            if self._open_until:
                if now < self._open_until:
                    return True
                self._open_until = 0.0
                self._events.clear()
                return False
            self._prune(now)
            total = len(self._events)
            if total < self.min_samples:
                return False
            failures = sum(1 for _, ok in self._events if not ok)
            if failures / total < self.error_rate:
                return False
            self._open_until = now + self.cooldown_seconds
            return True

    def snapshot(self) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            total = len(self._events)
            failures = sum(1 for _, ok in self._events if not ok)
            return {
                "open": bool(self._open_until and now < self._open_until),
                "samples": total,
                "failures": failures,
                "error_rate": round(failures / total, 4) if total else 0.0,
                "cooldown_remaining_seconds": round(max(0.0, self._open_until - now), 1),
            }


upstream_circuit = UpstreamCircuitBreaker()
