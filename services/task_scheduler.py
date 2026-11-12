"""Central budget for background work versus foreground image/text requests.

All pools ultimately share the same upstream account pool.  Independent worker
counts made background sweeps look cheap while they could still occupy nearly
every account-processing slot.  This module computes one budget from the
configured total and the number of image slots currently leased:

    total slots
    - foreground reserve (>= 20)
    - active image/text work
    = background budget

Queue and memory workers consult this budget instead of using fixed counts.
It is intentionally an advisory budget: it never rewrites saved settings.
"""

from __future__ import annotations

import threading

from services.account_processing import foreground_reserve

_LOCK = threading.Lock()


class TaskScheduler:
    def total_slots(self) -> int:
        from services.config import config

        try:
            return max(1, int(config.account_processing_concurrency or 1))
        except (TypeError, ValueError):
            return 1

    def active_foreground(self) -> int:
        from services.account_service import account_service

        try:
            return max(0, int(account_service.image_inflight_total()))
        except Exception:
            return 0

    def snapshot(self) -> dict[str, int]:
        total = self.total_slots()
        reserve = foreground_reserve(total)
        active = min(self.active_foreground(), max(0, total - reserve))
        background = max(1, total - reserve - active)
        return {
            "total_slots": total,
            "foreground_reserve": reserve,
            "active_foreground": active,
            "background_budget": background,
            "queue_workers": min(128, background),
            "memory_workers": min(10, max(1, background // 2)),
        }

    def queue_workers(self, requested: int) -> int:
        budget = self.snapshot()["background_budget"]
        return max(1, min(max(1, int(requested or 1)), budget))

    def memory_workers(self, requested: int = 10) -> int:
        budget = self.snapshot()["background_budget"]
        return max(1, min(max(1, int(requested or 1)), max(1, budget // 2)))


_TASK_SCHEDULER: TaskScheduler | None = None


def task_scheduler() -> TaskScheduler:
    global _TASK_SCHEDULER
    if _TASK_SCHEDULER is None:
        with _LOCK:
            if _TASK_SCHEDULER is None:
                _TASK_SCHEDULER = TaskScheduler()
    return _TASK_SCHEDULER
