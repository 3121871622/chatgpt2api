"""Bounded asynchronous worker queue for account maintenance.

Batch maintenance used to fan out one future per selected account inside a
fresh ``ThreadPoolExecutor``.  Selecting 50k accounts therefore allocated 50k
futures, 50k limiter waiters and a thread pool that held a request thread for
hours - the classic "queue piles up, threads stack up, box dies" failure.

This module replaces that shape with a single process-wide queue:

* one bounded pool of workers, sized from the operator setting and scaled down
  by the adaptive governor while the host is overloaded;
* jobs pull **one token at a time** from a ``deque``, so a million-account job
  costs one pointer per account instead of one future per account;
* work kinds (``check`` / ``refresh`` / ``sync``) live in separate lanes, so an
  AT refresh is never blocked behind a huge detection sweep;
* progress, per-account messages, cancellation and error capping are handled
  here instead of in each endpoint.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable

KIND_CHECK = "check"
KIND_REFRESH = "refresh"
KIND_SYNC = "sync"

LANES = (KIND_CHECK, KIND_REFRESH, KIND_SYNC)

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"

_ERROR_LIMIT = 200
_FINISHED_TTL_SECONDS = 600.0
_IDLE_WAIT_SECONDS = 0.15

Worker = Callable[[str], None]
FinishHook = Callable[["AccountJob", dict[str, object]], None]


@dataclass
class AccountJob:
    job_id: str
    kind: str
    lane: str
    total: int
    worker: Worker
    on_finish: FinishHook | None = None
    tokens: deque[str] = field(default_factory=deque)
    processed: int = 0
    failed: int = 0
    active: int = 0
    state: str = STATE_QUEUED
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    errors: deque[dict[str, object]] = field(default_factory=lambda: deque(maxlen=_ERROR_LIMIT))
    skipped: int = 0
    label: str = ""

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.processed - self.skipped)

    def as_dict(self) -> dict[str, object]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        rate = (self.processed / elapsed) if elapsed > 0 else 0.0
        remaining = self.remaining
        eta = int(remaining / rate) if rate > 0 else None
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "lane": self.lane,
            "label": self.label,
            "state": self.state,
            "total": self.total,
            "processed": self.processed,
            "failed": self.failed,
            "skipped": self.skipped,
            "remaining": remaining,
            "percent": round(self.processed / self.total * 100, 1) if self.total else 100.0,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(elapsed, 1),
            "rate_per_second": round(rate, 1),
            "eta_seconds": eta,
            "errors": list(self.errors),
            "cancelled": self.state == STATE_CANCELLED,
        }


class AccountJobQueue:
    """Process-wide bounded account maintenance queue."""

    def __init__(
        self,
        *,
        worker_target: Callable[[], int] | None = None,
        max_jobs: int = 64,
        name: str = "account-job",
    ) -> None:
        self._name = name
        self._worker_target = worker_target or (lambda: 16)
        self._max_jobs = max(1, int(max_jobs))
        self._lock = threading.RLock()
        self._jobs: dict[str, AccountJob] = {}
        self._pending: dict[str, deque[AccountJob]] = {lane: deque() for lane in LANES}
        self._workers: list[threading.Thread] = []
        self._worker_seq = 0
        self._lane_cursor = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._retire: dict[int, threading.Event] = {}
        self._manager: threading.Thread | None = None
        self._processed_total = 0
        self._failed_total = 0

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._lock:
            if self._manager is not None and self._manager.is_alive():
                return
            self._stop = threading.Event()
            self._wake = threading.Event()
            manager = threading.Thread(
                target=self._manage,
                name=f"{self._name}-manager",
                daemon=True,
            )
            self._manager = manager
        manager.start()
        self._reconcile_workers()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            for job in self._jobs.values():
                job.cancel.set()
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout=2)
        manager = self._manager
        if manager is not None:
            manager.join(timeout=2)
        self._manager = None

    def _manage(self) -> None:
        while not self._stop.is_set():
            self._reconcile_workers()
            self._prune_finished()
            self._stop.wait(3.0)

    def _reconcile_workers(self) -> None:
        try:
            target = max(1, int(self._worker_target()))
        except Exception:
            target = 1
        with self._lock:
            alive = [worker for worker in self._workers if worker.is_alive()]
            self._workers = alive
            current = len(alive)
            if current < target:
                for _ in range(target - current):
                    retirement = threading.Event()
                    worker = threading.Thread(
                        target=self._worker_loop,
                        args=(retirement,),
                        name=f"{self._name}-worker-{self._worker_seq + 1}",
                        daemon=True,
                    )
                    self._worker_seq += 1
                    self._retire[id(worker)] = retirement
                    self._workers.append(worker)
                    worker.start()
            elif current > target:
                for worker in alive[target:]:
                    retirement = self._retire.get(id(worker))
                    if retirement is not None:
                        retirement.set()
                    self._workers.remove(worker)
        self._wake.set()

    def _prune_finished(self) -> None:
        cutoff = time.time() - _FINISHED_TTL_SECONDS
        with self._lock:
            stale = [
                job_id
                for job_id, job in self._jobs.items()
                if job.finished_at is not None and job.finished_at < cutoff
            ]
            for job_id in stale:
                self._jobs.pop(job_id, None)

    # --------------------------------------------------------------- intake

    def submit(
        self,
        *,
        kind: str,
        tokens: Iterable[str],
        worker: Worker,
        label: str = "",
        on_finish: FinishHook | None = None,
        lane: str | None = None,
    ) -> dict[str, object]:
        normalized_tokens = [str(token).strip() for token in tokens if str(token or "").strip()]
        if not normalized_tokens:
            raise ValueError("no tokens to process")
        resolved_kind = str(kind or KIND_CHECK).strip().lower()
        resolved_lane = str(lane or resolved_kind).strip().lower()
        if resolved_lane not in self._pending:
            resolved_lane = KIND_CHECK
        with self._lock:
            active = sum(1 for job in self._jobs.values() if job.finished_at is None)
            if active >= self._max_jobs:
                raise RuntimeError("too many account jobs in flight")
            job = AccountJob(
                job_id=str(uuid.uuid4()),
                kind=resolved_kind,
                lane=resolved_lane,
                total=len(normalized_tokens),
                worker=worker,
                on_finish=on_finish,
                tokens=deque(normalized_tokens),
                label=label or resolved_kind,
            )
            self._jobs[job.job_id] = job
            self._pending[resolved_lane].append(job)
        self._wake.set()
        return {"job_id": job.job_id, "kind": job.kind, "lane": job.lane, "total": job.total}

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None or job.finished_at is not None:
                return False
            job.cancel.set()
            job.state = STATE_CANCELLED
        self._wake.set()
        return True

    def job(self, job_id: str) -> dict[str, object] | None:
        with self._lock:
            job = self._jobs.get(str(job_id))
            return job.as_dict() if job is not None else None

    def jobs(self) -> list[dict[str, object]]:
        with self._lock:
            items = sorted(self._jobs.values(), key=lambda job: job.started_at, reverse=True)
            return [job.as_dict() for job in items]

    def stats(self) -> dict[str, object]:
        with self._lock:
            active = [job for job in self._jobs.values() if job.finished_at is None]
            return {
                "workers": len([worker for worker in self._workers if worker.is_alive()]),
                "worker_target": self._worker_target(),
                "active_jobs": len(active),
                "queued_tokens": sum(job.remaining for job in active),
                "processed_total": self._processed_total,
                "failed_total": self._failed_total,
                "lanes": {
                    lane: len([job for job in self._pending[lane] if job.finished_at is None])
                    for lane in LANES
                },
            }

    # --------------------------------------------------------------- workers

    def _next_token(self, retirement: threading.Event) -> tuple[AccountJob, str] | None:
        with self._lock:
            if retirement.is_set() or self._stop.is_set():
                return None
            for offset in range(len(LANES)):
                lane = LANES[(self._lane_cursor + offset) % len(LANES)]
                queue = self._pending[lane]
                if not queue:
                    continue
                for job in list(queue)[:8]:
                    if job.finished_at is not None:
                        self._drop_from_lane_locked(job)
                        continue
                    if job.cancel.is_set():
                        self._finalize_locked(job)
                        continue
                    if not job.tokens:
                        if job.active <= 0:
                            self._finalize_locked(job)
                        continue
                    if job.state == STATE_QUEUED:
                        job.state = STATE_RUNNING
                    token = job.tokens.popleft()
                    job.active += 1
                    self._lane_cursor = (self._lane_cursor + 1) % len(LANES)
                    return job, token
            return None

    def _drop_from_lane_locked(self, job: AccountJob) -> None:
        pending = self._pending.get(job.lane)
        if pending is None:
            return
        try:
            pending.remove(job)
        except ValueError:
            pass

    def _worker_loop(self, retirement: threading.Event) -> None:
        while not self._stop.is_set() and not retirement.is_set():
            item = self._next_token(retirement)
            if item is None:
                self._wake.wait(_IDLE_WAIT_SECONDS)
                self._wake.clear()
                continue
            job, token = item
            try:
                job.worker(token)
            except Exception as exc:  # noqa: BLE001 - worker failures are data
                with self._lock:
                    job.failed += 1
                    self._failed_total += 1
                    job.errors.append({
                        "token_hint": token[:6] + "***" if len(token) > 9 else "***",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    })
            else:
                with self._lock:
                    self._processed_total += 1
            finally:
                with self._lock:
                    job.processed += 1
                    job.active = max(0, job.active - 1)
                    self._maybe_finalize_locked(job)
        with self._lock:
            current = threading.current_thread()
            self._retire.pop(id(current), None)
            try:
                self._workers.remove(current)
            except ValueError:
                pass

    def _maybe_finalize_locked(self, job: AccountJob) -> None:
        if job.finished_at is not None:
            return
        if job.tokens or job.active > 0:
            return
        self._finalize_locked(job)

    def _finalize_locked(self, job: AccountJob) -> None:
        if job.finished_at is not None:
            return
        job.skipped = len(job.tokens)
        job.tokens.clear()
        job.finished_at = time.time()
        job.state = STATE_CANCELLED if job.cancel.is_set() else STATE_DONE
        pending = self._pending.get(job.lane)
        if pending is not None:
            try:
                pending.remove(job)
            except ValueError:
                pass
        payload = job.as_dict()
        hook = job.on_finish
        if hook is not None:
            try:
                hook(job, payload)
            except Exception:
                pass


_QUEUE: AccountJobQueue | None = None
_QUEUE_LOCK = threading.Lock()


def _dynamic_target() -> int:
    from services.config import config

    base = int(config.account_check_workers)
    try:
        from services.task_scheduler import task_scheduler

        base = task_scheduler().queue_workers(base)
    except Exception:
        pass
    try:
        from services.adaptive_governor import governor

        return max(1, governor().effective_limit(base, floor=1))
    except Exception:
        return max(1, base)


def job_queue() -> AccountJobQueue:
    """Process-wide queue sized from settings and the adaptive governor."""
    global _QUEUE
    if _QUEUE is not None:
        return _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = AccountJobQueue(worker_target=_dynamic_target)
    return _QUEUE
