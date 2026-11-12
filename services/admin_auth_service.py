from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from services.config import DATA_DIR, config

LoginMode = Literal["key", "password"]

_PASSWORD_ITERATIONS = 260_000
_SESSION_TTL_SECONDS = 7 * 24 * 3600
_MIN_PASSWORD_LENGTH = 8
_DEFAULT_ADMIN_PATH = "/admin"
_RESERVED_ADMIN_PATHS = {
    "",
    "/",
    "/api",
    "/assets",
    "/images",
    "/login",
    "/admin-recover",
    "/favicon.ico",
    "/version",
}


def normalize_admin_path(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return _DEFAULT_ADMIN_PATH
    if not raw.startswith("/"):
        raw = "/" + raw
    raw = raw.rstrip("/") or "/"
    if raw in _RESERVED_ADMIN_PATHS:
        raise AdminAuthError(f"后台路径不能使用保留路径：{raw}")
    segments = raw.strip("/").split("/")
    for segment in segments:
        if not re.fullmatch(r"[A-Za-z0-9._~-]{1,64}", segment):
            raise AdminAuthError("后台路径只能包含字母、数字、点、下划线、~ 和 -")
        if segment in {".", ".."}:
            raise AdminAuthError("后台路径不能包含 . 或 ..")
    return raw


def _now() -> int:
    return int(time.time())


def _clean(value: object) -> str:
    return str(value or "").strip()


class AdminAuthError(ValueError):
    pass


class AdminAuthService:
    """Small, file-backed admin login policy and password verifier.

    The existing CHATGPT2API/QINGMENG auth key remains the recovery credential.
    Password sessions are stateless HMAC tokens so multiple workers can verify
    them without a shared session store.
    """

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._path = Path(data_dir) / "admin_auth.json"
        self._lock = Lock()
        self._cache: dict[str, Any] | None = None

    def _defaults(self) -> dict[str, Any]:
        return {
            "mode": "key",
            "username": "admin",
            "password_hash": "",
            "password_salt": "",
            "iterations": _PASSWORD_ITERATIONS,
            "session_secret": secrets.token_hex(32),
            "admin_path": _DEFAULT_ADMIN_PATH,
            "updated_at": _now(),
        }

    def _read(self) -> dict[str, Any]:
        if self._cache is not None:
            return dict(self._cache)
        state = self._defaults()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            for key in state:
                if key in raw:
                    state[key] = raw[key]
        if state.get("mode") not in {"key", "password"}:
            state["mode"] = "key"
        state["username"] = _clean(state.get("username")) or "admin"
        try:
            state["admin_path"] = normalize_admin_path(state.get("admin_path"))
        except AdminAuthError:
            state["admin_path"] = _DEFAULT_ADMIN_PATH
        if not _clean(state.get("session_secret")):
            state["session_secret"] = secrets.token_hex(32)
        self._cache = dict(state)
        return state

    def _write(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _now()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, self._path)
        self._cache = dict(state)

    @staticmethod
    def _hash_password(password: str, salt_hex: str, iterations: int) -> str:
        salt = bytes.fromhex(salt_hex)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            salt,
            max(10_000, int(iterations)),
        )
        return digest.hex()

    def view(self, *, include_admin_path: bool = False) -> dict[str, Any]:
        with self._lock:
            state = self._read()
        has_password = bool(_clean(state.get("password_hash")))
        result = {
            "mode": state.get("mode") or "key",
            "username": _clean(state.get("username")) or "admin",
            "has_password": has_password,
            "password_login_enabled": state.get("mode") == "password" and has_password,
            "recovery_key_enabled": bool(_clean(config.auth_key)),
        }
        if include_admin_path:
            result["admin_path"] = state.get("admin_path") or _DEFAULT_ADMIN_PATH
        return result

    def current_admin_path(self) -> str:
        with self._lock:
            state = self._read()
        return str(state.get("admin_path") or _DEFAULT_ADMIN_PATH)

    def update(
        self,
        *,
        mode: str,
        username: str,
        password: str | None = None,
        admin_path: str | None = None,
    ) -> dict[str, Any]:
        normalized_mode = _clean(mode).lower()
        if normalized_mode not in {"key", "password"}:
            raise AdminAuthError("登录方式只能是 key 或 password")
        normalized_username = _clean(username) or "admin"
        if len(normalized_username) > 64:
            raise AdminAuthError("管理员用户名过长")
        new_password = str(password or "")
        with self._lock:
            state = self._read()
            if new_password:
                if len(new_password) < _MIN_PASSWORD_LENGTH:
                    raise AdminAuthError(f"密码至少需要 {_MIN_PASSWORD_LENGTH} 位")
                salt = secrets.token_hex(16)
                state["password_salt"] = salt
                state["password_hash"] = self._hash_password(
                    new_password, salt, _PASSWORD_ITERATIONS
                )
                state["iterations"] = _PASSWORD_ITERATIONS
            if normalized_mode == "password" and not _clean(state.get("password_hash")):
                raise AdminAuthError("开启账号密码登录前请先设置密码")
            state["mode"] = normalized_mode
            state["username"] = normalized_username
            if admin_path is not None:
                state["admin_path"] = normalize_admin_path(admin_path)
            self._write(state)
        return self.view(include_admin_path=True)

    def reset_admin_path(self, admin_path: object) -> dict[str, Any]:
        normalized = normalize_admin_path(admin_path)
        with self._lock:
            state = self._read()
            state["admin_path"] = normalized
            self._write(state)
        return self.view(include_admin_path=True)

    def verify(self, username: str, password: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._read()
        if state.get("mode") != "password":
            return None
        if not hmac.compare_digest(_clean(username), _clean(state.get("username"))):
            return None
        expected = _clean(state.get("password_hash"))
        salt = _clean(state.get("password_salt"))
        if not expected or not salt:
            return None
        actual = self._hash_password(
            str(password or ""),
            salt,
            int(state.get("iterations") or _PASSWORD_ITERATIONS),
        )
        if not hmac.compare_digest(expected, actual):
            return None
        return {"id": "admin", "name": _clean(state.get("username")) or "admin", "role": "admin"}

    def reset_password(self, new_password: str) -> dict[str, Any]:
        password = str(new_password or "")
        if len(password) < _MIN_PASSWORD_LENGTH:
            raise AdminAuthError(f"密码至少需要 {_MIN_PASSWORD_LENGTH} 位")
        with self._lock:
            state = self._read()
            salt = secrets.token_hex(16)
            state["password_salt"] = salt
            state["password_hash"] = self._hash_password(
                password, salt, _PASSWORD_ITERATIONS
            )
            state["iterations"] = _PASSWORD_ITERATIONS
            self._write(state)
        return self.view(include_admin_path=True)

    def _session_secret(self) -> bytes:
        with self._lock:
            state = self._read()
        return _clean(state.get("session_secret")).encode("utf-8")

    def issue_session(self, identity: dict[str, Any]) -> str:
        payload = {
            "sub": _clean(identity.get("id")) or "admin",
            "name": _clean(identity.get("name")) or "admin",
            "role": "admin",
            "iat": _now(),
            "exp": _now() + _SESSION_TTL_SECONDS,
            "nonce": secrets.token_hex(8),
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).rstrip(b"=")
        signature = hmac.new(
            self._session_secret(), encoded, hashlib.sha256
        ).hexdigest()
        return f"qm_sess_{encoded.decode('ascii')}.{signature}"

    def verify_session(self, token: str) -> dict[str, Any] | None:
        raw = _clean(token)
        if not raw.startswith("qm_sess_") or "." not in raw:
            return None
        encoded_text, signature = raw[len("qm_sess_"):].split(".", 1)
        encoded = encoded_text.encode("ascii", "ignore")
        expected = hmac.new(
            self._session_secret(), encoded, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding.encode("ascii")))
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("role") != "admin":
            return None
        if int(payload.get("exp") or 0) <= _now():
            return None
        return {
            "id": _clean(payload.get("sub")) or "admin",
            "name": _clean(payload.get("name")) or "admin",
            "role": "admin",
        }


admin_auth_service = AdminAuthService()
