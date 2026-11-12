"""对外模型名与上游模型名的映射。

对外（`/v1/models`、响应体）统一暴露 ``gpt-6``；上游网页后端仍使用它认识的
slug（当前实测可用的是 ``gpt-5.6``，密码学上等价于 ``auto`` 的默认通道）。
"""

from __future__ import annotations

PUBLIC_CHAT_MODEL = "gpt-6"

# public name -> upstream slug
_UPSTREAM_MODELS = {
    "gpt-6": "gpt-5.6",
    "gpt-6-latest": "gpt-5.6",
    "gpt-6-mini": "gpt-5.6",
    # 兼容旧名，客户端仍可直接用 gpt-5.6
    "gpt-5.6": "gpt-5.6",
}

# upstream slug -> public name (catalog projection)
_PUBLIC_NAMES = {
    "gpt-5.6": PUBLIC_CHAT_MODEL,
}


def upstream_model(model: object) -> str:
    """Map the public model name to the upstream slug."""
    normalized = str(model or "").strip()
    if not normalized:
        return normalized
    return _UPSTREAM_MODELS.get(normalized.lower(), normalized)


def public_model_name(model: object) -> str:
    """Map a stored/upstream model name to the public name."""
    normalized = str(model or "").strip()
    if not normalized:
        return normalized
    return _PUBLIC_NAMES.get(normalized.lower(), normalized)
