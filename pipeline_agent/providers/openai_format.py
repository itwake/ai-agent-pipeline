"""Project the conversation into OpenAI-style payloads and parse replies.

Two wire formats are supported:

* the Responses API (``input`` items, ``instructions``, ``reasoning.effort``)
* Chat Completions (``messages``, ``tools[].function``)

Both are produced from the same ``Message`` list and both reply shapes are
parsed by ``parse_reply``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from ..session import Message, ToolCall
from ..tools.base import ToolSpec
from .base import ModelReply, ProviderError

CALL_ID_MAX_LENGTH = 64

NOOP_TOOL_RESPONSES: dict[str, Any] = {
    "type": "function",
    "name": "_noop",
    "description": "Placeholder so that tool-call history is accepted.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def normalize_call_id(call_id: str) -> str:
    if len(call_id) <= CALL_ID_MAX_LENGTH:
        return call_id
    digest = hashlib.sha256(call_id.encode("utf-8")).hexdigest()
    return "call_" + digest[: CALL_ID_MAX_LENGTH - 5]


def arguments_text(call: ToolCall) -> str:
    if call.arguments_text:
        return call.arguments_text
    return json.dumps(call.arguments or {}, ensure_ascii=False, separators=(",", ":"))


# --- request projection ----------------------------------------------------


def responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": normalize_call_id(message.tool_call_id or ""),
                    "output": message.content,
                }
            )
            continue
        if message.role == "assistant":
            if message.content:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": message.content}]})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": normalize_call_id(call.id),
                        "name": call.name,
                        "arguments": arguments_text(call),
                    }
                )
            continue
        role = "user" if message.role not in ("user", "system", "developer") else message.role
        items.append({"role": role, "content": [{"type": "input_text", "text": message.content}]})
    return items


def chat_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    if system:
        projected.append({"role": "system", "content": system})
    for message in messages:
        if message.role == "tool":
            entry: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": message.tool_call_id or "",
                "content": message.content,
            }
            if message.name:
                entry["name"] = message.name
            projected.append(entry)
            continue
        if message.role == "assistant":
            entry = {"role": "assistant", "content": message.content or ""}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": arguments_text(call)},
                    }
                    for call in message.tool_calls
                ]
                if not message.content:
                    entry["content"] = None
            projected.append(entry)
            continue
        projected.append({"role": "user", "content": message.content})
    return projected


def responses_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "name": spec.name, "description": spec.description, "parameters": spec.parameters}
        for spec in specs
    ]


def chat_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": spec.name, "description": spec.description, "parameters": spec.parameters},
        }
        for spec in specs
    ]


def has_function_calls(items: list[dict[str, Any]]) -> bool:
    return any(item.get("type") in ("function_call", "function_call_output") for item in items)


# --- reply parsing ---------------------------------------------------------


def parse_reply(raw: dict[str, Any]) -> ModelReply:
    error = raw.get("error")
    if error:
        raise ProviderError(_format_error(error))

    texts: list[str] = []
    calls: list[ToolCall] = []
    output = raw.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "message":
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in ("output_text", "text") and part.get("text"):
                        texts.append(str(part["text"]))
                    elif part.get("type") == "refusal" and part.get("refusal"):
                        texts.append(str(part["refusal"]))
            elif item_type == "function_call":
                calls.append(_tool_call(item.get("call_id") or item.get("id"), item.get("name"), item.get("arguments"), len(calls)))
    else:
        choices = raw.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in ("text", "output_text") and part.get("text"):
                        texts.append(str(part["text"]))
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") or {}
                calls.append(_tool_call(tool_call.get("id"), function.get("name"), function.get("arguments"), len(calls)))

    usage = _usage(raw.get("usage"))
    finish = _finish_reason(raw, bool(calls))
    return ModelReply(text="".join(texts).strip(), tool_calls=calls, usage=usage, finish_reason=finish, raw=raw)


def _tool_call(call_id: Any, name: Any, arguments: Any, index: int) -> ToolCall:
    identifier = str(call_id or f"call_{index}")
    parsed: Optional[dict[str, Any]]
    text: str
    if arguments is None or arguments == "":
        parsed, text = {}, ""
    elif isinstance(arguments, str):
        text = arguments
        try:
            loaded = json.loads(arguments)
        except json.JSONDecodeError:
            loaded = None
        parsed = loaded if isinstance(loaded, dict) else None
    elif isinstance(arguments, dict):
        parsed = arguments
        text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    else:
        parsed, text = None, str(arguments)
    return ToolCall(id=identifier, name=str(name or ""), arguments=parsed, arguments_text=text)


def _usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    usage: dict[str, int] = {}
    for key, aliases in (
        ("input_tokens", ("input_tokens", "prompt_tokens")),
        ("output_tokens", ("output_tokens", "completion_tokens")),
        ("total_tokens", ("total_tokens",)),
    ):
        for alias in aliases:
            value = raw.get(alias)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = int(value)
                break
    return usage


def _finish_reason(raw: dict[str, Any], has_calls: bool) -> str:
    details = raw.get("incomplete_details")
    if isinstance(details, dict) and details.get("reason"):
        reason = str(details["reason"])
        return "length" if reason == "max_output_tokens" else reason
    choices = raw.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        if reason:
            return str(reason)
    return "tool_calls" if has_calls else "stop"


def _format_error(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message") or error.get("error") or json.dumps(error, ensure_ascii=False)
        code = error.get("code") or error.get("type")
        return f"{message} ({code})" if code else str(message)
    return str(error)


__all__ = [
    "NOOP_TOOL_RESPONSES",
    "arguments_text",
    "chat_messages",
    "chat_tools",
    "has_function_calls",
    "normalize_call_id",
    "parse_reply",
    "responses_input",
    "responses_tools",
]
