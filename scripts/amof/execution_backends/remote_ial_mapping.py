"""Shared Remote IAL response mapping for execution adapters."""

from __future__ import annotations

import json
import math
from typing import Any

def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _extract_remote_ial_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    remote_messages: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role == "system":
            content = item.get("content")
            if content:
                system_parts.append(str(content))
            continue
        if role == "assistant":
            message: dict[str, Any] = {"role": "assistant", "content": item.get("content")}
            tool_calls = []
            for tool_call in item.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                name = str(function.get("name") or "").strip()
                raw_args = function.get("arguments")
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else {}
                except json.JSONDecodeError:
                    arguments = {}
                if name:
                    tool_calls.append(
                        {
                            "id": str(tool_call.get("id") or ""),
                            "name": name,
                            "arguments": arguments if isinstance(arguments, dict) else {},
                        }
                    )
            if tool_calls:
                message["tool_calls"] = tool_calls
            remote_messages.append(message)
            continue
        if role == "tool":
            remote_messages.append(
                {
                    "role": "tool",
                    "results": [
                        {
                            "id": str(item.get("tool_call_id") or ""),
                            "tool_call_id": str(item.get("tool_call_id") or ""),
                            "content": item.get("content"),
                        }
                    ],
                }
            )
            continue
        remote_messages.append({"role": role or "user", "content": item.get("content")})
    return "\n\n".join(system_parts), remote_messages


def _remote_ial_tool_to_openai(item: dict[str, Any], index: int) -> dict[str, Any]:
    arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    return {
        "id": str(item.get("id") or f"remote-tool-{index}"),
        "type": "function",
        "function": {
            "name": str(item.get("name") or ""),
            "arguments": json.dumps(arguments, sort_keys=True),
        },
    }


def _finish_reason(stop_reason: Any, tool_calls: list[dict[str, Any]]) -> str:
    if tool_calls:
        return "tool_calls"
    normalized = str(stop_reason or "").strip().lower()
    if normalized in {"max_tokens", "length"}:
        return "length"
    return "stop"


