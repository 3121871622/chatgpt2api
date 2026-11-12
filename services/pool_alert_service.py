"""号池健康告警：续期能力、可用账号数跌破阈值时写日志并可推送 Webhook。

只读统计账号凭据状态，不修改任何账号；告警状态落在 ``data/pool_alerts.json``，
避免重复轰炸（冷却期内同一问题只报一次）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from services.config import DATA_DIR

CONFIG_FILE_NAME = "pool_alerts.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "webhook_url": "",
    "min_usable": 1,
    "min_refresh_ratio_percent": 10,
    "check_interval_seconds": 600,
    "cooldown_seconds": 6 * 3600,
}


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def normalize_config(raw: object) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    url = str(data.get("webhook_url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = ""
    return {
        "enabled": bool(data.get("enabled", False)),
        "webhook_url": url,
        "min_usable": _clamp_int(data.get("min_usable"), 1, 0, 1_000_000),
        "min_refresh_ratio_percent": _clamp_int(
            data.get("min_refresh_ratio_percent"), 10, 0, 100
        ),
        "check_interval_seconds": _clamp_int(
            data.get("check_interval_seconds"), 600, 300, 86_400
        ),
        "cooldown_seconds": _clamp_int(data.get("cooldown_seconds"), 6 * 3600, 600, 7 * 86_400),
    }


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 300, ""
    except Exception as exc:  # noqa: BLE001 - 告警失败不能影响主流程
        return False, f"{type(exc).__name__}: {exc}"


class PoolAlertService:
    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        account_service: Any | None = None,
        notifier: Callable[[str, dict[str, Any]], tuple[bool, str]] | None = None,
    ) -> None:
        root = Path(data_dir) if data_dir else DATA_DIR
        self._config_path = root / CONFIG_FILE_NAME
        self._state_path = root / ".pool-alerts-state.json"
        self._account_service = account_service
        self._notifier = notifier or _post_json
        self._lock = threading.Lock()
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

    def _accounts(self) -> Any:
        if self._account_service is None:
            from services.account_service import account_service

            self._account_service = account_service
        return self._account_service

    def get_config(self) -> dict[str, Any]:
        return normalize_config({**DEFAULT_CONFIG, **self._load(self._config_path, {})})

    def save_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.get_config()
            merged = {key: patch.get(key, current.get(key)) for key in DEFAULT_CONFIG}
            config = normalize_config(merged)
            self._save(self._config_path, config)
        return config

    # -------------------------------------------------------------- metrics

    def measure(self) -> dict[str, Any]:
        from services.account_credentials import project_upstream_credential_availability

        totals = {"total": 0, "usable": 0, "recoverable": 0, "unavailable": 0}
        refresh_valid = 0
        for account in self._accounts().iter_account_records():
            totals["total"] += 1
            projection = project_upstream_credential_availability(
                str(account.get("access_token") or ""),
                str(account.get("refresh_token") or ""),
                access_confirmed_invalid=(
                    str(account.get("last_remote_check_result") or "").strip().lower()
                    == "invalid"
                ),
                refresh_confirmed_invalid=bool(account.get("refresh_token_invalid_at")),
            )
            totals[projection.status] += 1
            if projection.refresh_status == "valid":
                refresh_valid += 1
        total = totals["total"]
        ratio = (refresh_valid / total * 100) if total else 0.0
        try:
            from services.account_check_queue import job_queue

            queue_stats = job_queue().stats()
        except Exception:
            queue_stats = {"queued_tokens": 0, "active_jobs": 0, "failed_total": 0}
        return {
            **totals,
            "refresh_valid": refresh_valid,
            "refresh_ratio_percent": round(ratio, 1),
            "queue_queued_tokens": int(queue_stats.get("queued_tokens") or 0),
            "queue_active_jobs": int(queue_stats.get("active_jobs") or 0),
            "queue_failed_total": int(queue_stats.get("failed_total") or 0),
        }

    def evaluate(self, metrics: dict[str, Any], config: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        if metrics["usable"] < int(config["min_usable"]):
            warnings.append(
                f"可用账号 {metrics['usable']} 低于阈值 {config['min_usable']}"
            )
        if metrics["refresh_ratio_percent"] < float(config["min_refresh_ratio_percent"]):
            warnings.append(
                "可续期账号占比 "
                f"{metrics['refresh_ratio_percent']}% 低于阈值 "
                f"{config['min_refresh_ratio_percent']}%（{metrics['refresh_valid']}/{metrics['total']}）"
            )
        queue_backlog = int(metrics.get("queue_queued_tokens") or 0)
        if queue_backlog > max(500, metrics["total"]):
            warnings.append(
                f"账号任务队列积压 {queue_backlog}，可能超过 6 分钟未消化"
            )
        return warnings

    # ----------------------------------------------------------------- run

    def run_once(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        config = self.get_config()
        metrics = self.measure()
        warnings = self.evaluate(metrics, config)
        report: dict[str, Any] = {
            "checked_at": _now_text(),
            "metrics": metrics,
            "warnings": warnings,
            "notified": False,
        }
        if not warnings:
            self._state["last_ok_at"] = report["checked_at"]
            self._state["last_report"] = report
            if not dry_run:
                self._save(self._state_path, self._state)
            return report

        last_alert = float(self._state.get("last_alert_epoch") or 0.0)
        cooling = (time.time() - last_alert) < int(config["cooldown_seconds"])
        should_notify = bool(config["enabled"] and config["webhook_url"]) and (
            force or not cooling
        )
        report["cooldown_active"] = cooling
        if dry_run:
            report["notified"] = False
            return report

        if should_notify:
            ok, error = self._notifier(
                config["webhook_url"],
                {
                    "event": "pool_health",
                    "checked_at": report["checked_at"],
                    "warnings": warnings,
                    "metrics": metrics,
                },
            )
            report["notified"] = ok
            report["notify_error"] = error
            if ok:
                self._state["last_alert_epoch"] = time.time()
                self._state["last_alert_at"] = report["checked_at"]
        self._state["last_report"] = report
        self._save(self._state_path, self._state)
        return report

    def status(self) -> dict[str, Any]:
        config = self.get_config()
        return {
            "config": config,
            "last_report": self._state.get("last_report") or {},
            "last_ok_at": self._state.get("last_ok_at") or "",
            "last_alert_at": self._state.get("last_alert_at") or "",
            "running": bool(self._thread and self._thread.is_alive()),
        }

    # ---------------------------------------------------------------- loop

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            config = self.get_config()
            if config["enabled"]:
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001 - 后台循环不能退出
                    pass
            self._stop_event.wait(float(config["check_interval_seconds"]))

    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="pool-alerts", daemon=True)
        self._thread.start()

    def stop_loop(self) -> None:
        self._stop_event.set()


pool_alert_service = PoolAlertService()
