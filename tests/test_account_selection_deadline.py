"""The selection deadline must reach every remote validation call.

Measured production failures: selection claimed a 30s cap, but the deadline was
checked only between candidate rounds. One round leased 3 candidates and could
still pay 3 x 8s (full /me check) or 3 x 5s (light probe) after the last check,
producing 37s / 77s / 116s ``no_available_account`` failures. These tests pin
the timeout handed to each remote call (and to the parallel probe wait) to the
remaining selection window.
"""

from __future__ import annotations

import itertools
import time

import pytest

import services.account_service as account_module
from services.account_service import AccountService
from services.upstream_circuit import upstream_circuit

STALE = "2020-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _disable_gateway_trust(monkeypatch):
    """Force live-validation path so deadline tests are not bypassed."""
    monkeypatch.setenv("QINGMENG_IMAGE_SELECTION_TRUST_GATEWAY", "0")
    monkeypatch.setattr(account_module._gw, "is_enabled", lambda: False)


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


def _selection_service(accounts: dict) -> tuple[AccountService, list[str]]:
    service = object.__new__(AccountService)
    service._refresh_accounts_snapshot_if_stale = lambda **kwargs: None
    tokens = itertools.cycle(accounts)
    service._acquire_next_candidate_token = lambda **kwargs: next(tokens)
    service.get_account = lambda token: accounts[token]
    released: list[str] = []
    service.release_image_slot = released.append
    return service, released


def test_full_fetch_request_timeout_is_clipped_to_selection_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The circuit is global test state; pin it closed so the test only measures
    # the deadline clamp.
    monkeypatch.setattr(upstream_circuit, "is_open", lambda: False)
    service, _released = _selection_service({"tok-a": _account("tok-a", image_quota_unknown=True)})
    service.probe_access_token_light = lambda token, timeout=5.0: pytest.fail(
        "an unknown quota must take the full-fetch path"
    )
    request_timeouts: list[float] = []

    def slow_fetch(*args, **kwargs):
        request_timeouts.append(float(kwargs["request_timeout"]))
        # Behave like a real socket timeout: honour the timeout we were given.
        time.sleep(float(kwargs["request_timeout"]))
        raise TimeoutError("upstream request timed out")

    service.fetch_remote_info = slow_fetch

    started = time.monotonic()
    with pytest.raises(account_module.ImageAccountSelectionError) as info:
        AccountService.get_available_access_token(
            service,
            deadline_monotonic=time.monotonic() + 0.8,
        )
    elapsed = time.monotonic() - started

    assert info.value.kind == "unavailable"
    assert "timed out" in str(info.value)
    # The 8s default was clipped to the ~0.8s left in the selection window.
    assert request_timeouts and request_timeouts[0] < 0.9
    # One round cannot outlive the deadline: the old code would have slept the
    # full 8s before even looking at the clock again.
    assert elapsed < 2.0


def test_parallel_probe_timeout_is_clipped_to_selection_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upstream_circuit, "is_open", lambda: False)
    accounts = {name: _account(name) for name in ("tok-a", "tok-b", "tok-c")}
    service, released = _selection_service(accounts)
    probe_timeouts: list[float] = []

    def slow_probe(token, timeout=5.0):
        probe_timeouts.append(float(timeout))
        time.sleep(float(timeout))
        raise TimeoutError("probe timed out")

    service.probe_access_token_light = slow_probe
    service.fetch_remote_info = lambda *args, **kwargs: pytest.fail(
        "light-probe-eligible candidates must not trigger a full fetch"
    )

    started = time.monotonic()
    with pytest.raises(account_module.ImageAccountSelectionError) as info:
        AccountService.get_available_access_token(
            service,
            deadline_monotonic=time.monotonic() + 0.8,
        )
    elapsed = time.monotonic() - started

    assert info.value.kind == "unavailable"
    assert "timed out" in str(info.value)
    # All 3 probes started in the same round but each got the remaining ~0.8s
    # instead of the 5s default, and the wait was bounded by the same deadline.
    assert probe_timeouts and all(value < 0.9 for value in probe_timeouts)
    assert elapsed < 2.0
    # Leased candidates are handed back before the timeout is raised.
    assert set(released) == {"tok-a", "tok-b", "tok-c"}


def test_nearly_expired_window_skips_the_remote_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Less than the 0.5s floor left counts as timed out, not as one more call."""
    monkeypatch.setattr(upstream_circuit, "is_open", lambda: False)
    service, released = _selection_service({"tok-a": _account("tok-a", image_quota_unknown=True)})
    fetched: list[object] = []
    service.fetch_remote_info = lambda *args, **kwargs: fetched.append(kwargs)
    service.probe_access_token_light = lambda token, timeout=5.0: pytest.fail(
        "no remote validation may start inside the floor"
    )

    started = time.monotonic()
    with pytest.raises(account_module.ImageAccountSelectionError) as info:
        AccountService.get_available_access_token(
            service,
            deadline_monotonic=time.monotonic() + 0.4,
        )
    elapsed = time.monotonic() - started

    assert info.value.kind == "unavailable"
    assert "timed out" in str(info.value)
    assert fetched == []
    assert released == ["tok-a"]
    assert elapsed < 1.0
