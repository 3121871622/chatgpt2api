from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable, Iterator

from fastapi import HTTPException

from services.protocol.chat_completion_cache import cache_key, chat_completion_cache, normalize_text_messages
from services.protocol.conversation import (
    ConversationRequest,
    ImageOutput,
    count_message_image_tokens,
    count_message_text_tokens,
    count_text_tokens,
    encode_images,
    normalize_messages,
    stream_image_outputs_with_pool,
    stream_text_deltas,
    text_backend,
)
from services.protocol.history import compact_history
from services.protocol.openai_tool_bridge import to_upstream_messages
from services.protocol.openai_v1_chat_complete import tool_turn
from services.protocol.reasoning import thinking_effort_from_body
from services.protocol.web_search_tool import (
    WEB_SEARCH_TOOL_TYPES,
    has_unsupported_tools,
    has_web_search_tool,
    normalized_sources,
    run_web_search,
    search_query_from_messages,
    text_with_url_citations,
)
from utils.helper import (
    extract_image_from_message_content,
    extract_response_prompt,
    has_response_image_generation_tool,
    is_supported_image_model,
)
from utils.image_tokens import (
    count_image_content_tokens,
    count_image_output_items_tokens,
    image_usage,
    image_output_metadata,
    token_usage,
)

TOOL_UNAVAILABLE_SYSTEM_MESSAGE = (
    "This compatibility backend cannot execute local tools, shell commands, non-search tools, "
    "or file operations. Do not claim to have run tools or inspected external resources. "
    "If a user asks you to use a tool, say that tool execution is unavailable through this backend."
)

RESPONSE_CONTENT_PART_TYPES = {"text", "input_text", "output_text", "image_url", "input_image", "image"}
DEFAULT_IMAGE_MODEL = "gpt-image-2"


def is_text_response_request(body: dict[str, Any]) -> bool:
    return not has_response_image_generation_tool(body)


def has_unsupported_response_tools(body: dict[str, Any]) -> bool:
    return has_unsupported_tools(body, {"image_generation", *WEB_SEARCH_TOOL_TYPES})


def response_image_tool(body: dict[str, Any]) -> dict[str, object]:
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "image_generation":
            return tool
    return {}


def response_image_model(model: object) -> str:
    """``/v1/responses`` 图片分支的模型解析：``auto`` 和文本模型都回落到默认图片模型。"""
    text = str(model or "").strip()
    if not text or text.lower() == "auto" or not is_supported_image_model(text):
        return DEFAULT_IMAGE_MODEL
    return text


def extract_response_image(input_value: object) -> tuple[bytes, str] | None:
    if isinstance(input_value, dict):
        if str(input_value.get("type") or "").strip() == "input_image":
            images = extract_image_from_message_content([input_value])
            return images[0] if images else None
        images = extract_image_from_message_content(input_value.get("content"))
        return images[0] if images else None
    if not isinstance(input_value, list):
        return None
    for item in reversed(input_value):
        if isinstance(item, dict):
            if str(item.get("type") or "").strip() == "input_image":
                images = extract_image_from_message_content([item])
                if images:
                    return images[0]
            images = extract_image_from_message_content(item.get("content"))
            if images:
                return images[0]
    return None


def _input_image_parts(input_value: object) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if isinstance(input_value, dict):
        content = input_value.get("content")
        if isinstance(content, list):
            parts.extend(item for item in content if isinstance(item, dict))
        return parts
    if not isinstance(input_value, list):
        return parts
    if all(isinstance(item, dict) and item.get("type") for item in input_value):
        return [item for item in input_value if isinstance(item, dict)]
    for item in input_value:
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, list):
                parts.extend(part for part in content if isinstance(part, dict))
    return parts


def _is_response_content_part(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    part_type = str(value.get("type") or "").strip()
    return part_type in RESPONSE_CONTENT_PART_TYPES or ("image_url" in value and part_type != "message")


def _message_content_from_response_item(item: dict[str, Any]) -> object:
    content = item.get("content")
    if isinstance(content, list):
        return [dict(part) if isinstance(part, dict) else part for part in content]
    if isinstance(content, str):
        return content
    return extract_response_prompt([item]) or content or ""


def _append_response_message(messages: list[dict[str, Any]], role: object, content: object) -> None:
    if isinstance(content, str):
        if content.strip():
            messages.append({"role": str(role or "user"), "content": content.strip()})
        return
    if isinstance(content, list) and content:
        messages.append({"role": str(role or "user"), "content": content})


def messages_from_input(input_value: object, instructions: object = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system_text = str(instructions or "").strip()
    if system_text:
        messages.append({"role": "system", "content": system_text})
    if isinstance(input_value, str):
        if input_value.strip():
            messages.append({"role": "user", "content": input_value.strip()})
        return messages
    if isinstance(input_value, dict):
        if _is_response_content_part(input_value):
            _append_response_message(messages, "user", [dict(input_value)])
            return messages
        _append_response_message(
            messages,
            input_value.get("role") or "user",
            _message_content_from_response_item(input_value),
        )
        return messages
    if isinstance(input_value, list):
        if all(_is_response_content_part(item) for item in input_value):
            _append_response_message(messages, "user", [dict(item) for item in input_value if isinstance(item, dict)])
            return messages
        pending_parts: list[dict[str, Any]] = []
        for item in input_value:
            if _is_response_content_part(item):
                pending_parts.append(dict(item))
                continue
            if pending_parts:
                _append_response_message(messages, "user", pending_parts)
                pending_parts = []
            if not isinstance(item, dict):
                continue
            _append_response_message(
                messages,
                item.get("role") or "user",
                _message_content_from_response_item(item),
            )
        if pending_parts:
            _append_response_message(messages, "user", pending_parts)
    return messages


def text_output_item(
    text: str,
    item_id: str | None = None,
    status: str = "completed",
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id or f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": annotations or []}],
    }


def web_search_call_item(
    query: str,
    item_id: str | None = None,
    status: str = "completed",
    sources: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": "search",
        "query": query,
        "queries": [query],
    }
    if sources:
        action["sources"] = [
            {"type": "url", "url": source["url"]}
            for source in sources
            if source.get("url")
        ]
    return {
        "id": item_id or f"ws_{uuid.uuid4().hex}",
        "type": "web_search_call",
        "status": status,
        "action": action,
    }


def image_output_items(prompt: str, data: list[dict[str, Any]], item_id: str | None = None) -> list[dict[str, Any]]:
    output = []
    for item in data:
        b64_json = str(item.get("b64_json") or "").strip()
        if b64_json:
            output.append({
                "id": item_id or f"ig_{len(output) + 1}",
                "type": "image_generation_call",
                "status": "completed",
                "result": b64_json,
                "revised_prompt": str(item.get("revised_prompt") or prompt).strip() or prompt,
            })
    return output


def response_created(response_id: str, model: str, created: int) -> dict[str, Any]:
    return {
        "type": "response.created",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "in_progress",
            "error": None,
            "incomplete_details": None,
            "model": model,
            "output": [],
            "parallel_tool_calls": False,
        },
    }


def response_completed(
    response_id: str,
    model: str,
    created: int,
    output: list[dict[str, Any]],
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "model": model,
            "output": output,
            "parallel_tool_calls": False,
        },
    }
    if usage:
        response["response"]["usage"] = usage
    return response


def _with_log_metadata(
    payload: dict[str, Any],
    account_email: str = "",
    conversation_id: str = "",
    image_urls: Iterable[str] | None = None,
    image_attempts: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if account_email:
        payload["_account_email"] = account_email
    if conversation_id:
        payload["_conversation_id"] = conversation_id
    urls = [str(url).strip() for url in image_urls or [] if str(url).strip()]
    if urls:
        payload["_image_urls"] = list(dict.fromkeys(urls))
    attempts = [dict(item) for item in image_attempts or [] if isinstance(item, dict)]
    if attempts:
        payload["_image_attempts"] = attempts
    return payload


def _backend_account_email(backend: object) -> str:
    return str(getattr(backend, "account_email", "") or "").strip()


def text_response_parts(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    messages = normalize_text_messages(normalize_messages(messages_from_input(body.get("input"), body.get("instructions"))))
    if has_unsupported_response_tools(body):
        messages.insert(0, {"role": "system", "content": TOOL_UNAVAILABLE_SYSTEM_MESSAGE})
    return model, messages


# ---------------------------------------------------------------- tool bridge

RESPONSE_FUNCTION_TOOL_TYPES = {"function"}


def function_response_tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Responses tools are flat; the bridge expects chat-completion shape."""
    tools: list[dict[str, Any]] = []
    raw_tools = body.get("tools")
    if not isinstance(raw_tools, list):
        return tools
    for tool in raw_tools:
        if not isinstance(tool, dict):
            continue
        if str(tool.get("type") or "") not in RESPONSE_FUNCTION_TOOL_TYPES:
            continue
        function: dict[str, Any] = {
            "name": str(tool.get("name") or "").strip(),
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        }
        if not function["name"]:
            continue
        tools.append({"type": "function", "function": function})
    return tools


def _chat_tool_choice(choice: object) -> object:
    if isinstance(choice, dict) and str(choice.get("type") or "") == "function":
        name = str(choice.get("name") or "").strip()
        if name:
            return {"type": "function", "function": {"name": name}}
    if isinstance(choice, str):
        return choice
    return None


def _tool_choice_required(choice: object) -> bool:
    converted = _chat_tool_choice(choice)
    if converted == "required":
        return True
    return bool(isinstance(converted, dict) and converted.get("function", {}).get("name"))


def _function_call_tool_calls(item: dict[str, Any]) -> dict[str, Any]:
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {}, ensure_ascii=False)
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}"),
            "type": "function",
            "function": {"name": str(item.get("name") or "").strip(), "arguments": arguments},
        }],
    }


def tool_messages_from_input(
    input_value: object,
    instructions: object = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: object = None,
) -> list[dict[str, Any]]:
    """Fold Responses input items into bridge history (function_call/result aware)."""
    if isinstance(input_value, str):
        # A plain string input must not be wrapped in a list: the item loop
        # below only understands dict items and would silently drop the user's
        # actual request, leaving only the tool contract.
        messages = messages_from_input(input_value, instructions)
        if tools:
            messages = to_upstream_messages(
                messages,
                tools,
                required=_tool_choice_required(tool_choice),
            )
            if messages and str(messages[0].get("role") or "") == "system":
                prompt = str(messages[0].get("content") or "")
                messages = _inject_prompt_into_last_user(messages[1:], prompt)
        return messages
    messages: list[dict[str, Any]] = []
    items = input_value if isinstance(input_value, list) else [input_value]
    pending: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type == "function_call":
            if pending:
                messages.extend(messages_from_input(pending))
                pending = []
            messages.append(_function_call_tool_calls(item))
            continue
        if item_type == "function_call_output":
            if pending:
                messages.extend(messages_from_input(pending))
                pending = []
            messages.append({
                "role": "tool",
                "tool_call_id": str(item.get("call_id") or ""),
                "content": function_call_output_text(item.get("output")),
            })
            continue
        pending.append(item)
    if pending:
        messages.extend(messages_from_input(pending))
    if instructions and str(instructions).strip():
        messages.insert(0, {"role": "system", "content": str(instructions).strip()})
    if tools:
        messages = to_upstream_messages(
            messages,
            tools,
            required=_tool_choice_required(tool_choice),
        )
        if messages and str(messages[0].get("role") or "") == "system":
            prompt = str(messages[0].get("content") or "")
            messages = _inject_prompt_into_last_user(messages[1:], prompt)
    return messages


def _inject_prompt_into_last_user(
    messages: list[dict[str, Any]],
    prompt: str,
) -> list[dict[str, Any]]:
    if not prompt:
        return messages
    injected = [dict(message) for message in messages]
    for index in range(len(injected) - 1, -1, -1):
        if str(injected[index].get("role") or "") == "user":
            content = injected[index].get("content")
            text = content if isinstance(content, str) else _plain_content_text(content)
            injected[index] = {
                **injected[index],
                "content": f"{prompt}\n\n---\n\nUser request:\n{text}".strip(),
            }
            return injected
    injected.append({"role": "user", "content": prompt})
    return injected


def _plain_content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and str(item.get("type") or "text") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return ""


def function_call_output_text(output: object) -> str:
    """``function_call_output.output`` 可能是内容块数组：只抽文本，避免 ``str(list)``。"""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        output = [output]
    if isinstance(output, list):
        text = _plain_content_text(output)
        if text:
            return text
        return json.dumps(output, ensure_ascii=False) if output else ""
    if output is None:
        return ""
    return str(output)


def function_call_item(call: dict[str, Any], status: str = "completed", item_id: str = "") -> dict[str, Any]:
    return {
        "id": item_id or f"fc_{uuid.uuid4().hex}",
        "type": "function_call",
        "status": status,
        "call_id": call["id"],
        "name": call["function"]["name"],
        "arguments": call["function"]["arguments"],
    }


def stream_tool_response(body: dict[str, Any], tools: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Responses 端点的工具轮：缓冲整轮后按 function_call 输出。"""
    model = str(body.get("model") or "auto").strip() or "auto"
    # tool_messages_from_input already applies to_upstream_messages and moves
    # the tool contract into the last user message. Do not inject it twice.
    messages = normalize_text_messages(normalize_messages(tool_messages_from_input(
        body.get("input"),
        body.get("instructions"),
        tools,
        body.get("tool_choice"),
    )))
    messages = compact_history(messages)
    thinking_effort = thinking_effort_from_body(body)
    backend = text_backend()
    text, calls = tool_turn(backend, messages, model, thinking_effort, tools)
    account_email = _backend_account_email(backend)
    response_id = f"resp_{uuid.uuid4().hex}"
    created = int(time.time())
    yield response_created(response_id, model, created)
    output: list[dict[str, Any]] = []
    if calls:
        for index, call in enumerate(calls):
            item_id = f"fc_{uuid.uuid4().hex}"
            arguments = call["function"]["arguments"]
            yield _with_log_metadata(
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {**function_call_item(call, "in_progress", item_id), "arguments": ""},
                },
                account_email,
            )
            for start in range(0, len(arguments), 200):
                yield _with_log_metadata(
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item_id,
                        "output_index": index,
                        "delta": arguments[start:start + 200],
                    },
                    account_email,
                )
            item = function_call_item(call, "completed", item_id)
            yield _with_log_metadata(
                {"type": "response.output_item.done", "output_index": index, "item": item},
                account_email,
            )
            output.append(item)
    else:
        item_id = f"msg_{uuid.uuid4().hex}"
        yield _with_log_metadata(
            {"type": "response.output_item.added", "output_index": 0, "item": text_output_item("", item_id, "in_progress")},
            account_email,
        )
        if text:
            yield _with_log_metadata(
                {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text},
                account_email,
            )
        yield _with_log_metadata(
            {"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": text},
            account_email,
        )
        item = text_output_item(text, item_id, "completed")
        yield _with_log_metadata(
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            account_email,
        )
        output.append(item)
    usage = token_usage(
        input_text_tokens=count_message_text_tokens(messages, model),
        input_image_tokens=count_message_image_tokens(messages, model),
        output_text_tokens=count_text_tokens(text, model),
    )
    completed = response_completed(response_id, model, created, output, usage)
    _with_log_metadata(completed, account_email)
    _with_log_metadata(completed["response"], account_email)
    yield completed


def stream_text_response(backend, body: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    messages = messages if messages is not None else messages_from_input(body.get("input"), body.get("instructions"))
    thinking_effort = thinking_effort_from_body(body)
    response_id = f"resp_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())
    full_text = ""
    yield response_created(response_id, model, created)
    yield {"type": "response.output_item.added", "output_index": 0, "item": text_output_item("", item_id, "in_progress")}
    request = ConversationRequest(model=model, messages=messages, thinking_effort=thinking_effort)
    for delta in stream_text_deltas(backend, request):
        full_text += delta
        yield _with_log_metadata(
            {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": delta},
            _backend_account_email(backend),
        )
    yield _with_log_metadata(
        {"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": full_text},
        _backend_account_email(backend),
    )
    item = text_output_item(full_text, item_id, "completed")
    yield _with_log_metadata({"type": "response.output_item.done", "output_index": 0, "item": item}, _backend_account_email(backend))
    usage = token_usage(
        input_text_tokens=count_message_text_tokens(messages, model),
        input_image_tokens=count_message_image_tokens(messages, model),
        output_text_tokens=count_text_tokens(full_text, model),
    )
    completed = response_completed(response_id, model, created, [item], usage)
    _with_log_metadata(completed, _backend_account_email(backend))
    _with_log_metadata(completed["response"], _backend_account_email(backend))
    yield completed


def stream_web_search_response(body: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    messages = messages if messages is not None else messages_from_input(body.get("input"), body.get("instructions"))
    query = search_query_from_messages(messages) or extract_response_prompt(body.get("input"))
    if not query:
        raise HTTPException(status_code=400, detail={"error": "input text is required for web_search"})

    response_id = f"resp_{uuid.uuid4().hex}"
    search_id = f"ws_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())
    yield response_created(response_id, model, created)

    searching_item = web_search_call_item(query, search_id, "in_progress")
    yield {"type": "response.output_item.added", "output_index": 0, "item": searching_item}
    yield {"type": "response.web_search_call.in_progress", "output_index": 0, "item_id": search_id}
    yield {"type": "response.web_search_call.searching", "output_index": 0, "item_id": search_id}
    result = run_web_search(query)
    search_item = web_search_call_item(query, search_id, "completed", normalized_sources(result))
    yield {"type": "response.web_search_call.completed", "output_index": 0, "item_id": search_id}
    yield {"type": "response.output_item.done", "output_index": 0, "item": search_item}

    text, annotations = text_with_url_citations(result)
    message_item = text_output_item("", item_id, "in_progress", annotations)
    yield {"type": "response.output_item.added", "output_index": 1, "item": message_item}
    if text:
        yield {"type": "response.output_text.delta", "item_id": item_id, "output_index": 1, "content_index": 0, "delta": text}
    yield {"type": "response.output_text.done", "item_id": item_id, "output_index": 1, "content_index": 0, "text": text}
    message_item = text_output_item(text, item_id, "completed", annotations)
    yield {"type": "response.output_item.done", "output_index": 1, "item": message_item}
    usage = token_usage(
        input_text_tokens=count_message_text_tokens(messages, model),
        input_image_tokens=count_message_image_tokens(messages, model),
        output_text_tokens=count_text_tokens(text, model),
    )
    yield response_completed(response_id, model, created, [search_item, message_item], usage)


def stream_image_response(
    image_outputs: Iterable[ImageOutput],
    prompt: str,
    model: str,
    input_image_tokens: int = 0,
    size: object = None,
    quality: str = "auto",
) -> Iterator[dict[str, Any]]:
    response_id = f"resp_{uuid.uuid4().hex}"
    created = int(time.time())
    yield response_created(response_id, model, created)
    for output in image_outputs:
        if output.kind == "message":
            text = output.text
            item = text_output_item(text)
            usage = token_usage(
                input_text_tokens=count_text_tokens(prompt, model),
                input_image_tokens=input_image_tokens,
                output_text_tokens=count_text_tokens(text, model),
            )
            yield _with_log_metadata(
                {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": text},
                output.account_email,
                output.conversation_id,
                image_attempts=output.image_attempts,
            )
            yield _with_log_metadata(
                {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0, "content_index": 0, "text": text},
                output.account_email,
                output.conversation_id,
                image_attempts=output.image_attempts,
            )
            yield _with_log_metadata(
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                output.account_email,
                output.conversation_id,
                image_attempts=output.image_attempts,
            )
            completed = response_completed(response_id, model, created, [item], usage)
            _with_log_metadata(completed, output.account_email, output.conversation_id, image_attempts=output.image_attempts)
            _with_log_metadata(completed["response"], output.account_email, output.conversation_id, image_attempts=output.image_attempts)
            yield completed
            return
        if output.kind != "result":
            continue
        items = image_output_items(prompt, output.data)
        if items:
            usage = image_usage(
                input_text_tokens=count_text_tokens(prompt, model),
                input_image_tokens=input_image_tokens,
                output_tokens=count_image_output_items_tokens(output.data, size, quality),
            )
            for output_index, item in enumerate(items):
                yield _with_log_metadata(
                    {"type": "response.output_item.done", "output_index": output_index, "item": item},
                    output.account_email,
                    output.conversation_id,
                    output.image_urls,
                    output.image_attempts,
                )
            completed = response_completed(response_id, model, created, items, usage)
            _with_log_metadata(completed, output.account_email, output.conversation_id, output.image_urls, output.image_attempts)
            _with_log_metadata(completed["response"], output.account_email, output.conversation_id, output.image_urls, output.image_attempts)
            completed["response"]["_image_metadata"] = image_output_metadata(output.data)
            yield completed
            return
    raise RuntimeError("image generation failed")


def collect_response(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    completed = {}
    for event in events:
        if event.get("type") == "response.completed":
            completed = event.get("response") if isinstance(event.get("response"), dict) else {}
    if not completed:
        raise RuntimeError("response generation failed")
    texts: list[str] = []
    for item in completed.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                texts.append(str(part.get("text") or ""))
    completed["output_text"] = "".join(texts)
    return completed


def response_events(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if is_text_response_request(body):
        tools = function_response_tools(body)
        if tools:
            yield from stream_tool_response(body, tools)
            return
        model, messages = text_response_parts(body)
        if has_web_search_tool(body) and not has_unsupported_response_tools(body):
            yield from stream_web_search_response(body, messages)
            return
        key = cache_key(body, messages, stream=bool(body.get("stream")))
        yield from chat_completion_cache.get_or_compute_stream(
            key,
            lambda: stream_text_response(text_backend(), body, messages),
        )
        return

    prompt = extract_response_prompt(body.get("input"))
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": "input text is required"})
    model = response_image_model(body.get("model"))
    image_info = extract_response_image(body.get("input"))
    if image_info:
        image_data, mime_type = image_info
        images = encode_images([(image_data, "image.png", mime_type)])
    else:
        images = None
    input_image_tokens = count_image_content_tokens(_input_image_parts(body.get("input")), model)
    tool = response_image_tool(body)
    image_outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        size=tool.get("size"),
        quality=str(tool.get("quality") or "auto"),
        response_format="b64_json",
        images=images,
        message_as_error=True,
        call_id=str(body.get("_call_id") or ""),
        trace_image_perf=bool(body.get("_trace_image_perf")),
    ))
    yield from stream_image_response(image_outputs, prompt, model, input_image_tokens, tool.get("size"), str(tool.get("quality") or "auto"))


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    events = response_events(body)
    if body.get("stream"):
        return events
    return collect_response(events)
