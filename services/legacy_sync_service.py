"""旧项目持久化目录同步（账号 / 功能设置 / 账号组 / 日志），可选自动测活。

场景：旧项目仍在别的机器或别的容器里跑，持久化目录通过共享盘/只读挂载可见。
本服务按固定间隔读取旧库，把**新增账号**、**功能设置**、**账号组**与**增量日志**
同步到当前实例，并可把新增账号自动送进测活队列。

设计原则：
* 旧库只读打开，绝不修改；
* 账号只增不改不删（已存在的 token 直接跳过），不会覆盖当前实例的状态；
* 设置默认只补缺（``fill``），可切换为覆盖（``overwrite``），内部键永远跳过；
* 全部状态写在 ``data/legacy_sync.json`` 与 ``data/.legacy-sync-state.json``，
  不依赖数据库表结构，因此对旧库版本没有要求。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

from services.config import DATA_DIR
from utils.diagnostics import diagnostic_excerpt
from utils.log import logger

CONFIG_FILE_NAME = "legacy_sync.json"
STATE_FILE_NAME = ".legacy-sync-state.json"
IMPORT_DIR_NAME = "imports"
IMPORT_STATE_FILE_NAME = ".legacy-imports-state.json"
MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"
IMPORT_READ_CHUNK = 1024 * 1024
IMPORT_ID_MAX_LENGTH = 64
IMPORT_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-._")
STALE_UPLOAD_SECONDS = 3600

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "paths": [],
    "interval_seconds": 300,
    "sync_accounts": True,
    "sync_settings": "fill",  # off | fill | overwrite
    "sync_account_groups": True,
    "sync_call_records": True,
    "max_call_records_per_run": 2000,
    "auto_check": True,
    "check_per_run": 100,
}

# 内部键不允许被旧项目的值覆盖。
# * 代理类键必须走 ProxyManagementService，直接写设置会抛错并中断整次导入；
# * account_groups 有独立的存储边界（与普通设置不能同事务写入）；
# * 认证/引导类键属于当前实例身份，旧库的值不能覆盖；
# * 其余为本实例的运行期内部状态。
PROTECTED_SETTING_KEYS = frozenset(
    {
        "adaptive_tier_seeded",
        "log_retention_hours",
        "account_groups",
        "proxy",
        "fallback_proxy",
        "proxy_profiles",
        "proxy_groups",
        "proxy_configuration",
        "backup_state",
        "basic",
        "auth_key",
        "auth-key",
    }
)

SettingsMode = str


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _size_limit_text(limit: int) -> str:
    if limit >= 1024 ** 3 and limit % 1024 ** 3 == 0:
        return f"{limit // 1024 ** 3}GB"
    if limit >= 1024 ** 2 and limit % 1024 ** 2 == 0:
        return f"{limit // 1024 ** 2}MB"
    if limit >= 1024 and limit % 1024 == 0:
        return f"{limit // 1024}KB"
    return f"{limit} 字节"


def _parse_account_payload(payload: object) -> dict[str, Any]:
    """Parse the JSON ``accounts.data`` column tolerantly."""
    if not payload:
        return {}
    try:
        data = json.loads(payload)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _account_identity(data: dict[str, Any]) -> tuple[str, str]:
    """Return ``(account_id, email)`` for a legacy account payload.

    ``management_id`` is the canonical id of the current schema; older dumps
    only carry ``account_id``/``id`` so both are accepted.  Preview and import
    must resolve the same field, otherwise a selected row never matches.
    """
    account_id = ""
    for key in ("management_id", "account_id", "id"):
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            account_id = text
            break
    email_value = data.get("email")
    email = str(email_value).strip() if email_value is not None else ""
    return account_id, email


def _accounts_select(columns: set[str]) -> str:
    return "access_token, data" if "data" in columns else "access_token, null"


def normalize_config(raw: object) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    paths: list[str] = []
    for item in data.get("paths") or []:
        text = str(item or "").strip()
        if text and text not in paths:
            paths.append(text)
    mode = str(data.get("sync_settings") or "fill").strip().lower()
    if mode not in {"off", "fill", "overwrite"}:
        mode = "fill"
    return {
        "enabled": bool(data.get("enabled", False)),
        "paths": paths[:20],
        "interval_seconds": _clamp_int(data.get("interval_seconds"), 300, 60, 86_400),
        "sync_accounts": bool(data.get("sync_accounts", True)),
        "sync_settings": mode,
        "sync_account_groups": bool(data.get("sync_account_groups", True)),
        "sync_call_records": bool(data.get("sync_call_records", True)),
        "max_call_records_per_run": _clamp_int(
            data.get("max_call_records_per_run"), 2000, 0, 50_000
        ),
        "auto_check": bool(data.get("auto_check", True)),
        "check_per_run": _clamp_int(data.get("check_per_run"), 100, 0, 5_000),
    }


@dataclass(slots=True)
class PathProbe:
    path: str
    exists: bool = False
    accounts: int = 0
    fresh_accounts: int = 0
    settings_keys: int = 0
    account_groups: int = 0
    call_records: int = 0
    pending_call_records: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "accounts": self.accounts,
            "fresh_accounts": self.fresh_accounts,
            "settings_keys": self.settings_keys,
            "account_groups": self.account_groups,
            "call_records": self.call_records,
            "pending_call_records": self.pending_call_records,
            "error": self.error,
        }


@dataclass(slots=True)
class SyncResult:
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    trigger: str = ""
    added_accounts: int = 0
    skipped_accounts: int = 0
    repaired_accounts: int = 0
    updated_settings: list[str] = field(default_factory=list)
    synced_groups: int = 0
    imported_call_records: int = 0
    queued_checks: int = 0
    paths: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 2),
            "trigger": self.trigger,
            "added_accounts": self.added_accounts,
            "skipped_accounts": self.skipped_accounts,
            "repaired_accounts": self.repaired_accounts,
            "updated_settings": self.updated_settings,
            "synced_groups": self.synced_groups,
            "imported_call_records": self.imported_call_records,
            "queued_checks": self.queued_checks,
            "paths": self.paths,
            "errors": self.errors,
        }


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _resolve_legacy_db(root: Path) -> Path | None:
    if root.is_file() and root.suffix == ".db":
        return root
    candidates = [
        root / "chatgpt2api.db",
        root / "data" / "chatgpt2api.db",
        root / "app.db",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _open_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """Open a database strictly read-only.

    The filename is percent-encoded so data directories containing spaces,
    ``#``/``?`` or non-ASCII characters still open correctly.  Uploaded import
    snapshots are opened with ``immutable=1``: they never change after upload,
    so SQLite must not create ``-shm``/``-journal`` side files next to them.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    target = quote(resolved.as_posix(), safe="/:")
    suffix = "&immutable=1" if immutable else ""
    return sqlite3.connect(
        f"file:{target}?mode=ro{suffix}",
        uri=True,
        timeout=15,
    )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("select name from sqlite_master where type='table'")
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f'pragma table_info("{table}")').fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows if len(row) > 1}


def _read_accounts(conn: sqlite3.Connection) -> list[str]:
    tokens: list[str] = []
    for (token,) in conn.execute("select access_token from accounts"):
        text = str(token or "").strip()
        if text:
            tokens.append(text)
    return tokens


def _read_account_payloads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read full account rows, preserving refresh tokens and profile fields.

    Older versions only copied ``access_token``; the imported pool then had no
    refresh_token and died when the AT expired.
    """
    columns = _table_columns(conn, "accounts")
    select = "access_token, data" if "data" in columns else "access_token, null"
    payloads: list[dict[str, Any]] = []
    for token, raw in conn.execute(f"select {select} from accounts"):
        access_token = str(token or "").strip()
        if not access_token:
            continue
        payload: dict[str, Any] = {}
        if raw:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict):
                payload.update(data)
        payload["access_token"] = access_token
        payload.setdefault("source_type", "web")
        payloads.append(payload)
    return payloads


def _read_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = conn.execute("select data from system_settings limit 1").fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_account_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    tables = _table_names(conn)
    if "account_group_configuration" not in tables:
        return groups
    try:
        rows = conn.execute("select data from account_group_configuration").fetchall()
    except sqlite3.Error:
        return groups
    for (payload,) in rows:
        try:
            item = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict) and str(item.get("id") or "").strip():
            groups.append(item)
    return groups


def _read_call_records(
    conn: sqlite3.Connection,
    *,
    after_sequence: int,
    limit: int,
) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    tables = _table_names(conn)
    if "call_records" not in tables or limit <= 0:
        return [], 0
    try:
        rows = conn.execute(
            "select sequence, payload from call_records where sequence > ? "
            "order by sequence limit ?",
            (int(after_sequence), int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return [], 0
    items: list[tuple[int, dict[str, Any]]] = []
    last_sequence = int(after_sequence)
    for sequence, payload in rows:
        try:
            item = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            items.append((int(sequence), item))
            last_sequence = max(last_sequence, int(sequence))
    return items, last_sequence


def _read_accounts_revision(conn: sqlite3.Connection) -> str:
    tables = _table_names(conn)
    if "storage_revisions" not in tables:
        return ""
    try:
        row = conn.execute(
            "select version from storage_revisions where collection = 'accounts'"
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row else ""


class LegacySyncService:
    """Incremental sync of a legacy persistence directory."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        account_service: Any | None = None,
        config_store: Any | None = None,
        call_record_service: Any | None = None,
        check_handler: Callable[[list[str]], Any] | None = None,
    ) -> None:
        root = Path(data_dir) if data_dir else DATA_DIR
        self._config_path = root / CONFIG_FILE_NAME
        self._state_path = root / STATE_FILE_NAME
        self._data_dir = root
        self._account_service = account_service
        self._config_store = config_store
        self._call_record_service = call_record_service
        self._check_handler = check_handler
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        # Serialises upload/delete bookkeeping (short critical sections).
        self._import_lock = threading.RLock()
        # Serialises whole import runs so one import cannot advance a watermark
        # that a concurrent import of the same file already consumed.
        self._import_run_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = self._load_json(self._state_path, {})
        self._last_result: dict[str, Any] = self._state.get("last_result") or {}

    # ---------------------------------------------------------------- wiring

    def _accounts(self) -> Any:
        if self._account_service is None:
            from services.account_service import account_service

            self._account_service = account_service
        return self._account_service

    def _config(self) -> Any:
        if self._config_store is None:
            from services.config import config

            self._config_store = config
        return self._config_store

    def _records(self) -> Any:
        if self._call_record_service is None:
            # ``call_record_service`` is not a module-level singleton; the
            # application's single writer is ``log_service`` (an instance of
            # CallRecordService). A fresh instance here opened a second
            # repository and, on 1.1.7.x, raised ImportError so log sync did
            # nothing at all.
            from services.log_service import log_service

            self._call_record_service = log_service
        return self._call_record_service

    def set_check_handler(self, handler: Callable[[list[str]], Any] | None) -> None:
        self._check_handler = handler

    # ---------------------------------------------------------------- config

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        return data if isinstance(data, type(default)) else default

    def _save_json(self, path: Path, payload: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def get_config(self) -> dict[str, Any]:
        stored = self._load_json(self._config_path, {})
        merged = {**DEFAULT_CONFIG, **stored}
        return normalize_config(merged)

    def save_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.get_config()
            merged = {
                key: patch.get(key, current.get(key))
                for key in DEFAULT_CONFIG
            }
            config = normalize_config(merged)
            self._save_json(self._config_path, config)
        return config

    # ---------------------------------------------------------------- status

    def state(self) -> dict[str, Any]:
        config = self.get_config()
        return {
            "config": config,
            "last_result": self._last_result,
            "watermarks": self._state.get("watermarks") or {},
            "running": self._run_lock.locked(),
            "next_run_in_seconds": self._next_run_in_seconds(config),
        }

    def _next_run_in_seconds(self, config: dict[str, Any]) -> int:
        last_ts = float(self._state.get("last_run_epoch") or 0.0)
        if not last_ts:
            return 0
        due = last_ts + float(config["interval_seconds"])
        return max(0, int(due - time.time()))

    def probe(self, *, probe_accounts: bool = True) -> list[dict[str, Any]]:
        """Inspect each configured path without writing anything."""
        config = self.get_config()
        target_tokens: set[str] | None = None
        if probe_accounts:
            try:
                target_tokens = set(self._accounts().list_tokens())
            except Exception:
                target_tokens = set()
        probes: list[PathProbe] = []
        for raw_path in config["paths"]:
            probe = PathProbe(path=raw_path)
            root = Path(raw_path)
            db = _resolve_legacy_db(root)
            if db is None:
                probe.error = "未找到 chatgpt2api.db"
                probes.append(probe)
                continue
            probe.exists = True
            try:
                conn = _open_readonly(db)
            except sqlite3.Error as exc:
                probe.error = f"无法打开旧库: {exc}"
                probes.append(probe)
                continue
            try:
                tables = _table_names(conn)
                if "accounts" in tables:
                    tokens = _read_accounts(conn)
                    probe.accounts = len(tokens)
                    if target_tokens is not None:
                        probe.fresh_accounts = sum(
                            1 for token in tokens if token not in target_tokens
                        )
                if "system_settings" in tables:
                    probe.settings_keys = len(_read_settings(conn))
                if "account_group_configuration" in tables:
                    probe.account_groups = len(_read_account_groups(conn))
                if "call_records" in tables:
                    probe.call_records = int(
                        conn.execute("select count(*) from call_records").fetchone()[0]
                    )
                    watermark = self._watermark(raw_path)
                    probe.pending_call_records = int(
                        conn.execute(
                            "select count(*) from call_records where sequence > ?",
                            (int(watermark.get("call_sequence") or 0),),
                        ).fetchone()[0]
                    )
            except sqlite3.Error as exc:
                probe.error = str(exc)
            finally:
                conn.close()
            probes.append(probe)
        return [probe.to_dict() for probe in probes]

    # --------------------------------------------------------------- syncing

    def _watermark(self, path: str) -> dict[str, Any]:
        watermarks = self._state.get("watermarks") or {}
        item = watermarks.get(path)
        return item if isinstance(item, dict) else {}

    def _set_watermark(self, path: str, **values: Any) -> None:
        watermarks = dict(self._state.get("watermarks") or {})
        item = dict(watermarks.get(path) or {})
        item.update(values)
        watermarks[path] = item
        self._state["watermarks"] = watermarks
        self._save_json(self._state_path, self._state)

    def sync(self, *, trigger: str = "manual", dry_run: bool = False) -> dict[str, Any]:
        with self._run_lock:
            result = SyncResult(started_at=_now_text(), trigger=trigger)
            started = time.perf_counter()
            config = self.get_config()
            probes = self.probe(probe_accounts=config["sync_accounts"])
            new_tokens: list[str] = []
            for probe in probes:
                path = str(probe.get("path") or "")
                if not probe.get("exists"):
                    result.errors.append(f"{path}: {probe.get('error') or '不可用'}")
                    continue
                try:
                    summary = self._sync_path(
                        path,
                        config=config,
                        dry_run=dry_run,
                        new_tokens=new_tokens,
                        result=result,
                    )
                    progress = dict(probe)
                    progress.update(summary)
                    result.paths.append(progress)
                except Exception as exc:  # noqa: BLE001 - 单路径失败不影响其它路径
                    result.errors.append(f"{path}: {type(exc).__name__}: {exc}")
            result.duration_seconds = time.perf_counter() - started
            result.finished_at = _now_text()
            if not dry_run:
                result.queued_checks = self._queue_checks(new_tokens, config)
                self._state["last_run_epoch"] = time.time()
                self._state["last_run"] = result.finished_at
                self._state["last_result"] = result.to_dict()
                self._save_json(self._state_path, self._state)
                self._last_result = result.to_dict()
            return result.to_dict()

    def _sync_path(
        self,
        raw_path: str,
        *,
        config: dict[str, Any],
        dry_run: bool,
        new_tokens: list[str],
        result: SyncResult,
    ) -> dict[str, Any]:
        root = Path(raw_path)
        db = _resolve_legacy_db(root)
        if db is None:
            raise FileNotFoundError("未找到 chatgpt2api.db")
        summary: dict[str, Any] = {}
        conn = _open_readonly(db)
        try:
            tables = _table_names(conn)
            if config["sync_accounts"] and "accounts" in tables:
                revision = _read_accounts_revision(conn)
                watermark = self._watermark(raw_path)
                if revision and revision == watermark.get("accounts_revision"):
                    summary["accounts_skipped_reason"] = "版本未变化"
                else:
                    payloads = _read_account_payloads(conn)
                    tokens = [str(item.get("access_token") or "") for item in payloads]
                    accounts_api = self._accounts()
                    list_accounts = getattr(accounts_api, "list_accounts", None)
                    if callable(list_accounts):
                        target_accounts = {
                            str(item.get("access_token") or ""): item
                            for item in list_accounts()
                        }
                    else:
                        target_accounts = {
                            str(token): {} for token in accounts_api.list_tokens()
                        }
                    fresh_payloads = [
                        item for item in payloads
                        if str(item.get("access_token") or "") not in target_accounts
                    ]
                    fresh = [str(item.get("access_token") or "") for item in fresh_payloads]
                    if dry_run:
                        summary["dry_run_fresh_accounts"] = len(fresh)
                    else:
                        if fresh_payloads:
                            add_payloads = getattr(accounts_api, "add_account_payloads", None)
                            if callable(add_payloads):
                                added = add_payloads(fresh_payloads, return_items=False)
                            else:
                                added = accounts_api.add_accounts(fresh, return_items=False)
                            added_count = int(added.get("added") or 0)
                            result.added_accounts += added_count
                            result.skipped_accounts += max(0, len(fresh) - added_count)
                            new_tokens.extend(fresh[: max(0, config["check_per_run"])])
                            summary["added_accounts"] = added_count
                        # Repair rows imported by older versions that only copied
                        # access_token: fill the refresh token / identity fields
                        # without overwriting newer credentials.
                        backfill = getattr(accounts_api, "backfill_account_credentials", None)
                        if callable(backfill):
                            repairs = [
                                item for item in payloads
                                if str(item.get("access_token") or "") in target_accounts
                                and str(item.get("refresh_token") or "").strip()
                                and not str(
                                    (target_accounts.get(str(item.get("access_token") or "")) or {})
                                    .get("refresh_token") or ""
                                ).strip()
                            ]
                            if repairs:
                                repaired = backfill(repairs)
                                result.repaired_accounts += int(repaired or 0)
                                summary["repaired_accounts"] = int(repaired or 0)
                        self._set_watermark(
                            raw_path,
                            accounts_revision=revision or "",
                            accounts_synced_at=_now_text(),
                        )
            if config["sync_settings"] != "off" and "system_settings" in tables:
                legacy_settings = _read_settings(conn)
                applied = self._merge_settings(legacy_settings, config["sync_settings"], dry_run)
                result.updated_settings.extend(applied)
                summary["updated_settings"] = len(applied)
            if config["sync_account_groups"] and "account_group_configuration" in tables:
                groups = _read_account_groups(conn)
                merged = self._merge_groups(groups, dry_run)
                result.synced_groups += merged
                summary["synced_groups"] = merged
            if config["sync_call_records"] and "call_records" in tables:
                limit = int(config["max_call_records_per_run"])
                watermark = self._watermark(raw_path)
                items, _last_sequence = _read_call_records(
                    conn,
                    after_sequence=int(watermark.get("call_sequence") or 0),
                    limit=limit,
                )
                if dry_run:
                    summary["dry_run_call_records"] = len(items)
                elif items:
                    imported, imported_sequence = self._append_records(items)
                    result.imported_call_records += imported
                    # Only advance across the contiguous imported prefix; a
                    # failed row must be retried instead of silently dropped.
                    if imported_sequence is not None:
                        self._set_watermark(raw_path, call_sequence=imported_sequence)
                    summary["imported_call_records"] = imported
        finally:
            conn.close()
        return summary

    def _merge_settings(self, legacy: dict[str, Any], mode: SettingsMode, dry_run: bool) -> list[str]:
        if not legacy:
            return []
        store = self._config()
        reload_if_changed = getattr(store, "reload_if_changed", None)
        if callable(reload_if_changed):
            try:
                reload_if_changed()
            except Exception:  # noqa: BLE001 - 刷新失败时退回当前快照
                pass
        try:
            current = dict(store.data or {})
        except Exception:
            return []
        applied: list[str] = []
        updates: dict[str, Any] = {}
        for raw_key, value in legacy.items():
            key = str(raw_key)
            if not key or key in PROTECTED_SETTING_KEYS or key.startswith("_"):
                continue
            if value is None:
                continue
            if mode == "fill" and key in current:
                continue
            if current.get(key) == value:
                continue
            updates[key] = value
            applied.append(key)
        if updates and not dry_run:
            applied = self._apply_settings_updates(store, updates)
        return applied

    def _apply_settings_updates(self, store: Any, updates: dict[str, Any]) -> list[str]:
        """Write settings, degrading to per-key writes when one value is bad.

        ``ConfigStore.update`` rejects proxy keys and cross-boundary writes and
        validates nested payloads (image storage, backup, ...).  A single bad
        legacy value must not abort the whole import after accounts were added.
        """
        try:
            store.update(updates)
            return list(updates)
        except Exception:  # noqa: BLE001 - 批写失败后逐键重试
            pass
        applied: list[str] = []
        for key, value in updates.items():
            try:
                store.update({key: value})
            except Exception:  # noqa: BLE001 - 单个键失败不影响其它键
                continue
            applied.append(key)
        return applied

    def _merge_groups(self, legacy_groups: list[dict[str, Any]], dry_run: bool) -> int:
        if not legacy_groups:
            return 0
        try:
            current = list(self._config().data.get("account_groups") or [])
        except Exception:
            current = []
        known = {
            str(group.get("id") or "").strip()
            for group in current
            if isinstance(group, dict)
        }
        merged = list(current)
        added = 0
        for group in legacy_groups:
            group_id = str(group.get("id") or "").strip()
            if not group_id or group_id in known:
                continue
            merged.append(group)
            known.add(group_id)
            added += 1
        if added and not dry_run:
            self._config().update({"account_groups": merged})
        return added

    def _append_records(
        self, items: Iterable[tuple[int, dict[str, Any]]]
    ) -> tuple[int, int | None]:
        """Append records until the first failure.

        Returns ``(imported, last_imported_sequence)``.  Callers persist the
        watermark only for the contiguous imported prefix, so a retry neither
        loses failed rows nor duplicates already written ones.
        """
        imported = 0
        last_sequence: int | None = None
        service = self._records()
        append_items = getattr(service, "append_items", None)
        chunk: list[tuple[int, dict[str, Any]]] = []

        def flush_chunk() -> bool:
            nonlocal imported, last_sequence
            if not chunk:
                return True
            if callable(append_items):
                try:
                    count = append_items([item for _seq, item in chunk])
                    imported += int(count or 0)
                    last_sequence = int(chunk[-1][0])
                    chunk.clear()
                    return True
                except Exception as exc:  # noqa: BLE001
                    logger.warning({
                        "event": "legacy_sync_record_batch_failed",
                        "size": len(chunk),
                        "error": diagnostic_excerpt(exc, 300),
                    })
            for sequence, item in list(chunk):
                try:
                    service.append_item(item)
                except Exception as exc:  # noqa: BLE001
                    message = str(exc).lower()
                    if "unique" in message or "duplicate" in message or "constraint" in message:
                        imported += 1
                        last_sequence = int(sequence)
                        continue
                    logger.warning({
                        "event": "legacy_sync_record_failed",
                        "sequence": sequence,
                        "error": diagnostic_excerpt(exc, 300),
                    })
                    chunk.clear()
                    return False
                imported += 1
                last_sequence = int(sequence)
            chunk.clear()
            return True

        for sequence, item in items:
            chunk.append((sequence, item))
            if len(chunk) >= 500 and not flush_chunk():
                break
        if chunk:
            flush_chunk()
        return imported, last_sequence

    def _queue_checks(self, tokens: list[str], config: dict[str, Any]) -> int:
        if not config["auto_check"] or not tokens or self._check_handler is None:
            return 0
        limit = int(config["check_per_run"])
        selected = tokens[:limit]
        if not selected:
            return 0
        try:
            self._check_handler(selected)
            return len(selected)
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------- uploaded files

    def _imports_dir(self) -> Path:
        return self._data_dir / IMPORT_DIR_NAME

    def _import_state_path(self) -> Path:
        return self._data_dir / IMPORT_STATE_FILE_NAME

    def _import_state(self) -> dict[str, Any]:
        return self._load_json(self._import_state_path(), {})

    def _save_import_state(self, state: dict[str, Any]) -> None:
        self._save_json(self._import_state_path(), state)

    def _update_import_state(self, import_id: object, patch: dict[str, Any]) -> None:
        """Merge ``patch`` into the per-import bookkeeping entry."""
        key = str(import_id or "").strip().lower()
        if not key:
            return
        with self._import_lock:
            state = self._import_state()
            entry = state.get(key)
            merged = dict(entry) if isinstance(entry, dict) else {}
            merged.update(patch)
            state[key] = merged
            self._save_import_state(state)

    def _import_watermark(self, import_id: object) -> int:
        key = str(import_id or "").strip().lower()
        entry = self._import_state().get(key) or {}
        if not isinstance(entry, dict):
            return 0
        return _clamp_int(entry.get("call_sequence"), 0, 0, 2 ** 63 - 1)

    @staticmethod
    def _valid_import_id(import_id: object) -> str:
        text = str(import_id or "").strip()
        if not text or len(text) > IMPORT_ID_MAX_LENGTH:
            return ""
        normalized = text.lower()
        if normalized.startswith(".") or ".." in normalized:
            return ""
        if any(char not in IMPORT_ID_CHARS for char in normalized):
            return ""
        return text

    def _import_path(self, import_id: object) -> Path | None:
        valid = self._valid_import_id(import_id)
        if not valid:
            return None
        directory = self._imports_dir()
        path = directory / f"{valid}.db"
        # Defense in depth: the validated id cannot contain separators, so the
        # candidate must still resolve inside data/imports (catches symlinks).
        try:
            if path.resolve().parent != directory.resolve():
                return None
        except OSError:
            return None
        return path if path.is_file() else None

    def _open_import_db(self, path: Path) -> sqlite3.Connection:
        if not path.is_file():
            raise ValueError("导入文件不存在")
        try:
            return _open_readonly(path, immutable=True)
        except sqlite3.Error as exc:
            raise ValueError(f"导入文件无法打开: {exc}") from exc

    def _probe_import(self, path: Path) -> dict[str, Any]:
        """Validate an uploaded snapshot and count its importable content."""
        summary: dict[str, Any] = {
            "exists": True,
            "accounts": 0,
            "settings_keys": 0,
            "account_groups": 0,
            "call_records": 0,
        }
        try:
            conn = _open_readonly(path, immutable=True)
        except (sqlite3.Error, OSError) as exc:
            return {"exists": False, "error": f"无法打开数据库: {exc}"}
        try:
            try:
                tables = _table_names(conn)
            except sqlite3.Error as exc:
                return {"exists": False, "error": f"不是有效的 SQLite 数据库: {exc}"}
            if "accounts" not in tables:
                return {"exists": False, "error": "不是有效的数据文件：缺少 accounts 表"}
            columns = _table_columns(conn, "accounts")
            if "access_token" not in columns:
                return {
                    "exists": False,
                    "error": "不是有效的数据文件：accounts 表缺少 access_token 字段",
                }
            try:
                summary["accounts"] = int(
                    conn.execute("select count(*) from accounts").fetchone()[0]
                )
            except sqlite3.Error as exc:
                return {"exists": False, "error": f"accounts 表无法读取: {exc}"}
            # Optional collections must not invalidate an otherwise usable dump.
            if "system_settings" in tables:
                summary["settings_keys"] = len(_read_settings(conn))
            if "account_group_configuration" in tables:
                summary["account_groups"] = len(_read_account_groups(conn))
            if "call_records" in tables:
                try:
                    summary["call_records"] = int(
                        conn.execute("select count(*) from call_records").fetchone()[0]
                    )
                except sqlite3.Error:
                    summary["call_records"] = 0
        finally:
            conn.close()
        return summary

    @staticmethod
    def _clean_filename(filename: object) -> str:
        text = str(filename or "").replace("\x00", "").strip()
        text = text.replace("\\", "/").rsplit("/", 1)[-1].strip()
        return text[:255] or "chatgpt2api.db"

    @staticmethod
    def _has_sqlite_header(path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return handle.read(16) == SQLITE_HEADER
        except OSError:
            return False

    def _cleanup_stale_uploads(self, directory: Path) -> None:
        """Remove abandoned ``.<uuid>.db.part`` files from crashed uploads."""
        now = time.time()
        try:
            candidates = list(directory.glob(".*.db.part"))
        except OSError:
            return
        for item in candidates:
            try:
                if not item.is_file():
                    continue
                if now - item.stat().st_mtime < STALE_UPLOAD_SECONDS:
                    continue
                item.unlink(missing_ok=True)
            except OSError:
                continue

    def save_upload(
        self, filename: object, stream: Any, *, max_bytes: int = MAX_IMPORT_BYTES
    ) -> dict[str, Any]:
        """Persist an uploaded SQLite database under ``data/imports``.

        The body is streamed into ``.<uuid>.db.part``, validated (SQLite magic
        header, size cap, ``accounts`` schema) and only then atomically renamed
        to ``<uuid>.db``.  Every failure path deletes the partial file, so a
        rejected upload never shows up in the imports list.
        """
        try:
            limit = int(max_bytes)
        except (TypeError, ValueError):
            limit = MAX_IMPORT_BYTES
        limit = max(0, limit)
        directory = self._imports_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"无法创建导入目录: {exc}") from exc
        self._cleanup_stale_uploads(directory)
        import_id = str(uuid.uuid4())
        final_path = directory / f"{import_id}.db"
        temp_path = directory / f".{import_id}.db.part"
        uploaded_at = time.strftime("%Y-%m-%d %H:%M:%S")
        total = 0
        first_chunk = True
        try:
            with temp_path.open("wb") as handle:
                while True:
                    chunk = stream.read(IMPORT_READ_CHUNK)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    data_chunk = bytes(chunk)
                    if first_chunk:
                        first_chunk = False
                        if not data_chunk.startswith(SQLITE_HEADER):
                            raise ValueError("不是 SQLite 数据库文件")
                    total += len(data_chunk)
                    if total > limit:
                        raise ValueError(
                            f"数据库文件超过 {_size_limit_text(limit)} 上限"
                        )
                    handle.write(data_chunk)
            if total <= 0:
                raise ValueError("上传文件为空")
            if not self._has_sqlite_header(temp_path):
                raise ValueError("不是 SQLite 数据库文件")
            summary = self._probe_import(temp_path)
            if not summary.get("exists"):
                raise ValueError(summary.get("error") or "无效的数据文件")
            try:
                os.replace(temp_path, final_path)
            except OSError as exc:
                raise ValueError(f"保存上传文件失败: {exc}") from exc
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        summary.update(
            {
                "import_id": import_id,
                "filename": self._clean_filename(filename),
                "size_bytes": total,
                "uploaded_at": uploaded_at,
            }
        )
        self._record_import_meta(import_id, final_path, summary)
        return summary

    def _record_import_meta(
        self, import_id: str, path: Path, summary: dict[str, Any]
    ) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        meta = {
            "size_bytes": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "summary": {
                "filename": self._clean_filename(summary.get("filename")),
                "uploaded_at": str(summary.get("uploaded_at") or _now_text()),
                "accounts": int(summary.get("accounts") or 0),
                "settings_keys": int(summary.get("settings_keys") or 0),
                "account_groups": int(summary.get("account_groups") or 0),
                "call_records": int(summary.get("call_records") or 0),
            },
        }
        try:
            self._update_import_state(import_id, {"meta": meta})
        except Exception:  # noqa: BLE001 - 元数据缓存失败不影响上传结果
            pass

    def list_imports(self) -> list[dict[str, Any]]:
        directory = self._imports_dir()
        if not directory.is_dir():
            return []
        state = self._import_state()
        items: list[dict[str, Any]] = []
        for path in directory.glob("*.db"):
            import_id = path.stem
            if self._valid_import_id(import_id) != import_id:
                continue
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            entry = state.get(import_id) or state.get(import_id.lower()) or {}
            meta = entry.get("meta") if isinstance(entry, dict) else None
            summary: dict[str, Any] | None = None
            if isinstance(meta, dict):
                try:
                    fresh_cache = (
                        int(meta.get("size_bytes") or -1) == int(stat.st_size)
                        and int(meta.get("mtime_ns") or -1) == int(stat.st_mtime_ns)
                    )
                except (TypeError, ValueError):
                    fresh_cache = False
                if fresh_cache and isinstance(meta.get("summary"), dict):
                    summary = dict(meta["summary"])
            if summary is None:
                probe = self._probe_import(path)
                summary = {
                    "filename": "chatgpt2api.db",
                    "uploaded_at": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
                    ),
                    "accounts": int(probe.get("accounts") or 0),
                    "settings_keys": int(probe.get("settings_keys") or 0),
                    "account_groups": int(probe.get("account_groups") or 0),
                    "call_records": int(probe.get("call_records") or 0),
                }
                try:
                    self._record_import_meta(import_id, path, summary)
                except Exception:  # noqa: BLE001
                    pass
            items.append(
                {
                    "import_id": import_id,
                    "filename": self._clean_filename(summary.get("filename")),
                    "size_bytes": int(stat.st_size),
                    "uploaded_at": str(summary.get("uploaded_at") or _now_text()),
                    "accounts": int(summary.get("accounts") or 0),
                    "settings_keys": int(summary.get("settings_keys") or 0),
                    "account_groups": int(summary.get("account_groups") or 0),
                    "call_records": int(summary.get("call_records") or 0),
                }
            )
        items.sort(key=lambda item: (item["uploaded_at"], item["import_id"]), reverse=True)
        return items

    def delete_import(self, import_id: object) -> bool:
        valid = self._valid_import_id(import_id)
        if not valid:
            return False
        with self._import_lock:
            path = self._import_path(valid)
            if path is None:
                return False
            try:
                path.unlink()
            except OSError:
                return False
            state = self._import_state()
            changed = False
            for key in {valid.lower(), valid}:
                if key and state.pop(key, None) is not None:
                    changed = True
            if changed:
                self._save_import_state(state)
        return True

    def delete_all_imports(self) -> int:
        removed = 0
        for item in self.list_imports():
            import_id = str(item.get("import_id") or "")
            if import_id and self.delete_import(import_id):
                removed += 1
        return removed

    # ------------------------------------------------------ account preview

    @staticmethod
    def _preview_item(
        token: object,
        payload: object,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parsed = data if isinstance(data, dict) else _parse_account_payload(payload)
        account_id, email = _account_identity(parsed)
        token_text = str(token or "")
        return {
            "account_id": account_id,
            "email": email,
            "status": str(parsed.get("status") or ""),
            "plan_type": str(parsed.get("plan_type") or ""),
            "type": str(parsed.get("type") or ""),
            "quota": parsed.get("quota"),
            "token_hint": f"{token_text[:8]}…{token_text[-4:]}" if token_text else "",
        }

    @staticmethod
    def _selection_values(value: object) -> set[str]:
        if isinstance(value, str):
            source: list[object] = [value]
        elif isinstance(value, (list, tuple, set, frozenset)):
            source = list(value)
        else:
            return set()
        values: set[str] = set()
        for item in source:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                values.add(text.lower())
        return values

    @staticmethod
    def _settings_key_filter(value: object) -> set[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            source: list[object] = [value]
        elif isinstance(value, (list, tuple, set, frozenset)):
            source = list(value)
        else:
            # A malformed selector must not silently mean "every setting".
            return set()
        keys: set[str] = set()
        for item in source:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                keys.add(text)
        return keys or None

    @staticmethod
    def _truthy(value: object, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "no", "off"}
        return bool(value)

    def preview_accounts(
        self,
        import_id: object,
        *,
        page: int = 1,
        page_size: int = 20,
        keyword: str = "",
    ) -> dict[str, Any]:
        valid_id = self._valid_import_id(import_id)
        path = self._import_path(valid_id)
        if path is None:
            raise ValueError("导入文件不存在")
        safe_page = _clamp_int(page, 1, 1, 1_000_000)
        safe_size = _clamp_int(page_size, 20, 1, 200)
        needle = str(keyword or "").strip().lower()
        items: list[dict[str, Any]] = []
        total = 0
        start = (safe_page - 1) * safe_size
        conn = self._open_import_db(path)
        try:
            try:
                tables = _table_names(conn)
            except sqlite3.Error as exc:
                raise ValueError(f"导入文件已损坏或不可读: {exc}") from exc
            if "accounts" not in tables:
                raise ValueError("导入文件缺少 accounts 表")
            columns = _table_columns(conn, "accounts")
            if "access_token" not in columns:
                raise ValueError("导入文件 accounts 表缺少 access_token 字段")
            select_columns = _accounts_select(columns)
            where = "where length(trim(coalesce(access_token, ''))) > 0"
            try:
                if not needle:
                    # Pagination must not stream the whole table: count in SQL
                    # and read only the requested window.
                    total = int(
                        conn.execute(
                            f"select count(*) from accounts {where}"
                        ).fetchone()[0]
                    )
                    rows = conn.execute(
                        f"select {select_columns} from accounts {where} "
                        "limit ? offset ?",
                        (safe_size, start),
                    ).fetchall()
                    for token, payload in rows:
                        items.append(self._preview_item(token, payload))
                else:
                    # JSON is only decoded for rows whose raw payload contains
                    # the needle; non-ASCII needles, escape-bearing needles and
                    # BLOB payloads fall back to a full decode.
                    prefilter = needle.isascii() and not any(
                        char in needle for char in '\\"'
                    )
                    for token, payload in conn.execute(
                        f"select {select_columns} from accounts {where}"
                    ):
                        if (
                            prefilter
                            and isinstance(payload, str)
                            and needle not in payload.lower()
                        ):
                            continue
                        data = _parse_account_payload(payload)
                        account_id, email = _account_identity(data)
                        if needle not in email.lower() and needle not in account_id.lower():
                            continue
                        if start <= total < start + safe_size:
                            items.append(self._preview_item(token, payload, data))
                        total += 1
            except sqlite3.Error as exc:
                raise ValueError(f"导入文件 accounts 表无法读取: {exc}") from exc
        finally:
            conn.close()
        return {
            "items": items,
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
            "import_id": valid_id,
        }

    # --------------------------------------------------------- import flow

    def _selection_tokens(
        self, conn: sqlite3.Connection, accounts_opt: dict[str, Any]
    ) -> list[str]:
        selection = str(accounts_opt.get("mode") or "all").strip().lower()
        if selection not in {"all", "selected"}:
            # Unknown modes never fall back to "all": a typo must not import
            # the entire legacy pool.
            selection = "selected"
        selected_ids = self._selection_values(accounts_opt.get("account_ids"))
        selected_emails = self._selection_values(accounts_opt.get("emails"))
        columns = _table_columns(conn, "accounts")
        select_columns = _accounts_select(columns)
        tokens: list[str] = []
        for token, payload in conn.execute(
            f"select {select_columns} from accounts"
        ):
            access_token = str(token or "").strip()
            if not access_token:
                continue
            if selection == "selected":
                data = _parse_account_payload(payload)
                account_id, email = _account_identity(data)
                matched = bool(account_id) and account_id.lower() in selected_ids
                if not matched:
                    # An empty legacy email must never match an empty selector.
                    matched = bool(email) and email.lower() in selected_emails
                if not matched:
                    continue
            tokens.append(access_token)
        # Legacy dumps may contain duplicate rows without a primary key.
        return list(dict.fromkeys(tokens))

    def import_selection(
        self,
        import_id: object,
        options: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        valid_id = self._valid_import_id(import_id)
        path = self._import_path(valid_id)
        if path is None:
            raise ValueError("导入文件不存在")
        options = options if isinstance(options, dict) else {}
        accounts_opt = options.get("accounts") if isinstance(options.get("accounts"), dict) else {}
        settings_opt = options.get("settings") if isinstance(options.get("settings"), dict) else {}
        result: dict[str, Any] = {
            "import_id": valid_id,
            "dry_run": dry_run,
            "accounts_added": 0,
            "accounts_skipped": 0,
            "settings_updated": [],
            "groups_added": 0,
            "call_records_imported": 0,
        }
        with self._import_run_lock:
            conn = self._open_import_db(path)
            try:
                try:
                    tables = _table_names(conn)
                except sqlite3.Error as exc:
                    raise ValueError(f"导入文件已损坏或不可读: {exc}") from exc
                if self._truthy(accounts_opt.get("enabled"), False) and "accounts" in tables:
                    tokens = self._selection_tokens(conn, accounts_opt)
                    try:
                        existing = {
                            str(token).strip()
                            for token in self._accounts().list_tokens()
                            if str(token or "").strip()
                        }
                    except Exception:  # noqa: BLE001 - 现网账号读取失败时交给 add_accounts 去重
                        existing = set()
                    fresh = [token for token in tokens if token not in existing]
                    result["accounts_skipped"] = len(tokens) - len(fresh)
                    if dry_run:
                        result["accounts_added"] = len(fresh)
                    else:
                        added_count = 0
                        if fresh:
                            added = self._accounts().add_accounts(fresh, return_items=False)
                            added_count = int((added or {}).get("added") or 0)
                        result["accounts_added"] = added_count
                        result["accounts_skipped"] = max(0, len(tokens) - added_count)
                if self._truthy(settings_opt.get("enabled"), False) and "system_settings" in tables:
                    mode = str(settings_opt.get("mode") or "fill").strip().lower()
                    legacy_settings = _read_settings(conn)
                    key_filter = self._settings_key_filter(settings_opt.get("keys"))
                    if key_filter is not None:
                        legacy_settings = {
                            key: value
                            for key, value in legacy_settings.items()
                            if key in key_filter
                        }
                    result["settings_updated"] = self._merge_settings(
                        legacy_settings,
                        "overwrite" if mode == "overwrite" else "fill",
                        dry_run,
                    )
                if self._truthy(options.get("account_groups"), False) and "account_group_configuration" in tables:
                    groups = _read_account_groups(conn)
                    result["groups_added"] = self._merge_groups(groups, dry_run)
                if self._truthy(options.get("call_records"), False) and "call_records" in tables:
                    limit = _clamp_int(options.get("max_call_records"), 2000, 0, 20_000)
                    watermark = self._import_watermark(valid_id)
                    items, _last_sequence = _read_call_records(
                        conn,
                        after_sequence=watermark,
                        limit=limit,
                    )
                    if dry_run:
                        result["call_records_imported"] = len(items)
                    else:
                        imported, imported_sequence = self._append_records(items)
                        result["call_records_imported"] = imported
                        # Persist only the contiguous imported prefix.  A
                        # retry after an append failure picks up exactly where
                        # it stopped instead of duplicating or dropping rows.
                        if imported_sequence is not None:
                            self._update_import_state(
                                valid_id,
                                {
                                    "call_sequence": imported_sequence,
                                    "last_import_at": _now_text(),
                                },
                            )
            except sqlite3.Error as exc:
                raise ValueError(f"导入文件已损坏或不可读: {exc}") from exc
            finally:
                conn.close()
        return result

    # ----------------------------------------------------------------- loop

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            config = self.get_config()
            if config["enabled"] and config["paths"]:
                due_in = self._next_run_in_seconds(config)
                if due_in <= 0:
                    try:
                        self.sync(trigger="schedule")
                    except Exception:  # noqa: BLE001 - 后台循环不能退出
                        time.sleep(30)
            self._stop_event.wait(30)

    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="legacy-sync", daemon=True)
        self._thread.start()

    def stop_loop(self) -> None:
        self._stop_event.set()


legacy_sync_service = LegacySyncService()
