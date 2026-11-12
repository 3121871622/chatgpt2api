"""本地自动备份：每日把 SQLite 数据库用备份 API 快照到 ``data/backups``。

不依赖 R2/云存储，适合「一台服务器 + 一块盘」的部署：SQLite 用官方 backup API
取一致快照（WAL 已提交数据也包含），保留最近 N 份，旧的自动清理。
PostgreSQL 部署下本服务会跳过（请使用既有 R2 备份或 pg_dump）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from services.application_database import DEFAULT_DATA_DIR
from services.config import DATA_DIR

CONFIG_FILE_NAME = "local_backup.json"
STATE_FILE_NAME = ".local-backup-state.json"
BACKUP_DIR_NAME = "backups"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "interval_hours": 24,
    "keep": 7,
}


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def normalize_config(raw: object) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    return {
        "enabled": bool(data.get("enabled", False)),
        "interval_hours": _clamp_int(data.get("interval_hours"), 24, 1, 24 * 30),
        "keep": _clamp_int(data.get("keep"), 7, 1, 365),
    }


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


class LocalBackupService:
    def __init__(self, *, data_dir: Path | None = None) -> None:
        root = Path(data_dir) if data_dir else DATA_DIR
        self._data_dir = root
        self._config_path = root / CONFIG_FILE_NAME
        self._state_path = root / STATE_FILE_NAME
        self._backup_dir = root / BACKUP_DIR_NAME
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = self._load(self._state_path, {})

    # ------------------------------------------------------------- plumbing

    @staticmethod
    def _load(path: Path, default: Any) -> Any:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        return data if isinstance(data, type(default)) else default

    def _save(self, path: Path, payload: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def get_config(self) -> dict[str, Any]:
        return normalize_config({**DEFAULT_CONFIG, **self._load(self._config_path, {})})

    def save_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.get_config()
            merged = {key: patch.get(key, current.get(key)) for key in DEFAULT_CONFIG}
            config = normalize_config(merged)
            self._save(self._config_path, config)
        return config

    # -------------------------------------------------------------- backup

    def _database_path(self) -> Path | None:
        configured = os.getenv("DATABASE_URL", "").strip()
        if configured and not configured.startswith("sqlite"):
            return None
        if configured.startswith("sqlite:///"):
            return Path(configured.removeprefix("sqlite:///"))
        root = Path(os.getenv("QINGMENG_DATA_DIR") or DATA_DIR) / "chatgpt2api.db"
        return root if root.is_file() else None

    def list_backups(self) -> list[dict[str, Any]]:
        if not self._backup_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self._backup_dir.glob("chatgpt2api-*.db"), reverse=True):
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append({
                "name": path.name,
                "size_bytes": stat.st_size,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            })
        return items

    def _prune(self, keep: int) -> int:
        files = sorted(self._backup_dir.glob("chatgpt2api-*.db"), reverse=True)
        removed = 0
        for path in files[keep:]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def run_once(self) -> dict[str, Any]:
        with self._run_lock:
            config = self.get_config()
            started = time.perf_counter()
            source = self._database_path()
            report: dict[str, Any] = {"started_at": _now_text()}
            if source is None:
                report["skipped"] = "仅支持 SQLite 本地部署（PostgreSQL 请用 R2 备份或 pg_dump）"
                report["finished_at"] = _now_text()
                return report
            self._backup_dir.mkdir(parents=True, exist_ok=True)
            target = self._backup_dir / f"chatgpt2api-{_stamp()}.db"
            try:
                src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
                try:
                    dst = sqlite3.connect(target)
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                finally:
                    src.close()
            except Exception as exc:  # noqa: BLE001 - 备份失败不能影响主流程
                report["error"] = f"{type(exc).__name__}: {exc}"
                report["finished_at"] = _now_text()
                self._save(self._state_path, {**self._state, "last_report": report})
                return report
            pruned = self._prune(int(config["keep"]))
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            report.update({
                "file": target.name,
                "size_bytes": size,
                "pruned": pruned,
                "kept": len(self.list_backups()),
                "duration_seconds": round(time.perf_counter() - started, 2),
                "finished_at": _now_text(),
            })
            self._state["last_run_epoch"] = time.time()
            self._state["last_report"] = report
            self._save(self._state_path, self._state)
            return report

    def status(self) -> dict[str, Any]:
        return {
            "config": self.get_config(),
            "last_report": self._state.get("last_report") or {},
            "backups": self.list_backups()[:10],
            "running": self._run_lock.locked(),
        }

    # ----------------------------------------------------------------- loop

    def _due(self, config: dict[str, Any]) -> bool:
        last = float(self._state.get("last_run_epoch") or 0.0)
        if not last:
            return True
        return (time.time() - last) >= float(config["interval_hours"]) * 3600

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            config = self.get_config()
            if config["enabled"] and self._due(config):
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001
                    pass
            self._stop_event.wait(600)

    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="local-backup", daemon=True)
        self._thread.start()

    def stop_loop(self) -> None:
        self._stop_event.set()


local_backup_service = LocalBackupService()
