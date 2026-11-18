import base64
import io

from PIL import Image

from services.protocol.conversation import format_image_result


def _png_bytes(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (3, 3), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _prepare(monkeypatch) -> list[bytes]:
    saved: list[bytes] = []

    def fake_save(data, base_url=None, deadline_monotonic=None):
        saved.append(data)
        return f"https://example.test/{len(saved)}.png"

    monkeypatch.setattr("services.protocol.conversation.upscale_image_if_needed", lambda data, size: data)
    monkeypatch.setattr("services.protocol.conversation.save_image_bytes", fake_save)
    return saved


def test_n1_slot_with_three_upstream_images_returns_one(monkeypatch):
    saved = _prepare(monkeypatch)
    result = format_image_result(
        [
            {"b64_json": _b64(_png_bytes("red"))},
            {"b64_json": _b64(_png_bytes("green"))},
            {"b64_json": _b64(_png_bytes("blue"))},
        ],
        "one slot three images",
        "b64_json",
    )
    assert len(result["data"]) == 1
    assert saved == [_png_bytes("red")]


def test_byte_identical_repeats_are_deduped(monkeypatch):
    saved = _prepare(monkeypatch)
    png = _b64(_png_bytes("white"))
    result = format_image_result(
        [{"b64_json": png}, {"b64_json": png}, {"b64_json": png}],
        "duplicate bytes",
        "b64_json",
    )
    assert len(result["data"]) == 1
    assert saved == [_png_bytes("white")]


def test_max_images_keeps_first_distinct_images(monkeypatch):
    saved = _prepare(monkeypatch)
    result = format_image_result(
        [
            {"b64_json": _b64(_png_bytes("red"))},
            {"b64_json": _b64(_png_bytes("red"))},
            {"b64_json": _b64(_png_bytes("green"))},
            {"b64_json": _b64(_png_bytes("blue"))},
        ],
        "two slots",
        "b64_json",
        max_images=2,
    )
    assert len(result["data"]) == 2
    assert saved == [_png_bytes("red"), _png_bytes("green")]


def test_single_image_slot_unchanged(monkeypatch):
    saved = _prepare(monkeypatch)
    result = format_image_result(
        [{"b64_json": _b64(_png_bytes("black"))}],
        "single",
        "b64_json",
    )
    assert len(result["data"]) == 1
    assert saved == [_png_bytes("black")]
