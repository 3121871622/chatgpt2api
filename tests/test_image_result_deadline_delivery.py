"""A finished download must not be lost to the request budget at the storage step.

The 11/19 image failures included 7 requests that had already downloaded and
upscaled the image and were killed inside ``save_image_bytes`` because the
request deadline (about 170s) expired right before the disk write. Once the
bytes are in hand, delivery is unconditional: the storage stage gets
``deadline_monotonic=None`` while download/upload/generation stay bounded.
"""

import base64
import io
import time

from PIL import Image

from services.protocol.conversation import format_image_result


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (3, 3), color="white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_format_image_result_saves_after_request_deadline(monkeypatch):
    png = _png_bytes()
    # Sentinel default catches a missing keyword: the point of the fix is that
    # storage is told None explicitly, not that the parameter was dropped.
    deadlines: list[object] = []

    def fake_save(data, base_url=None, deadline_monotonic="keyword-missing"):
        deadlines.append(deadline_monotonic)
        return "https://example.test/after-deadline.png"

    monkeypatch.setattr(
        "services.protocol.conversation.upscale_image_if_needed",
        lambda data, size: data,
    )
    monkeypatch.setattr("services.protocol.conversation.save_image_bytes", fake_save)

    result = format_image_result(
        [{"b64_json": base64.b64encode(png).decode("ascii")}],
        "already downloaded and upscaled",
        "url",
        deadline_monotonic=time.monotonic() - 5.0,
    )

    assert len(result["data"]) == 1
    assert result["data"][0]["url"] == "https://example.test/after-deadline.png"
    assert result["_image_urls"] == ["https://example.test/after-deadline.png"]
    # None — not the expired request budget that used to raise task_interrupted.
    assert deadlines == [None]


def test_format_image_result_returns_bytes_after_request_deadline(monkeypatch):
    png = _png_bytes()
    deadlines: list[object] = []

    def fake_save(data, base_url=None, deadline_monotonic="keyword-missing"):
        deadlines.append(deadline_monotonic)
        return "https://example.test/after-deadline.png"

    monkeypatch.setattr(
        "services.protocol.conversation.upscale_image_if_needed",
        lambda data, size: data,
    )
    monkeypatch.setattr("services.protocol.conversation.save_image_bytes", fake_save)

    result = format_image_result(
        [{"b64_json": base64.b64encode(png).decode("ascii")}],
        "b64 response after deadline",
        "b64_json",
        deadline_monotonic=time.monotonic() - 5.0,
    )

    assert len(result["data"]) == 1
    assert base64.b64decode(result["data"][0]["b64_json"]) == png
    assert deadlines == [None]
