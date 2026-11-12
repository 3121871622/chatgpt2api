# Timing-aware lock: finds out who holds the account lock for seconds.
#
# Drop-in replacement for threading.Lock (also valid for threading.Condition).
# It records, per acquire/release, how long the caller waited and how long the
# lock was held, and which function held it. Slow events are kept in a small
# ring buffer and reported through an optional callback.
from __future__ import annotations

import sys
import threading
import time
from collections import deque
from typing import Callable


def _caller_label() -> str:
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get('__name__') == __name__:
        frame = frame.f_back
    if frame is None:
        return 'unknown'
    return f'{frame.f_code.co_name}:{frame.f_lineno}'


class InstrumentedLock:
    def __init__(
        self,
        name: str = 'lock',
        *,
        slow_seconds: float = 1.0,
        on_slow: Callable[[dict], None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.name = name
        self._slow_seconds = float(slow_seconds)
        self._on_slow = on_slow
        self._holder = ''
        self._held_since = 0.0
        self._max_wait = 0.0
        self._max_hold = 0.0
        self._slow_waits = 0
        self._slow_holds = 0
        self._events: deque[dict] = deque(maxlen=20)

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        started = time.monotonic()
        blocker = ''
        if not self._lock.acquire(False):
            if not blocking:
                return False
            blocker = self._holder
            if not self._lock.acquire(True, timeout):
                return False
        label = _caller_label()
        now = time.monotonic()
        waited = now - started
        self._holder = label
        self._held_since = now
        if waited > self._max_wait:
            self._max_wait = waited
        if waited >= self._slow_seconds:
            self._slow_waits += 1
            self._emit('slow_wait', label, waited, blocker)
        return True

    def release(self) -> None:
        held = time.monotonic() - self._held_since
        holder = self._holder
        slow = held >= self._slow_seconds
        if held > self._max_hold:
            self._max_hold = held
        if slow:
            self._slow_holds += 1
        self._holder = ''
        self._lock.release()
        if slow:
            self._emit('slow_hold', holder, held, '')

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> 'InstrumentedLock':
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def _emit(self, kind: str, label: str, seconds: float, blocker: str) -> None:
        event = {
            'kind': kind,
            'lock': self.name,
            'caller': label,
            'blocked_by': blocker,
            'seconds': round(seconds, 3),
            'at': round(time.time(), 3),
        }
        self._events.append(event)
        if self._on_slow is not None:
            try:
                self._on_slow(event)
            except Exception:
                pass

    def snapshot(self) -> dict:
        holder = self._holder
        held_for = (
            round((time.monotonic() - self._held_since) * 1000)
            if holder
            else 0
        )
        return {
            'name': self.name,
            'slow_threshold_ms': round(self._slow_seconds * 1000),
            'max_wait_ms': round(self._max_wait * 1000),
            'max_hold_ms': round(self._max_hold * 1000),
            'slow_waits': self._slow_waits,
            'slow_holds': self._slow_holds,
            'current_holder': holder,
            'current_held_ms': held_for,
            'recent_slow': list(self._events),
        }
