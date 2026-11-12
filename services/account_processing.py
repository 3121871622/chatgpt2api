"""Shared concurrency control for remote and local account batch work.

The configured limit is the operator's ceiling.  It is no longer capped at 100:
the effective value is derived from the saved setting, then scaled down
temporarily by the adaptive governor while the host is overloaded and restored
to the operator's exact number once load is healthy again.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from functools import wraps
from threading import Condition, Lock, local
from typing import Callable, Iterator, ParamSpec, TypeVar


_DEFAULT_CONCURRENCY = 256
_MIN_CONCURRENCY = 1

P = ParamSpec("P")
R = TypeVar("R")


def account_processing_base_concurrency() -> int:
    """Return the operator-saved global account-processing limit."""
    try:
        from services.config import config

        value = int(config.account_processing_concurrency)
    except (AttributeError, TypeError, ValueError):
        value = _DEFAULT_CONCURRENCY
    return max(_MIN_CONCURRENCY, value)


def account_processing_concurrency() -> int:
    """Return the current effective global account-processing limit."""
    base = account_processing_base_concurrency()
    try:
        from services.adaptive_governor import governor

        return max(_MIN_CONCURRENCY, governor().effective_limit(base, floor=_MIN_CONCURRENCY))
    except Exception:
        return base


def account_processing_worker_count(item_count: int) -> int:
    """Size a local worker pool without exceeding the shared global limit."""
    count = max(0, int(item_count or 0))
    return min(count, account_processing_concurrency())


def foreground_reserve(limit: int) -> int:
    """Slots kept free for foreground image/text work.

    Production has 512 account-processing slots, so this reserves 20 of them.
    Small pools keep the 20% proportion instead of losing a fixed 20 slots.
    """
    normalized = max(1, int(limit or 1))
    return min(20, max(1, normalized // 5))


class AccountProcessingLimiter:
    """A process-wide, dynamically sized and thread-reentrant limiter."""

    def __init__(self) -> None:
        self._condition = Condition(Lock())
        self._active = 0
        self._priority_active = 0
        self._local = local()

    @contextmanager
    def slot(self) -> Iterator[None]:
        depth = int(getattr(self._local, "depth", 0) or 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return

        with self._condition:
            while True:
                limit = account_processing_concurrency()
                # Always keep the foreground reserve free.  Reserving only when
                # a priority request is already active is too late: a 512-worker
                # batch can occupy every slot first, and the next image request
                # then waits minutes for a slot before its own 8s upstream
                # timeout even starts.  Capping batch work at limit - reserve
                # means the first foreground request always finds headroom.
                reserve = foreground_reserve(limit)
                if self._active < max(1, limit - reserve):
                    break
                self._condition.wait(timeout=0.2)
            self._active += 1
            self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            with self._condition:
                self._active = max(0, self._active - 1)
                self._condition.notify_all()

    @contextmanager
    def priority_slot(self) -> Iterator[None]:
        """Foreground slot: can use the reserved headroom, never queues behind batch."""
        depth = int(getattr(self._local, "depth", 0) or 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return

        with self._condition:
            while self._active >= account_processing_concurrency():
                self._condition.wait(timeout=0.2)
            self._active += 1
            self._priority_active += 1
            self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            with self._condition:
                self._active = max(0, self._active - 1)
                self._priority_active = max(0, self._priority_active - 1)
                self._condition.notify_all()

    @contextmanager
    def batch_slot(self) -> Iterator[None]:
        """Reserve one shared slot for the lifetime of an account batch.

        Batch work and individual upstream requests share the same limiter and
        thread-local re-entry depth.  A batch may therefore call code that
        acquires a regular slot on its coordinator thread without consuming a
        second slot or deadlocking when the configured limit is one.
        """
        with self.slot():
            yield


account_processing_limiter = AccountProcessingLimiter()


def account_processing_slot() -> AbstractContextManager[None]:
    """Acquire one shared slot for an upstream account-maintenance request."""
    return account_processing_limiter.slot()


def account_processing_priority_slot() -> AbstractContextManager[None]:
    """Acquire a foreground slot that can use the headroom reserved from batch work."""
    return account_processing_limiter.priority_slot()


def account_processing_batch_slot() -> AbstractContextManager[None]:
    """Acquire one shared slot for a complete local account batch mutation.

    The slot is intentionally the same limiter used by remote maintenance. A
    local mutation stays atomic inside the account service, but still participates
    in the one process-wide capacity budget.
    """
    return account_processing_limiter.batch_slot()


def account_processing_batch(
    function: Callable[P, R],
) -> Callable[P, R]:
    """Decorate a synchronous account batch entry point with one shared slot."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with account_processing_batch_slot():
            return function(*args, **kwargs)

    return wrapped
