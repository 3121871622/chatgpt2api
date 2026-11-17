from __future__ import annotations

import time

import pytest

from services import openai_backend_api as api
from services.config import config
from services.image_failure import ImagePollTimeoutError
from utils.helper import UpstreamHTTPError


class FakeClock:
    """Deterministic monotonic clock used by the poll loop."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return time.perf_counter()

    def sleep(self, seconds: float) -> None:
        value = max(0.0, float(seconds or 0))
        self.sleeps.append(round(value, 6))
        self.now += value

    def time(self) -> float:
        return time.time()


def _patch_poll_config(
        monkeypatch: pytest.MonkeyPatch,
        *,
        initial_wait: float,
        interval: float,
        settle: float,
        settle_enabled: bool = True,
        check_before_hit: bool = True,
) -> None:
    cls = type(config)
    values = {
        "image_poll_initial_wait_secs": float(initial_wait),
        "image_poll_interval_secs": float(interval),
        "image_settle_secs": float(settle),
        "image_settle_enabled": bool(settle_enabled),
        "image_check_before_hit_enabled": bool(check_before_hit),
    }
    for name, value in values.items():
        monkeypatch.setattr(cls, name, property(lambda self, v=value: v))


def _fake_backend(
        monkeypatch: pytest.MonkeyPatch,
        clock: FakeClock,
        conversations: list[object],
        *,
        request_delay: float = 0.0,
) -> tuple[api.OpenAIBackendAPI, dict[str, int]]:
    backend = object.__new__(api.OpenAIBackendAPI)
    backend._image_result_timing = {}
    calls = {"conversation": 0}

    def _get_conversation(_conversation_id: str, timeout_secs: float = 60) -> object:
        clock.now += request_delay
        index = min(calls["conversation"], len(conversations) - 1)
        calls["conversation"] += 1
        item = conversations[index]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(backend, "_query_backend_tasks", lambda **_kwargs: [])
    monkeypatch.setattr(backend, "_get_conversation", _get_conversation)
    monkeypatch.setattr(
        backend,
        "_extract_image_tool_records",
        lambda conversation: conversation.get("records", []),
    )
    monkeypatch.setattr(backend, "_conversation_poll_snapshot", lambda _conversation: ({}, ""))
    return backend, calls


def _records(file_id: str) -> dict[str, list[dict[str, list[str]]]]:
    return {"records": [{"file_ids": [file_id], "sediment_ids": []}]}


def _patch_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(api, "time", clock)
    return clock


def _patch_random(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    monkeypatch.setattr(api.random, "uniform", lambda _low, _high: value)


def test_initial_ids_use_short_settle_total_under_two_seconds(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_poll_config(monkeypatch, initial_wait=60.0, interval=3.0, settle=5.0)
    clock = _patch_clock(monkeypatch)
    _patch_random(monkeypatch, 0.0)
    backend, _ = _fake_backend(
        monkeypatch,
        clock,
        [_records("file_new"), _records("file_new")],
    )

    file_ids, sediment_ids = backend._poll_image_results(
        "conversation-1",
        timeout_secs=60.0,
        initial_file_ids=["file_initial"],
    )

    assert file_ids == ["file_initial", "file_new"]
    assert sediment_ids == []
    # Initial settle (1.0s) + pending-hit confirmation (0.8s) stay under 2s.
    assert clock.sleeps == pytest.approx([1.0, 0.8])
    assert sum(clock.sleeps) <= 2.0


def test_no_initial_first_wait_is_hard_capped_at_three_seconds(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_poll_config(monkeypatch, initial_wait=60.0, interval=2.0, settle=1.0)
    clock = _patch_clock(monkeypatch)
    _patch_random(monkeypatch, 0.3)
    backend, _ = _fake_backend(
        monkeypatch,
        clock,
        [_records("file_1"), _records("file_1")],
    )

    file_ids, _ = backend._poll_image_results("conversation-2", timeout_secs=60.0)

    assert file_ids == ["file_1"]
    assert clock.sleeps[0] == pytest.approx(3.0)
    assert clock.sleeps[0] <= 3.0


def test_consecutive_misses_back_off_and_hit_settle_is_short(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_poll_config(monkeypatch, initial_wait=2.0, interval=2.0, settle=5.0)
    clock = _patch_clock(monkeypatch)
    _patch_random(monkeypatch, 0.0)
    backend, _ = _fake_backend(
        monkeypatch,
        clock,
        [{}, {}, _records("file_2"), _records("file_2")],
    )

    file_ids, _ = backend._poll_image_results("conversation-3", timeout_secs=60.0)

    assert file_ids == ["file_2"]
    assert clock.sleeps == pytest.approx([2.0, 2.0, 3.0, 0.8])
    assert clock.sleeps[1] < clock.sleeps[2]
    assert clock.sleeps[-1] < 1.0


def test_429_retries_with_exponential_backoff(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_poll_config(monkeypatch, initial_wait=0.5, interval=2.0, settle=1.0)
    clock = _patch_clock(monkeypatch)
    _patch_random(monkeypatch, 0.0)
    rate_limited = UpstreamHTTPError("conversation", 429, {"error": "rate limit"})
    backend, calls = _fake_backend(
        monkeypatch,
        clock,
        [rate_limited, rate_limited, _records("file_3"), _records("file_3")],
    )

    file_ids, _ = backend._poll_image_results("conversation-4", timeout_secs=60.0)

    assert file_ids == ["file_3"]
    assert calls["conversation"] == 4
    # The canonical policy marks upstream_rate_limited non-retryable for the
    # account layer; the poll loop keeps a bounded in-place retry so a transient
    # 429 does not regenerate the image.  Attempts still grow 2s -> 4s.
    assert clock.sleeps == pytest.approx([0.5, 2.0, 4.0, 0.8])


def test_429_retry_is_bounded_then_falls_through_to_account_layer(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_poll_config(monkeypatch, initial_wait=0.5, interval=2.0, settle=1.0)
    clock = _patch_clock(monkeypatch)
    _patch_random(monkeypatch, 0.0)
    rate_limited = UpstreamHTTPError("conversation", 429, {"error": "rate limit"})
    backend, calls = _fake_backend(
        monkeypatch,
        clock,
        [rate_limited, rate_limited, rate_limited, _records("file_5")],
    )

    with pytest.raises(UpstreamHTTPError) as error_info:
        backend._poll_image_results("conversation-5", timeout_secs=60.0)

    assert error_info.value.status_code == 429
    assert error_info.value.failure.code == "upstream_rate_limited"
    assert calls["conversation"] == 3
    assert clock.sleeps == pytest.approx([0.5, 2.0, 4.0])


def test_429_honors_retry_after(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_poll_config(monkeypatch, initial_wait=0.5, interval=2.0, settle=1.0)
    clock = _patch_clock(monkeypatch)
    _patch_random(monkeypatch, 0.0)
    rate_limited = UpstreamHTTPError(
        "conversation",
        429,
        {"error": "rate limit"},
        retry_after=6,
    )
    backend, _ = _fake_backend(
        monkeypatch,
        clock,
        [rate_limited, _records("file_4"), _records("file_4")],
    )

    file_ids, _ = backend._poll_image_results("conversation-5", timeout_secs=60.0)

    assert file_ids == ["file_4"]
    assert clock.sleeps == pytest.approx([0.5, 6.0, 0.8])


def test_all_sleeps_stay_within_timeout_budget(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_poll_config(monkeypatch, initial_wait=2.0, interval=2.0, settle=1.0)
    clock = _patch_clock(monkeypatch)
    _patch_random(monkeypatch, 0.0)
    backend, _ = _fake_backend(
        monkeypatch,
        clock,
        [{}],
        request_delay=0.1,
    )

    with pytest.raises(ImagePollTimeoutError) as error_info:
        backend._poll_image_results("conversation-6", timeout_secs=5.0)

    exc = error_info.value
    assert clock.now - 1000.0 == pytest.approx(5.0)
    assert sum(clock.sleeps) == pytest.approx(4.8)
    assert clock.sleeps == pytest.approx([2.0, 2.0, 0.8])
    assert exc.poll_attempts == 2
    assert exc.poll_timeout_secs == 5.0
