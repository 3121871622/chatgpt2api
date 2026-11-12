from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, Text, select
from sqlalchemy.orm import sessionmaker

from services.application_database import DatabaseBase, initialize_application_database, resolve_database_url


LEGACY_IMAGE_STORAGE_TARGET_ID = "legacy-default"
DEFAULT_IMAGE_STORAGE_PREFIX = "images"


def _clean(value: object) -> str:
    return str(value or "").strip()


def _positive_int(value: object, default: int, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    result = max(1, result)
    return min(result, maximum) if maximum is not None else result


def _target_id(value: object) -> str:
    raw = _clean(value).lower()
    if raw and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", raw):
        return raw
    return uuid4().hex


class ImageStorageTargetModel(DatabaseBase):
    __tablename__ = "image_storage_targets"

    id = Column(String(64), primary_key=True)
    name = Column(String(160), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    account_id = Column(String(128), nullable=False)
    bucket = Column(String(255), nullable=False)
    access_key_id = Column(String(256), nullable=False)
    secret_access_key = Column(Text, nullable=False)
    prefix = Column(String(512), nullable=False, default=DEFAULT_IMAGE_STORAGE_PREFIX)
    public_base_url = Column(Text, nullable=False, default="")
    signed_url_expiry_minutes = Column(Integer, nullable=False, default=60)
    upload_timeout_secs = Column(Integer, nullable=False, default=60)
    used_bytes = Column(BigInteger, nullable=False, default=0)
    object_count = Column(BigInteger, nullable=False, default=0)
    reserved_bytes = Column(BigInteger, nullable=False, default=0)
    capacity_checked_at = Column(DateTime(timezone=True), nullable=True)
    capacity_error = Column(Text, nullable=False, default="")
    health_status = Column(String(24), nullable=False, default="unknown")
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True, slots=True)
class ImageStorageTarget:
    id: str
    name: str
    enabled: bool
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    prefix: str
    public_base_url: str
    signed_url_expiry_minutes: int
    upload_timeout_secs: int
    used_bytes: int = 0
    object_count: int = 0
    reserved_bytes: int = 0
    capacity_checked_at: str | None = None
    capacity_error: str = ""
    health_status: str = "unknown"

    def client_settings(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
            "bucket": self.bucket,
            "prefix": self.prefix,
        }

    def public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("secret_access_key", None)
        value["has_secret_access_key"] = bool(self.secret_access_key)
        return value


class ImageStorageTargetRepository:
    """Owns image R2 target configuration, separate from the image catalog."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or resolve_database_url()
        self.engine = initialize_application_database(self.database_url)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    @staticmethod
    def _from_model(row: ImageStorageTargetModel) -> ImageStorageTarget:
        checked = row.capacity_checked_at
        if checked is not None and checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        return ImageStorageTarget(
            id=str(row.id),
            name=str(row.name or row.id),
            enabled=bool(row.enabled),
            account_id=str(row.account_id or ""),
            bucket=str(row.bucket or ""),
            access_key_id=str(row.access_key_id or ""),
            secret_access_key=str(row.secret_access_key or ""),
            prefix=str(row.prefix or DEFAULT_IMAGE_STORAGE_PREFIX),
            public_base_url=str(row.public_base_url or ""),
            signed_url_expiry_minutes=int(row.signed_url_expiry_minutes or 60),
            upload_timeout_secs=int(row.upload_timeout_secs or 60),
            used_bytes=int(row.used_bytes or 0),
            object_count=int(row.object_count or 0),
            reserved_bytes=int(row.reserved_bytes or 0),
            capacity_checked_at=checked.isoformat() if checked else None,
            capacity_error=str(row.capacity_error or ""),
            health_status=str(row.health_status or "unknown"),
        )

    @staticmethod
    def _normalize(payload: Mapping[str, object], *, existing: ImageStorageTarget | None = None) -> ImageStorageTarget:
        current = existing
        secret = _clean(payload.get("secret_access_key")) or (current.secret_access_key if current else "")
        account_id = _clean(payload.get("account_id")) or (current.account_id if current else "")
        bucket = _clean(payload.get("bucket")) or (current.bucket if current else "")
        access_key_id = _clean(payload.get("access_key_id")) or (current.access_key_id if current else "")
        if not account_id or not bucket or not access_key_id or not secret:
            raise ValueError("R2 目标必须填写 Account ID、Access Key ID、Secret Access Key 和 Bucket")
        prefix = _clean(payload.get("prefix")) or (current.prefix if current else DEFAULT_IMAGE_STORAGE_PREFIX)
        prefix = prefix.replace("\\", "/").strip("/") or DEFAULT_IMAGE_STORAGE_PREFIX
        if any(part in {".", ".."} for part in prefix.split("/")):
            raise ValueError("R2 对象前缀无效")
        url = _clean(payload.get("public_base_url")) if "public_base_url" in payload else (current.public_base_url if current else "")
        parsed_url = urlsplit(url) if url else None
        if url and (
            parsed_url is None
            or parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("R2 公开访问前缀必须是 HTTP(S) 地址")
        return ImageStorageTarget(
            id=_target_id(payload.get("id") or (current.id if current else "")),
            name=_clean(payload.get("name")) or (current.name if current else "R2 图片存储"),
            enabled=bool(payload.get("enabled", current.enabled if current else True)),
            account_id=account_id,
            bucket=bucket,
            access_key_id=access_key_id,
            secret_access_key=secret,
            prefix=prefix,
            public_base_url=url.rstrip("/"),
            signed_url_expiry_minutes=_positive_int(payload.get("signed_url_expiry_minutes", current.signed_url_expiry_minutes if current else 60), 60, 7 * 24 * 60),
            upload_timeout_secs=_positive_int(payload.get("upload_timeout_secs", current.upload_timeout_secs if current else 60), 60, 300),
            used_bytes=current.used_bytes if current else 0,
            object_count=current.object_count if current else 0,
            reserved_bytes=current.reserved_bytes if current else 0,
            capacity_checked_at=current.capacity_checked_at if current else None,
            capacity_error=current.capacity_error if current else "",
            health_status=current.health_status if current else "unknown",
        )

    def list(self, *, enabled_only: bool = False) -> list[ImageStorageTarget]:
        session = self.Session()
        try:
            statement = select(ImageStorageTargetModel).order_by(ImageStorageTargetModel.name, ImageStorageTargetModel.id)
            if enabled_only:
                statement = statement.where(ImageStorageTargetModel.enabled.is_(True))
            return [self._from_model(row) for row in session.scalars(statement).all()]
        finally:
            session.close()

    def get(self, target_id: str) -> ImageStorageTarget | None:
        session = self.Session()
        try:
            row = session.get(ImageStorageTargetModel, _clean(target_id))
            return self._from_model(row) if row else None
        finally:
            session.close()

    def save(self, payload: Mapping[str, object]) -> ImageStorageTarget:
        target_id = _clean(payload.get("id"))
        existing = self.get(target_id) if target_id else None
        item = self._normalize(payload, existing=existing)
        session = self.Session()
        try:
            row = session.get(ImageStorageTargetModel, item.id)
            now = datetime.now(timezone.utc)
            values = asdict(item)
            values.pop("capacity_checked_at", None)
            values.pop("capacity_error", None)
            values.pop("health_status", None)
            values.pop("used_bytes", None)
            values.pop("object_count", None)
            values.pop("reserved_bytes", None)
            if row is None:
                row = ImageStorageTargetModel(
                    **values,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
                row.updated_at = now
            session.commit()
            return self._from_model(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete(self, target_id: str) -> bool:
        if _clean(target_id) == LEGACY_IMAGE_STORAGE_TARGET_ID:
            raise ValueError("旧版默认 R2 目标不能删除")
        session = self.Session()
        try:
            row = session.get(ImageStorageTargetModel, _clean(target_id))
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ensure_legacy_target(self, settings: Mapping[str, object]) -> ImageStorageTarget | None:
        existing = self.get(LEGACY_IMAGE_STORAGE_TARGET_ID)
        if existing is not None:
            return existing
        values = {
            "id": LEGACY_IMAGE_STORAGE_TARGET_ID,
            "name": "旧版默认 R2",
            "enabled": True,
            "account_id": settings.get("r2_account_id"),
            "access_key_id": settings.get("r2_access_key_id"),
            "secret_access_key": settings.get("r2_secret_access_key"),
            "bucket": settings.get("r2_bucket"),
            "prefix": settings.get("r2_prefix") or DEFAULT_IMAGE_STORAGE_PREFIX,
            "public_base_url": settings.get("r2_public_base_url") or settings.get("public_base_url"),
        }
        if not all(_clean(values.get(key)) for key in ("account_id", "access_key_id", "secret_access_key", "bucket")):
            return None
        return self.save(values)

    def update_health(self, target_id: str, *, status: str, error: str = "") -> None:
        session = self.Session()
        try:
            row = session.get(ImageStorageTargetModel, _clean(target_id))
            if row is None:
                return
            row.health_status = _clean(status) or "unknown"
            row.capacity_error = _clean(error)
            row.capacity_checked_at = datetime.now(timezone.utc)
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()


image_storage_target_repository = ImageStorageTargetRepository()
