from __future__ import annotations

import io

import pytest
from PIL import Image

from services import image_upscale_service as svc
from services.config import config as app_config


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 40, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


@pytest.fixture()
def upscale_config(monkeypatch):
    def _set(*, enabled: bool, engine: str = "pillow_lanczos") -> None:
        monkeypatch.setattr(
            type(app_config),
            "image_upscale_enabled",
            property(lambda self: enabled),
        )
        monkeypatch.setattr(
            type(app_config),
            "image_upscale_engine",
            property(lambda self: engine),
        )

    return _set


def test_disabled_returns_original_for_oversized_source(upscale_config) -> None:
    upscale_config(enabled=False)
    # Source larger than target: downscale path, which the disabled flag does
    # not govern here either (only image_downscale_to_requested would).
    data = _png(256, 256)
    assert svc.upscale_image_if_needed(data, "64x64") == data


def test_explicit_target_disabled_returns_original(upscale_config) -> None:
    upscale_config(enabled=False, engine="pillow_lanczos")
    # Global switch off → upscale is skipped even with an explicit larger target.
    data = _png(64, 64)
    assert svc.upscale_image_if_needed(data, "256x256") == data


def test_k_alias_parses_to_square(upscale_config) -> None:
    upscale_config(enabled=True, engine="pillow_lanczos")
    for alias, expected in (("2k", (2048, 2048)), ("4k", (4096, 4096)), ("8k", (8192, 8192))):
        result = svc.upscale_image_if_needed(_png(64, 64), alias)
        assert _size(result) == expected


def test_k_alias_uppercase_parses(upscale_config) -> None:
    upscale_config(enabled=True, engine="pillow_lanczos")
    assert _size(svc.upscale_image_if_needed(_png(64, 64), "2K")) == (2048, 2048)


def test_target_not_larger_returns_original(upscale_config) -> None:
    upscale_config(enabled=True)
    data = _png(128, 128)
    assert svc.upscale_image_if_needed(data, "64x64") == data
    assert svc.upscale_image_if_needed(data, "auto") == data
    assert svc.upscale_image_if_needed(data, "not-a-size") == data


def test_pillow_engine_upscales(upscale_config) -> None:
    upscale_config(enabled=True, engine="pillow_lanczos")
    result = svc.upscale_image_if_needed(_png(64, 64), "256x192")
    assert _size(result) == (256, 192)


def test_sharp_failure_falls_back_to_pillow(upscale_config, monkeypatch) -> None:
    upscale_config(enabled=True, engine="sharp_lanczos3")

    def boom(_data: bytes, _target: tuple[int, int]) -> bytes:
        raise RuntimeError("sharp unavailable")

    monkeypatch.setattr(svc, "_sharp_lanczos3", boom)
    result = svc.upscale_image_if_needed(_png(32, 32), "128x128")
    assert _size(result) == (128, 128)


def test_invalid_image_is_returned_unchanged(upscale_config) -> None:
    upscale_config(enabled=True, engine="pillow_lanczos")
    data = b"not an image"
    assert svc.upscale_image_if_needed(data, "512x512") == data


def test_wide_source_is_not_stretched_to_square_target(upscale_config) -> None:
    upscale_config(enabled=True, engine="pillow_lanczos")
    # 1672x941 already carries more pixels than 1024x1024. The old axis-wise
    # check saw height 941 < 1024 and squashed the image into a square.
    data = _png(1672, 941)
    assert svc.upscale_image_if_needed(data, "1024x1024") == data


def test_tall_source_is_not_stretched_to_square_target(upscale_config) -> None:
    upscale_config(enabled=True, engine="pillow_lanczos")
    data = _png(941, 1672)
    assert svc.upscale_image_if_needed(data, "1024x1024") == data


def test_pixel_budget_decides_upscale() -> None:
    # Fewer pixels than the target → upscale; equal or more → leave alone.
    assert svc._needs_upscale((64, 64), (128, 128)) is True
    assert svc._needs_upscale((128, 128), (128, 128)) is False
    assert svc._needs_upscale((1672, 941), (1024, 1024)) is False
    assert svc._needs_upscale((512, 512), (1824, 1024)) is True


def test_upscale_slots_allow_concurrency() -> None:
    # A bounded semaphore, not a lock: independent upscales must be able to
    # overlap instead of queueing behind one another.
    import threading

    assert isinstance(svc._UPSCALE_SLOTS, threading.BoundedSemaphore)
    assert svc._UPSCALE_CONCURRENCY >= 1
