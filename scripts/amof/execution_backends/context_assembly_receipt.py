"""Per-call Native context-assembly receipts.

Side-channel evidence only. Does not select, plan, rewrite, or persist
model-visible prompt bodies. Does not change the model request.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "amof.context-assembly.receipt.v1"
RECEIPT_DIRNAME = "context-assembly"
MISSION_HEADER = "\nMission:\n"

# Hash input is the AMOF-owned OpenAI-style messages + tools list passed into
# `_chat_completion`, serialized as UTF-8 canonical JSON (sort_keys, compact
# separators). Transport remapping (Remote IAL system split / tool_call shape)
# is not included; receipts do not claim byte-perfect provider-wire equality.
HASH_INPUT = "amof_native.messages+tools.canonical_json_utf8"

NATIVE_SYSTEM_CONTENT = (
    "You are AMOF Native Agent. Use tools for repository work; stay within approved grants."
)

SYNTHESIS_INSTRUCTION = (
    "SYNTHESIS_REQUIRED: Stop tool exploration. Produce the final bounded result "
    "from the evidence already collected. Do not call tools. Do not invent "
    "unobserved facts."
)


class ReceiptExistsError(FileExistsError):
    """A completed receipt already occupies the deterministic path."""


def sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_canonical_json(value: Any) -> str:
    return sha256_utf8(canonical_json(value))


def visible_request(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Model-visible request at the `_chat_completion` argument boundary."""
    return {"messages": list(messages), "tools": list(tools or [])}


def call_receipt_path(receipt_dir: Path, call_index: int) -> Path:
    return receipt_dir / f"call-{int(call_index):04d}.json"


def mission_source_ref(request_id: str | None) -> str:
    rid = str(request_id or "").strip()
    if rid.startswith("handoff-"):
        return f"handoff:{rid[len('handoff-'):]}"
    if rid.startswith("handoff:"):
        return rid
    if rid:
        return f"amof_native:goal:{rid}"
    return "amof_native:goal"


def split_user_prompt(user_prompt: str, goal: str) -> tuple[str, str, str]:
    """Split `_build_prompt` output into envelope / mission / phase override.

    Does not change the prompt. Mission text is the original `goal` when the
    standard `\\nMission:\\n{goal}` suffix is present.
    """
    idx = user_prompt.rfind(MISSION_HEADER)
    if idx < 0:
        return user_prompt, "", ""
    envelope = user_prompt[:idx]
    rest = user_prompt[idx + len(MISSION_HEADER) :]
    if rest == goal:
        return envelope, goal, ""
    if goal and rest.startswith(goal):
        return envelope, goal, rest[len(goal) :]
    return envelope, rest, ""


def _section(
    *,
    name: str,
    source_kind: str,
    source_ref: str,
    authority_class: str,
    text: str,
) -> dict[str, Any]:
    raw = text.encode("utf-8")
    return {
        "name": name,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "authority_class": authority_class,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "tokens": None,
    }


def _known_omissions(*, tool_result_count: int) -> list[dict[str, str]]:
    omissions = [
        {"class": "repository-files", "reason": "jit-only"},
        {"class": "context-builder-master-md", "reason": "not-on-native-path"},
        {"class": "factory-markdown", "reason": "not-on-native-path"},
        {"class": "intake-packet-json", "reason": "not-on-native-path"},
    ]
    if tool_result_count == 0:
        omissions.insert(1, {"class": "prior-tool-results", "reason": "none-yet"})
    return omissions


def sections_from_messages(
    messages: list[dict[str, Any]],
    *,
    goal: str,
    request_id: str | None,
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    first_user = True
    extra_user = 0
    assistant_n = 0
    tool_n = 0
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role == "system":
            sections.append(
                _section(
                    name="system",
                    source_kind="runtime",
                    source_ref="amof_native:_run_model_loop",
                    authority_class="runtime-contract",
                    text=str(item.get("content") or ""),
                )
            )
            continue
        if role == "user":
            content = str(item.get("content") or "")
            if first_user:
                first_user = False
                envelope, mission, override = split_user_prompt(content, goal)
                if envelope:
                    sections.append(
                        _section(
                            name="runtime-envelope",
                            source_kind="runtime",
                            source_ref="hermes_opensandbox:_build_prompt",
                            authority_class="runtime-contract",
                            text=envelope,
                        )
                    )
                if mission:
                    sections.append(
                        _section(
                            name="mission",
                            source_kind="handoff",
                            source_ref=mission_source_ref(request_id),
                            authority_class="sealed-mission",
                            text=mission,
                        )
                    )
                if override.strip():
                    sections.append(
                        _section(
                            name="phase-override",
                            source_kind="runtime",
                            source_ref="hermes_opensandbox:_build_prompt",
                            authority_class="runtime-contract",
                            text=override.lstrip("\n"),
                        )
                    )
                if not envelope and not mission:
                    sections.append(
                        _section(
                            name="user",
                            source_kind="runtime",
                            source_ref="amof_native:_run_model_loop",
                            authority_class="runtime-state",
                            text=content,
                        )
                    )
                continue
            extra_user += 1
            source_ref = (
                "native_loop_budget:SYNTHESIS_INSTRUCTION"
                if content == SYNTHESIS_INSTRUCTION
                else "amof_native:_run_model_loop"
            )
            sections.append(
                _section(
                    name=f"user-{extra_user}",
                    source_kind="runtime",
                    source_ref=source_ref,
                    authority_class="runtime-contract",
                    text=content,
                )
            )
            continue
        if role == "assistant":
            assistant_n += 1
            sections.append(
                _section(
                    name=f"assistant-{assistant_n}",
                    source_kind="model",
                    source_ref="prior-model-message",
                    authority_class="model-generated",
                    text=canonical_json(
                        {
                            "content": item.get("content"),
                            "tool_calls": item.get("tool_calls") or [],
                        }
                    ),
                )
            )
            continue
        if role == "tool":
            tool_n += 1
            sections.append(
                _section(
                    name=f"tool-result-{tool_n}",
                    source_kind="tool",
                    source_ref=f"tool_call_id:{item.get('tool_call_id') or tool_n}",
                    authority_class="tool-result",
                    text=str(item.get("content") or ""),
                )
            )
    return sections


def build_call_receipt(
    *,
    run_id: str,
    call_index: int,
    model: str,
    provider: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    goal: str,
    request_id: str | None,
    prompt_tokens_reported: int | None,
    backend: str = "amof_native",
) -> dict[str, Any]:
    visible = visible_request(messages, tools)
    visible_text = canonical_json(visible)
    tool_specs = list(tools or [])
    tools_text = canonical_json(tool_specs)
    sections = sections_from_messages(messages, goal=goal, request_id=request_id)
    tool_result_count = sum(1 for item in sections if str(item.get("name") or "").startswith("tool-result-"))
    return {
        "schema": RECEIPT_SCHEMA,
        "run_id": run_id,
        "call_index": int(call_index),
        "backend": backend,
        "provider": provider,
        "model": model,
        "sections": sections,
        "tools": {
            "count": len(tool_specs),
            "schema_sha256": sha256_utf8(tools_text),
            "bytes": len(tools_text.encode("utf-8")),
        },
        "assembled": {
            "sha256": sha256_utf8(visible_text),
            "bytes": len(visible_text.encode("utf-8")),
            "prompt_tokens_reported": prompt_tokens_reported,
        },
        "known_omissions": _known_omissions(tool_result_count=tool_result_count),
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ReceiptExistsError(str(path))
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return path


def persist_call_receipt(
    *,
    run_dir: Path,
    run_id: str,
    call_index: int,
    model: str,
    provider: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    goal: str,
    request_id: str | None,
    prompt_tokens_reported: int | None,
    backend: str = "amof_native",
) -> Path:
    receipt_dir = run_dir / RECEIPT_DIRNAME
    path = call_receipt_path(receipt_dir, call_index)
    payload = build_call_receipt(
        run_id=run_id,
        call_index=call_index,
        model=model,
        provider=provider,
        messages=messages,
        tools=tools,
        goal=goal,
        request_id=request_id,
        prompt_tokens_reported=prompt_tokens_reported,
        backend=backend,
    )
    return atomic_write_json(path, payload)
