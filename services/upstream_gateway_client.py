"""Client for the Go upstream-gateway service.

The upstream-gateway exposes two endpoints:
  POST /v1/upstream/stream  — proxy a ChatGPT SSE conversation request
  POST /v1/upstream/poll    — poll a conversation document for image file_ids

Both endpoints are tried first; on any failure (network, gateway not running,
non-200 response) the caller falls back to the existing Python path without
raising.

Configuration (re-read on every call):

    UPSTREAM_GATEWAY_URL    base URL, default http://127.0.0.1:8082
    UPSTREAM_GATEWAY_TOKEN  shared secret (X-Gateway-Token header); optional
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Generator

import requests

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_session: requests.Session | None = None


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _base_url() -> str:
    return _env("UPSTREAM_GATEWAY_URL", "http://127.0.0.1:8082").rstrip("/")


def _token() -> str:
    return _env("UPSTREAM_GATEWAY_TOKEN")


def is_enabled() -> bool:
    return bool(_env("UPSTREAM_GATEWAY_TOKEN") or _env("UPSTREAM_GATEWAY_URL"))


def _get_session() -> requests.Session:
    global _session
    with _lock:
        if _session is None:
            _session = requests.Session()
        return _session


def _headers() -> dict[str, str]:
    h: dict[str, str] = {}
    tok = _token()
    if tok:
        h["X-Gateway-Token"] = tok
    return h


# ---------------------------------------------------------------------------
# SSE stream proxy
# ---------------------------------------------------------------------------

def stream_conversation(
    *,
    url: str,
    method: str = "POST",
    headers: dict[str, str],
    body: Any,
    impersonate: str = "chrome110",
    timeout_ms: int = 300_000,
) -> Generator[str, None, None] | None:
    """Delegate a ChatGPT SSE request to the Go gateway.

    Yields SSE payload lines (stripped of "data: " prefix and empty lines)
    as they arrive.  Returns None (not a generator) if the gateway is
    unavailable, unreachable, or returns a non-200 status so the caller can
    fall back to the Python path.
    """
    if not is_enabled():
        return None
    payload = {
        "url": url,
        "method": method,
        "headers": headers,
        "body": body,
        "impersonate": impersonate,
        "timeout_ms": timeout_ms,
    }
    try:
        resp = _get_session().post(
            _base_url() + "/v1/upstream/stream",
            json=payload,
            headers=_headers(),
            stream=True,
            timeout=(5, timeout_ms / 1000 + 5),
        )
        if resp.status_code != 200:
            logger.debug("upstream-gateway stream non-200: %d", resp.status_code)
            resp.close()
            return None
    except Exception as exc:
        logger.debug("upstream-gateway stream unavailable: %s", exc)
        return None

    def _iter() -> Generator[str, None, None]:
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw
                if line.startswith("data: "):
                    line = line[6:]
                yield line
        finally:
            resp.close()

    return _iter()


# ---------------------------------------------------------------------------
# Conversation document poll
# ---------------------------------------------------------------------------

def poll_image_ids(
    *,
    url: str,
    headers: dict[str, str],
    timeout_ms: int = 90_000,
    interval_ms: int = 2_000,
    max_attempts: int = 50,
) -> dict[str, Any] | None:
    """Poll a conversation document for image file_ids via the Go gateway.

    Returns a dict with 'file_ids', 'sediment_ids', 'attempts', 'elapsed_ms'
    on success.  Returns None if the gateway is unavailable or the poll times
    out (caller should fall back to the Python poll path).
    """
    if not is_enabled():
        return None
    payload = {
        "url": url,
        "headers": headers,
        "timeout_ms": timeout_ms,
        "interval_ms": interval_ms,
        "max_attempts": max_attempts,
    }
    try:
        resp = _get_session().post(
            _base_url() + "/v1/upstream/poll",
            json=payload,
            headers=_headers(),
            timeout=(5, timeout_ms / 1000 + 10),
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 504:
            # gateway-level poll timeout — tell caller to fall back
            logger.debug("upstream-gateway poll timeout (504)")
            return None
        logger.debug("upstream-gateway poll unexpected status: %d", resp.status_code)
        return None
    except Exception as exc:
        logger.debug("upstream-gateway poll unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------

def healthz(timeout: float = 1.0) -> bool:
    if not is_enabled():
        return False
    try:
        resp = _get_session().get(
            _base_url() + "/healthz",
            timeout=timeout,
        )
        return resp.status_code == 200
    except Exception:
        return False
