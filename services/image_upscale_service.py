from __future__ import annotations

import io
import re
import shutil
import subprocess
import threading
from pathlib import Path

from PIL import Image, ImageOps

from services.config import config
from services.runtime_configuration import env_int
from utils.diagnostics import diagnostic_excerpt
from utils.image_tokens import image_mime_type, image_size_from_bytes
from utils.log import logger


_SHARP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "image_upscale" / "upscale.mjs"
# Cap concurrent upscales instead of serialising every request through a single
# lock: each upscale spawns a node subprocess and encodes a multi-megabyte PNG,
# so an exclusive lock made one slow image stall every other request in the
# service. Four slots keeps CPU bounded on a small ops box while still letting
# independent requests overlap.
_UPSCALE_CONCURRENCY = env_int("QINGMENG_UPSCALE_CONCURRENCY", 4, 1, 16)
_UPSCALE_SLOTS = threading.BoundedSemaphore(_UPSCALE_CONCURRENCY)
_MAX_TARGET_DIMENSION = 8192

# Shorthand aliases users may pass as the size parameter.  These map to the
# square pixel dimensions most commonly understood as "2K / 4K / 8K" in the
# context of AI image generation (GPT Image produces square outputs by default).
_K_ALIASES: dict[str, tuple[int, int]] = {
    "2k": (2048, 2048),
    "4k": (4096, 4096),
    "8k": (8192, 8192),
}

# Size strings the upstream OpenAI codex image endpoint accepts.  When a
# request uses a k-alias (2k/4k/8k) the upstream cannot interpret it, so we
# request the largest accepted size and upscale locally afterwards.
_UPSTREAM_SIZES: tuple[str, ...] = (
    "1024x1024",
    "1024x1536",
    "1536x1024",
)


def _is_k_alias(value: object) -> bool:
    return str(value or "").strip().lower() in _K_ALIASES


def normalize_upstream_size(value: object) -> str:
    """Return a size the upstream OpenAI image endpoint accepts.

    K-aliases (``2k``/``4k``/``8k``) are not understood upstream and would be
    rejected or silently produce a 1024 image; map them to ``1024x1024`` so
    the upstream generates a full-size base image, and let the local
    upscaler raise it to the requested resolution afterwards.  Other size
    strings are passed through unchanged (the caller already validated them).
    """
    text = str(value or "").strip().lower()
    if not text or text == "auto":
        return "1024x1024"
    if text in _K_ALIASES:
        return "1024x1024"
    return str(value).strip() or "1024x1024"


def _target_size(value: object) -> tuple[int, int] | None:
    text = str(value or "").strip().lower()
    if not text or text == "auto":
        return None
    # Accept shorthand k-notation: "2k", "4k", "8k".
    if text in _K_ALIASES:
        return _K_ALIASES[text]
    match = re.fullmatch(r"(\d{2,5})\s*x\s*(\d{2,5})", text)
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width > _MAX_TARGET_DIMENSION or height > _MAX_TARGET_DIMENSION:
        return None
    return width, height


def _pillow_lanczos(image_data: bytes, target: tuple[int, int]) -> bytes:
    with Image.open(io.BytesIO(image_data)) as source:
        image_format = str(source.format or "PNG").upper()
        image = ImageOps.exif_transpose(source)
        resized = image.resize(target, Image.Resampling.LANCZOS, reducing_gap=3.0)
        output = io.BytesIO()
        save_options: dict[str, object] = {}
        if image_format in {"JPG", "JPEG"}:
            image_format = "JPEG"
            if resized.mode in {"RGBA", "LA"} or (
                resized.mode == "P" and "transparency" in resized.info
            ):
                # Composite onto white before dropping alpha, matching
                # convert_image_output. A bare convert("RGB") turned transparent
                # regions black, so the same image had a different background
                # depending on whether it happened to be upscaled.
                rgba = resized.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.getchannel("A"))
                resized = canvas
            elif resized.mode not in {"RGB", "L"}:
                resized = resized.convert("RGB")
            save_options.update(quality=95, subsampling=0)
        elif image_format == "WEBP":
            save_options.update(quality=95, method=4)
        elif image_format not in {"PNG", "WEBP"}:
            image_format = "PNG"
        resized.save(output, format=image_format, **save_options)
        return output.getvalue()


def _sharp_lanczos3(image_data: bytes, target: tuple[int, int]) -> bytes:
    node = shutil.which("node")
    if not node or not _SHARP_SCRIPT.is_file():
        raise RuntimeError("Sharp runtime is unavailable")
    completed = subprocess.run(
        [node, str(_SHARP_SCRIPT), str(target[0]), str(target[1])],
        input=image_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=180,
    )
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"Sharp exited with code {completed.returncode}")
    return completed.stdout


def _needs_upscale(source: tuple[int, int], target: tuple[int, int]) -> bool:
    """Return True only when the source is smaller than the target.

    Comparing both axes independently (``source[0] >= target[0] and
    source[1] >= target[1]``) treats a portrait/landscape source as "already
    big enough only if it is bigger on BOTH axes", so a 1672x941 image
    requested at 1024x1024 was resized — stretching it to a square and paying
    a pointless node subprocess. The meaningful test is the pixel budget the
    caller asked for: upscale only when the source carries fewer pixels than
    the target, so a request that already has the resolution is left alone.
    """
    return source[0] * source[1] < target[0] * target[1]


def _oriented_size(image_data: bytes) -> tuple[int, int] | None:
    """Source dimensions after EXIF rotation, matching what actually gets resized.

    ``image_size_from_bytes`` reports raw stored dimensions. For a JPEG with
    orientation=5..8 those are swapped relative to what the user sees, so the
    upscale decision was made in one coordinate system while
    ``_pillow_lanczos`` resized in the other.

    Reading the orientation tag is cheap — ``Image.open`` is lazy and does not
    decode pixels — so this does not add a full decode to every request.
    """
    try:
        with Image.open(io.BytesIO(image_data)) as probe:
            width, height = probe.size
            try:
                orientation = int(probe.getexif().get(0x0112, 1) or 1)
            except Exception:
                orientation = 1
    except Exception:
        return image_size_from_bytes(image_data)
    if width <= 0 or height <= 0:
        return image_size_from_bytes(image_data)
    # EXIF orientations 5-8 rotate by 90/270 degrees, swapping the axes.
    if orientation in {5, 6, 7, 8}:
        return int(height), int(width)
    return int(width), int(height)


def upscale_image_if_needed(image_data: bytes, requested_size: object) -> bytes:
    if not image_data or not config.image_upscale_enabled:
        return image_data
    # Resizing a GIF through Sharp/Pillow flattens it to a single frame.
    if image_mime_type(image_data) == "image/gif":
        return image_data
    target = _target_size(requested_size)
    # Check the target first: when the request carries no explicit size there is
    # nothing to compare against, so skip opening the image at all.
    if not target:
        return image_data
    source = _oriented_size(image_data)
    if not source:
        return image_data
    if not _needs_upscale(source, target):
        return image_data

    engine = config.image_upscale_engine
    with _UPSCALE_SLOTS:
        try:
            if engine == "sharp_lanczos3":
                try:
                    result = _sharp_lanczos3(image_data, target)
                except Exception as exc:
                    logger.warning({
                        "event": "image_upscale_sharp_fallback",
                        "source_size": list(source),
                        "target_size": list(target),
                        "error": diagnostic_excerpt(exc, 500),
                    })
                    result = _pillow_lanczos(image_data, target)
                    engine = "pillow_lanczos"
            else:
                result = _pillow_lanczos(image_data, target)
            logger.info({
                "event": "image_upscale_done",
                "engine": engine,
                "source_size": list(source),
                "target_size": list(target),
                "source_bytes": len(image_data),
                "result_bytes": len(result),
            })
            return result
        except Exception as exc:
            logger.warning({
                "event": "image_upscale_failed",
                "engine": engine,
                "source_size": list(source),
                "target_size": list(target),
                "error": diagnostic_excerpt(exc, 500),
            })
            return image_data
