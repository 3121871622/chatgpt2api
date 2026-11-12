"""全局请求准入控制（500+ 并发的第一道闸门）。

FastAPI 默认不限制在途请求数：并发一高，请求会全部涌进 anyio 线程池排队，
而线程池队列没有上限，图片轮次又长时间占住线程，结果就是「没崩但全在等」。

本模块提供有界准入：

* 上限按硬件档位推导（32 核约 500，64 核约 1000，128 核约 2000，可用
  ``QINGMENG_MAX_REQUEST_CONCURRENCY`` 覆盖），并由自适应调度器按负载缩放；
* 超过上限时请求**排队等待**，等待超过超时时间返回 503 + ``Retry-After``；
* 在途/排队/拒绝计数可观测（``/api/system/load``、``/metrics``）。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from services.hardware_profile import build_profile

DEFAULT_QUEUE_TIMEOUT_SECONDS = 60.0
MIN_LIMIT = 64
MAX_LIMIT = 4096


def base_limit_for_cores(cores: int) -> int:
    """Hardware-derived request ceiling (IO-bound workloads tolerate > cores)."""
    try:
        normalized = max(1, int(cores))
    except (TypeError, ValueError):
        normalized = 4
    return max(MIN_LIMIT, min(MAX_LIMIT, normalized * 16))


def resolve_base_limit() -> int:
    override = os.getenv("QINGMENG_MAX_REQUEST_CONCURRENCY", "").strip()
    if override:
        try:
            return max(MIN_LIMIT, min(MAX_LIMIT, int(override)))
        except ValueError:
            pass
    try:
        cores = int(build_profile().cpu_cores or 0)
    except Exception:
        cores = 4
    return base_limit_for_cores(cores)


class AdmissionTimeout(RuntimeError):
    def __init__(self, waited_seconds: float) -> None:
        self.waited_seconds = waited_seconds
        super().__init__(f"server busy: queue wait exceeded {waited_seconds:.0f}s")


class AdmissionGate:
    """Async counting gate with bounded waiting."""

    def __init__(
        self,
        *,
        limit_getter: Callable[[], int] | None = None,
        queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        minimum: int = MIN_LIMIT,
    ) -> None:
        self._limit_getter = limit_getter or resolve_base_limit
        self._queue_timeout = max(0.01, float(queue_timeout_seconds))
        self._minimum = max(1, int(minimum))
        self._lock = threading.Lock()
        self._condition: asyncio.Condition | None = None
        self._inflight = 0
        self._queued = 0
        self._admitted_total = 0
        self._rejected_total = 0
        self._wait_samples_ms: list[float] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------- helpers

    def _binding(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._condition is None or self._loop is not loop:
                self._condition = asyncio.Condition()
                self._loop = loop
            return self._condition

    def limit(self) -> int:
        try:
            value = int(self._limit_getter())
        except Exception:
            value = self._minimum
        return max(self._minimum, min(MAX_LIMIT, value))

    def stats(self) -> dict[str, object]:
        with self._lock:
            samples = sorted(self._wait_samples_ms[-200:])
            p50 = samples[len(samples) // 2] if samples else 0.0
            p95 = samples[int(len(samples) * 0.95)] if samples else 0.0
            return {
                "limit": self.limit(),
                "inflight": self._inflight,
                "queued": self._queued,
                "admitted_total": self._admitted_total,
                "rejected_total": self._rejected_total,
                "wait_p50_ms": round(p50, 1),
                "wait_p95_ms": round(p95, 1),
            }

    # ------------------------------------------------------------- acquire

    async def acquire(self, timeout: float | None = None) -> bool:
        condition = self._binding()
        deadline = time.monotonic() + (self._queue_timeout if timeout is None else max(0.1, timeout))
        began = time.monotonic()
        async with condition:
            with self._lock:
                self._queued += 1
            try:
                while True:
                    limit = self.limit()
                    with self._lock:
                        if self._inflight < limit:
                            self._inflight += 1
                            self._admitted_total += 1
                            break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        with self._lock:
                            self._rejected_total += 1
                        raise AdmissionTimeout(time.monotonic() - began)
                    try:
                        await asyncio.wait_for(condition.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        continue
            finally:
                with self._lock:
                    self._queued = max(0, self._queued - 1)
            with self._lock:
                self._wait_samples_ms.append((time.monotonic() - began) * 1000)
                if len(self._wait_samples_ms) > 500:
                    del self._wait_samples_ms[:-500]
        return True

    def release(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
        condition = self._condition
        if condition is None or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._notify(condition)))
        except RuntimeError:
            pass

    @staticmethod
    async def _notify(condition: asyncio.Condition) -> None:
        async with condition:
            condition.notify_all()

    @asynccontextmanager
    async def slot(self, timeout: float | None = None) -> AsyncIterator[None]:
        await self.acquire(timeout)
        try:
            yield
        finally:
            self.release()


_gate: AdmissionGate | None = None
_gate_lock = threading.Lock()


def admission_gate() -> AdmissionGate:
    global _gate
    with _gate_lock:
        if _gate is None:
            from services.adaptive_governor import governor

            _gate = AdmissionGate(
                limit_getter=lambda: governor().effective_limit(
                    resolve_base_limit(),
                    floor=MIN_LIMIT,
                ),
            )
        return _gate


def reset_admission_gate() -> None:
    """Test helper: drop the singleton (and its bound loop)."""
    global _gate
    with _gate_lock:
        _gate = None
