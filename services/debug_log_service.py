"""可开关的调试日志：默认关闭，按「时间」或「体积」两种方式滚动保留。

* 时间模式：保留最近 N 天，更早的文件自动删除；
* 体积模式：总量目标 `max_size_mb`，允许上浮 `slack_mb`；超过「目标 + 上浮」
  就从最旧的文件开始删，直到回到目标以内。当前正在写入的文件永不删除。

日志写入 ``data/debug-logs/debug-*.log``（JSON Lines），单文件在 64MB 处滚动；
进程重启后若最新文件未达上限会继续追加，否则新建文件。导出时打包成 zip，
最多取最新的 300MB 日志，附带 ``system-info.json``（版本、号池/设置摘要，
递归脱敏，不含密钥）。

写入前统一脱敏：JWT、Bearer、``sk-`` 密钥、token/password/api_key 等 JSON
字段、URL 查询参数；邮箱地址保留。data 目录权限会在启用捕获前探测，属主为
root 且进程按非 root 运行时给出可执行的 chown 提示。
"""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable

from services.config import DATA_DIR

CONFIG_FILE_NAME = "debug_logs.json"
LOG_DIR_NAME = "debug-logs"
FILE_MAX_BYTES = 64 * 1024 * 1024
EXPORT_MAX_BYTES = 300 * 1024 * 1024
TRIM_INTERVAL_SECONDS = 300
LOGGER_NAME = "chatgpt2api"
MESSAGE_MAX_CHARS = 16000
ACCESS_CHECK_TTL_SECONDS = 5.0
RUN_UID = 10001
RUN_GID = 10001

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "mode": "time",  # time | size
    "retention_days": 30,
    "max_size_mb": 1024,
    "slack_mb": 100,
    "level": "debug",  # debug | info
}

_UNSET = object()

# ---------------------------------------------------------------- scrubbing

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;\"']{1,}")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{4,}")
_JSON_FIELD_RE = re.compile(
    r'"(?P<key>[^"\\]{1,160})"(?P<sep>\s*:\s*)"(?P<value>(?:[^"\\]|\\.)*)"'
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|"
    r"apikey|token|key|secret)=)[^&\s\"'#]+"
)
_AUTH_HEADER_RE = re.compile(
    r"(?im)^(?P<prefix>\s*(?:x-api-key|api-key|authorization)\s*:\s*)"
    r"(?!\s*Bearer\b)(?P<value>[^\r\n]+)$"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s]+)@")

# ``token_type``/``max_tokens`` 是计数或枚举，不应被当成密钥；其余包含
# token/secret/password 等片段的键都按密钥处理。
_NON_SECRET_KEY_PARTS = frozenset({
    "maxtokens",
    "tokencount",
    "tokenstotal",
    "tokentype",
    "totaltokens",
    "prompttokens",
    "completiontokens",
    "tokensused",
    "tokenusage",
})
_PRESENCE_FLAG_PREFIXES = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "password",
    "secret",
    "token",
    "cfcookies",
    "cfclearance",
    "pushkey",
    "passphrase",
    "webdavpassword",
    "accesskey",
    "credential",
)
_SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "privatekey",
    "pushkey",
    "authkey",
    "authorization",
    "cookie",
    "credential",
    "cfclearance",
    "session",
)


def _normalize_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def is_secret_key(key: object) -> bool:
    normalized = _normalize_key(key)
    if not normalized or normalized in _NON_SECRET_KEY_PARTS:
        return False
    # ``has_api_key`` / ``has_cf_cookies`` are booleans that deliberately
    # report presence without carrying the value; keep them readable.
    if normalized.startswith("has") and any(
        normalized[3:].startswith(prefix) for prefix in _PRESENCE_FLAG_PREFIXES
    ):
        return False
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def scrub_text(text: str) -> str:
    """Mask credentials while keeping e-mail addresses and ordinary text."""
    value = str(text or "")
    value = _JWT_RE.sub("jwt:***", value)
    value = _BEARER_RE.sub("Bearer ***", value)
    value = _SK_RE.sub("sk-***", value)
    value = _AUTH_HEADER_RE.sub(lambda match: f"{match.group('prefix')}***", value)
    value = _JSON_FIELD_RE.sub(_mask_json_field, value)
    value = _QUERY_SECRET_RE.sub(r"\1***", value)
    value = _redact_url_credentials(value)
    return value


def _mask_json_field(match: re.Match[str]) -> str:
    key = match.group("key")
    if not is_secret_key(key):
        return match.group(0)
    return f'"{key}"{match.group("sep")}"***"'


def _redact_url_credentials(value: str) -> str:
    return _URL_CREDENTIAL_RE.sub(r"\1***@", value)


def sanitize_for_export(value: Any, *, key: object = None) -> Any:
    """Recursively scrub a JSON-like payload; secret keys become ``***``."""
    if key is not None and is_secret_key(key):
        return "***"
    if isinstance(value, dict):
        return {
            item_key: sanitize_for_export(item_value, key=item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_for_export(item) for item in value]
    if isinstance(value, str):
        return _redact_url_credentials(scrub_text(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_url_credentials(scrub_text(str(value)))


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _clean_text(value: object) -> str:
    return _redact_url_credentials(scrub_text(str(value or "").strip()))


def _collect_secret_values(
    value: object,
    *,
    key: object = None,
    out: list[str] | None = None,
) -> list[str]:
    collected = out if out is not None else []
    if (
        key is not None
        and isinstance(value, str)
        and value.strip()
        and is_secret_key(key)
    ):
        collected.append(value.strip())
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            _collect_secret_values(item_value, key=item_key, out=collected)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_secret_values(item, out=collected)
    return collected


def _collect_proxy_values(
    value: object,
    *,
    key: object = None,
    out: list[str] | None = None,
) -> list[str]:
    collected = out if out is not None else []
    normalized = _normalize_key(key)
    if (
        normalized in {"proxy", "proxyurl", "resourceproxyurl"}
        and isinstance(value, str)
        and value.strip()
    ):
        collected.append(value.strip())
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            _collect_proxy_values(item_value, key=item_key, out=collected)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_proxy_values(item, out=collected)
    return collected


def _settings_projection(raw: dict[str, Any]) -> dict[str, Any]:
    ai_review = raw.get("ai_review") if isinstance(raw.get("ai_review"), dict) else {}
    proxy_runtime = (
        raw.get("proxy_runtime")
        if isinstance(raw.get("proxy_runtime"), dict)
        else {}
    )
    clearance = (
        proxy_runtime.get("clearance")
        if isinstance(proxy_runtime.get("clearance"), dict)
        else {}
    )
    image_storage = (
        raw.get("image_storage")
        if isinstance(raw.get("image_storage"), dict)
        else {}
    )
    backup = raw.get("backup") if isinstance(raw.get("backup"), dict) else {}
    has_cf_cookies = bool(str(clearance.get("cf_cookies") or "").strip()) or _normalize_bool(
        clearance.get("has_cf_cookies"), False
    )
    has_secret_access_key = bool(
        str(backup.get("secret_access_key") or "").strip()
    ) or _normalize_bool(backup.get("has_secret_access_key"), False)
    return {
        "ai_review": {
            "enabled": _normalize_bool(ai_review.get("enabled"), False),
            "base_url": _clean_text(ai_review.get("base_url")),
            "has_api_key": bool(str(ai_review.get("api_key") or "").strip()),
        },
        "proxy_runtime": {
            "enabled": _normalize_bool(proxy_runtime.get("enabled"), False),
            "clearance": {
                "enabled": _normalize_bool(clearance.get("enabled"), False),
                "mode": _clean_text(clearance.get("mode") or "none"),
                "has_cf_cookies": has_cf_cookies,
            },
        },
        "image_storage": {
            "mode": _clean_text(image_storage.get("mode") or "local"),
        },
        "backup": {
            "enabled": _normalize_bool(backup.get("enabled"), False),
            "has_secret_access_key": has_secret_access_key,
        },
    }


def _account_group_projection(groups: object) -> list[dict[str, Any]]:
    if not isinstance(groups, list) or not groups:
        return []
    node_counts: dict[str, int] = {}
    try:
        from services.proxy_management_service import proxy_management_service

        for proxy_group in proxy_management_service.list_groups().groups:
            group_id = _clean_text(getattr(proxy_group, "id", ""))
            if group_id:
                node_counts[group_id] = len(getattr(proxy_group, "nodes", []) or [])
    except Exception:  # noqa: BLE001 - node counts are best effort only
        node_counts = {}

    projection: list[dict[str, Any]] = []
    for item in groups if isinstance(groups, list) else []:
        if not isinstance(item, dict):
            continue
        group_id = _clean_text(item.get("id") or "").strip()
        if not group_id:
            continue
        raw_count = item.get("node_count")
        if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool):
            node_count = max(0, int(raw_count))
        elif isinstance(item.get("nodes"), list):
            node_count = len(item["nodes"])
        else:
            proxy_group_id = str(item.get("proxy_group_id") or "").strip()
            proxy_ref = str(item.get("proxy") or "").strip()
            if proxy_ref.lower().startswith("group:"):
                proxy_group_id = proxy_ref.split(":", 1)[1].strip()
            if proxy_group_id:
                node_count = max(0, node_counts.get(proxy_group_id, 0))
            elif proxy_ref and proxy_ref.lower() not in {
                "direct",
                "inherit",
                "system",
                "default",
            }:
                # A custom proxy URL is one dedicated egress node.
                node_count = 1
            else:
                node_count = 0
        projection.append({
            "id": group_id,
            "name": _clean_text(item.get("name") or group_id) or group_id,
            "enabled": _normalize_bool(item.get("enabled"), True),
            "node_count": node_count,
        })
    return projection


def _scheduler_projection() -> dict[str, Any]:
    try:
        from services.adaptive_governor import governor

        snapshot = governor().snapshot()
        limit = 0
        try:
            from services.admission import admission_gate

            limit = _as_int(admission_gate().stats().get("limit"), 0)
        except Exception:  # noqa: BLE001
            try:
                from services.admission import resolve_base_limit

                limit = int(governor().effective_limit(resolve_base_limit(), floor=1))
            except Exception:  # noqa: BLE001
                limit = 0
        return {
            "mode": _clean_text(snapshot.get("mode") or "auto"),
            "scale": snapshot.get("scale"),
            "limit": limit,
        }
    except Exception:  # noqa: BLE001 - keep a stable, secret-free shape
        return {"mode": "unknown", "scale": None, "limit": 0}


def _sanitize_system_info(
    info: dict[str, Any],
    raw_config: dict[str, Any],
) -> dict[str, Any]:
    """Whitelist projection is not enough: scrub again before serialisation."""
    sensitive_values = _collect_secret_values(raw_config)
    proxy_values = _collect_proxy_values(raw_config)
    scrubbed: object = info
    try:
        from utils.diagnostics import scrub_diagnostic_value

        scrubbed = scrub_diagnostic_value(
            info,
            sensitive_values=sensitive_values,
            proxy_values=proxy_values,
        )
    except Exception:  # noqa: BLE001 - fall through to the local sanitizer
        scrubbed = info
    result = sanitize_for_export(scrubbed)
    return result if isinstance(result, dict) else info


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def normalize_config(raw: object) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    mode = str(data.get("mode") or "time").strip().lower()
    if mode not in {"time", "size"}:
        mode = "time"
    level = str(data.get("level") or "debug").strip().lower()
    if level not in {"debug", "info"}:
        level = "debug"
    max_size = _clamp_int(data.get("max_size_mb"), 1024, 16, 64 * 1024)
    slack = _clamp_int(data.get("slack_mb"), 100, 0, max_size // 2)
    return {
        "enabled": _normalize_bool(data.get("enabled"), False),
        "mode": mode,
        "retention_days": _clamp_int(data.get("retention_days"), 30, 1, 3650),
        "max_size_mb": max_size,
        "slack_mb": slack,
        "level": level,
    }


# ------------------------------------------------------- directory access

def _current_process_uid() -> int | None:
    for name in ("geteuid", "getuid"):
        getter = getattr(os, name, None)
        if getter is None:
            continue
        try:
            return int(getter())
        except (OSError, TypeError, ValueError):
            return None
    return None


def _path_owner_uid(path: Path) -> int | None:
    try:
        return int(path.stat().st_uid)
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def _probe_directory_writable(directory: Path) -> str:
    probe = directory / (
        f".debug-log-probe-{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}"
    )
    try:
        with probe.open("xb") as handle:
            handle.write(b"probe")
        return ""
    except OSError as exc:
        return str(exc)
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def check_data_dir_access(
    directory: Path | str,
    *,
    process_uid: int | None | object = _UNSET,
    owner_uid: int | None | object = _UNSET,
    posix: bool | object = _UNSET,
    create: bool = False,
) -> dict[str, Any]:
    """Return a structured writability report for a data/log directory.

    ``process_uid``/``owner_uid``/``posix`` exist so the root-owned diagnostic
    can be exercised on every platform without actually chowning files.
    """
    path = Path(directory)
    if posix is _UNSET:
        # On Linux ``geteuid`` exists; treating a forced/simulated euid the same
        # way keeps the diagnostic testable on other platforms too.
        posix_mode = os.name == "posix" or hasattr(os, "geteuid")
    else:
        posix_mode = bool(posix)
    uid = _current_process_uid() if process_uid is _UNSET else process_uid
    if owner_uid is _UNSET:
        owner = _path_owner_uid(path) if posix_mode and path.exists() else None
    else:
        owner = owner_uid

    report: dict[str, Any] = {
        "directory": str(path),
        "writable": False,
        "owner_uid": owner,
        "process_uid": uid,
        "expected_uid": RUN_UID,
        "error": "",
        "warning": "",
    }

    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            report["error"] = (
                f"data 目录 {path} 创建失败：{exc}；请确认挂载卷权限，"
                f"或执行 chown -R {RUN_UID}:{RUN_GID} {path}"
            )
            return report

    if not path.exists():
        report["error"] = f"data 目录不存在：{path}"
        return report

    if owner is None and posix_mode:
        owner = _path_owner_uid(path)
        report["owner_uid"] = owner

    if posix_mode and owner == 0 and uid != 0:
        report["error"] = (
            f"data 目录 {path} 属主为 root(uid=0)，当前进程 uid="
            f"{uid if uid is not None else '未知'}；"
            f"请执行 chown -R {RUN_UID}:{RUN_GID} {path}"
            f"（宿主机执行 sudo chown -R {RUN_UID}:{RUN_GID} <data 挂载目录>）"
        )
        return report

    if posix_mode and owner == 0 and uid == 0:
        report["warning"] = (
            f"data 目录 {path} 属主为 root(uid=0)，当前进程也以 uid=0 运行；"
            f"容器内请用 gosu 降权到 {RUN_UID}:{RUN_GID}"
        )

    probe_error = _probe_directory_writable(path)
    if probe_error:
        report["error"] = (
            f"data 目录 {path} 权限不足，写入探测失败：{probe_error}；"
            f"属主 uid={owner if owner is not None else '未知'}，"
            f"当前进程 uid={uid if uid is not None else '未知'}；"
            f"请执行 chown -R {RUN_UID}:{RUN_GID} {path}"
        )
        return report

    report["writable"] = True
    return report


# ------------------------------------------------------------------ handler

class _FileHandler(logging.Handler):
    def __init__(self, service: "DebugLogService", path: Path) -> None:
        super().__init__(level=logging.DEBUG)
        self._service = service
        self._path = path

    def set_path(self, path: Path) -> None:
        self._path = path

    def emit(self, record: logging.LogRecord) -> None:
        # The same record can reach us through several loggers (root + named);
        # write it once.
        if getattr(record, "_qm_debug_emitted", False):
            return
        try:
            if record.levelno < self.level:
                return
            setattr(record, "_qm_debug_emitted", True)
            message = scrub_text(record.getMessage())[:MESSAGE_MAX_CHARS]
            line = self._encode_line(record, message)
            self._service._write_line(line)
        except Exception:  # noqa: BLE001 - logging must never break the caller
            self.handleError(record)

    @staticmethod
    def _encode_line(record: logging.LogRecord, message: str) -> str:
        limit = max(128, int(FILE_MAX_BYTES))
        while True:
            line = json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }, ensure_ascii=False)
            overflow = len(line.encode("utf-8")) + 1 - limit
            if overflow <= 0 or not message:
                return line
            # A single record must fit in one file even when the cap is tiny.
            message = message[: max(0, len(message) - max(16, overflow))]


class DebugLogService:
    def __init__(self, *, data_dir: Path | None = None) -> None:
        root = Path(data_dir) if data_dir else DATA_DIR
        self._root = root
        self._config_path = root / CONFIG_FILE_NAME
        self._log_dir = root / LOG_DIR_NAME
        self._lock = threading.RLock()
        self._apply_lock = threading.RLock()
        self._access_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._handler: _FileHandler | None = None
        self._attached_loggers: list[logging.Logger] = []
        self._path: Path | None = None
        self._active_size = 0
        self._access_report: dict[str, Any] | None = None
        self._access_checked_at = 0.0
        self._reported_access_error = ""
        self._write_error = ""
        self._run_pid = os.getpid()

    # ------------------------------------------------------------ config

    def _load(self) -> dict[str, Any]:
        raw: object = {}
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return normalize_config({**DEFAULT_CONFIG, **raw})

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    def is_capturing(self) -> bool:
        """True while the debug file handler is attached (cheap, lock-free)."""
        return self._handler is not None

    def last_error(self) -> str:
        report = self._inspect_access(force=True, create=False)
        return str(report.get("error") or self._write_error or "")

    def save_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        changes = patch if isinstance(patch, dict) else {}
        with self._lock:
            current = self._load()
            merged = normalize_config({
                **current,
                **{key: value for key, value in changes.items() if value is not None},
            })
            try:
                self._config_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._config_path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(merged, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self._config_path)
            except OSError as exc:
                self._write_error = f"调试日志配置写入失败：{exc}"
        self.apply()
        return merged

    # ------------------------------------------------------------- access

    def _inspect_access(self, *, force: bool = False, create: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force:
            with self._access_lock:
                cached = self._access_report
                if (
                    cached is not None
                    and now - self._access_checked_at < ACCESS_CHECK_TTL_SECONDS
                ):
                    return cached

        root_report = check_data_dir_access(self._root, create=create)
        report = dict(root_report)
        report["data_directory"] = str(self._root)
        report["log_directory"] = str(self._log_dir)
        error = str(root_report.get("error") or "")
        warning = str(root_report.get("warning") or "")

        if create and not error:
            try:
                self._log_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                error = (
                    f"调试日志目录 {self._log_dir} 创建失败：{exc}；"
                    f"请执行 chown -R {RUN_UID}:{RUN_GID} {self._root}"
                )
            else:
                log_report = check_data_dir_access(self._log_dir, create=False)
                if not log_report["writable"]:
                    error = str(log_report["error"])
                elif log_report.get("warning") and not warning:
                    warning = str(log_report["warning"])
                report["owner_uid"] = log_report["owner_uid"]
                report["process_uid"] = log_report["process_uid"]
        elif not error and self._log_dir.exists():
            log_report = check_data_dir_access(self._log_dir, create=False)
            if not log_report["writable"]:
                error = str(log_report["error"])
            elif log_report.get("warning") and not warning:
                warning = str(log_report["warning"])

        report["writable"] = bool(not error and root_report["writable"])
        report["error"] = error
        report["warning"] = warning

        with self._access_lock:
            self._access_report = report
            self._access_checked_at = now

        if error and error != self._reported_access_error:
            self._reported_access_error = error
            logging.getLogger(f"{LOGGER_NAME}.debug_logs").warning(
                "调试日志捕获不可用：%s", error
            )
        elif not error:
            self._reported_access_error = ""
        return report

    # ------------------------------------------------------------- files

    def _files(self) -> list[dict[str, Any]]:
        try:
            paths = list(self._log_dir.glob("debug-*.log")) if self._log_dir.is_dir() else []
        except OSError:
            return []
        items: list[dict[str, Any]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append({
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified_at": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
                ),
                "_mtime": stat.st_mtime,
                "_path": path,
            })
        items.sort(key=lambda item: (item["_mtime"], item["name"]))
        return items

    @staticmethod
    def _total_bytes(files: Iterable[dict[str, Any]]) -> int:
        return sum(int(item["size_bytes"]) for item in files)

    @staticmethod
    def _unlink(item: dict[str, Any]) -> bool:
        try:
            item["_path"].unlink()
            return True
        except OSError:
            return False

    def trim(self) -> dict[str, int]:
        config = self.get_config()
        files = self._files()
        active = self._path
        removed = 0
        freed = 0

        if config["mode"] == "time":
            cutoff = time.time() - config["retention_days"] * 86400
            for item in files:
                if item["_path"] == active or item["_mtime"] >= cutoff:
                    continue
                if self._unlink(item):
                    removed += 1
                    freed += int(item["size_bytes"])
            return {"removed": removed, "freed_bytes": freed}

        target = config["max_size_mb"] * 1024 * 1024
        slack = config["slack_mb"] * 1024 * 1024
        total = self._total_bytes(files)
        # Grow up to target + slack, then trim the oldest files back to target.
        # The boundary is strict: exactly target + slack is still allowed.
        if total <= target + slack:
            return {"removed": removed, "freed_bytes": freed}

        for item in files:
            if total <= target:
                break
            if item["_path"] == active:
                continue
            if self._unlink(item):
                total -= int(item["size_bytes"])
                removed += 1
                freed += int(item["size_bytes"])
        return {"removed": removed, "freed_bytes": freed}

    # ------------------------------------------------------------ handler

    def _current_path(self) -> Path:
        files = self._files()
        if files:
            newest = files[-1]
            if int(newest["size_bytes"]) < FILE_MAX_BYTES:
                return newest["_path"]
        return self._new_rotation_path()

    def _new_rotation_path(self, current: Path | None = None) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        base = f"debug-{stamp}-{self._run_pid}"
        for index in range(10000):
            suffix = f"-{index}" if index else ""
            candidate = self._log_dir / f"{base}{suffix}.log"
            if candidate == current:
                continue
            if not candidate.exists():
                return candidate
        return self._log_dir / f"{base}-{time.time_ns()}.log"

    @staticmethod
    def _handler_level(config: dict[str, Any]) -> int:
        return logging.INFO if config["level"] == "info" else logging.DEBUG

    def _attach_handler(self, path: Path, level: int | None = None) -> None:
        if level is None:
            level = self._handler_level(self.get_config())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._write_error = f"调试日志目录创建失败：{exc}"
            return
        handler = _FileHandler(self, path)
        handler.setLevel(level)
        loggers = [
            logging.getLogger(LOGGER_NAME),
            logging.getLogger(),
            logging.getLogger("uvicorn"),
            logging.getLogger("uvicorn.error"),
            logging.getLogger("uvicorn.access"),
        ]
        with self._write_lock:
            if self._handler is not None:
                return
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            self._handler = handler
            self._path = path
            self._active_size = int(size)
            self._attached_loggers = loggers
        for target in loggers:
            target.addHandler(handler)
            if target.level == logging.NOTSET or target.level > level:
                target.setLevel(logging.DEBUG)

    def _detach_handler(self) -> None:
        with self._write_lock:
            handler = self._handler
            loggers = list(self._attached_loggers)
            self._handler = None
            self._attached_loggers = []
            self._path = None
            self._active_size = 0
        if handler is None:
            return
        for target in loggers:
            try:
                target.removeHandler(handler)
            except Exception:  # noqa: BLE001
                pass

    def _rotate_if_needed(self) -> None:
        """Rotate the active file when it already reached the per-file cap."""
        rotated = False
        with self._write_lock:
            if self._handler is None or self._path is None:
                return
            try:
                size = self._path.stat().st_size
            except OSError:
                return
            if size < FILE_MAX_BYTES:
                return
            path = self._new_rotation_path(self._path)
            self._path = path
            self._handler.set_path(path)
            self._active_size = 0
            rotated = True
        if rotated:
            self.trim()

    def _write_line(self, line: str) -> None:
        payload = (line + "\n").encode("utf-8")
        rotated = False
        with self._write_lock:
            if self._handler is None or self._path is None:
                return
            path = self._path
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if self._active_size:
                    try:
                        self._active_size = path.stat().st_size
                    except OSError:
                        self._active_size = 0
                if (
                    self._active_size
                    and self._active_size + len(payload) > FILE_MAX_BYTES
                ):
                    path = self._new_rotation_path(path)
                    self._path = path
                    self._handler.set_path(path)
                    self._active_size = 0
                    rotated = True
                with path.open("ab") as handle:
                    handle.write(payload)
                self._active_size += len(payload)
                self._write_error = ""
            except OSError as exc:
                self._write_error = f"调试日志写入失败：{exc}"
                return
        if rotated:
            try:
                self.trim()
            except Exception:  # noqa: BLE001 - trimming is best effort
                pass

    def apply(self) -> None:
        with self._apply_lock:
            if self._stop_event.is_set():
                self._detach_handler()
                return
            config = self.get_config()
            if not config["enabled"]:
                self._detach_handler()
                self.trim()
                return

            report = self._inspect_access(force=True, create=True)
            if not report["writable"]:
                self._detach_handler()
                self.trim()
                return

            level = self._handler_level(config)
            with self._write_lock:
                exists = self._handler is not None
            if not exists:
                self._attach_handler(self._current_path(), level)
            elif self._handler is not None:
                self._handler.setLevel(level)
            self._rotate_if_needed()
            self.trim()

    # ------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        files = self._files()
        config = self.get_config()
        report = self._inspect_access(force=True, create=False)
        public_files = [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in files[-5:]
        ]
        active_path = self._path
        return {
            "config": config,
            "active": self._handler is not None,
            "active_file": str(active_path) if active_path is not None else "",
            "files": public_files,
            "file_count": len(files),
            "total_bytes": self._total_bytes(files),
            "budget_bytes": config["max_size_mb"] * 1024 * 1024,
            "slack_bytes": config["slack_mb"] * 1024 * 1024,
            "file_max_bytes": FILE_MAX_BYTES,
            "export_max_bytes": EXPORT_MAX_BYTES,
            "directory": str(self._log_dir),
            "data_directory": str(self._root),
            "writable": bool(report["writable"]),
            "last_error": str(report.get("error") or self._write_error or ""),
            "warning": str(report.get("warning") or ""),
            "owner_uid": report.get("owner_uid"),
            "process_uid": report.get("process_uid"),
        }

    # -------------------------------------------------------------- export

    def _system_info(self) -> dict[str, Any]:
        """Build a whitelisted, recursively scrubbed system-info payload.

        Never export raw ``config.data``: the projection only carries booleans,
        modes, counters and sanitised URLs. ``_sanitize_system_info`` then runs
        the shared diagnostics scrubber plus the local key/URL scrubber over
        the projection (defence in depth if a field is added later).
        """
        info: dict[str, Any] = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            from services.config import BASE_DIR

            info["version"] = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            try:
                info["version"] = (self._root / "VERSION").read_text(
                    encoding="utf-8"
                ).strip()
            except OSError:
                info["version"] = ""

        raw_config: dict[str, Any] = {}
        try:
            from services.config import config

            if isinstance(config.data, dict):
                raw_config = copy.deepcopy(dict(config.data))
        except Exception:  # noqa: BLE001 - projection still works with defaults
            raw_config = {}

        try:
            from services.pool_alert_service import pool_alert_service

            measured = pool_alert_service.measure()
            info["pool"] = {
                "total": _as_int(measured.get("total"), 0),
                "usable": _as_int(measured.get("usable"), 0),
                "refresh_valid": _as_int(measured.get("refresh_valid"), 0),
            }
        except Exception:  # noqa: BLE001 - keep the whitelist shape stable
            info["pool"] = {"total": 0, "usable": 0, "refresh_valid": 0}

        info["scheduler"] = _scheduler_projection()
        info["settings"] = _settings_projection(raw_config)
        info["account_groups"] = _account_group_projection(
            raw_config.get("account_groups")
        )
        return _sanitize_system_info(info, raw_config)

    def _select_export_items(
        self, files: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], int, bool]]:
        """Return ``(item, bytes_to_write, is_tail)`` newest-first."""
        selected: list[tuple[dict[str, Any], int, bool]] = []
        used = 0
        for item in reversed(files):
            size = int(item["size_bytes"])
            if size <= 0:
                if used >= EXPORT_MAX_BYTES:
                    break
                selected.append((item, 0, False))
                continue
            if used == 0:
                take = min(size, EXPORT_MAX_BYTES)
                selected.append((item, take, take < size))
                used += take
                if take < size:
                    break
                continue
            if used + size > EXPORT_MAX_BYTES:
                break
            selected.append((item, size, False))
            used += size
        return selected

    def _write_zip_entry(
        self,
        archive: zipfile.ZipFile,
        path: Path,
        arcname: str,
        limit: int,
        *,
        tail: bool,
    ) -> bool:
        try:
            stat = path.stat()
            with path.open("rb") as source:
                if tail:
                    start = max(0, int(stat.st_size) - limit)
                    source.seek(start)
                stamp = time.localtime(stat.st_mtime)
                date_time = stamp[:6]
                if date_time[0] < 1980:
                    date_time = time.localtime()[:6]
                zinfo = zipfile.ZipInfo(arcname, date_time=date_time)
                zinfo.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(zinfo, "w") as destination:
                    remaining = limit
                    while remaining > 0:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        destination.write(chunk)
                        remaining -= len(chunk)
            return True
        except OSError:
            return False

    def export_zip(self) -> tuple[bytes, str]:
        files = self._files()
        selected = self._select_export_items(files)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item, take, tail in selected:
                if take <= 0 and int(item["size_bytes"]) > 0:
                    continue
                self._write_zip_entry(
                    archive,
                    item["_path"],
                    item["name"],
                    take,
                    tail=tail,
                )
            archive.writestr(
                "system-info.json",
                json.dumps(self._system_info(), ensure_ascii=False, indent=2),
            )
            archive.writestr("README.txt", (
                "倾梦GPT 调试日志包\n"
                "包含：应用日志（JSON Lines）、system-info.json（版本/号池/设置摘要）\n"
                "单文件上限 64MB；导出按最新文件截取，日志总量最多 300MB。\n"
                "system-info.json：version / pool / scheduler / settings / "
                "account_groups 白名单投影，导出前递归脱敏，不含密钥。\n"
                "debug-*.log：写入前脱敏 JWT、Bearer、密钥前缀、token/password/"
                "api_key 字段与 URL 查询参数；邮箱地址保留。\n"
            ))
        name = f"qingmeng-debug-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        return buffer.getvalue(), name

    def clear(self) -> dict[str, int]:
        removed = 0
        freed = 0
        with self._write_lock:
            active = self._path
            for item in self._files():
                if not self._unlink(item):
                    continue
                removed += 1
                freed += int(item["size_bytes"])
                if active is not None and item["_path"] == active:
                    self._active_size = 0
        return {"removed": removed, "freed_bytes": freed}

    # ---------------------------------------------------------------- loop

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.apply()
            except Exception:  # noqa: BLE001
                pass
            self._stop_event.wait(TRIM_INTERVAL_SECONDS)

    def start(self) -> None:
        with self._apply_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self.apply()
            self._thread = threading.Thread(
                target=self._loop, name="debug-logs", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._apply_lock:
            self._detach_handler()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)


debug_log_service = DebugLogService()
