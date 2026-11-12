"""从旧版 chatgpt2api 部署迁移数据到倾梦GPT v1.0.0。

两种目标后端：

1. SQLite（默认）：直接复制一份一致的数据库快照 + 数据目录文件，启动新实例即可。
2. PostgreSQL（``--database-url``）：读取旧 SQLite，通过新版 StorageBackend 写入目标库，
   同时复制图片/任务等文件。

用法示例：

    # 预演（只统计，不写任何东西）
    python scripts/migrate_legacy_data.py \
        --source /legacy/data --target /data --dry-run

    # SQLite 迁移（目标必须为空目录或加 --force，自动备份）
    python scripts/migrate_legacy_data.py \
        --source /legacy/data --target /data

    # 迁到 PostgreSQL
    python scripts/migrate_legacy_data.py \
        --source /legacy/data --target /data \
        --database-url postgresql://user:pass@host:5432/qingmeng

    # 迁移后校验：账号是否全部可加载、可调度
    python scripts/migrate_legacy_data.py --target /data --verify

迁移不会修改源目录（SQLite 以只读模式打开）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

LEGACY_DB_NAMES = ("chatgpt2api.db", "accounts.db")

# 数据目录里需要随库一起搬走的文件与目录（新版本只使用这些）。
COPY_FILES = (
    "config.json",
    "image_tasks.json",
    "image_index.json",
    "image_tags.json",
    "prompt_library.json",
    "editable_file_tasks.json",
    ".cumulative_total",
)
COPY_DIRS = (
    "images",
    "image_thumbnails",
    "image_task_spool",
    "files",
)

_SETTINGS_TABLES = ("system_settings",)
_ACCOUNT_GROUP_TABLES = ("account_group_configuration",)
_PROXY_TABLES = ("proxy_configuration",)


def _human(bytes_value: int) -> str:
    size = float(bytes_value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _find_source_db(source: Path) -> Path:
    for name in LEGACY_DB_NAMES:
        candidate = source / name
        if candidate.is_file():
            return candidate
    raise SystemExit(f"在 {source} 中找不到旧版数据库（{' / '.join(LEGACY_DB_NAMES)}）")


def _read_source(path: Path) -> dict[str, object]:
    """以只读方式读取旧库的账号/设置/代理概况。"""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table'"
            )
        }
        accounts = connection.execute("select count(*) from accounts").fetchone()[0]
        auth_keys = (
            connection.execute("select count(*) from auth_keys").fetchone()[0]
            if "auth_keys" in tables
            else 0
        )
        call_records = (
            connection.execute("select count(*) from call_records").fetchone()[0]
            if "call_records" in tables
            else 0
        )
        settings = None
        for table in _SETTINGS_TABLES:
            if table in tables:
                row = connection.execute(f"select * from {table} limit 1").fetchone()
                if row is not None:
                    settings = json.loads(dict(row)["data"])
                break
        proxy_groups = 0
        for table in _PROXY_TABLES:
            if table in tables:
                row = connection.execute(f"select * from {table} limit 1").fetchone()
                if row is not None:
                    payload = json.loads(dict(row)["data"])
                    proxy_groups = len(payload.get("proxy_groups") or [])
                break
        groups = 0
        for table in _ACCOUNT_GROUP_TABLES:
            if table in tables:
                row = connection.execute(f"select * from {table} limit 1").fetchone()
                if row is not None:
                    payload = json.loads(dict(row)["data"])
                    groups = len(payload.get("items") or [])
                break
        return {
            "tables": sorted(tables),
            "accounts": accounts,
            "auth_keys": auth_keys,
            "call_records": call_records,
            "settings_keys": sorted((settings or {}).keys()),
            "proxy_groups": proxy_groups,
            "account_groups": groups,
        }
    finally:
        connection.close()


def _copy_data_files(source: Path, target: Path, *, dry_run: bool) -> list[tuple[str, str]]:
    copied: list[tuple[str, str]] = []
    for name in COPY_FILES:
        src = source / name
        if not src.is_file():
            continue
        copied.append((name, _human(src.stat().st_size)))
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target / name)
    for name in COPY_DIRS:
        src = source / name
        if not src.is_dir():
            continue
        total = sum(item.stat().st_size for item in src.rglob("*") if item.is_file())
        count = sum(1 for item in src.rglob("*") if item.is_file())
        copied.append((f"{name}/ ({count} files)", _human(total)))
        if not dry_run:
            shutil.copytree(src, target / name, dirs_exist_ok=True)
    return copied


def _snapshot_sqlite(source_db: Path, target_db: Path, *, dry_run: bool) -> None:
    """用 SQLite 备份 API 生成一致性快照（含 WAL 中已提交的数据）。"""
    if dry_run:
        return
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(str(target_db))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _migrate_to_database_url(source_db: Path, database_url: str) -> dict[str, object]:
    """把旧 SQLite 的账号/设置/代理写入目标数据库（SQLite 或 PostgreSQL）。"""
    from services.storage.database_storage import DatabaseStorageBackend

    backend = DatabaseStorageBackend(database_url)
    connection = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table'"
            )
        }
        accounts = [
            json.loads(row["data"])
            for row in connection.execute("select data from accounts order by id")
        ]
        auth_keys = [
            json.loads(row["data"])
            for row in connection.execute("select data from auth_keys order by id")
        ] if "auth_keys" in tables else []

        result: dict[str, object] = {
            "accounts": len(accounts),
            "auth_keys": len(auth_keys),
        }
        if accounts:
            written = backend.replace_accounts(accounts)
            result["accounts_written"] = written.inserted + written.updated
        if auth_keys:
            written = backend.replace_auth_keys(auth_keys)
            result["auth_keys_written"] = written.inserted + written.updated
        return result
    finally:
        connection.close()


def _preserve_history(target_db: Path, hours: int) -> int | None:
    """Raise the log retention in the migrated settings before first boot.

    The application runs retention cleanup on startup.  A legacy install that
    had no explicit retention falls back to 24h, so importing months of call
    records and then starting the container would silently delete them.  The
    migration therefore freezes cleanup by writing a long retention window;
    the operator can lower it afterwards in the UI.
    """
    if hours <= 0 or not target_db.is_file():
        return None
    connection = sqlite3.connect(str(target_db))
    try:
        row = connection.execute("select rowid, data from system_settings limit 1").fetchone()
        if row is None:
            return None
        settings = json.loads(row[1])
        previous = settings.get("log_retention_hours")
        settings["log_retention_hours"] = int(hours)
        connection.execute(
            "update system_settings set data = ? where rowid = ?",
            (json.dumps(settings, ensure_ascii=False), row[0]),
        )
        connection.commit()
        return previous
    finally:
        connection.close()


def _verify(target: Path, database_url: str | None) -> int:
    """启动级校验：新代码能否完整加载旧数据。"""
    import os

    from services.account_service import AccountService
    from services.storage.database_storage import DatabaseStorageBackend

    if database_url:
        url = database_url
    else:
        db_path = target / "chatgpt2api.db"
        if not db_path.is_file():
            raise SystemExit(f"{db_path} 不存在")
        url = f"sqlite:///{db_path.as_posix()}"

    os.environ.setdefault("QINGMENG_AUTH_KEY", "verify-only")
    service = AccountService(DatabaseStorageBackend(url))
    total = service.account_count()
    index = service._selection_index.stats()
    statuses: dict[str, int] = {}
    no_credential = 0
    for account in service.iter_account_records():
        statuses[str(account.get("status"))] = statuses.get(str(account.get("status")), 0) + 1
        if not str(account.get("access_token") or ""):
            no_credential += 1
    print(f"目标数据库: {url}")
    print(f"可加载账号: {total}")
    print(f"状态分布: {statuses}")
    print(f"缺少 access_token 的账号: {no_credential}")
    print(f"调度索引: tracked={index['tracked']} image_ready={index['image_ready']} text_ready={index['text_ready']}")
    sample = next(iter(service.iter_account_records()), None)
    if sample:
        print(
            "样例:",
            json.dumps(
                {
                    key: sample.get(key)
                    for key in ("management_id", "status", "type", "source_type", "quota", "email")
                },
                ensure_ascii=False,
            ),
        )
    return 0 if total > 0 and no_credential == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", help="旧版数据目录（含 chatgpt2api.db）")
    parser.add_argument("--target", help="新实例数据目录（容器内通常是 /data）")
    parser.add_argument("--database-url", help="目标数据库 URL；留空则复制 SQLite")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    parser.add_argument("--force", action="store_true", help="目标已存在数据库时仍覆盖（自动备份）")
    parser.add_argument("--verify", action="store_true", help="只做启动级校验")
    parser.add_argument(
        "--keep-history-hours",
        type=int,
        default=8760,
        help="迁移后先把日志保留期设为该值，避免首次启动按旧保留期清理历史（默认 8760 小时；0 表示不改）",
    )
    args = parser.parse_args()

    if args.verify:
        if not args.target:
            parser.error("--verify 需要 --target 或 --database-url")
        return _verify(Path(args.target), args.database_url)

    if not args.source or not args.target:
        parser.error("需要 --source 与 --target")

    source = Path(args.source)
    target = Path(args.target)
    source_db = _find_source_db(source)
    summary = _read_source(source_db)

    print("=" * 70)
    print(f"源数据库: {source_db} ({_human(source_db.stat().st_size)})")
    print(f"账号: {summary['accounts']}  授权密钥: {summary['auth_keys']}  调用日志: {summary['call_records']}")
    print(f"代理组: {summary['proxy_groups']}  账号组: {summary['account_groups']}")
    print(f"设置项: {', '.join(summary['settings_keys'][:12])}{' ...' if len(summary['settings_keys']) > 12 else ''}")
    print(f"表({len(summary['tables'])}): {', '.join(summary['tables'])}")

    files = _copy_data_files(source, target, dry_run=True)
    if files:
        print("将一并复制:")
        for name, size in files:
            print(f"  - {name}  {size}")

    if args.dry_run:
        print("\n[dry-run] 未写入任何文件。")
        return 0

    if not args.force and not args.database_url:
        existing = target / "chatgpt2api.db"
        if existing.exists() and existing.stat().st_size > 0:
            raise SystemExit(
                f"{existing} 已存在。确认要覆盖请加 --force（会自动备份为 .bak-时间戳）"
            )

    target.mkdir(parents=True, exist_ok=True)
    if not args.database_url:
        existing = target / "chatgpt2api.db"
        if existing.exists():
            backup = existing.with_name(f"{existing.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.move(str(existing), str(backup))
            for suffix in ("-wal", "-shm"):
                sidecar = existing.with_name(existing.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            print(f"已备份原库: {backup}")

    started = time.perf_counter()
    if args.database_url:
        result = _migrate_to_database_url(source_db, args.database_url)
        print(f"已写入目标数据库: {result}")
        print(
            "注意：迁移到外部数据库时请在设置页把「日志保留小时」调大，"
            "否则首次启动的保留清理会删除超过保留期的历史日志。"
        )
    else:
        _snapshot_sqlite(source_db, target / "chatgpt2api.db", dry_run=False)
        print(f"已生成数据库快照: {target / 'chatgpt2api.db'}")
        previous = _preserve_history(target / "chatgpt2api.db", args.keep_history_hours)
        if previous is not None:
            print(
                f"日志保留期: {previous if previous is not None else '(未设置)'} -> "
                f"{args.keep_history_hours} 小时（避免首次启动清理历史，可在设置页调整）"
            )

    copied = _copy_data_files(source, target, dry_run=False)
    print(f"已复制数据文件: {len(copied)} 项")
    print(f"迁移耗时: {time.perf_counter() - started:.1f}s")
    print("\n下一步：重启服务，然后执行 --verify 或直接访问 /api/accounts 核对数量。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
