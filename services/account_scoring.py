"""Quality, quota, capability and egress scoring for account selection.

The account index answers *who is ready*; this module answers *who is likely
to succeed fastest right now*.  Scores are intentionally explainable and cheap:
each account carries a few EWMA values, and the selector samples two ready
candidates and keeps the better one (power-of-two choices).
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlparse

_DEFAULT_SUCCESS = 0.9
_DEFAULT_QUOTA = 25.0
_ALPHA = 0.2

_REQUEST_FAILURE_CODES = {
    "content_policy_violation",
    "invalid_image_input",
    "upstream_text_reply",
    "no_image_generated",
    "unsupported_model",
    "task_interrupted",
}

_EGRESS_FAILURE_CODES = {
    "upstream_unavailable",
    "upstream_connection_failed",
    "upstream_connection_timeout",
    "upstream_rate_limited",
    "image_stream_timeout",
    "image_poll_timeout",
    "image_stream_interrupted",
    "file_upload_throttled",
    "insufficient_quota",
    "image_quota_exhausted",
}

_EGRESS_LOCK = threading.Lock()
_EGRESS_SUCCESS_EWMA: dict[str, float] = {}
_EGRESS_RATE_LIMIT_EWMA: dict[str, float] = {}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _number(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def image_health_score(account: dict) -> float:
    """0..1 score; higher means more likely to succeed quickly."""
    success = _clamp(_number(account.get("image_success_ewma"), _DEFAULT_SUCCESS))
    rate_limited = _clamp(_number(account.get("image_rate_limit_ewma"), 0.0))
    timeout = _clamp(_number(account.get("image_timeout_ewma"), 0.0))
    if bool(account.get("image_quota_unknown")):
        quota_score = 0.5
    else:
        quota_score = _clamp(_number(account.get("quota"), 0.0) / _DEFAULT_QUOTA)
    score = (
        0.55 * success
        + 0.15 * (1.0 - rate_limited)
        + 0.10 * (1.0 - timeout)
        + 0.20 * quota_score
    )
    return _clamp(score)


def thompson_score(account: dict) -> float:
    """Beta-sample a success probability; high-variance accounts get explored."""
    alpha = max(1.0, _number(account.get("image_success_alpha"), 1.0))
    beta = max(1.0, _number(account.get("image_success_beta"), 1.0))
    try:
        return _clamp(random.betavariate(alpha, beta))
    except (ValueError, OverflowError):
        return image_health_score(account)


def latency_score(account: dict) -> float:
    """0..1 speed score; 45s is the neutral midpoint."""
    latency_ms = max(0.0, _number(account.get("image_latency_ewma_ms"), 45000.0))
    return _clamp(1.0 / (1.0 + latency_ms / 45000.0))


def last_used_epoch(account: dict) -> float:
    """Epoch seconds of the last successful use; 0.0 when never used.

    ``last_used_at`` is written in two shapes (``%Y-%m-%d %H:%M:%S`` UTC and
    ISO-8601), and older rows may carry epoch numbers.  Unparseable values are
    treated as "never used" so a bad row is favoured rather than starved.
    """
    raw = account.get("last_used_at") if isinstance(account, dict) else None
    if raw in (None, ""):
        return 0.0
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value / 1000.0 if value > 1e12 else max(0.0, value)
    text = str(raw).strip()
    if not text:
        return 0.0
    try:
        value = float(text)
        return value / 1000.0 if value > 1e12 else max(0.0, value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, parsed.timestamp())


def freshness_score(account: dict, *, now: float | None = None) -> float:
    """0..1 fairness score: 1.0 = never used or idle >= 24h, 0.0 = just used.

    Without this term the selector kept re-picking accounts whose EWMA was
    already proven while thousands of equally healthy accounts never got a
    single request, exhausting the head of the pool's quota early.
    """
    last = last_used_epoch(account)
    if last <= 0:
        return 1.0
    current = time.time() if now is None else float(now)
    idle = max(0.0, current - last)
    return _clamp(idle / 86400.0)


def quota_urgency_score(account: dict) -> float:
    """Prefer accounts whose quota resets soon and is still unused.

    Burning quota that is about to expire is free capacity; leaving it idle
    until reset wastes it.  Returns 0..1.
    """
    if bool(account.get("image_quota_unknown")):
        return 0.3
    quota = max(0.0, _number(account.get("quota"), 0.0))
    if quota <= 0:
        return 0.0
    raw = str(account.get("restore_at") or "").strip()
    if not raw:
        return 0.2
    try:
        reset_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        hours = max(0.0, (reset_at - datetime.now(timezone.utc)).total_seconds() / 3600.0)
    except ValueError:
        return 0.2
    soon = 1.0 - min(hours / 72.0, 1.0)
    return _clamp(0.5 * min(quota / _DEFAULT_QUOTA, 1.0) + 0.5 * soon)


def plan_value_factor(account: dict) -> float:
    """Slight preference for paid plans, but never enough to starve free accounts."""
    plan = str(account.get("type") or account.get("plan") or "").strip().lower()
    if plan in {"pro", "prolite", "team"}:
        return 1.10
    if plan == "plus":
        return 1.06
    return 1.0


def record_image_health(
    account: dict,
    *,
    success: bool,
    failure_code: str = "",
    capabilities: Iterable[str] | None = None,
    duration_ms: float | None = None,
) -> None:
    """Update the in-memory account dict; persistence is handled by the caller."""
    normalized_code = str(failure_code or "").strip().lower()
    if not success and normalized_code in _REQUEST_FAILURE_CODES:
        # A user prompt that violates content policy says nothing about the
        # account's health; only account/upstream failures should lower the score.
        return
    old_success = _clamp(_number(account.get("image_success_ewma"), _DEFAULT_SUCCESS))
    target = 1.0 if success else 0.0
    account["image_success_ewma"] = round(old_success * (1 - _ALPHA) + target * _ALPHA, 6)
    account["image_health_samples"] = int(account.get("image_health_samples") or 0) + 1
    if success:
        account["image_success_alpha"] = min(
            1000.0, _number(account.get("image_success_alpha"), 1.0) + 1.0
        )
        if duration_ms is not None and duration_ms > 0:
            previous = _number(account.get("image_latency_ewma_ms"), 45000.0)
            account["image_latency_ewma_ms"] = round(
                previous * (1 - _ALPHA) + min(float(duration_ms), 300000.0) * _ALPHA,
                1,
            )
    else:
        account["image_success_beta"] = min(
            1000.0, _number(account.get("image_success_beta"), 1.0) + 1.0
        )

    if normalized_code in {"upstream_rate_limited", "image_quota_exhausted", "insufficient_quota"}:
        old = _clamp(_number(account.get("image_rate_limit_ewma"), 0.0))
        account["image_rate_limit_ewma"] = round(old * (1 - _ALPHA) + _ALPHA, 6)
    elif normalized_code in {
        "upstream_connection_timeout",
        "image_stream_timeout",
        "image_poll_timeout",
    }:
        old = _clamp(_number(account.get("image_timeout_ewma"), 0.0))
        account["image_timeout_ewma"] = round(old * (1 - _ALPHA) + _ALPHA, 6)
    elif success:
        account["image_rate_limit_ewma"] = round(
            _clamp(_number(account.get("image_rate_limit_ewma"), 0.0)) * (1 - _ALPHA), 6
        )
        account["image_timeout_ewma"] = round(
            _clamp(_number(account.get("image_timeout_ewma"), 0.0)) * (1 - _ALPHA), 6
        )

    if capabilities and success:
        known = account.get("image_capabilities")
        capabilities_map = dict(known) if isinstance(known, dict) else {}
        for capability in capabilities:
            value = str(capability or "").strip()
            if value:
                capabilities_map[value] = 1
        account["image_capabilities"] = capabilities_map
    elif normalized_code == "unsupported_model":
        known = account.get("image_capabilities")
        if isinstance(known, dict):
            account["image_capabilities"] = {
                key: value for key, value in known.items() if not str(key).startswith("model:")
            }


def capability_match_score(account: dict, required: Iterable[str] | None) -> float:
    required_set = {str(item).strip() for item in (required or ()) if str(item).strip()}
    if not required_set:
        return 1.0
    known = account.get("image_capabilities")
    if not isinstance(known, dict):
        known = {}
    matched = sum(1 for item in required_set if item in known)
    # Soft routing: an unknown capability is a preference, not a hard filter, so
    # a fresh pool can still serve the request.
    return 0.2 + 0.8 * (matched / len(required_set))


def egress_key(account: dict) -> str:
    proxy = str(account.get("proxy") or "").strip()
    if proxy:
        try:
            host = urlparse(proxy if "://" in proxy else f"http://{proxy}").hostname
            if host:
                return f"proxy:{host.lower()}"
        except Exception:
            pass
        return "proxy:unknown"
    group = str(account.get("proxy_group_id") or "").strip()
    return f"group:{group}" if group else "direct"


def egress_score(account: dict) -> float:
    key = egress_key(account)
    with _EGRESS_LOCK:
        success = _EGRESS_SUCCESS_EWMA.get(key, _DEFAULT_SUCCESS)
        limited = _EGRESS_RATE_LIMIT_EWMA.get(key, 0.0)
    return _clamp(0.7 * success + 0.3 * (1.0 - limited))


def record_egress_result(account: dict, *, success: bool, failure_code: str = "") -> None:
    key = egress_key(account)
    code = str(failure_code or "").strip().lower()
    if not success and code not in _EGRESS_FAILURE_CODES:
        return
    with _EGRESS_LOCK:
        old_success = _EGRESS_SUCCESS_EWMA.get(key, _DEFAULT_SUCCESS)
        _EGRESS_SUCCESS_EWMA[key] = old_success * (1 - _ALPHA) + (1.0 if success else 0.0) * _ALPHA
        if code in {"upstream_rate_limited", "insufficient_quota"}:
            old_limited = _EGRESS_RATE_LIMIT_EWMA.get(key, 0.0)
            _EGRESS_RATE_LIMIT_EWMA[key] = old_limited * (1 - _ALPHA) + _ALPHA
        elif success:
            old_limited = _EGRESS_RATE_LIMIT_EWMA.get(key, 0.0)
            _EGRESS_RATE_LIMIT_EWMA[key] = old_limited * (1 - _ALPHA)


def candidate_score(account: dict, required_capabilities: Iterable[str] | None = None) -> float:
    """Composite score used by power-of-choices selection."""
    health = image_health_score(account)
    capability = capability_match_score(account, required_capabilities)
    egress = egress_score(account)
    urgency = quota_urgency_score(account)
    plan = plan_value_factor(account)
    sample = thompson_score(account)
    latency = latency_score(account)
    freshness = freshness_score(account)
    score = (
        health
        * (0.15 + 0.85 * capability)
        * (0.5 + 0.5 * egress)
        * (0.8 + 0.2 * latency)
        * (0.9 + 0.1 * sample)
        * (1.0 + 0.08 * urgency)
        * (1.0 + 0.35 * freshness)
        * plan
    )
    # Freshness can lift a score above 1.0; keep the full range so idle
    # accounts actually outrank recently used ones.
    return round(max(0.0, score), 6)


def egress_snapshot() -> dict[str, dict[str, float]]:
    with _EGRESS_LOCK:
        keys = set(_EGRESS_SUCCESS_EWMA) | set(_EGRESS_RATE_LIMIT_EWMA)
        return {
            key: {
                "success": round(_EGRESS_SUCCESS_EWMA.get(key, _DEFAULT_SUCCESS), 6),
                "rate_limited": round(_EGRESS_RATE_LIMIT_EWMA.get(key, 0.0), 6),
            }
            for key in keys
        }
