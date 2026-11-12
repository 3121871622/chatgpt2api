from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from services.config import DATA_DIR

_MAX_HTML_BYTES = 512 * 1024


class HomepageError(ValueError):
    pass


class HomepageService:
    """Admin-managed custom HTML served at the site root.

    The SPA stays reachable at /admin; the root is deliberately not redirected
    so operators can publish a real landing page (or let it 404).
    """

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._path = Path(data_dir) / "custom_homepage.json"
        self._lock = Lock()

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "enabled": bool(raw.get("enabled", False)),
            "html": str(raw.get("html") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
        }

    def view(self) -> dict[str, Any]:
        with self._lock:
            return self._read()

    def public_html(self) -> str:
        state = self.view()
        if not state["enabled"]:
            return ""
        return str(state["html"] or "").strip()

    def update(self, *, enabled: bool, html: str) -> dict[str, Any]:
        content = str(html or "")
        if len(content.encode("utf-8")) > _MAX_HTML_BYTES:
            raise HomepageError("自定义首页 HTML 不能超过 512 KB")
        if enabled and not content.strip():
            raise HomepageError("开启自定义首页时 HTML 不能为空")
        state = {
            "enabled": bool(enabled),
            "html": content,
            "updated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
        }
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self._path)
        return state


homepage_service = HomepageService()
