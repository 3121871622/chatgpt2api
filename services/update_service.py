"""版本检测与一键更新（GitHub 仓库 + 宿主机更新代理）。

控制台的「更新中心」分两步：

1. `check()` 读取本地 `VERSION`，向 GitHub 查询仓库标签（公开仓库、匿名 API），
   找出比当前版本更新的最新 tag，返回版本对比与更新日志摘录；
2. `apply()` 不直接改容器：它把更新请求写进数据目录 `update/request.json`，
   宿主机上的更新代理（/root/qm_update_agent.sh，cron 每分钟）读取后执行
   `docker pull ghcr.io/<repo>:<tag>` → 备份旧 tag → 重打 tag →
   `docker compose up -d --force-recreate`，并把进度写回 `update/state.json`。

这样应用容器不需要挂载 docker.sock，更新动作始终由宿主机 root 代理执行；
代理没安装时 `status()` 的 `updater_available` 为 False，控制台会明确提示。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Lock
from typing import Any

from services.config import DATA_DIR

_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"
_DEFAULT_REPO = "3121871622/chatgpt2api"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_CHECK_TTL_SECONDS = 300
_HTTP_TIMEOUT_SECONDS = 8
_UPDATER_STALE_SECONDS = 180
_REQUEST_STALE_SECONDS = 600


def _read_local_version() -> str:
    for candidate in (_VERSION_FILE, Path("/opt/qingmeng/VERSION")):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def parse_version(value: object) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(str(value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _repo() -> str:
    return str(os.getenv("QINGMENG_UPDATE_REPO") or _DEFAULT_REPO).strip() or _DEFAULT_REPO


def _enabled() -> bool:
    raw = str(os.getenv("QINGMENG_UPDATE_ENABLED") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _update_dir() -> Path:
    directory = DATA_DIR / "update"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _http_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "qingmeng-update-checker",
        },
    )
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "qingmeng-update-checker"})
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", "replace")


def _release_notes(changelog_text: str, version: str) -> str:
    """Extract the `## [version]` section from a CHANGELOG.md body."""
    if not changelog_text or not version:
        return ""
    lines = changelog_text.splitlines()
    collected: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if inside:
                break
            heading = stripped[3:]
            if version in heading:
                inside = True
                continue
        if inside:
            if stripped.startswith(("### ", "## ")):
                collected.append(stripped.lstrip("# ").strip())
                continue
            if stripped:
                collected.append(stripped)
    text = "\n".join(collected).strip()
    return text[:2000]


def _timestamp_is_fresh(epoch_value: object, text_value: object) -> bool:
    """Return True when the update agent wrote its heartbeat recently.

    The agent runs on the host (usually UTC), the app container runs with
    TZ=Asia/Shanghai. The agent therefore writes `updated_epoch` (absolute
    seconds), which is timezone-proof; older state files are parsed as both
    UTC and local time and counted fresh when either reading is close.
    """
    now = time.time()
    epoch = None
    try:
        epoch = float(str(epoch_value).strip())
    except (TypeError, ValueError):
        epoch = None
    if epoch is not None and epoch > 0:
        return abs(now - epoch) <= _UPDATER_STALE_SECONDS
    text = str(text_value or "").strip()
    if not text:
        return False
    try:
        parsed = time.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError):
        return False
    candidates = []
    try:
        candidates.append(int(time.mktime(parsed)))
    except (ValueError, OverflowError):
        pass
    try:
        import calendar

        candidates.append(int(calendar.timegm(parsed)))
    except (ValueError, OverflowError):
        pass
    return any(abs(now - candidate) <= _UPDATER_STALE_SECONDS for candidate in candidates)


class UpdateService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    # --------------------------------------------------------------- helpers

    def _updater_state(self) -> dict[str, Any]:
        state = _read_json(_update_dir() / "state.json")
        updated_at = str(state.get("updated_at") or "")
        fresh = _timestamp_is_fresh(state.get("updated_epoch"), updated_at)
        return {
            "phase": str(state.get("phase") or "idle"),
            "target_version": str(state.get("target_version") or ""),
            "current_version": str(state.get("current_version") or ""),
            "message": str(state.get("message") or ""),
            "updated_at": updated_at,
            "finished_at": str(state.get("finished_at") or ""),
            "updater_available": fresh,
        }

    def _pending_request(self) -> dict[str, Any]:
        request = _read_json(_update_dir() / "request.json")
        if not request:
            return {}
        requested_at = str(request.get("requested_at") or "")
        if requested_at:
            try:
                age = time.time() - time.mktime(time.strptime(requested_at, "%Y-%m-%d %H:%M:%S"))
                if age > _REQUEST_STALE_SECONDS:
                    return {}
            except (ValueError, OverflowError):
                pass
        return request

    # ----------------------------------------------------------------- check

    def check(self, *, force: bool = False) -> dict[str, Any]:
        current = _read_local_version()
        base: dict[str, Any] = {
            "enabled": _enabled(),
            "source": "github",
            "repo": _repo(),
            "current_version": current,
            "latest_version": current,
            "update_available": False,
            "release_url": "",
            "notes": "",
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updater": self._updater_state(),
            "error": "",
        }
        if not base["enabled"]:
            base["error"] = "自动更新已通过 QINGMENG_UPDATE_ENABLED=0 关闭"
            return base
        with self._lock:
            if (
                not force
                and self._cached is not None
                and time.time() - self._cached_at < _CHECK_TTL_SECONDS
            ):
                return dict(self._cached)
        repo = _repo()
        tags: list[tuple[tuple[int, int, int], str]] = []
        try:
            payload = _http_json(f"https://api.github.com/repos/{repo}/tags?per_page=100")
            if not isinstance(payload, list):
                raise ValueError("unexpected GitHub tags payload")
            for item in payload:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                parsed = parse_version(name)
                if parsed is not None:
                    tags.append((parsed, name.lstrip("v")))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError, OSError) as exc:
            base["error"] = f"GitHub 版本查询失败：{exc}"
            base["updater"] = self._updater_state()
            with self._lock:
                self._cached = dict(base)
                self._cached_at = time.time()
            return base
        if not tags:
            base["error"] = "GitHub 仓库没有可识别的版本标签"
            return base
        tags.sort(key=lambda item: item[0])
        latest_parsed, latest = tags[-1]
        current_parsed = parse_version(current)
        update_available = bool(current_parsed and latest_parsed > current_parsed)
        base["latest_version"] = latest
        base["update_available"] = update_available
        base["release_url"] = f"https://github.com/{repo}/releases/tag/v{latest}"
        if update_available:
            try:
                changelog = _http_text(
                    f"https://raw.githubusercontent.com/{repo}/v{latest}/CHANGELOG.md"
                )
                base["notes"] = _release_notes(changelog, latest)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
                base["notes"] = ""
        base["updater"] = self._updater_state()
        with self._lock:
            self._cached = dict(base)
            self._cached_at = time.time()
        return base

    # ----------------------------------------------------------------- apply

    def apply(self, version: object, *, requested_by: str = "") -> dict[str, Any]:
        target = str(version or "").strip().lstrip("v")
        if parse_version(target) is None:
            raise ValueError("目标版本格式不正确，应为 x.y.z")
        current = _read_local_version()
        current_parsed = parse_version(current)
        target_parsed = parse_version(target)
        if current_parsed is not None and target_parsed is not None and target_parsed <= current_parsed:
            raise ValueError(f"目标版本 {target} 不高于当前版本 {current}")
        if not _enabled():
            raise ValueError("自动更新已关闭")
        pending = self._pending_request()
        if pending:
            return {
                "submitted": False,
                "pending": True,
                "target_version": str(pending.get("target_version") or ""),
                "message": "已有更新请求正在处理中，请等待宿主机代理完成",
            }
        state = self._updater_state()
        if not state["updater_available"]:
            raise ValueError("宿主机更新代理未安装或未运行（/root/qm_update_agent.sh + cron）")
        if state["phase"] in {"pulling", "applying"}:
            raise ValueError("宿主机代理正在执行更新，请稍后")
        request = {
            "target_version": target,
            "current_version": current,
            "image": f"ghcr.io/{_repo()}:{target}",
            "requested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "requested_by": str(requested_by or "")[:120],
        }
        _write_json(_update_dir() / "request.json", request)
        return {
            "submitted": True,
            "pending": True,
            "target_version": target,
            "current_version": current,
            "message": "已提交更新请求，宿主机代理将在 1 分钟内拉取镜像并重启容器",
        }

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        return {
            "current_version": _read_local_version(),
            "pending": self._pending_request(),
            "updater": self._updater_state(),
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


update_service = UpdateService()
