"""端点与模型的强校验。

绘画模型只能走图片端点（`/v1/images/generations`、`/v1/images/edits`），
对话模型只能走文本端点（`/v1/chat/completions`、`/v1/responses`、`/v1/messages`）。
"""

from __future__ import annotations

from fastapi import HTTPException

from utils.helper import IMAGE_MODELS, is_supported_image_model

_IMAGE_MODEL_HINT = (
    "绘画模型只能用于 /v1/images/generations 或 /v1/images/edits；"
    "对话请使用文本模型（如 gpt-6 / auto）"
)


def ensure_chat_model(model: object) -> str:
    """Reject image-generation models on text endpoints."""
    normalized = str(model or "").strip()
    if normalized and is_supported_image_model(normalized):
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"model {normalized} is an image model and cannot be used on chat endpoints. "
                    + _IMAGE_MODEL_HINT,
                    "type": "invalid_request_error",
                    "code": "model_endpoint_mismatch",
                }
            },
        )
    return normalized


def ensure_image_model(model: object) -> str:
    """Reject text models on image endpoints."""
    normalized = str(model or "").strip()
    if normalized and not is_supported_image_model(normalized):
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "unsupported image model, supported models: "
                    + ", ".join(sorted(IMAGE_MODELS)),
                    "type": "invalid_request_error",
                    "code": "unsupported_model",
                }
            },
        )
    return normalized
