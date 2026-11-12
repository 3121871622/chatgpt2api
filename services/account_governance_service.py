"""死号自动治理 + 账号日志瘦身。

扫描口径固定为 **AT 失效且 RT 不可用**（RT 失效或缺失）的账号：

* ``usable``      AT 仍有效，能继续请求；
* ``recoverable`` AT 失效但 RT 可用，下一轮刷新即可恢复；
* ``unavailable`` AT 失效且 RT 失效/缺失，才进入治理候选。

运行语义：

* ``off``     —— 不扫描、不清理，直接返回；
* ``report``  —— 只统计并记录，不改账号；``account`` 类型日志按独立保留期清理；
* ``disable`` —— 把候选账号置为「禁用」；
* ``purge``   —— 删除候选账号（走正常批量删除通道，会写账号日志）。

确认语义：

* ``dry_run=True`` 或 ``confirm=False`` 只预检，返回候选数量、指纹和样本；
* 真正执行 ``disable`` / ``purge`` 需要 ``confirm=True``；
* ``expected_fingerprint`` 与本次扫描不一致时拒绝执行，避免「预检后号池又
  刷新了一批账号」仍按旧清单删除；
* ``confirm=None`` 保留 1.0.9 的兼容行为（直接执行），但会写 warning。新调用方
  应使用两阶段调用：``preview_once()`` → ``run_once(confirm=True,
  expected_fingerprint=...)``。

扫描按账号窗口分页读取，内存只保留候选 token（受 ``max_per_run`` 限制）
和少量样本，不会为百万级号池复制全量账号。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from services.account_credentials import project_upstream_credential_availability
from services.config import DATA_DIR

CONFIG_FILE_NAME = "account_governance.json"
STATE_FILE_NAME = ".governance-state.json"

# One page is copied at a time; large pools must not be materialised as a list.
_SCAN_PAGE_SIZE = 2_000
_SAMPLE_LIMIT = 5

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "mode": "report",  # off | report | disable | purge
    "run_every_hours": 24,
    "account_log_retention_hours": 168,
    "max_per_run": 20_000,
}


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def normalize_config(raw: object) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    mode = str(data.get("mode") or "report").strip().lower()
    if mode not in {"off", "report", "disable", "purge"}:
        mode = "report"
    return {
        "enabled": bool(data.get("enabled", False)),
        "mode": mode,
        "run_every_hours": _clamp_int(data.get("run_every_hours"), 24, 1, 720),
        "account_log_retention_hours": _clamp_int(
            data.get("account_log_retention_hours"), 168, 1, 8760
        ),
        "max_per_run": _clamp_int(data.get("max_per_run"), 20_000, 1, 1_000_000),
    }


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _mask_email(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" not in text:
        return text[:2] + "***"
    local, _, domain = text.partition("@")
    return f"{local[:1]}***@{domain}"


class AccountGovernanceService:
    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        account_service: Any | None = None,
        log_service: Any | None = None,
    ) -> None:
        root = Path(data_dir) if data_dir else DATA_DIR
        self._config_path = root / CONFIG_FILE_NAME
        self._state_path = root / STATE_FILE_NAME
        self._account_service = account_service
        self._log_service = log_service
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

    def _accounts(self) -> Any:
        if self._account_service is None:
            from services.account_service import account_service

            self._account_service = account_service
        return self._account_service

    def _logs(self) -> Any:
        if self._log_service is None:
            from services.log_service import log_service

            self._log_service = log_service
        return self._log_service

    def get_config(self) -> dict[str, Any]:
        return normalize_config({**DEFAULT_CONFIG, **self._load(self._config_path, {})})

    def save_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.get_config()
            merged = {key: patch.get(key, current.get(key)) for key in DEFAULT_CONFIG}
            config = normalize_config(merged)
            self._save(self._config_path, config)
        return config

    def status(self) -> dict[str, Any]:
        return {
            "config": self.get_config(),
            "last_report": self._state.get("last_report") or {},
            "last_preview": self._state.get("last_preview") or {},
            "running": self._run_lock.locked(),
        }

    # ------------------------------------------------------------- scanning

    @staticmethod
    def _projection(account: dict) -> Any:
        return project_upstream_credential_availability(
            str(account.get("access_token") or ""),
            str(account.get("refresh_token") or ""),
            access_confirmed_invalid=(
                str(account.get("last_remote_check_result") or "").strip().lower()
                == "invalid"
            ),
            refresh_confirmed_invalid=bool(account.get("refresh_token_invalid_at")),
        )

    def _iter_account_pages(self) -> Iterator[list[dict]]:
        """Yield bounded account pages.

        The real AccountService exposes an insertion-order window that copies
        only one page; the fallback keeps the service testable with the small
        read-only stubs used by the existing governance tests.
        """
        accounts = self._accounts()
        window = getattr(accounts, "iter_account_window", None)
        count = getattr(accounts, "account_count", None)
        if callable(window) and callable(count):
            try:
                total = int(count())
            except (TypeError, ValueError):
                total = 0
            start = 0
            while start < total:
                stop = min(start + _SCAN_PAGE_SIZE, total)
                page = window(start, stop)
                if page:
                    yield page
                start = stop
            return

        page: list[dict] = []
        for account in accounts.iter_account_records():
            page.append(account)
            if len(page) >= _SCAN_PAGE_SIZE:
                yield page
                page = []
        if page:
            yield page

    def _scan(
        self,
        *,
        collect_tokens: bool,
        token_limit: int | None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        pages = 0
        total = 0
        usable = 0
        recoverable = 0
        unavailable = 0
        dead_status: dict[str, int] = {}
        rt_invalid = 0
        rt_missing = 0
        fingerprint = 0
        tokens: list[str] = []
        sample: list[dict[str, Any]] = []
        truncated = False

        for page in self._iter_account_pages():
            pages += 1
            for account in page:
                total += 1
                projection = self._projection(account)
                if projection.status == "usable":
                    usable += 1
                    continue
                if projection.status == "recoverable":
                    recoverable += 1
                    continue
                unavailable += 1
                status = str(account.get("status") or "")
                if status:
                    dead_status[status] = dead_status.get(status, 0) + 1
                if projection.refresh_status == "missing":
                    rt_missing += 1
                else:
                    rt_invalid += 1
                token = str(account.get("access_token") or "").strip()
                if not token:
                    continue
                fingerprint ^= int.from_bytes(
                    hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest(),
                    "big",
                )
                if collect_tokens:
                    if token_limit is None or len(tokens) < token_limit:
                        tokens.append(token)
                    else:
                        truncated = True
                if len(sample) < _SAMPLE_LIMIT:
                    sample.append(
                        {
                            "account_id": str(account.get("management_id") or "").strip(),
                            "email": _mask_email(account.get("email")),
                            "status": status,
                            "access": projection.access.status,
                            "refresh": projection.refresh_status,
                            "refresh_confirmed_invalid": bool(
                                account.get("refresh_token_invalid_at")
                            ),
                        }
                    )

        candidate_fingerprint = f"{fingerprint:032x}-{unavailable}"
        return {
            "scanned_accounts": total,
            "scan_pages": pages,
            "credential_counts": {
                "usable": usable,
                "recoverable": recoverable,
                "unavailable": unavailable,
            },
            "dead_accounts": unavailable,
            "dead_breakdown": {
                "rt_invalid": rt_invalid,
                "rt_missing": rt_missing,
            },
            "dead_status": dead_status,
            "candidate_fingerprint": candidate_fingerprint,
            "candidate_sample": sample,
            "candidate_sample_truncated": unavailable > len(sample),
            "dead_tokens": tokens,
            "dead_tokens_truncated": truncated,
            "scan_duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def _revalidate_dead_tokens(
        self,
        tokens: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        """Filter out accounts that recovered between the scan and the mutation.

        ``delete_accounts`` / ``update_accounts`` are unconditional, so a token
        refreshed by a concurrent worker after the scan would otherwise be
        disabled or deleted using a stale credential decision.
        """
        accounts = self._accounts()
        lookup = getattr(accounts, "get_account_by_token_identity", None)
        if not callable(lookup):
            return list(tokens), {
                "supported": False,
                "checked": 0,
                "recovered": 0,
                "missing": 0,
            }

        still_dead: list[str] = []
        recovered = 0
        missing = 0
        for token in tokens:
            account = lookup(token)
            if not isinstance(account, dict):
                missing += 1
                continue
            if self._projection(account).status != "unavailable":
                recovered += 1
                continue
            current = str(account.get("access_token") or token).strip()
            if current:
                still_dead.append(current)
        return still_dead, {
            "supported": True,
            "checked": len(tokens),
            "recovered": recovered,
            "missing": missing,
        }

    # ------------------------------------------------------------------ run

    def run_once(
        self,
        *,
        mode_override: str | None = None,
        dry_run: bool = False,
        confirm: bool | None = None,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        with self._run_lock:
            config = self.get_config()
            mode = (mode_override or config["mode"]).strip().lower()
            if mode not in {"off", "report", "disable", "purge"}:
                mode = "report"
            started = time.perf_counter()
            max_per_run = int(config["max_per_run"])
            destructive = mode in {"disable", "purge"}
            # confirm=False is an explicit preview even when dry_run was omitted;
            # confirm=None keeps the 1.0.9 compatibility path (see module docstring).
            preview_only = dry_run or confirm is False
            may_execute = destructive and not preview_only
            report: dict[str, Any] = {
                "started_at": _now_text(),
                "mode": mode,
                "effective_mode": mode,
                "dry_run": dry_run,
                "confirm": confirm,
                "executed": False,
                "warnings": [],
            }

            if mode == "off":
                report.update(
                    {
                        "skipped": "mode_off",
                        "dead_accounts": 0,
                        "scanned_accounts": 0,
                        "finished_at": _now_text(),
                        "duration_seconds": round(time.perf_counter() - started, 2),
                    }
                )
                return report

            scan = self._scan(
                collect_tokens=may_execute,
                token_limit=max_per_run + 1 if may_execute else None,
            )
            report.update(
                {
                    "scanned_accounts": scan["scanned_accounts"],
                    "scan_pages": scan["scan_pages"],
                    "credential_counts": scan["credential_counts"],
                    "dead_accounts": scan["dead_accounts"],
                    "dead_breakdown": scan["dead_breakdown"],
                    "dead_status": scan["dead_status"],
                    "candidate_fingerprint": scan["candidate_fingerprint"],
                    "candidate_sample": scan["candidate_sample"],
                    "candidate_sample_truncated": scan["candidate_sample_truncated"],
                    "max_per_run": max_per_run,
                    "scan_duration_ms": scan["scan_duration_ms"],
                }
            )
            warnings: list[str] = report["warnings"]

            # A destructive run is executed only when it was explicitly confirmed
            # (or through the legacy confirm=None path) and the scan still fits
            # the per-run guard.
            if may_execute:
                if scan["dead_accounts"] > max_per_run:
                    may_execute = False
                    report["effective_mode"] = "report"
                    report["over_limit"] = True
                    report["suggested_chunks"] = (
                        scan["dead_accounts"] + max_per_run - 1
                    ) // max_per_run
                    warnings.append(
                        f"死号 {scan['dead_accounts']} 超过单次上限 {max_per_run}，"
                        "已降级为只报告；请提高上限或分批执行"
                    )
                elif (
                    expected_fingerprint
                    and expected_fingerprint != scan["candidate_fingerprint"]
                ):
                    may_execute = False
                    preview_only = True
                    report["effective_mode"] = "report"
                    report["confirmation_stale"] = True
                    warnings.append(
                        "候选清单已变化（指纹不匹配），本次未执行；请重新预检后再确认"
                    )
                elif confirm is None:
                    warnings.append(
                        "confirm 未提供，按 1.0.9 兼容行为直接执行；"
                        "新调用方应使用 preview_once() 后再 confirm=True"
                    )

            if destructive and not may_execute:
                preview_only = True
            report["confirmation"] = {
                "required": destructive,
                "received": confirm,
                "confirmed": confirm is True,
                "preview_only": preview_only,
                "legacy_implicit": bool(destructive and confirm is None and not dry_run),
            }

            if may_execute:
                tokens = list(scan["dead_tokens"])
                report["destructive_candidates"] = len(tokens)
                report["would_touch"] = 0
                if tokens:
                    still_dead, revalidation = self._revalidate_dead_tokens(tokens)
                    report["revalidation"] = revalidation
                    report["revalidated_out"] = len(tokens) - len(still_dead)
                    report["would_touch"] = len(still_dead)
                    if still_dead:
                        if mode == "purge":
                            result = self._accounts().delete_accounts(
                                still_dead,
                                return_items=False,
                            )
                            report["removed"] = int(result.get("removed") or 0)
                            report["remove_missing"] = len(
                                result.get("missing_tokens") or []
                            )
                        else:
                            result = self._accounts().update_accounts(
                                still_dead,
                                {"status": "禁用"},
                                quiet=True,
                            )
                            report["disabled"] = len(result.get("updated_ids") or [])
                            report["already_disabled"] = int(
                                scan["dead_status"].get("禁用") or 0
                            )
                report["executed"] = True
            elif destructive:
                # Preview or a blocked destructive run: never mutate accounts.
                report["would_touch"] = scan["dead_accounts"]
                report["requires_confirmation"] = True

            retention = int(config["account_log_retention_hours"])
            if preview_only:
                try:
                    preview = self._logs().preview_cleanup_old(
                        retention,
                        record_type="account",
                    )
                    report["account_logs_pending"] = int(preview.get("removed") or 0)
                    report["account_logs_pending_bytes"] = int(
                        preview.get("removed_size_bytes") or 0
                    )
                except Exception as exc:  # noqa: BLE001
                    report["account_logs_error"] = f"{type(exc).__name__}: {exc}"
            else:
                try:
                    cleanup = self._logs().cleanup_old(
                        retention,
                        record_type="account",
                    )
                    report["account_logs_removed"] = int(cleanup.get("removed") or 0)
                    report["account_logs_removed_bytes"] = int(
                        cleanup.get("removed_size_bytes") or 0
                    )
                except Exception as exc:  # noqa: BLE001 - 清理失败不影响治理结果
                    report["account_logs_error"] = f"{type(exc).__name__}: {exc}"

            report["finished_at"] = _now_text()
            report["duration_seconds"] = round(time.perf_counter() - started, 2)

            if preview_only:
                if destructive:
                    self._state["last_preview"] = {
                        "mode": mode,
                        "dead_accounts": scan["dead_accounts"],
                        "fingerprint": scan["candidate_fingerprint"],
                        "max_per_run": max_per_run,
                        "at": time.time(),
                    }
                    self._save(self._state_path, self._state)
            else:
                self._state["last_run_epoch"] = time.time()
                self._state["last_report"] = report
                self._state["last_preview"] = {}
                self._save(self._state_path, self._state)
            return report

    def preview_once(self, *, mode_override: str | None = None) -> dict[str, Any]:
        """Read-only two-phase preview; safe to call with the configured mode."""
        return self.run_once(mode_override=mode_override, dry_run=True, confirm=False)

    def apply_once(
        self,
        *,
        mode_override: str | None = None,
        expected_fingerprint: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute a previously previewed plan, guarded by its fingerprint."""
        if not str(expected_fingerprint or "").strip():
            raise ValueError("expected_fingerprint is required")
        return self.run_once(
            mode_override=mode_override,
            dry_run=dry_run,
            confirm=True,
            expected_fingerprint=str(expected_fingerprint).strip(),
        )

    # ----------------------------------------------------------------- loop

    def _due(self, config: dict[str, Any]) -> bool:
        last = float(self._state.get("last_run_epoch") or 0.0)
        if not last:
            return True
        return (time.time() - last) >= float(config["run_every_hours"]) * 3600

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            config = self.get_config()
            if config["enabled"] and config["mode"] != "off" and self._due(config):
                try:
                    # Enabling a destructive mode in configuration is the
                    # standing confirmation for background runs.
                    self.run_once(confirm=True)
                except Exception:  # noqa: BLE001 - 后台循环不能退出
                    pass
            self._stop_event.wait(600)

    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="account-governance", daemon=True)
        self._thread.start()

    def stop_loop(self) -> None:
        self._stop_event.set()


account_governance_service = AccountGovernanceService()
