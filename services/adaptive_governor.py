"""Self-adaptive load governor for the million-account control plane.

The operator's saved concurrency settings are treated as the *ceiling*.  The
governor never rewrites them; it only derives a temporary, in-memory effective
budget from live load so a saturated box sheds work instead of collapsing, and
then springs back to the operator's exact numbers once the box is healthy
again.

Contract
--------
* overload  = ``cpu > cpu_high`` **or** ``memory > mem_high``
* recovery  = ``cpu < cpu_low`` **and** ``memory < mem_low`` sustained for
  ``restore_hold_seconds`` (default 300s)
* a downgrade is immediate (one step per evaluation), the restore is a single
  jump back to the user's original value;
* ``manual`` mode pins the scale at 1.0 and only observes;
* stored settings are never mutated, so restarting the process always begins
  from the operator's presets.

Every dependency is injectable so the state machine is unit-testable without
real hardware or wall-clock sleeps.
"""

from __future__ import annotations

import threading
from collections import deque
from time import monotonic
from typing import Callable, Mapping

from services.hardware_profile import (
    LoadSample,
    LoadSampler,
    build_profile,
    merge_profile,
    recommended_parameters,
)

MODE_AUTO = "auto"
MODE_MANUAL = "manual"

LEVEL_NORMAL = "normal"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"

SCALE_STEPS = (1.0, 0.75, 0.5, 0.35, 0.2)

DEFAULT_ADAPTIVE_SETTINGS: dict[str, object] = {
    "enabled": True,
    "mode": MODE_AUTO,
    "cpu_high": 85,
    "mem_high": 90,
    "cpu_low": 70,
    "mem_low": 80,
    "restore_hold_seconds": 300,
    "sample_interval_seconds": 5,
    "min_scale_percent": 20,
    "hardware_override": "auto",
}

_HISTORY_LIMIT = 240
_EVENT_LIMIT = 40

_Listener = Callable[[dict[str, object]], None]


class AdaptiveGovernor:
    """State machine translating load samples into an effective budget."""

    def __init__(
        self,
        *,
        settings_getter: Callable[[], Mapping[str, object]] | None = None,
        base_getter: Callable[[], Mapping[str, int]] | None = None,
        sampler: LoadSampler | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._clock = clock
        self._settings_getter = settings_getter or (lambda: DEFAULT_ADAPTIVE_SETTINGS)
        self._base_getter = base_getter or (lambda: {})
        self._sampler = sampler or LoadSampler()
        self._lock = threading.RLock()
        self._profile = build_profile()
        self._scale = 1.0
        self._level = LEVEL_NORMAL
        self._mode = MODE_AUTO
        self._enabled = True
        self._overload_started_at: float | None = None
        self._healthy_since: float | None = None
        self._last_adjust_at: float | None = None
        self._last_evaluated_at: float | None = None
        self._last_sample = LoadSample(None, None, None, None, None, None, 0.0)
        self._history: deque[dict[str, object]] = deque(maxlen=_HISTORY_LIMIT)
        self._events: deque[dict[str, object]] = deque(maxlen=_EVENT_LIMIT)
        self._listeners: list[_Listener] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._adjustments = 0

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            thread = threading.Thread(
                target=self._run,
                name="adaptive-governor",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
        self._thread = None

    def add_listener(self, listener: _Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def refresh_profile(self) -> None:
        with self._lock:
            self._profile = build_profile()

    def _run(self) -> None:
        # Prime the CPU delta so the first comparable sample arrives quickly.
        try:
            self._sampler.sample()
        except Exception:
            pass
        while not self._stop.is_set():
            interval = 5.0
            try:
                interval = max(1.0, float(self._settings().get("sample_interval_seconds") or 5))
            except (TypeError, ValueError):
                interval = 5.0
            try:
                self.tick()
            except Exception:
                pass
            self._stop.wait(interval)

    # -------------------------------------------------------------- sampling

    def _settings(self) -> dict[str, object]:
        try:
            raw = self._settings_getter() or {}
        except Exception:
            raw = {}
        merged = dict(DEFAULT_ADAPTIVE_SETTINGS)
        if isinstance(raw, Mapping):
            merged.update({key: value for key, value in raw.items() if key in merged})
        return merged

    def _base_values(self) -> dict[str, int]:
        try:
            raw = self._base_getter() or {}
        except Exception:
            raw = {}
        values: dict[str, int] = {}
        for key, value in raw.items():
            try:
                values[str(key)] = max(1, int(value))
            except (TypeError, ValueError):
                continue
        return values

    def tick(self) -> dict[str, object]:
        sample = self._sampler.sample()
        return self._apply_sample(sample)

    def _apply_sample(self, sample: LoadSample) -> dict[str, object]:
        settings = self._settings()
        now = self._clock()
        with self._lock:
            self._last_sample = sample
            self._last_evaluated_at = now
            self._mode = MODE_MANUAL if str(settings.get("mode") or "").lower() == MODE_MANUAL else MODE_AUTO
            self._enabled = bool(settings.get("enabled", True))
            cpu = sample.cpu_percent
            memory = sample.memory_percent
            cpu_high = _number(settings.get("cpu_high"), 85.0)
            mem_high = _number(settings.get("mem_high"), 90.0)
            cpu_low = _number(settings.get("cpu_low"), 70.0)
            mem_low = _number(settings.get("mem_low"), 80.0)
            hold = max(10.0, _number(settings.get("restore_hold_seconds"), 300.0))
            min_scale = max(0.05, min(1.0, _number(settings.get("min_scale_percent"), 20.0) / 100.0))

            overloaded = _at_least(cpu, cpu_high) or _at_least(memory, mem_high)
            healthy = _below(cpu, cpu_low) and _below(memory, mem_low)
            # Load average stays observational: it is host-wide, includes other
            # containers and IO wait, and in practice caused false "overload"
            # at 60% CPU. Only the operator's CPU/memory watermarks may change
            # the scale; load is reported in history/snapshot for diagnosis.

            if not self._enabled or self._mode == MODE_MANUAL:
                self._healthy_since = None
                self._overload_started_at = None
                if self._scale != 1.0:
                    self._scale = 1.0
                    self._level = LEVEL_NORMAL
                    self._last_adjust_at = now
                    self._events.append({
                        "at": now,
                        "event": "pinned",
                        "reason": "manual mode" if self._mode == MODE_MANUAL else "disabled",
                        "scale": self._scale,
                    })
                self._record(sample, cpu, memory, now)
                return self.snapshot_locked()

            if overloaded:
                self._healthy_since = None
                if self._overload_started_at is None:
                    self._overload_started_at = now
                next_scale = _next_lower(self._scale, min_scale)
                if next_scale < self._scale:
                    self._scale = next_scale
                    self._last_adjust_at = now
                    self._adjustments += 1
                    self._level = LEVEL_CRITICAL if self._scale <= min_scale or (
                        _at_least(cpu, 95.0) or _at_least(memory, 95.0)
                    ) else LEVEL_WARNING
                    self._events.append({
                        "at": now,
                        "event": "downgrade",
                        "reason": _overload_reason(cpu, memory, cpu_high, mem_high),
                        "scale": self._scale,
                    })
                else:
                    self._level = LEVEL_CRITICAL
            elif healthy:
                self._overload_started_at = None
                if self._scale < 1.0:
                    if self._healthy_since is None:
                        self._healthy_since = now
                    held_seconds = now - self._healthy_since
                    if held_seconds >= hold:
                        self._scale = 1.0
                        self._level = LEVEL_NORMAL
                        self._healthy_since = None
                        self._last_adjust_at = now
                        self._adjustments += 1
                        self._events.append({
                            "at": now,
                            "event": "restore",
                            "reason": f"healthy for {int(held_seconds)}s",
                            "scale": self._scale,
                        })
                else:
                    self._level = LEVEL_NORMAL
            else:
                # Between the watermark bands: keep the current scale and reset
                # the recovery timer so recovery must be continuous.
                self._healthy_since = None
                if self._scale < 1.0:
                    self._level = LEVEL_WARNING

            self._record(sample, cpu, memory, now)
            snapshot = self.snapshot_locked()
        self._notify(snapshot)
        return snapshot

    def _record(self, sample: LoadSample, cpu: float | None, memory: float | None, now: float) -> None:
        self._history.append({
            "at": now,
            "cpu": None if cpu is None else round(cpu, 1),
            "memory": None if memory is None else round(memory, 1),
            "scale": round(self._scale, 2),
            "level": self._level,
            "load_average": sample.load_average,
        })

    def _notify(self, snapshot: dict[str, object]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                continue

    # ------------------------------------------------------------- effective

    def scale(self) -> float:
        with self._lock:
            return self._scale

    def effective_limit(self, base: int | float, *, floor: int = 1) -> int:
        try:
            normalized = int(base)
        except (TypeError, ValueError):
            return max(floor, 1)
        with self._lock:
            scale = self._scale
        return max(int(floor), int(round(normalized * scale)))

    def effective_concurrency(self, base: int | float, *, floor: int = 1) -> int:
        return self.effective_limit(base, floor=floor)

    # ----------------------------------------------------------------- status

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict[str, object]:
        settings = self._settings()
        base = self._base_values()
        now = self._clock()
        hardware = merge_profile(self._profile, settings)
        # Mode / enablement are read from the saved policy on every snapshot so
        # a switch in the UI is visible immediately instead of on the next tick.
        mode = MODE_MANUAL if str(settings.get("mode") or "").lower() == MODE_MANUAL else MODE_AUTO
        enabled = bool(settings.get("enabled", True))
        effective = {
            key: max(1, int(round(value * self._scale)))
            for key, value in base.items()
        }
        load = self._last_sample.as_dict()
        return {
            "enabled": enabled,
            "mode": mode,
            "level": self._level,
            "scale": round(self._scale, 2),
            "scale_percent": int(round(self._scale * 100)),
            "min_scale_percent": int(_number(settings.get("min_scale_percent"), 20.0)),
            "watermarks": {
                "cpu_high": _number(settings.get("cpu_high"), 85.0),
                "mem_high": _number(settings.get("mem_high"), 90.0),
                "cpu_low": _number(settings.get("cpu_low"), 70.0),
                "mem_low": _number(settings.get("mem_low"), 80.0),
                "restore_hold_seconds": _number(settings.get("restore_hold_seconds"), 300.0),
                "sample_interval_seconds": _number(settings.get("sample_interval_seconds"), 5.0),
            },
            "hardware": hardware,
            "load": load,
            "base": base,
            "effective": effective,
            "overload_since": self._overload_started_at,
            "healthy_since": self._healthy_since,
            "restore_in_seconds": self._restore_eta(settings, now),
            "last_adjust_at": self._last_adjust_at,
            "last_evaluated_at": self._last_evaluated_at,
            "adjustments": self._adjustments,
            "history": list(self._history),
            "events": list(self._events),
        }

    def _restore_eta(self, settings: Mapping[str, object], now: float) -> int | None:
        if self._scale >= 1.0 or self._healthy_since is None:
            return None
        hold = max(10.0, _number(settings.get("restore_hold_seconds"), 300.0))
        return max(0, int(hold - (now - self._healthy_since)))

    # -------------------------------------------------------- single sample

    def evaluate_sample(
        self,
        cpu: float | None,
        memory: float | None,
        load_average: float | None = None,
    ) -> dict[str, object]:
        """Test seam: evaluate an explicit sample without touching hardware."""
        sample = LoadSample(
            cpu_percent=cpu,
            memory_percent=memory,
            memory_available_bytes=None,
            load_average=load_average,
            disk_read_bytes_per_sec=None,
            disk_write_bytes_per_sec=None,
            sampled_at=self._clock(),
        )
        return self._apply_sample(sample)

    def base_from_tier(self) -> dict[str, int]:
        return recommended_parameters(self._profile.tier)


def _number(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _below(value: float | None, threshold: float) -> bool:
    return value is not None and value < threshold


def _next_lower(scale: float, min_scale: float) -> float:
    for step in SCALE_STEPS:
        if step < scale - 1e-9:
            return max(step, min_scale) if step >= min_scale else min_scale
    return max(min_scale, min(scale, SCALE_STEPS[-1]))


def _overload_reason(
    cpu: float | None,
    memory: float | None,
    cpu_high: float,
    mem_high: float,
) -> str:
    parts: list[str] = []
    if _at_least(cpu, cpu_high):
        parts.append(f"cpu {cpu:.0f}%")
    if _at_least(memory, mem_high):
        parts.append(f"memory {memory:.0f}%")
    return ", ".join(parts) or "overload"


def create_governor(
    *,
    settings_getter: Callable[[], Mapping[str, object]] | None = None,
    base_getter: Callable[[], Mapping[str, int]] | None = None,
) -> AdaptiveGovernor:
    return AdaptiveGovernor(settings_getter=settings_getter, base_getter=base_getter)


_GOVERNOR: AdaptiveGovernor | None = None
_GOVERNOR_LOCK = threading.Lock()


def governor() -> AdaptiveGovernor:
    """Process-wide governor wired to the live configuration store."""
    global _GOVERNOR
    if _GOVERNOR is not None:
        return _GOVERNOR
    with _GOVERNOR_LOCK:
        if _GOVERNOR is None:
            from services.config import config

            _GOVERNOR = AdaptiveGovernor(
                settings_getter=lambda: config.adaptive_settings,
                base_getter=lambda: {
                    "account_processing_concurrency": config.account_processing_concurrency,
                    "account_check_workers": config.account_check_workers,
                    "worker_threads": config.worker_threads,
                    "image_task_workers": config.image_task_workers,
                },
            )
    return _GOVERNOR
