"""容器启动时自动发现并迁移旧版 chatgpt2api 数据。

由 ``deploy/docker-entrypoint.sh`` 在 uvicorn 启动之前调用，因此不会有两个进程同时
写同一个数据库。

行为
----
1. 自动扫描常见挂载点与 ``QINGMENG_LEGACY_DIRS`` 指定的目录，找出包含
   ``chatgpt2api.db`` 且账号数 > 0 的旧数据目录；
2. 新库不存在或为空时，以账号最多的旧库为基线生成快照，再把其余旧库的
   账号 / 授权密钥 / 调用日志 **合并**进去（按唯一键去重）；
3. 目标表为空时继承旧库的系统设置、代理组、账号组；
4. 复制图片、缩略图、任务记录、可编辑文件等数据文件；
5. 若旧库没有显式日志保留期，写入 8760 小时避免首次启动清理历史；
6. 写 ``.auto-migrate.json`` 标记，之后启动直接跳过。

安全
----
* 目标库已经有账号时 **绝不覆盖**，只记录一条跳过日志；
* 旧库始终以只读方式打开（SQLite ``mode=ro`` + 备份 API）；
* 失败只打印原因并返回 0，不阻塞服务启动；需要人工处理时可再用
  ``scripts/migrate_legacy_data.py``。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate_legacy_data import COPY_DIRS, COPY_FILES, _human  # noqa: E402

MARKER_NAME = ".auto-migrate.json"
DB_NAME = "chatgpt2api.db"

# 常见的旧数据挂载点；按顺序探测，不存在则跳过。
KNOWN_SOURCE_DIRS = (
    "/legacy",
    "/legacy-data",
    "/old-data",
    "/app/data",
    "/data/legacy",
)

LEGACY_TABLES = ("accounts", "call_records")


def _data_dir() -> Path:
    configured = (os.getenv("QINGMENG_DATA_DIR") or os.getenv("DATA_DIR") or "").strip()
    return Path(configured or "/data").resolve()


def _auto_migrate_enabled() -> bool:
    flag = (os.getenv("QINGMENG_AUTO_MIGRATE") or "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def _configured_sources() -> list[Path]:
    raw = os.getenv("QINGMENG_LEGACY_DIRS") or ""
    if not raw.strip():
        return []
    # Use the platform path separator: ":" on Linux, ";" on Windows (where a
    # drive letter contains ":"). Commas are accepted everywhere.
    separator = ";" if os.name == "nt" else ":"
    normalized = raw.replace(",", separator)
    return [Path(item.strip()) for item in normalized.split(separator) if item.strip()]


def _db_stats(path: Path) -> dict[str, int]:
    """只读读取一个候选库的规模。"""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        stats = {}
        for table in LEGACY_TABLES:
            if table not in tables:
                stats[table] = 0
                continue
            try:
                stats[table] = int(
                    connection.execute(f"select count(*) from {table}").fetchone()[0]
                )
            except sqlite3.Error:
                stats[table] = 0
        stats["has_settings"] = int("system_settings" in tables)
        return stats
    finally:
        connection.close()


def _discover(target_dir: Path) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in [*_configured_sources(), *(Path(item) for item in KNOWN_SOURCE_DIRS)]:
        try:
            directory = raw.expanduser().resolve()
        except OSError:
            continue
        db_path = directory / DB_NAME
        if not db_path.is_file():
            continue
        key = str(db_path)
        if key in seen:
            continue
        seen.add(key)
        # 目标目录本身不算迁移源：那是已经就位的数据。
        if db_path == (target_dir / DB_NAME).resolve():
            continue
        try:
            stats = _db_stats(db_path)
        except (sqlite3.Error, OSError) as exc:
            print(f"[auto-migrate] 跳过无法读取的库 {db_path}: {exc}")
            continue
        if stats.get("accounts", 0) <= 0:
            continue
        candidates.append(
            {
                "directory": directory,
                "db": db_path,
                "accounts": stats.get("accounts", 0),
                "call_records": stats.get("call_records", 0),
            }
        )
    candidates.sort(key=lambda item: int(item["accounts"]), reverse=True)
    return candidates


def _snapshot(source_db: Path, target_db: Path) -> None:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
    try:
        destination = sqlite3.connect(str(target_db), timeout=30)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _table_is_empty(connection: sqlite3.Connection, table: str) -> bool:
    try:
        return connection.execute(f"select count(*) from {table}").fetchone()[0] == 0
    except sqlite3.Error:
        return True


def _merge(source_db: Path, target_db: Path) -> dict[str, int]:
    """把一旧库的账号/密钥/日志合并进目标库；空表继承设置与代理。"""
    merged = {"accounts": 0, "auth_keys": 0, "call_records": 0, "settings": 0, "proxy": 0, "groups": 0}
    connection = sqlite3.connect(str(target_db), timeout=120)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ATTACH DATABASE ? AS legacy", (str(source_db),))
        tables = {
            row[0]
            for row in connection.execute("select name from legacy.sqlite_master where type='table'")
        }

        before = connection.execute("select count(*) from accounts").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO accounts (access_token, data) "
            "SELECT access_token, data FROM legacy.accounts"
        )
        after = connection.execute("select count(*) from accounts").fetchone()[0]
        merged["accounts"] = after - before

        if "auth_keys" in tables:
            before = connection.execute("select count(*) from auth_keys").fetchone()[0]
            connection.execute(
                "INSERT OR IGNORE INTO auth_keys (key_id, data) "
                "SELECT key_id, data FROM legacy.auth_keys"
            )
            merged["auth_keys"] = (
                connection.execute("select count(*) from auth_keys").fetchone()[0] - before
            )

        if "call_records" in tables:
            before = connection.execute("select count(*) from call_records").fetchone()[0]
            target_columns = [
                row[1] for row in connection.execute("pragma table_info(call_records)")
                if row[1] != "sequence"
            ]
            legacy_columns = {
                row[1] for row in connection.execute("pragma legacy.table_info(call_records)")
            }
            shared = [column for column in target_columns if column in legacy_columns]
            if shared:
                column_list = ", ".join(shared)
                connection.execute(
                    f"INSERT OR IGNORE INTO call_records ({column_list}) "
                    f"SELECT {column_list} FROM legacy.call_records"
                )
            merged["call_records"] = (
                connection.execute("select count(*) from call_records").fetchone()[0] - before
            )

        # 设置/代理/账号组：目标为空时继承（避免覆盖新实例已配置的内容）。
        if "system_settings" in tables and _table_is_empty(connection, "system_settings"):
            connection.execute(
                "INSERT INTO system_settings (data) SELECT data FROM legacy.system_settings"
            )
            merged["settings"] = 1
        elif "system_settings" in tables:
            # 目标已有设置：只补充新实例没有的键（旧值优先保留目标）。
            row = connection.execute("select rowid, data from system_settings limit 1").fetchone()
            legacy_row = connection.execute(
                "select data from legacy.system_settings limit 1"
            ).fetchone()
            if row and legacy_row:
                current = json.loads(row[1])
                inherited = json.loads(legacy_row[0])
                changed = False
                for key, value in inherited.items():
                    if key not in current:
                        current[key] = value
                        changed = True
                if changed:
                    connection.execute(
                        "update system_settings set data = ? where rowid = ?",
                        (json.dumps(current, ensure_ascii=False), row[0]),
                    )
                    merged["settings"] = 1

        if "proxy_configuration" in tables and _table_is_empty(connection, "proxy_configuration"):
            connection.execute(
                "INSERT INTO proxy_configuration (data) SELECT data FROM legacy.proxy_configuration"
            )
            merged["proxy"] = 1
        if (
            "account_group_configuration" in tables
            and _table_is_empty(connection, "account_group_configuration")
        ):
            connection.execute(
                "INSERT INTO account_group_configuration (data) "
                "SELECT data FROM legacy.account_group_configuration"
            )
            merged["groups"] = 1

        # 数据变了就推进 revision，确保读取方看到新版本。
        connection.execute(
            "UPDATE storage_revisions SET version = version + 1 "
            "WHERE collection IN ('accounts', 'auth_keys')"
        )
        connection.commit()
        connection.execute("DETACH DATABASE legacy")
        return merged
    finally:
        connection.close()


def _copy_files(source_dir: Path, target_dir: Path) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in COPY_FILES:
        src = source_dir / name
        if not src.is_file():
            continue
        dst = target_dir / name
        try:
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                continue
            dst.write_bytes(src.read_bytes())
            copied += 1
        except OSError as exc:
            print(f"[auto-migrate] 复制 {name} 失败: {exc}")
    for name in COPY_DIRS:
        src = source_dir / name
        if not src.is_dir():
            continue
        try:
            for path in src.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(src)
                destination = target_dir / name / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and destination.stat().st_size == path.stat().st_size:
                    continue
                destination.write_bytes(path.read_bytes())
                copied += 1
        except OSError as exc:
            print(f"[auto-migrate] 复制目录 {name} 失败: {exc}")
    return copied


def _freeze_retention(target_db: Path, hours: int = 8760) -> int | None:
    connection = sqlite3.connect(str(target_db), timeout=30)
    try:
        row = connection.execute("select rowid, data from system_settings limit 1").fetchone()
        if row is None:
            return None
        settings = json.loads(row[1])
        if "log_retention_hours" in settings and settings["log_retention_hours"]:
            return int(settings["log_retention_hours"])
        settings["log_retention_hours"] = hours
        connection.execute(
            "update system_settings set data = ? where rowid = ?",
            (json.dumps(settings, ensure_ascii=False), row[0]),
        )
        connection.commit()
        return hours
    except (sqlite3.Error, ValueError) as exc:
        print(f"[auto-migrate] 冻结日志保留期失败: {exc}")
        return None
    finally:
        connection.close()


def run(*, merge_into_existing: bool = False) -> int:
    data_dir = _data_dir()
    target_db = data_dir / DB_NAME
    marker = data_dir / MARKER_NAME

    if not _auto_migrate_enabled():
        print("[auto-migrate] 已通过 QINGMENG_AUTO_MIGRATE=0 关闭")
        return 0

    sources = _discover(data_dir)
    if not sources:
        print("[auto-migrate] 未发现旧版数据库，跳过")
        return 0

    target_accounts = 0
    if target_db.is_file():
        try:
            target_accounts = int(_db_stats(target_db).get("accounts", 0))
        except (sqlite3.Error, OSError):
            target_accounts = 0

    print(
        f"[auto-migrate] 发现 {len(sources)} 个旧库，目标库账号数={target_accounts}："
        + ", ".join(
            f"{item['db']}({item['accounts']} 账号/{item['call_records']} 日志)"
            for item in sources
        )
    )

    if target_accounts > 0 and not merge_into_existing:
        print("[auto-migrate] 目标库已有账号，跳过自动迁移（不会覆盖现有数据）")
        print("[auto-migrate] 如需把其它旧库并入现有库，请执行：python auto_migrate_legacy.py --merge")
        return 0

    started = time.perf_counter()
    try:
        if target_accounts == 0:
            base = sources[0]
            _snapshot(Path(str(base["db"])), target_db)
            print(f"[auto-migrate] 已以 {base['db']} 为基线生成快照")
        else:
            print("[auto-migrate] 目标库已有账号：仅做增量合并，不覆盖现有数据")
        summary = []
        for item in sources:
            merged = _merge(Path(str(item["db"])), target_db)
            copied = _copy_files(Path(str(item["directory"])), data_dir)
            summary.append(
                {
                    "source": str(item["db"]),
                    "source_accounts": item["accounts"],
                    "source_call_records": item["call_records"],
                    "merged": merged,
                    "files_copied": copied,
                }
            )
            print(
                f"[auto-migrate] 合并 {item['db']}: +{merged['accounts']} 账号, "
                f"+{merged['call_records']} 日志, 文件 {copied} 个"
            )
        retention = _freeze_retention(target_db)
        final_accounts = int(_db_stats(target_db).get("accounts", 0))
        duration = time.perf_counter() - started
        previous_marker: dict[str, object] = {}
        if marker.is_file():
            try:
                previous_marker = json.loads(marker.read_text(encoding="utf-8"))
            except ValueError:
                previous_marker = {}
        history = list(previous_marker.get("runs") or [])
        run_entry = {
            "migrated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": round(duration, 1),
            "final_accounts": final_accounts,
            "retention_hours": retention,
            "sources": summary,
        }
        history.append(run_entry)
        marker.write_text(
            json.dumps(
                {
                    **run_entry,
                    "merge_mode": bool(target_accounts),
                    "runs": history[-10:],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            f"[auto-migrate] 完成：账号 {final_accounts}，耗时 {duration:.1f}s，"
            f"日志保留期={retention}h，标记={marker}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - 迁移失败不阻塞服务启动
        print(f"[auto-migrate] 迁移失败（服务将继续以现有数据库启动）：{type(exc).__name__}: {exc}")
        return 1 if merge_into_existing else 0


if __name__ == "__main__":
    raise SystemExit(
        run(merge_into_existing="--merge" in sys.argv[1:])
    )
