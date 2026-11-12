"""本地更新日志（CHANGELOG.md）解析与投影。

控制台需要一个「更新日志」入口，但项目已经移除了任何联网版本检查，所以这里只读取
仓库自带的 CHANGELOG.md，按版本拆成结构化数据供前端展示：

    ## [1.0.4] 2026-10-24
    ### 新增
    + [新增] 控制台内置更新日志查看器
    （两个空格开头的续行会合并到上一条）

解析结果按文件 mtime + size 缓存，编辑日志后无需重启即可看到新内容。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from threading import Lock
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHANGELOG_PATH = _ROOT / "CHANGELOG.md"
VERSION_FILE = _ROOT / "VERSION"

_RELEASE_PATTERN = re.compile(
    r"^##\s+(?:\[(?P<bracket>[^\]]+)\]|(?P<plain>\S+))"
    r"\s*(?:[-–—]\s*)?(?P<date>\d{4}-\d{2}-\d{2})?"
    r"(?:\s+(?P<time>\d{2}:\d{2}))?\s*$"
)
_SECTION_PATTERN = re.compile(r"^###\s+(?P<title>.+?)\s*$")
_ITEM_PATTERN = re.compile(r"^[+*-]\s+\[(?P<type>[^\]]+)\]\s+(?P<text>.+)$")
_ARCHIVE_MARKERS = ("历史版本", "更新日志存档")


class ChangelogService:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_CHANGELOG_PATH
        self._lock = Lock()
        self._cache_key: str | None = None
        self._cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------ read

    def current_version(self) -> str:
        try:
            return VERSION_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def view(self, limit: int = 10) -> dict[str, Any]:
        limit = max(1, min(int(limit or 10), 50))
        releases, archive = self._parse()
        return {
            "current_version": self.current_version(),
            "source": self._path.name,
            "available": bool(releases or archive),
            "releases": releases[:limit],
            "archive": archive[:limit],
            "totals": {"releases": len(releases), "archive": len(archive)},
        }

    # ----------------------------------------------------------------- parse

    def _parse(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            raw = self._path.read_bytes()
        except OSError:
            return [], []
        # Content hash beats mtime for an editor that rewrites in place within
        # the same timestamp granularity; the file is only a few KB.
        key = hashlib.sha1(raw).hexdigest()
        with self._lock:
            if self._cache_key == key and self._cache is not None:
                return self._cache["releases"], self._cache["archive"]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return [], []

        releases: list[dict[str, Any]] = []
        archive: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        section: dict[str, Any] | None = None
        archived = False
        last_item: dict[str, Any] | None = None

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                last_item = None
                continue
            if stripped.startswith("## "):
                heading = stripped[3:].strip()
                if any(marker in heading for marker in _ARCHIVE_MARKERS):
                    archived = True
                    current = None
                    section = None
                    last_item = None
                    continue
                match = _RELEASE_PATTERN.match(stripped)
                if match:
                    version = (match.group("bracket") or match.group("plain") or "").strip()
                    version = version.strip("[]")
                    current = {
                        "version": version,
                        "date": (match.group("date") or "").strip(),
                        "time": (match.group("time") or "").strip(),
                        "sections": [],
                        "archived": archived,
                    }
                    (archive if archived else releases).append(current)
                    section = None
                    last_item = None
                continue
            if current is None:
                continue
            match = _SECTION_PATTERN.match(stripped)
            if match:
                section = {"title": match.group("title").strip(), "items": []}
                current["sections"].append(section)
                last_item = None
                continue
            if section is None:
                section = {"title": "变更", "items": []}
                current["sections"].append(section)
            match = _ITEM_PATTERN.match(stripped)
            if match:
                last_item = {
                    "type": match.group("type").strip(),
                    "text": match.group("text").strip(),
                }
                section["items"].append(last_item)
                continue
            if last_item is not None and line.startswith("  "):
                # Continuation line of the previous item.
                last_item["text"] = f"{last_item['text']} {stripped}"
                continue
            if stripped.startswith((">", "|", "#")):
                last_item = None
                continue
            # Plain paragraph inside a release: keep it as a note item.
            last_item = {"type": "说明", "text": stripped}
            section["items"].append(last_item)

        with self._lock:
            self._cache_key = key
            self._cache = {"releases": releases, "archive": archive}
        return releases, archive


changelog_service = ChangelogService()
