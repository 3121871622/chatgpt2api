"""Proxy health tracking, degradation and automatic failover.

The pool used to keep using a dead egress forever: nothing recorded failures,
so a broken proxy stayed in rotation and requests kept timing out instead of
falling back to a healthy node (or to direct).  This registry turns both
passive request outcomes and active probes into a per-egress health state:

* ``healthy``  - last attempt worked;
* ``degraded`` - recent failures below the threshold, still usable;
* ``down``     - consecutive failures reached the threshold; the node is
  removed from rotation for a cooldown that doubles on repeated failures;
* ``unknown``  - never probed.

The registry only answers questions and never touches the network.  Probing is
opt-in through :meth:`ProxyHealthRegistry.probe_all`, which takes the probe
function from the caller so this module stays dependency-free and testable.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

STATE_UNKNOWN = "unknown"
STATE_HEALTHY = "healthy"
STATE_DEGRADED = "degraded"
STATE_DOWN = "down"

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_BASE_COOLDOWN_SECONDS = 60.0
DEFAULT_MAX_COOLDOWN_SECONDS = 1800.0
DEFAULT_PROBE_INTERVAL_SECONDS = 120.0

# Failures that say "this egress is broken" rather than "this account is bad".
EGRESS_FAILURE_CODES = frozenset({
    "upstream_connection_failed",
    "upstream_connection_timeout",
    "upstream_unavailable",
    "proxy_connection_failed",
    "image_download_failed",
    "task_interrupted",
})


@dataclass
class EgressHealth:
    key: str
    label: str = ""
    state: str = STATE_UNKNOWN
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    latency_ms: float | None = None
    last_error: str = ""
    last_ok_at: float | None = None
    last_fail_at: float | None = None
    cooldown_until: float = 0.0
    cooldown_seconds: float = 0.0

    def as_dict(self, now: float) -> dict[str, object]:
        state = self.state
        if state == STATE_DOWN and self.cooldown_until <= now:
            state = STATE_DEGRADED
        return {
            "key": self.key,
            "label": self.label or self.key,
            "state": state,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 1),
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
            "last_fail_at": self.last_fail_at,
            "cooldown_remaining": max(0, int(self.cooldown_until - now)),
        }


class ProxyHealthRegistry:
    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        base_cooldown_seconds: float = DEFAULT_BASE_COOLDOWN_SECONDS,
        max_cooldown_seconds: float = DEFAULT_MAX_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._lock = threading.RLock()
        self._nodes: dict[str, EgressHealth] = {}
        self._failure_threshold = max(1, int(failure_threshold))
        self._base_cooldown = max(1.0, float(base_cooldown_seconds))
        self._max_cooldown = max(self._base_cooldown, float(max_cooldown_seconds))
        self._clock = clock

    # ----------------------------------------------------------- recording

    def report_success(
        self,
        key: str,
        *,
        latency_ms: float | None = None,
        label: str = "",
    ) -> None:
        normalized = str(key or "").strip()
        if not normalized:
            return
        with self._lock:
            entry = self._entry(normalized, label)
            entry.state = STATE_HEALTHY
            entry.consecutive_failures = 0
            entry.total_successes += 1
            entry.last_ok_at = self._clock()
            entry.last_error = ""
            entry.cooldown_until = 0.0
            entry.cooldown_seconds = 0.0
            if latency_ms is not None:
                entry.latency_ms = (
                    float(latency_ms)
                    if entry.latency_ms is None
                    else entry.latency_ms * 0.7 + float(latency_ms) * 0.3
                )

    def report_failure(
        self,
        key: str,
        error: object = "",
        *,
        label: str = "",
    ) -> None:
        normalized = str(key or "").strip()
        if not normalized:
            return
        with self._lock:
            entry = self._entry(normalized, label)
            now = self._clock()
            entry.consecutive_failures += 1
            entry.total_failures += 1
            entry.last_fail_at = now
            entry.last_error = str(error or "")[:300]
            if entry.consecutive_failures >= self._failure_threshold:
                over = entry.consecutive_failures - self._failure_threshold
                cooldown = min(
                    self._max_cooldown,
                    self._base_cooldown * (2 ** min(over, 6)),
                )
                entry.state = STATE_DOWN
                entry.cooldown_seconds = cooldown
                entry.cooldown_until = now + cooldown
            else:
                entry.state = STATE_DEGRADED

    def note_probe(
        self,
        key: str,
        *,
        ok: bool,
        latency_ms: float | None = None,
        error: object = "",
        label: str = "",
    ) -> None:
        if ok:
            self.report_success(key, latency_ms=latency_ms, label=label)
            return
        # A failed probe counts as a full failure so a dead egress is removed
        # quickly even when no traffic happens to hit it.
        self.report_failure(key, error, label=label)

    def forget(self, key: str) -> None:
        with self._lock:
            self._nodes.pop(str(key or "").strip(), None)

    # ------------------------------------------------------------ questions

    def is_available(self, key: str) -> bool:
        normalized = str(key or "").strip()
        if not normalized:
            return True
        with self._lock:
            entry = self._nodes.get(normalized)
            if entry is None:
                return True
            if entry.state != STATE_DOWN:
                return True
            return entry.cooldown_until <= self._clock()

    def filter_available(
        self,
        items: Iterable[tuple[str, Mapping[str, object]]],
    ) -> list[tuple[str, Mapping[str, object]]]:
        return [(key, item) for key, item in items if self.is_available(key)]

    def state(self, key: str) -> dict[str, object] | None:
        with self._lock:
            entry = self._nodes.get(str(key or "").strip())
            return entry.as_dict(self._clock()) if entry is not None else None

    def snapshot(self) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            nodes = [entry.as_dict(now) for entry in self._nodes.values()]
        counts = {state: 0 for state in (STATE_UNKNOWN, STATE_HEALTHY, STATE_DEGRADED, STATE_DOWN)}
        for node in nodes:
            counts[str(node["state"])] = counts.get(str(node["state"]), 0) + 1
        down = [node for node in nodes if node["state"] == STATE_DOWN]
        return {
            "nodes": sorted(nodes, key=lambda item: str(item["key"])),
            "total": len(nodes),
            "counts": counts,
            "down_keys": [str(node["key"]) for node in down],
            "failure_threshold": self._failure_threshold,
            "base_cooldown_seconds": self._base_cooldown,
            "max_cooldown_seconds": self._max_cooldown,
        }

    def reset(self) -> None:
        with self._lock:
            self._nodes.clear()

    # -------------------------------------------------------------- probing

    def probe_all(
        self,
        targets: Iterable[tuple[str, str, str]],
        probe: Callable[[str], Mapping[str, object]],
    ) -> dict[str, object]:
        """Probe ``(key, url, label)`` targets through the caller's probe fn."""
        results: list[dict[str, object]] = []
        for key, url, label in targets:
            started = time.perf_counter()
            try:
                outcome = probe(url)
            except Exception as exc:  # noqa: BLE001 - probe failures are data
                outcome = {"ok": False, "error": str(exc)}
            latency_ms = (time.perf_counter() - started) * 1000
            ok = bool(outcome.get("ok"))
            self.note_probe(
                key,
                ok=ok,
                latency_ms=latency_ms,
                error=outcome.get("error") or outcome.get("message") or "",
                label=label,
            )
            results.append({
                "key": key,
                "ok": ok,
                "latency_ms": round(latency_ms, 1),
                "error": str(outcome.get("error") or "")[:200],
            })
        return {"probed": len(results), "results": results}

    def _entry(self, key: str, label: str) -> EgressHealth:
        entry = self._nodes.get(key)
        if entry is None:
            entry = EgressHealth(key=key, label=label)
            self._nodes[key] = entry
        elif label and not entry.label:
            entry.label = label
        return entry


proxy_health = ProxyHealthRegistry()


class ProxyProber:
    """Background active probe loop over configured proxy-group nodes."""

    def __init__(
        self,
        *,
        target_provider: Callable[[], list[tuple[str, str, str]]],
        probe: Callable[[str], Mapping[str, object]],
        registry: ProxyHealthRegistry | None = None,
        interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS,
    ) -> None:
        self._target_provider = target_provider
        self._probe = probe
        self._registry = registry or proxy_health
        self._interval = max(15.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run_at: float | None = None
        self._last_result: dict[str, object] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        thread = threading.Thread(target=self._run, name="proxy-health-prober", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
        self._thread = None

    def status(self) -> dict[str, object]:
        return {
            "running": bool(self._thread is not None and self._thread.is_alive()),
            "interval_seconds": self._interval,
            "last_run_at": self._last_run_at,
            "last_result": self._last_result,
        }

    def probe_once(self) -> dict[str, object]:
        try:
            targets = self._target_provider()
        except Exception as exc:  # noqa: BLE001
            self._last_result = {"error": str(exc)}
            return self._last_result
        if not targets:
            self._last_result = {"probed": 0, "results": []}
            return self._last_result
        result = self._registry.probe_all(targets, self._probe)
        self._last_run_at = time.time()
        self._last_result = result
        return result

    def _run(self) -> None:
        # First sweep shortly after boot so a broken node is removed before
        # traffic ramps up.
        if self._stop.wait(5.0):
            return
        while not self._stop.is_set():
            try:
                self.probe_once()
            except Exception:
                pass
            self._stop.wait(self._interval)
