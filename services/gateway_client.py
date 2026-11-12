"""Thin HTTP client for the Go gateway service.

The gateway exposes a small REST API over a local TCP socket.  Python calls
it for account lease operations; when the gateway is not reachable the caller
falls back to the built-in Python index without raising.

Configuration (read once at import time, re-read on every call if the module
is reloaded):

    GATEWAY_URL   base URL, default http://127.0.0.1:8081
    GATEWAY_TOKEN shared secret — must match the Go process's GATEWAY_TOKEN

A missing or empty GATEWAY_TOKEN means the client is disabled; every call
returns None / False immediately without touching the network.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


class _Config:
    """Live-reloadable config snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refresh()

    def _refresh(self) -> None:
        self._url = _env("GATEWAY_URL", "http://127.0.0.1:8081").rstrip("/")
        self._token = _env("GATEWAY_TOKEN")

    @property
    def url(self) -> str:
        return self._url

    @property
    def token(self) -> str:
        return self._token

    @property
    def enabled(self) -> bool:
        return bool(self._token)


_cfg = _Config()

# ---------------------------------------------------------------------------
# HTTP session (keep-alive, short timeouts)
# ---------------------------------------------------------------------------

_session_lock = threading.Lock()
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            s.headers.update({"Content-Type": "application/json"})
            _session = s
        return _session


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_cfg.token}"}


def _post(path: str, payload: dict, *, timeout: float = 2.0) -> dict | None:
    """POST to the gateway; returns parsed JSON or None on any error."""
    if not _cfg.enabled:
        return None
    url = f"{_cfg.url}{path}"
    try:
        resp = _get_session().post(
            url,
            json=payload,
            headers=_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.debug("gateway %s failed: %s", path, exc)
        return None


def _get(path: str, *, timeout: float = 2.0) -> dict | None:
    if not _cfg.enabled:
        return None
    url = f"{_cfg.url}{path}"
    try:
        resp = _get_session().get(url, headers=_headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.debug("gateway %s failed: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """True when GATEWAY_TOKEN is set and the gateway URL is configured."""
    return _cfg.enabled


def healthz(timeout: float = 1.0) -> bool:
    """Cheap liveness check — no auth required."""
    if not _cfg.enabled:
        return False
    try:
        resp = _get_session().get(
            f"{_cfg.url}/healthz",
            timeout=timeout,
        )
        return resp.status_code == 200
    except Exception:
        return False


def upsert_account(
    token: str,
    *,
    plan: str = "",
    source: str = "",
    ready: bool = True,
    status: str = "",
    quota: int = 0,
    quota_unknown: bool = False,
    last_used_at: str | None = None,
    restore_at: str | None = None,
) -> bool | None:
    """Upsert one account into the gateway index (and optionally its PG store).

    Returns True if the index changed, False if unchanged, None if the gateway
    is disabled or unreachable.
    """
    payload: dict[str, Any] = {
        "token": token,
        "plan": plan,
        "source": source,
        "ready": ready,
        "quota": quota,
        "quota_unknown": quota_unknown,
    }
    if status:
        payload["status"] = status
    if last_used_at:
        payload["last_used_at"] = last_used_at
    if restore_at:
        payload["restore_at"] = restore_at
    result = _post("/v1/accounts/upsert", payload)
    if result is None:
        return None
    return bool(result.get("changed"))


def remove_account(token: str) -> bool | None:
    """Remove one account from the gateway index and PG store.

    Returns True if removed, False if not found, None if unavailable.
    """
    result = _post("/v1/accounts/remove", {"token": token})
    if result is None:
        return None
    return bool(result.get("removed"))


def acquire(
    *,
    plans: list[str] | None = None,
    sources: list[str] | None = None,
    excluded: list[str] | None = None,
    limit: int = 1,
    timeout: float = 2.0,
) -> str | None:
    """Try to lease one account token from the gateway.

    Returns the token string on success, empty string when the pool is
    exhausted, or None when the gateway is unavailable.
    """
    payload: dict[str, Any] = {"limit": limit}
    if plans:
        payload["plans"] = plans
    if sources:
        payload["sources"] = sources
    if excluded:
        payload["excluded"] = excluded
    result = _post("/v1/lease/acquire", payload, timeout=timeout)
    if result is None:
        return None
    if not result.get("ok"):
        return ""
    return str(result.get("token") or "")


def release(token: str, *, limit: int = 1) -> bool:
    """Release a previously acquired token.  Best-effort; never raises."""
    if not token:
        return False
    result = _post("/v1/lease/release", {"token": token, "limit": limit})
    return result is not None


def stats() -> dict | None:
    """Return the gateway diagnostic snapshot, or None if unavailable."""
    return _get("/v1/stats")


# ---------------------------------------------------------------------------
# Bulk sync helper (called from account_service on startup / snapshot reload)
# ---------------------------------------------------------------------------

def sync_accounts(
    accounts: list[dict],
    *,
    batch_size: int = 200,
    request_timeout: float = 5.0,
) -> tuple[int, int]:
    """Push a full account snapshot to the gateway in batches.

    Each account dict is expected to carry at least ``access_token``,
    ``type`` (plan), ``source_type``, ``status``, ``quota``,
    ``image_quota_unknown``, ``last_used_at``, ``restore_at``.

    Returns ``(sent, errors)`` counts.
    """
    if not _cfg.enabled:
        return 0, 0

    sent = errors = 0
    for i in range(0, len(accounts), batch_size):
        batch = accounts[i: i + batch_size]
        for acc in batch:
            token = str(acc.get("access_token") or "").strip()
            if not token:
                continue
            status_raw = str(acc.get("status") or "").strip()
            # Map Chinese status labels to gateway canonical values
            status_map = {
                "正常": "normal",
                "限流": "limited",
                "禁用": "disabled",
                "异常": "invalid",
            }
            status = status_map.get(status_raw, status_raw.lower() or "normal")
            quota = 0
            try:
                quota = int(acc.get("quota") or 0)
            except (TypeError, ValueError):
                pass
            quota_unknown = bool(acc.get("image_quota_unknown"))
            is_ready = (
                status == "normal"
                and (quota_unknown or quota > 0)
            )
            restore_at = str(acc.get("restore_at") or "").strip() or None
            last_used = str(acc.get("last_used_at") or "").strip() or None
            result = _post(
                "/v1/accounts/upsert",
                {
                    "token": token,
                    "plan": str(acc.get("type") or "").strip().lower(),
                    "source": str(acc.get("source_type") or "").strip().lower(),
                    "ready": is_ready,
                    "status": status,
                    "quota": quota,
                    "quota_unknown": quota_unknown,
                    "last_used_at": last_used,
                    "restore_at": restore_at,
                },
                timeout=request_timeout,
            )
            if result is None:
                errors += 1
            else:
                sent += 1
        # Short pause between batches to avoid flooding the gateway
        if i + batch_size < len(accounts):
            time.sleep(0.05)

    return sent, errors
