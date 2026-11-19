import threading
import time

import services.account_service as account_module
from services.account_service import account_service


def _isolate(monkeypatch, tokens=None):
    monkeypatch.setattr(account_service, "_gw_bad_tokens", dict(tokens or {}))
    monkeypatch.setattr(account_service, "_gw_bad_lock", threading.Lock())


def test_mark_and_list_gateway_bad_token(monkeypatch):
    _isolate(monkeypatch)
    assert account_service.gateway_bad_tokens() == set()

    assert account_service.mark_gateway_token_bad("tok-x", ttl_seconds=60) is True
    assert "tok-x" in account_service.gateway_bad_tokens()


def test_expired_gateway_bad_tokens_are_pruned(monkeypatch):
    _isolate(monkeypatch, {"tok-old": time.monotonic() - 5, "tok-live": time.monotonic() + 300})
    alive = account_service.gateway_bad_tokens()
    assert alive == {"tok-live"}


def test_empty_token_is_ignored(monkeypatch):
    _isolate(monkeypatch)
    assert account_service.mark_gateway_token_bad("") is False
    assert account_service.gateway_bad_tokens() == set()


def test_bad_tokens_are_excluded_from_gateway_lease(monkeypatch):
    """The next lease must not hand back a token that already 401'd."""
    _isolate(monkeypatch, {"tok-bad": time.monotonic() + 300})
    monkeypatch.setattr(account_module._gw, "is_enabled", lambda: True)
    captured: dict = {}

    def fake_acquire(**kwargs):
        captured.update(kwargs)
        return ""

    monkeypatch.setattr(account_module._gw, "acquire", fake_acquire)

    account_service._acquire_next_candidate_token(
        deadline_monotonic=None,
        wait=False,
    )

    excluded = captured.get("excluded") or []
    assert "tok-bad" in excluded
