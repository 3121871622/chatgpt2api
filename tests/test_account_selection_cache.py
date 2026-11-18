from __future__ import annotations

from datetime import datetime, timezone

import pytest

import services.account_service as account_module
from services.account_service import AccountService


@pytest.fixture(autouse=True)
def _disable_gateway_trust(monkeypatch):
    """Force live-validation path so cache / probe tests are not bypassed."""
    monkeypatch.setenv("QINGMENG_IMAGE_SELECTION_TRUST_GATEWAY", "0")
    monkeypatch.setattr(account_module._gw, "is_enabled", lambda: False)


def _account(**overrides):
    now = datetime.now(timezone.utc).isoformat()
    account = {
        "access_token": "tok-1",
        "status": "正常",
        "quota": 10,
        "image_quota_unknown": False,
        "last_remote_check_result": "ok",
        "last_remote_checked_at": now,
        "type": "free",
        "source_type": "web",
    }
    account.update(overrides)
    return account


def test_fresh_remote_check_is_usable() -> None:
    assert AccountService._remote_check_fresh(_account()) is True


def test_stale_remote_check_is_not_usable() -> None:
    old = "2020-01-01T00:00:00+00:00"
    assert AccountService._remote_check_fresh(_account(last_remote_checked_at=old)) is False
    assert AccountService._remote_check_fresh(_account(last_remote_checked_at="")) is False


def test_failed_remote_check_is_not_usable() -> None:
    assert AccountService._remote_check_fresh(_account(last_remote_check_result="error")) is False
    assert AccountService._remote_check_fresh(_account(last_remote_check_result="pending")) is False
    assert AccountService._remote_check_fresh(_account(status="限流")) is False


def test_selection_skips_remote_call_for_fresh_account() -> None:
    service = object.__new__(AccountService)
    service._refresh_accounts_snapshot_if_stale = lambda **kwargs: None
    service._acquire_next_candidate_token = lambda **kwargs: "tok-1"
    service.get_account = lambda token: _account()
    service.release_image_slot = lambda token: None
    calls: list[tuple] = []
    service.fetch_remote_info = lambda *args, **kwargs: calls.append(args) or {}

    token = AccountService.get_available_access_token(service)

    assert token == "tok-1"
    assert calls == []


def test_selection_uses_remote_check_for_stale_account() -> None:
    # Unknown quota cannot take the light-probe shortcut, so a stale cache
    # entry is validated with exactly one full remote fetch.
    service = object.__new__(AccountService)
    service._refresh_accounts_snapshot_if_stale = lambda **kwargs: None
    service._acquire_next_candidate_token = lambda **kwargs: "tok-1"
    service.get_account = lambda token: _account(
        last_remote_checked_at="2020-01-01T00:00:00+00:00",
        image_quota_unknown=True,
    )
    service.release_image_slot = lambda token: None
    calls: list[tuple] = []

    def fake_fetch(*args, **kwargs):
        calls.append(args)
        return _account()

    service.fetch_remote_info = fake_fetch

    token = AccountService.get_available_access_token(service)

    assert token == "tok-1"
    assert len(calls) == 1


def test_light_probe_success_skips_full_fetch() -> None:
    service = object.__new__(AccountService)
    service._refresh_accounts_snapshot_if_stale = lambda **kwargs: None
    service._acquire_next_candidate_token = lambda **kwargs: "tok-1"
    service.get_account = lambda token: _account(
        last_remote_checked_at="2020-01-01T00:00:00+00:00",
    )
    service.release_image_slot = lambda token: None
    service.probe_access_token_light = lambda token, timeout=5.0: True
    calls: list[tuple] = []
    service.fetch_remote_info = lambda *args, **kwargs: calls.append(args)

    assert AccountService.get_available_access_token(service) == "tok-1"
    assert calls == []


def test_failed_light_probe_moves_on_without_full_fetch() -> None:
    """A dead candidate must cost one 5s probe, not probe + 8s full fetch."""
    candidates = iter(["tok-dead", "tok-good"])
    accounts = {
        "tok-dead": _account(
            access_token="tok-dead",
            last_remote_checked_at="2020-01-01T00:00:00+00:00",
        ),
        "tok-good": _account(access_token="tok-good"),
    }
    service = object.__new__(AccountService)
    service._refresh_accounts_snapshot_if_stale = lambda **kwargs: None
    service._acquire_next_candidate_token = (
        lambda **kwargs: None if kwargs.get("wait") is False else next(candidates)
    )
    service.get_account = lambda token: accounts[token]
    released: list[str] = []
    service.release_image_slot = released.append

    def dead_probe(token, timeout=5.0):
        raise RuntimeError("probe timed out")

    service.probe_access_token_light = dead_probe
    calls: list[tuple] = []
    service.fetch_remote_info = lambda *args, **kwargs: calls.append(args)

    assert AccountService.get_available_access_token(service) == "tok-good"
    assert calls == []
    assert "tok-dead" in released
