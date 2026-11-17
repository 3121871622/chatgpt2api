"""Concurrency contract for the image download and reference-upload helpers.

Both paths used to be plain sequential loops, so a multi-image call paid the
sum of every image's latency. These tests pin the parts that must not regress
when the fan-out is in play: input order is preserved, work really overlaps,
a single item never pays for a thread pool, and worker sessions are closed.
"""
from __future__ import annotations

import threading
import time

from services.openai_backend_api import OpenAIBackendAPI


def _api() -> OpenAIBackendAPI:
    return OpenAIBackendAPI(access_token="dummy")


class _Session:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_single_url_uses_the_primary_session(monkeypatch) -> None:
    api = _api()
    seen: list[object] = []

    def fake_download(url: str, session: object) -> bytes:
        seen.append(session)
        return b"img:" + url.encode()

    monkeypatch.setattr(api, "_download_single_image", fake_download)
    result = api.download_image_bytes(["https://example.test/a.png"])

    assert result == [b"img:https://example.test/a.png"]
    assert seen == [api.session]


def test_multi_url_preserves_order(monkeypatch) -> None:
    api = _api()
    sessions: list[_Session] = []

    def make_session() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    def slow_download(url: str, session: object) -> bytes:
        # Later URLs finish first; the caller must still get input order.
        delay = 0.05 if url.endswith("a.png") else 0.0
        time.sleep(delay)
        return b"img:" + url.encode()

    monkeypatch.setattr(api, "_new_download_session", make_session)
    monkeypatch.setattr(api, "_download_single_image", slow_download)

    urls = ["https://example.test/a.png", "https://example.test/b.png"]
    assert api.download_image_bytes(urls) == [b"img:" + u.encode() for u in urls]
    # One scratch session per worker, all closed afterwards.
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)


def test_multi_url_really_overlaps(monkeypatch) -> None:
    api = _api()
    barrier = threading.Barrier(2, timeout=5)

    def blocking_download(url: str, session: object) -> bytes:
        # Deadlocks (and fails via timeout) unless both downloads run at once.
        barrier.wait()
        return url.encode()

    monkeypatch.setattr(api, "_new_download_session", _Session)
    monkeypatch.setattr(api, "_download_single_image", blocking_download)

    result = api.download_image_bytes(["https://example.test/a.png", "https://example.test/b.png"])
    assert result == [b"https://example.test/a.png", b"https://example.test/b.png"]


def test_identical_payloads_from_distinct_urls_are_kept(monkeypatch) -> None:
    api = _api()

    monkeypatch.setattr(api, "_new_download_session", _Session)
    monkeypatch.setattr(
        api, "_download_single_image",
        lambda url, session: b"same-bytes",
    )

    # Two distinct URLs that happen to carry identical bytes are two real
    # results; deduplicating by payload silently dropped one of them.
    result = api.download_image_bytes(
        ["https://example.test/a.png", "https://example.test/b.png"],
    )
    assert result == [b"same-bytes", b"same-bytes"]


def test_download_propagates_failure(monkeypatch) -> None:
    api = _api()

    def failing_download(url: str, session: object) -> bytes:
        raise RuntimeError("upstream 500")

    monkeypatch.setattr(api, "_new_download_session", _Session)
    monkeypatch.setattr(api, "_download_single_image", failing_download)

    try:
        api.download_image_bytes(["https://example.test/a.png", "https://example.test/b.png"])
    except RuntimeError as exc:
        assert "upstream 500" in str(exc)
    else:  # pragma: no cover - the call must not silently succeed
        raise AssertionError("download_image_bytes swallowed a worker failure")


def test_single_upload_uses_the_primary_session(monkeypatch) -> None:
    api = _api()
    seen: list[object] = []

    def fake_upload(image: str, file_name: str = "image.png", session: object = None):
        seen.append(session)
        return {"file_id": file_name}

    monkeypatch.setattr(api, "_upload_image", fake_upload)
    result = api._upload_images([("data:x", "image_1.png")])

    assert result == [{"file_id": "image_1.png"}]
    assert seen == [api.session]


def test_multi_upload_preserves_order_and_closes_sessions(monkeypatch) -> None:
    api = _api()
    sessions: list[_Session] = []

    def make_session() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    def slow_upload(image: str, file_name: str = "image.png", session: object = None):
        if file_name == "image_1.png":
            time.sleep(0.05)
        return {"file_id": file_name}

    monkeypatch.setattr(api, "_new_download_session", make_session)
    monkeypatch.setattr(api, "_upload_image", slow_upload)

    result = api._upload_images([("data:a", "image_1.png"), ("data:b", "image_2.png")])
    assert [item["file_id"] for item in result] == ["image_1.png", "image_2.png"]
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)


def test_empty_inputs_are_noops() -> None:
    api = _api()
    assert api.download_image_bytes([]) == []
    assert api._upload_images([]) == []
