"""轻量审计日志：管理员关键操作写入 ``data/audit.jsonl``。

只记录「谁、什么时候、做了什么、影响多少」，不记录 token、密钥等敏感值；
文件超过 2 MB 自动轮转一份 ``.1``，避免无限增长。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from services.config import DATA_DIR

AUDIT_FILE_NAME = "audit.jsonl"
ROTATE_BYTES = 2 * 1024 * 1024
MAX_LIMIT = 500


class AuditService:
    def __init__(self, *, data_dir: Path | None = None) -> None:
        root = Path(data_dir) if data_dir else DATA_DIR
        self._path = root / AUDIT_FILE_NAME
        self._lock = threading.Lock()

    def _rotate_if_needed(self) -> None:
        try:
            if self._path.is_file() and self._path.stat().st_size > ROTATE_BYTES:
                os.replace(self._path, self._path.with_suffix(".jsonl.1"))
        except OSError:
            pass

    def record(
        self,
        action: str,
        *,
        target: str = "",
        detail: dict[str, Any] | None = None,
        actor: str = "admin",
    ) -> None:
        entry = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "actor": str(actor or "admin"),
            "action": str(action or "").strip()[:80],
            "target": str(target or "")[:200],
            "detail": detail or {},
        }
        with self._lock:
            self._rotate_if_needed()
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def list(self, *, limit: int = 100, action: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 100), MAX_LIMIT))
        prefix = str(action or "").strip()
        if not self._path.is_file():
            return []
        items: list[dict[str, Any]] = []
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if prefix and not str(entry.get("action") or "").startswith(prefix):
                        continue
                    items.append(entry)
        except OSError:
            return []
        return items[-limit:][::-1]


audit_service = AuditService()
