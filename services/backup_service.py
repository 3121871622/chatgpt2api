from __future__ import annotations

import io
import json
import os
import random
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from services.r2_client import CloudflareR2Client as SharedCloudflareR2Client, R2Error
from sqlalchemy import make_url

from services.application_database import database_backend_name
from services.config import DATA_DIR, config
from services.image_storage_service import IMAGE_INDEX_FILE
from services.image_tags_service import TAGS_FILE
from services.storage.coordination_repository import BackupExecutionStateRepository


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_backup_state(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    status = _clean(source.get("last_status")) or "idle"
    if status not in {"idle", "running", "success", "error"}:
        status = "idle"
    return {
        "last_started_at": _clean(source.get("last_started_at")) or None,
        "last_finished_at": _clean(source.get("last_finished_at")) or None,
        "last_status": status,
        "last_error": _clean(source.get("last_error")) or None,
        "last_object_key": _clean(source.get("last_object_key")) or None,
    }


def _is_backup_object(key: object) -> bool:
    name = _clean(key).rsplit("/", 1)[-1]
    return name.startswith("backup-") and (name.endswith(".tar.gz") or name.endswith(".tar.gz.enc"))


def _openssl_encrypt(data: bytes, passphrase: str) -> bytes:
    env = dict(os.environ)
    env["CHATGPT2API_BACKUP_PASSPHRASE"] = passphrase
    try:
        result = subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-md",
                "sha256",
                "-pass",
                "env:CHATGPT2API_BACKUP_PASSPHRASE",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise BackupError("当前环境缺少 openssl，无法执行加密备份") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise BackupError(f"加密备份失败：{detail or 'openssl 执行失败'}") from exc
    return result.stdout


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


class BackupError(RuntimeError):
    pass


class CloudflareR2Client(SharedCloudflareR2Client):
    def __init__(self, settings: dict[str, object]) -> None:
        settings = dict(settings)
        settings["prefix"] = _clean(settings.get("prefix")) or "backups"
        super().__init__(settings, timeout=60.0)

    def list_objects(self) -> list[dict[str, object]]:
        try:
            return super().list_objects(prefix=f"{self.prefix.rstrip('/')}/")
        except R2Error as exc:
            raise BackupError(str(exc)) from exc

    def validate(self) -> None:
        try:
            super().validate()
        except R2Error as exc:
            raise BackupError(str(exc)) from exc

    def test_connection(self) -> dict[str, object]:
        try:
            return super().test_connection()
        except R2Error as exc:
            raise BackupError(str(exc)) from exc

    def upload_bytes(self, *args, **kwargs):
        try:
            return super().upload_bytes(*args, **kwargs)
        except R2Error as exc:
            raise BackupError(str(exc)) from exc

    def delete_object(self, *args, **kwargs) -> None:
        try:
            super().delete_object(*args, **kwargs)
        except R2Error as exc:
            raise BackupError(str(exc)) from exc


class BackupService:
    def __init__(
        self,
        *,
        repository: BackupExecutionStateRepository | None = None,
        database_url: str | None = None,
    ) -> None:
        if repository is not None and database_url is not None:
            raise ValueError("provide repository or database_url, not both")
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._repository = repository or BackupExecutionStateRepository(database_url)
        self._recover_interrupted_execution()

    def _recover_interrupted_execution(self) -> None:
        try:
            with self._repository.run_lock(timeout_seconds=0):
                state = _normalize_backup_state(self._repository.load())
                if state["last_status"] != "running":
                    return
                state.update({
                    "last_finished_at": _iso_now(),
                    "last_status": "error",
                    "last_error": "备份执行被进程重启中断",
                })
                self._repository.replace(state)
        except TimeoutError:
            # Another process still owns the backup execution lease.
            return

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="r2-backup-scheduler")
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_scheduled_backup_if_needed()
            except Exception:
                pass
            self._stop_event.wait(30)

    def run_scheduled_backup_if_needed(self) -> None:
        settings = config.get_backup_settings()
        if not settings.get("enabled"):
            return
        state = self.get_status()
        if state.get("running"):
            return
        interval_minutes = int(settings.get("interval_minutes") or 360)
        last_finished_raw = _clean(state.get("last_finished_at"))
        if last_finished_raw:
            try:
                last_finished = datetime.fromisoformat(last_finished_raw.replace("Z", "+00:00"))
                elapsed = (_utc_now() - last_finished.astimezone(UTC)).total_seconds()
                if elapsed < interval_minutes * 60:
                    return
            except Exception:
                pass
        self.run_backup(trigger="schedule")

    def get_status(self) -> dict[str, object]:
        state = _normalize_backup_state(self._repository.load())
        return {**state, "running": self._running or state["last_status"] == "running"}

    def is_configured(self) -> bool:
        settings = config.get_backup_settings()
        return all([
            _clean(settings.get("account_id")),
            _clean(settings.get("access_key_id")),
            _clean(settings.get("secret_access_key")),
            _clean(settings.get("bucket")),
        ])

    def get_settings(self) -> dict[str, object]:
        settings = dict(config.get_backup_settings())
        settings["secret_access_key"] = "********" if _clean(settings.get("secret_access_key")) else ""
        settings["passphrase"] = "********" if _clean(settings.get("passphrase")) else ""
        return settings

    def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        current = config.get_backup_settings()
        merged = dict(current)
        merged.update(dict(payload or {}))
        if "include" in payload and isinstance(payload.get("include"), dict):
            include = dict(current.get("include") or {})
            include.update(payload.get("include") or {})
            merged["include"] = include
        if payload.get("secret_access_key") == "********":
            merged["secret_access_key"] = current.get("secret_access_key")
        if payload.get("passphrase") == "********":
            merged["passphrase"] = current.get("passphrase")
        updated = config.update({"backup": merged})
        return dict(updated.get("backup") or {})

    def test_connection(self) -> dict[str, object]:
        client = CloudflareR2Client(config.get_backup_settings())
        try:
            return client.test_connection()
        finally:
            client.close()

    def list_backups(self) -> list[dict[str, object]]:
        if not self.is_configured():
            return []
        client = CloudflareR2Client(config.get_backup_settings())
        try:
            items = client.list_objects()
        finally:
            client.close()
        parsed: list[dict[str, object]] = []
        for item in items:
            key = _clean(item.get("key"))
            name = key.rsplit("/", 1)[-1]
            encrypted = name.endswith(".enc")
            parsed.append({
                "key": key,
                "name": name,
                "size": int(item.get("size") or 0),
                "updated_at": item.get("updated_at"),
                "encrypted": encrypted,
            })
        return parsed

    def delete_backup(self, key: str) -> None:
        candidate = _clean(key)
        if not candidate:
            raise BackupError("备份对象 key 不能为空")
        client = CloudflareR2Client(config.get_backup_settings())
        try:
            client.delete_object(candidate)
        finally:
            client.close()

    def run_backup(self, *, trigger: str = "manual") -> dict[str, object]:
        try:
            with self._repository.run_lock(timeout_seconds=0):
                with self._lock:
                    current = self.get_status()
                    if self._running:
                        raise BackupError("当前已有备份任务正在执行")
                    started_at = _iso_now()
                    self._running = True
                    self._repository.replace({
                        "last_started_at": started_at,
                        "last_finished_at": current.get("last_finished_at"),
                        "last_status": "running",
                        "last_error": None,
                        "last_object_key": current.get("last_object_key"),
                    })
                try:
                    result = self._run_backup_once(trigger=trigger)
                    self._repository.replace({
                        "last_started_at": started_at,
                        "last_finished_at": _iso_now(),
                        "last_status": "success",
                        "last_error": None,
                        "last_object_key": result["key"],
                    })
                    return result
                except Exception as exc:
                    self._repository.replace({
                        "last_started_at": started_at,
                        "last_finished_at": _iso_now(),
                        "last_status": "error",
                        "last_error": str(exc) or exc.__class__.__name__,
                        "last_object_key": current.get("last_object_key"),
                    })
                    raise
                finally:
                    self._running = False
        except TimeoutError as exc:
            raise BackupError("当前已有备份任务正在执行") from exc

    def _run_backup_once(self, *, trigger: str) -> dict[str, object]:
        settings = config.get_backup_settings()
        client = CloudflareR2Client(settings)
        client.validate()
        payload_raw = self._build_backup_archive(settings, trigger=trigger)
        encrypted = bool(settings.get("encrypt"))
        if encrypted:
            passphrase = _clean(settings.get("passphrase"))
            if not passphrase:
                raise BackupError("已启用备份加密，但未设置加密口令")
            payload = _openssl_encrypt(payload_raw, passphrase)
            suffix = ".tar.gz.enc"
        else:
            payload = payload_raw
            suffix = ".tar.gz"
        timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        random_tag = f"{random.randint(0, 0xFFFF):04x}"
        object_key = f"{client.prefix.rstrip('/')}/backup-{timestamp}-{random_tag}{suffix}"
        metadata = {
            "created-at": _iso_now(),
            "encrypted": "true" if encrypted else "false",
            "trigger": trigger,
        }
        try:
            result = client.upload_bytes(object_key, payload, content_type="application/octet-stream", metadata=metadata)
            self._apply_rotation(client, int(settings.get("rotation_keep") or 0))
            return {
                "key": result["key"],
                "size": len(payload),
                "encrypted": encrypted,
            }
        finally:
            client.close()

    def _apply_rotation(self, client: CloudflareR2Client, keep: int) -> None:
        if keep <= 0:
            return
        items = [item for item in client.list_objects() if _is_backup_object(item.get("key"))]
        if len(items) <= keep:
            return
        for item in items[keep:]:
            key = _clean(item.get("key"))
            if key:
                client.delete_object(key)

    def _build_backup_archive(self, settings: dict[str, object], *, trigger: str) -> bytes:
        include = settings.get("include") if isinstance(settings.get("include"), dict) else {}
        database_backend = database_backend_name(self._repository.database_url)
        metadata = {
            "version": 3,
            "created_at": _iso_now(),
            "trigger": trigger,
            "app_version": config.app_version,
            "database_backend": database_backend,
            "application_database": config.get_storage_backend().get_backend_info(),
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            self._add_bytes_to_archive(archive, "backup-metadata.json", _json_bytes(metadata))
            database_name = (
                "data/application-database.sqlite3"
                if database_backend == "sqlite"
                else "data/application-database.pgdump"
            )
            self._add_bytes_to_archive(
                archive,
                database_name,
                self._application_database_backup(database_backend),
            )
            if include.get("image_tasks"):
                self._add_file_to_archive(archive, DATA_DIR / "image_tasks.json", "data/image_tasks.json")
                self._add_file_to_archive(archive, IMAGE_INDEX_FILE, "data/image_index.json")
            if include.get("editable_files"):
                self._add_directory_to_archive(archive, DATA_DIR / "files", "data/files")
            if include.get("images"):
                self._add_file_to_archive(archive, TAGS_FILE, "data/image_tags.json")
                self._add_directory_to_archive(archive, config.images_dir, "data/images")
        return buffer.getvalue()

    def _application_database_backup(self, backend: str) -> bytes:
        if backend == "sqlite":
            return self._sqlite_database_backup()
        if backend == "postgresql":
            return self._postgresql_database_backup()
        raise BackupError(f"不支持备份数据库类型：{backend}")

    def _sqlite_database_backup(self) -> bytes:
        temporary_path = ""
        raw_connection = self._repository.engine.raw_connection()
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as temporary:
                temporary_path = temporary.name
            destination = sqlite3.connect(temporary_path)
            try:
                raw_connection.driver_connection.backup(destination)
            finally:
                destination.close()
            return Path(temporary_path).read_bytes()
        except Exception as exc:
            raise BackupError("创建 SQLite Application Database 快照失败") from exc
        finally:
            raw_connection.close()
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)

    def _postgresql_database_backup(self) -> bytes:
        url = make_url(self._repository.database_url)
        command = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
        if url.host:
            command.extend(["--host", url.host])
        if url.port:
            command.extend(["--port", str(url.port)])
        if url.username:
            command.extend(["--username", url.username])
        if url.database:
            command.extend(["--dbname", url.database])

        env = dict(os.environ)
        if url.password:
            env["PGPASSWORD"] = url.password
        sslmode = str(url.query.get("sslmode") or "").strip()
        if sslmode:
            env["PGSSLMODE"] = sslmode
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise BackupError("当前环境缺少 pg_dump，无法备份 PostgreSQL") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise BackupError(f"PostgreSQL 备份失败：{detail or 'pg_dump 执行失败'}") from exc
        if not result.stdout:
            raise BackupError("PostgreSQL 备份失败：pg_dump 未输出数据")
        return result.stdout

    def _add_bytes_to_archive(self, archive: tarfile.TarFile, name: str, payload: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        info.mtime = int(_utc_now().timestamp())
        archive.addfile(info, io.BytesIO(payload))

    def _add_file_to_archive(self, archive: tarfile.TarFile, source: Path, arcname: str) -> None:
        if not source.exists() or not source.is_file():
            return
        archive.add(source, arcname=arcname)

    def _add_directory_to_archive(self, archive: tarfile.TarFile, source_dir: Path, arcname_root: str) -> None:
        if not source_dir.exists() or not source_dir.is_dir():
            return
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source_dir).as_posix()
                archive.add(path, arcname=f"{arcname_root}/{relative}")


backup_service = BackupService()
