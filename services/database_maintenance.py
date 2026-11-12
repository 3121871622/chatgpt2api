"""One-click application-database repair and optimisation.

The control plane runs unattended for months, so the database accumulates free
pages (fragmentation), stale planner statistics and, on SQLite, a growing WAL
file.  Left alone that shows up as slow queries, "database is locked" bursts and
eventually a corrupt page after a hard kill.

This service exposes the maintenance an operator should not have to know about:

* **check**    - ``integrity_check`` / ``quick_check`` plus fragmentation stats;
* **optimise** - ``ANALYZE`` + ``REINDEX`` (+ ``PRAGMA optimize`` on SQLite);
* **compact**  - ``VACUUM`` and a WAL checkpoint on SQLite, ``VACUUM ANALYZE``
  on PostgreSQL;
* **repair**   - check first, then optimise and compact only when the database
  reports a problem or fragmentation is above the operational threshold.

Every step is recorded with its duration so the UI can show what actually ran.
"""

from __future__ import annotations

import time
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from sqlalchemy import text

from services.application_database import (
    create_database_engine,
    database_backend_name,
    display_database_url,
    resolve_database_url,
)

OPERATION_CHECK = "check"
OPERATION_OPTIMIZE = "optimize"
OPERATION_COMPACT = "compact"
OPERATION_REPAIR = "repair"
OPERATION_REINDEX = "reindex"

ALL_OPERATIONS = (
    OPERATION_CHECK,
    OPERATION_OPTIMIZE,
    OPERATION_REINDEX,
    OPERATION_COMPACT,
    OPERATION_REPAIR,
)

# Above this share of free pages the table is compacted during a repair.
FRAGMENTATION_THRESHOLD_PERCENT = 20.0
_SLOW_STEP_SECONDS = 2.0


class DatabaseMaintenanceError(RuntimeError):
    pass


class DatabaseMaintenanceService:
    """Serialised maintenance runner for the configured application database."""

    def __init__(self, *, data_dir: Path | None = None) -> None:
        self._lock = Lock()
        self._data_dir = data_dir
        self._last_run: dict[str, Any] | None = None

    # --------------------------------------------------------------- public

    def status(self) -> dict[str, Any]:
        database_url = self._database_url()
        backend = database_backend_name(database_url)
        payload: dict[str, Any] = {
            "backend": backend,
            "database": display_database_url(database_url),
            "busy": self._lock.locked(),
            "last_run": self._last_run,
            "thresholds": {"fragmentation_percent": FRAGMENTATION_THRESHOLD_PERCENT},
        }
        payload.update(self._size_stats(database_url, backend))
        return payload

    def run(self, operations: Iterable[str] | None = None) -> dict[str, Any]:
        requested = _normalise_operations(operations)
        if not self._lock.acquire(blocking=False):
            raise DatabaseMaintenanceError("database maintenance is already running")
        try:
            started = time.perf_counter()
            database_url = self._database_url()
            backend = database_backend_name(database_url)
            before = self._size_stats(database_url, backend)
            steps: list[dict[str, Any]] = []
            errors: list[str] = []
            integrity = None

            try:
                if OPERATION_CHECK in requested or OPERATION_REPAIR in requested:
                    integrity, check_step = self._integrity_check(database_url, backend)
                    steps.append(check_step)
                if OPERATION_OPTIMIZE in requested:
                    steps.extend(self._optimize(database_url, backend))
                if OPERATION_REINDEX in requested:
                    steps.extend(self._reindex(database_url, backend))
                if OPERATION_REPAIR in requested:
                    if integrity is not None and integrity.get("ok") is False:
                        steps.extend(self._optimize(database_url, backend))
                    steps.extend(self._reindex(database_url, backend))
                needs_compaction = (
                    OPERATION_COMPACT in requested
                    or OPERATION_REPAIR in requested
                    or (
                        before.get("fragmentation_percent") is not None
                        and float(before["fragmentation_percent"]) >= FRAGMENTATION_THRESHOLD_PERCENT
                    )
                )
                if needs_compaction:
                    steps.extend(self._compact(database_url, backend))
            except Exception as exc:  # noqa: BLE001 - surfaced to the operator
                errors.append(f"{type(exc).__name__}: {exc}")

            after = self._size_stats(database_url, backend)
            result = {
                "ok": not errors,
                "operations": requested,
                "backend": backend,
                "database": display_database_url(database_url),
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "integrity": integrity,
                "steps": steps,
                "errors": errors,
                "before": before,
                "after": after,
                "freed_bytes": max(
                    0,
                    int(before.get("size_bytes") or 0) - int(after.get("size_bytes") or 0),
                ),
                "fragmentation_percent": after.get("fragmentation_percent"),
            }
            self._last_run = result
            return result
        finally:
            self._lock.release()

    # ------------------------------------------------------------- internals

    def _database_url(self) -> str:
        if self._data_dir is not None:
            return resolve_database_url(self._data_dir)
        return resolve_database_url()

    def _engine(self, database_url: str):
        return create_database_engine(database_url)

    def _autocommit_connection(self, database_url: str):
        engine = self._engine(database_url)
        connection = engine.connect()
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        return connection

    def _integrity_check(self, database_url: str, backend: str) -> tuple[dict[str, Any], dict[str, Any]]:
        started = time.perf_counter()
        rows: list[str] = []
        with self._autocommit_connection(database_url) as connection:
            if backend == "sqlite":
                rows = [str(row[0]) for row in connection.execute(text("PRAGMA integrity_check"))]
            else:
                connection.execute(text("SELECT 1"))
                rows = ["ok"]
        ok = bool(rows) and rows[0].lower() == "ok"
        result = {
            "ok": ok,
            "detail": "; ".join(rows[:5]) if not ok else "ok",
        }
        return result, {
            "operation": OPERATION_CHECK,
            "ok": ok,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "detail": result["detail"],
        }

    def _optimize(self, database_url: str, backend: str) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        statements = (
            ["PRAGMA optimize", "ANALYZE"]
            if backend == "sqlite"
            else ["ANALYZE"]
        )
        for statement in statements:
            steps.append(self._execute(database_url, statement, OPERATION_OPTIMIZE))
        return steps

    def _reindex(self, database_url: str, backend: str) -> list[dict[str, Any]]:
        if backend == "sqlite":
            return [self._execute(database_url, "REINDEX", OPERATION_REINDEX)]
        steps: list[dict[str, Any]] = []
        tables = self._postgres_tables(database_url)
        for table in tables:
            steps.append(
                self._execute(
                    database_url,
                    f'REINDEX TABLE CONCURRENTLY IF EXISTS "{table}"',
                    OPERATION_REINDEX,
                )
            )
        return steps or [{"operation": OPERATION_REINDEX, "ok": True, "duration_ms": 0, "detail": "no tables"}]

    def _compact(self, database_url: str, backend: str) -> list[dict[str, Any]]:
        if backend == "sqlite":
            steps = [
                self._execute(database_url, "VACUUM", OPERATION_COMPACT),
                self._execute(database_url, "PRAGMA wal_checkpoint(TRUNCATE)", OPERATION_COMPACT),
            ]
            return steps
        return [
            self._execute(database_url, "VACUUM (ANALYZE)", OPERATION_COMPACT),
        ]

    def _execute(self, database_url: str, statement: str, operation: str) -> dict[str, Any]:
        started = time.perf_counter()
        step: dict[str, Any] = {"operation": operation, "statement": statement}
        try:
            with self._autocommit_connection(database_url) as connection:
                connection.execute(text(statement))
        except Exception as exc:  # noqa: BLE001 - a failed optimisation is reported
            step.update({
                "ok": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "detail": f"{type(exc).__name__}: {exc}"[:300],
            })
            return step
        duration_ms = int((time.perf_counter() - started) * 1000)
        step.update({
            "ok": True,
            "duration_ms": duration_ms,
            "detail": "slow" if duration_ms / 1000 >= _SLOW_STEP_SECONDS else "",
        })
        return step

    def _postgres_tables(self, database_url: str) -> list[str]:
        try:
            with self._autocommit_connection(database_url) as connection:
                rows = connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = current_schema()"
                    )
                )
                return [str(row[0]) for row in rows]
        except Exception:
            return []

    def _size_stats(self, database_url: str, backend: str) -> dict[str, Any]:
        if backend == "sqlite":
            return self._sqlite_stats(database_url)
        return self._postgres_stats(database_url)

    def _sqlite_database_file(self, database_url: str) -> Path | None:
        prefix = "sqlite:///"
        if not database_url.startswith(prefix):
            return None
        raw = database_url[len(prefix):]
        # SQLAlchemy URL-encodes non-ASCII paths.
        from urllib.parse import unquote

        return Path(unquote(raw))

    def _sqlite_stats(self, database_url: str) -> dict[str, Any]:
        path = self._sqlite_database_file(database_url)
        stats: dict[str, Any] = {
            "size_bytes": 0,
            "wal_size_bytes": 0,
            "page_count": 0,
            "free_pages": 0,
            "fragmentation_percent": None,
        }
        if path is None:
            return stats
        try:
            if path.exists():
                stats["size_bytes"] = path.stat().st_size
            wal = path.with_name(path.name + "-wal")
            if wal.exists():
                stats["wal_size_bytes"] = wal.stat().st_size
        except OSError:
            pass
        try:
            with self._autocommit_connection(database_url) as connection:
                page_count = int(connection.execute(text("PRAGMA page_count")).scalar() or 0)
                free_pages = int(connection.execute(text("PRAGMA freelist_count")).scalar() or 0)
                page_size = int(connection.execute(text("PRAGMA page_size")).scalar() or 0)
            stats["page_count"] = page_count
            stats["free_pages"] = free_pages
            stats["page_size"] = page_size
            if page_count > 0:
                stats["fragmentation_percent"] = round(free_pages / page_count * 100, 2)
        except Exception:
            pass
        return stats

    def _postgres_stats(self, database_url: str) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "size_bytes": 0,
            "wal_size_bytes": 0,
            "fragmentation_percent": None,
        }
        try:
            with self._autocommit_connection(database_url) as connection:
                size = connection.execute(
                    text("SELECT pg_database_size(current_database())")
                ).scalar()
                stats["size_bytes"] = int(size or 0)
                dead = connection.execute(
                    text(
                        "SELECT COALESCE(SUM(n_dead_tup), 0) FROM pg_stat_user_tables"
                    )
                ).scalar()
                live = connection.execute(
                    text(
                        "SELECT COALESCE(SUM(n_live_tup), 0) FROM pg_stat_user_tables"
                    )
                ).scalar()
                total = float(dead or 0) + float(live or 0)
                if total > 0:
                    stats["fragmentation_percent"] = round(float(dead or 0) / total * 100, 2)
        except Exception:
            pass
        return stats


def _normalise_operations(operations: Iterable[str] | None) -> list[str]:
    if not operations:
        return [OPERATION_REPAIR]
    normalized: list[str] = []
    for item in operations:
        value = str(item or "").strip().lower()
        if value in ALL_OPERATIONS and value not in normalized:
            normalized.append(value)
    if not normalized:
        raise DatabaseMaintenanceError(
            "unknown maintenance operation; expected one of " + ", ".join(ALL_OPERATIONS)
        )
    return normalized


database_maintenance_service = DatabaseMaintenanceService()
