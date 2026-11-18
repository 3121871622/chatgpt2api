from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

import services.account_service as account_module
from services.account_service import AccountService

STALE = "2020-01-01T00:00:00+00:00"


def _account(token: str, **overrides) -> dict:
    account = {
        "access_token": token,
        "status": "正常",
        "quota": 10,
        "image_quota_unknown": False,
        "last_remote_check_result": "ok",
        "last_remote_checked_at": STALE,
        "type": "free",
        "source_type": "web",
    }
    account.update(overrides)
    return account


def _service(accounts: dict, probe):
    queue = iter(list(accounts))
    service = object.__new__(AccountService)
    service._refresh_accounts_snapshot_if_stale = lambda **kwargs: None

    def acquire(**kwargs):
        token = next(queue, None)
        if token is None and kwargs.get("wait") is not False:
            raise account_module.ImageAccountSelectionError("unavailable", "no candidate")
        return token

    service._acquire_next_candidate_token = acquire
    service.get_account = lambda token: accounts[token]
    released: list[str] = []
    service.release_image_slot = released.append
    service.probe_access_token_light = probe
    service.fetch_remote_info = lambda *args, **kwargs: pytest.fail("full fetch must not run")
    return service, released


def test_parallel_probe_takes_fastest_healthy_candidate() -> None:
    accounts = {name: _account(name) for name in ("tok-a", "tok-b", "tok-c")}

    def probe(token, timeout=5.0):
        if token == "tok-b":
            return True
        if token == "tok-c":
            time.sleep(0.5)
            return True
        raise RuntimeError("probe timed out")

    service, released = _service(accounts, probe)
    started = time.monotonic()
    token = AccountService.get_available_access_token(service)
    elapsed = time.monotonic() - started

    assert token == "tok-b"
    assert set(released) == {"tok-a", "tok-c"}
    # Serial probing would have waited for the slow candidate too.
    assert elapsed < 0.4


def test_fresh_candidate_skips_network_probe() -> None:
    fresh_at = datetime.now(timezone.utc).isoformat()
    accounts = {
        "tok-a": _account("tok-a"),
        "tok-b": _account("tok-b", last_remote_checked_at=fresh_at),
        "tok-c": _account("tok-c"),
    }

    def probe(token, timeout=5.0):
        pytest.fail("a fresh candidate must not trigger any probe")

    service, released = _service(accounts, probe)
    token = AccountService.get_available_access_token(service)

    assert token == "tok-b"
    assert set(released) == {"tok-a", "tok-c"}


def test_all_candidates_failing_releases_every_slot_and_raises() -> None:
    accounts = {name: _account(name) for name in ("tok-a", "tok-b", "tok-c")}

    def probe(token, timeout=5.0):
        raise RuntimeError("probe timed out")

    service, released = _service(accounts, probe)
    with pytest.raises(account_module.ImageAccountSelectionError):
        AccountService.get_available_access_token(service)

    assert set(released) == {"tok-a", "tok-b", "tok-c"}


@pytest.fixture(autouse=True)
def _disable_gateway_trust(monkeypatch):
    """These tests exercise the live-validation path explicitly."""
    monkeypatch.setenv("QINGMENG_IMAGE_SELECTION_TRUST_GATEWAY", "0")
    monkeypatch.setattr(account_module._gw, "is_enabled", lambda: False)


def test_gateway_trust_skips_live_validation(monkeypatch) -> None:
    """A gateway lease is accepted after local screening only.

    8/8 concurrent image requests used to return 503 no_available_account
    because every lease was re-validated with /backend-api/me and the probe
    storm produced 401s / refresh races. The trusted path must return a
    locally-usable candidate without any probe.
    """
    monkeypatch.setenv("QINGMENG_IMAGE_SELECTION_TRUST_GATEWAY", "1")
    monkeypatch.setattr(account_module._gw, "is_enabled", lambda: True)
    accounts = {name: _account(name) for name in ("tok-a", "tok-b", "tok-c")}

    def probe(token, timeout=5.0):
        pytest.fail("gateway-trusted selection must not probe upstream")

    service, released = _service(accounts, probe)
    token = AccountService.get_available_access_token(service)

    assert token in accounts
    assert len(released) == 2


def test_gateway_trust_skips_locally_invalid_candidate(monkeypatch) -> None:
    monkeypatch.setenv("QINGMENG_IMAGE_SELECTION_TRUST_GATEWAY", "1")
    monkeypatch.setattr(account_module._gw, "is_enabled", lambda: True)
    accounts = {
        "tok-a": _account("tok-a", last_remote_check_result="invalid"),
        "tok-b": _account("tok-b"),
    }

    def probe(token, timeout=5.0):
        pytest.fail("gateway-trusted selection must not probe upstream")

    service, released = _service(accounts, probe)
    token = AccountService.get_available_access_token(service)

    assert token == "tok-b"
    assert released == ["tok-a"]


def test_gateway_trust_skips_dead_refresh_token_account(monkeypatch) -> None:
    """An account whose refresh token is dead cannot be renewed.

    Upstream revokes the old access token as soon as a refresh happens, so
    these accounts surface as 401 auth_invalid and burn a whole attempt.
    Measured 1.9.30 traffic: ~11% auth_invalid, all refresh-token-invalid.
    """
    monkeypatch.setenv("QINGMENG_IMAGE_SELECTION_TRUST_GATEWAY", "1")
    monkeypatch.setattr(account_module._gw, "is_enabled", lambda: True)
    accounts = {
        "tok-dead": _account("tok-dead", refresh_token_invalid_at="2026-11-19T00:00:00+00:00"),
        "tok-good": _account("tok-good"),
    }

    def probe(token, timeout=5.0):
        pytest.fail("gateway-trusted selection must not probe upstream")

    service, released = _service(accounts, probe)
    token = AccountService.get_available_access_token(service)

    assert token == "tok-good"
    assert released == ["tok-dead"]
