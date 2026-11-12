"""Hardware discovery and lightweight load sampling.

The control plane must size itself from the machine it actually runs on: a
16-core/16 GiB box cannot run the same worker budget as a 32-core/128 GiB box.
This module gathers that picture without adding a mandatory dependency:

* Linux uses ``/proc`` (``cpu``, ``meminfo``, ``diskstats``, ``loadavg``);
* Windows uses kernel32 through :mod:`ctypes` (``GetSystemTimes`` and
  ``GlobalMemoryStatusEx``);
* when a sample is unavailable the field is reported as ``None`` and callers
  degrade to the dimensions they *can* observe, instead of guessing ``0``.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Mapping

TIER_FLAGSHIP = "flagship"
TIER_HIGH = "high"
TIER_MEDIUM = "medium"
TIER_LOW = "low"

TIER_LABELS = {
    TIER_FLAGSHIP: "旗舰机",
    TIER_HIGH: "高配",
    TIER_MEDIUM: "中配",
    TIER_LOW: "低配",
}

GB = 1024 ** 3


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    cpu_cores: int
    cpu_model: str
    memory_total_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    platform: str
    tier: str

    @property
    def memory_total_gb(self) -> float:
        return round(self.memory_total_bytes / GB, 1) if self.memory_total_bytes else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "cpu_cores": self.cpu_cores,
            "cpu_model": self.cpu_model,
            "memory_total_bytes": self.memory_total_bytes,
            "memory_total_gb": self.memory_total_gb,
            "disk_total_bytes": self.disk_total_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "platform": self.platform,
            "tier": self.tier,
            "tier_label": TIER_LABELS.get(self.tier, self.tier),
        }


@dataclass(frozen=True, slots=True)
class LoadSample:
    cpu_percent: float | None
    memory_percent: float | None
    memory_available_bytes: int | None
    load_average: float | None
    disk_read_bytes_per_sec: float | None
    disk_write_bytes_per_sec: float | None
    sampled_at: float = field(default_factory=monotonic)

    def as_dict(self) -> dict[str, object]:
        return {
            "cpu_percent": None if self.cpu_percent is None else round(self.cpu_percent, 1),
            "memory_percent": None if self.memory_percent is None else round(self.memory_percent, 1),
            "memory_available_bytes": self.memory_available_bytes,
            "load_average": None if self.load_average is None else round(self.load_average, 2),
            "disk_read_bytes_per_sec": self.disk_read_bytes_per_sec,
            "disk_write_bytes_per_sec": self.disk_write_bytes_per_sec,
            "sampled_at": self.sampled_at,
        }


def detect_hardware(root: Path | None = None) -> HardwareProfile:
    return HardwareProfile(
        cpu_cores=max(1, int(os.cpu_count() or 1)),
        cpu_model=_cpu_model(),
        memory_total_bytes=_memory_total_bytes() or 0,
        disk_total_bytes=_disk().total,
        disk_free_bytes=_disk().free,
        platform=f"{platform.system()} {platform.release()}".strip(),
        tier=TIER_LOW,
    )


def classify_tier(cpu_cores: int, memory_total_bytes: int) -> str:
    cores = max(1, int(cpu_cores or 1))
    memory_gb = (memory_total_bytes or 0) / GB
    if cores >= 32 and memory_gb >= 64:
        return TIER_FLAGSHIP
    if cores >= 16 and memory_gb >= 24:
        return TIER_HIGH
    if cores >= 8 and memory_gb >= 12:
        return TIER_MEDIUM
    return TIER_LOW


def recommended_parameters(tier: str) -> dict[str, int]:
    """Baseline worker budget for a hardware tier.

    These are suggestions, not overrides: they seed defaults only while the
    operator has not saved an explicit value.
    """
    if tier == TIER_FLAGSHIP:
        return {
            "account_processing_concurrency": 512,
            "account_check_workers": 512,
            "image_task_workers": 64,
            "image_task_queue_size": 4096,
            "thread_tokens": 512,
            "selection_scan_budget": 8,
        }
    if tier == TIER_HIGH:
        return {
            "account_processing_concurrency": 256,
            "account_check_workers": 256,
            "image_task_workers": 32,
            "image_task_queue_size": 2048,
            "thread_tokens": 256,
            "selection_scan_budget": 8,
        }
    if tier == TIER_MEDIUM:
        return {
            "account_processing_concurrency": 96,
            "account_check_workers": 96,
            "image_task_workers": 16,
            "image_task_queue_size": 1024,
            "thread_tokens": 128,
            "selection_scan_budget": 8,
        }
    return {
        "account_processing_concurrency": 24,
        "account_check_workers": 24,
        "image_task_workers": 8,
        "image_task_queue_size": 256,
        "thread_tokens": 64,
        "selection_scan_budget": 8,
    }


def build_profile(root: Path | None = None) -> HardwareProfile:
    base = detect_hardware(root)
    tier = classify_tier(base.cpu_cores, base.memory_total_bytes)
    return HardwareProfile(
        cpu_cores=base.cpu_cores,
        cpu_model=base.cpu_model,
        memory_total_bytes=base.memory_total_bytes,
        disk_total_bytes=base.disk_total_bytes,
        disk_free_bytes=base.disk_free_bytes,
        platform=base.platform,
        tier=tier,
    )


class LoadSampler:
    """Stateful sampler because CPU utilisation is a delta between two reads."""

    def __init__(self) -> None:
        self._previous_cpu: tuple[int, int] | None = None
        self._previous_disk: tuple[float, int, int] | None = None

    def sample(self) -> LoadSample:
        cpu = self._sample_cpu_percent()
        memory_total, memory_available = _memory_snapshot()
        memory_percent = None
        if memory_total and memory_available is not None:
            used = max(0, memory_total - memory_available)
            memory_percent = used / memory_total * 100.0
        read_bps, write_bps = self._sample_disk_throughput()
        return LoadSample(
            cpu_percent=cpu,
            memory_percent=memory_percent,
            memory_available_bytes=memory_available,
            load_average=_load_average(),
            disk_read_bytes_per_sec=read_bps,
            disk_write_bytes_per_sec=write_bps,
        )

    # ------------------------------------------------------------------ CPU

    def _sample_cpu_percent(self) -> float | None:
        if sys.platform.startswith("linux"):
            return self._linux_cpu_percent()
        if sys.platform == "win32":
            return self._windows_cpu_percent()
        try:
            load = os.getloadavg()[0]
        except (AttributeError, OSError):
            return None
        cores = max(1, os.cpu_count() or 1)
        return min(100.0, max(0.0, load / cores * 100.0))

    def _linux_cpu_percent(self) -> float | None:
        try:
            raw = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
            fields = [int(part) for part in raw.split()[1:]]
        except (OSError, ValueError, IndexError):
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        previous = self._previous_cpu
        self._previous_cpu = (total, idle)
        if previous is None or total <= previous[0]:
            return None
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0:
            return None
        return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))

    def _windows_cpu_percent(self) -> float | None:
        try:
            idle, kernel, user = _windows_system_times()
        except OSError:
            return None
        previous = self._previous_cpu
        self._previous_cpu = (kernel + user, idle)
        if previous is None:
            return None
        total_delta = (kernel + user) - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0:
            return None
        busy = max(0, total_delta - idle_delta)
        return max(0.0, min(100.0, busy / total_delta * 100.0))

    # ----------------------------------------------------------------- disk

    def _sample_disk_throughput(self) -> tuple[float | None, float | None]:
        if not sys.platform.startswith("linux"):
            return (None, None)
        try:
            read_sectors = write_sectors = 0
            for line in Path("/proc/diskstats").read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) < 10:
                    continue
                name = fields[2]
                if any(char.isdigit() for char in name):
                    continue
                read_sectors += int(fields[5])
                write_sectors += int(fields[9])
        except (OSError, ValueError, IndexError):
            return (None, None)
        now = monotonic()
        previous = self._previous_disk
        self._previous_disk = (now, read_sectors, write_sectors)
        if previous is None:
            return (None, None)
        elapsed = now - previous[0]
        if elapsed <= 0:
            return (None, None)
        read_bps = max(0.0, (read_sectors - previous[1]) * 512 / elapsed)
        write_bps = max(0.0, (write_sectors - previous[2]) * 512 / elapsed)
        return (round(read_bps, 1), round(write_bps, 1))


# --------------------------------------------------------------- internals


def _cpu_model() -> str:
    if sys.platform == "win32":
        name = os.getenv("PROCESSOR_IDENTIFIER") or ""
        return name.strip()
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _disk(root: Path | None = None) -> shutil._ntuple_diskusage:  # type: ignore[attr-defined]
    target = Path(root) if root is not None else Path.cwd()
    try:
        return shutil.disk_usage(target)
    except OSError:
        return shutil._ntuple_diskusage(0, 0, 0)  # type: ignore[attr-defined]


def _memory_total_bytes() -> int | None:
    total, _available = _memory_snapshot()
    return total


def _memory_snapshot() -> tuple[int | None, int | None]:
    if sys.platform.startswith("linux"):
        try:
            values: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[key.strip()] = int(parts[0]) * 1024
            total = values.get("MemTotal")
            available = values.get("MemAvailable")
            if available is None and total is not None:
                available = values.get("MemFree")
            return (total, available)
        except OSError:
            return (None, None)
    if sys.platform == "win32":
        return _windows_memory()
    return (None, None)


def _load_average() -> float | None:
    try:
        return float(os.getloadavg()[0])
    except (AttributeError, OSError):
        return None


if sys.platform == "win32":

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    def _filetime_to_int(value: _FILETIME) -> int:
        return (value.dwHighDateTime << 32) | value.dwLowDateTime

    def _windows_system_times() -> tuple[int, int, int]:
        idle = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            raise OSError("GetSystemTimes failed")
        return (
            _filetime_to_int(idle),
            _filetime_to_int(kernel),
            _filetime_to_int(user),
        )

    def _windows_memory() -> tuple[int | None, int | None]:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        # Use a private kernel32 handle: another module may set argtypes on the
        # shared windll function for a different struct and break this call.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
        kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return (None, None)
        return (int(status.ullTotalPhys), int(status.ullAvailPhys))

else:  # pragma: no cover - non Windows hosts never call these

    def _windows_system_times() -> tuple[int, int, int]:
        raise OSError("not supported")

    def _windows_memory() -> tuple[int | None, int | None]:
        return (None, None)


def merge_profile(profile: HardwareProfile, settings: Mapping[str, object]) -> dict[str, object]:
    """Return the hardware view enriched with the effective tier."""
    override = str(settings.get("hardware_override") or "auto").strip().lower()
    effective_tier = profile.tier if override not in TIER_LABELS else override
    payload = profile.as_dict()
    payload["detected_tier"] = profile.tier
    payload["hardware_override"] = override if override in TIER_LABELS else "auto"
    payload["effective_tier"] = effective_tier
    payload["effective_tier_label"] = TIER_LABELS.get(effective_tier, effective_tier)
    payload["recommended"] = recommended_parameters(effective_tier)
    return payload


TIER_SEEDED_FLAG = "adaptive_tier_seeded"

_SEEDABLE_SETTINGS = (
    "account_processing_concurrency",
    "account_check_workers",
    "worker_threads",
    "image_task_workers",
)


def tier_default_updates(
    stored: Mapping[str, object],
    tier: str,
    *,
    already_seeded: bool,
) -> dict[str, object]:
    """Compute the first-run defaults for a hardware tier.

    Only settings the operator never saved are seeded; an existing stored value
    is always preserved.  ``already_seeded`` makes the seeding a one-shot
    operation so later hardware changes never overwrite explicit choices.
    """
    if already_seeded:
        return {}
    recommended = recommended_parameters(tier)
    updates: dict[str, object] = {}
    for key in _SEEDABLE_SETTINGS:
        if key in stored:
            continue
        value = recommended.get(key)
        if value is not None:
            updates[key] = int(value)
    return updates
