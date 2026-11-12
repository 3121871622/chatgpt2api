"""对话历史压缩与角色归一化。

网页后端没有原生 tool 通道，且长对话会把整段历史重复发给上游；这里做两件事：

* 把 ``role: tool`` / ``function`` 与 assistant 的 ``tool_calls`` 归一成纯文本，
  避免上游 500；
* 超过消息条数或字符预算时，保留全部 system 与最近若干轮，避免请求体无限膨胀。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

DEFAULT_MAX_MESSAGES = 60
DEFAULT_MAX_CHARS = 60_000


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _calls_text(calls: object) -> str:
    if not isinstance(calls, list):
        return ""
    rendered: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else call
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            rendered.append(f"{name}({arguments})")
        else:
            rendered.append(f"{name}({json.dumps(arguments or {}, ensure_ascii=False)})")
    return "; ".join(rendered)


def normalize_history_for_upstream(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip().lower()
        text = _text(message.get("content"))
        if role == "tool":
            label = str(message.get("name") or message.get("tool_call_id") or "tool").strip()
            normalized.append({"role": "user", "content": f"【工具结果 {label}】\n{text}".strip()})
            continue
        if role == "function":
            normalized.append({"role": "user", "content": f"【工具结果】\n{text}".strip()})
            continue
        if role == "assistant":
            calls = _calls_text(message.get("tool_calls"))
            if calls:
                prefix = "【调用工具】" + calls
                normalized.append({"role": "assistant", "content": f"{prefix}\n{text}".strip()})
                continue
        normalized.append({"role": role, "content": text})
    return normalized


def _total_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(_text(message.get("content"))) for message in messages)


def compact_history(
    messages: Iterable[dict[str, Any]],
    *,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Keep system prompts and the most recent turns within budget."""
    items = [message for message in messages if isinstance(message, dict)]
    systems = [message for message in items if str(message.get("role") or "") == "system"]
    rest = [message for message in items if str(message.get("role") or "") != "system"]
    kept = rest[-max(1, int(max_messages)):] if len(rest) > max_messages else list(rest)
    while len(kept) > 1 and _total_chars(systems + kept) > max_chars:
        kept.pop(0)
    return systems + kept


def prepare_chat_history(
    messages: Iterable[dict[str, Any]],
    *,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Normalize roles, then compact within budget."""
    normalized = normalize_history_for_upstream(messages)
    return compact_history(
        normalized,
        max_messages=max_messages,
        max_chars=max_chars,
    )
