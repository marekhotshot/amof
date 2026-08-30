"""First-turn Native context-assembly receipt.

Records what AMOF assembled before JIT. Does not select, plan, or rewrite
context. Does not persist prompt bodies.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "amof.context-assembly.receipt.v1"
RECEIPT_FILENAME = "context-assembly.json"
MISSION_HEADER = "\nMission:\n"

NATIVE_SYSTEM_CONTENT = (
    "You are AMOF Native Agent. Use tools for repository work; stay within approved grants."
)

NATIVE_OMISSIONS: tuple[dict[str, str], ...] = (
    {"class": "repository-files", "reason": "jit-only"},
    {"class": "conversation-history", "reason": "single-run"},
    {"class": "context-builder-master-md", "reason": "not-on-native-path"},
    {"class": "factory-markdown", "reason": "not-on-native-path"},
    {"class": "intake-packet-json", "reason": "not-on-native-path"},
)


def sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_canonical_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_utf8(encoded)


def _section(
    *,
    name: str,
    authority_class: str,
    source: str,
    text: str,
    tokens: int | None = None,
) -> dict[str, Any]:
    raw = text.encode("utf-8")
    return {
        "name": name,
        "authority_class": authority_class,
        "source": source,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "tokens": tokens,
    }


def mission_source(request_id: str | None) -> str:
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


def build_receipt(
    *,
    run_id: str,
    model: str,
    system_text: str,
    user_prompt: str,
    goal: str,
    tool_specs: list[dict[str, Any]],
    request_id: str | None = None,
    prompt_tokens: int | None = None,
    call_index: int = 1,
) -> dict[str, Any]:
    envelope, mission, phase_override = split_user_prompt(user_prompt, goal)
    sections: list[dict[str, Any]] = [
        _section(
            name="system",
            authority_class="runtime-contract",
            source="amof_native:_chat_completion",
            text=system_text,
        ),
        _section(
            name="runtime-envelope",
            authority_class="runtime-contract",
            source="hermes_opensandbox:_build_prompt",
            text=envelope,
        ),
    ]
    if mission:
        sections.append(
            _section(
                name="mission",
                authority_class="sealed-mission",
                source=mission_source(request_id),
                text=mission,
            )
        )
    if phase_override.strip():
        sections.append(
            _section(
                name="phase-override",
                authority_class="runtime-contract",
                source="hermes_opensandbox:_build_prompt",
                text=phase_override.lstrip("\n"),
            )
        )
    tools_hash = sha256_canonical_json(tool_specs)
    assembled_hash = sha256_canonical_json(
        {
            "system": system_text,
            "user": user_prompt,
            "tools_schema_hash": tools_hash,
        }
    )
    return {
        "schema": RECEIPT_SCHEMA,
        "run_id": run_id,
        "call_index": call_index,
        "model": model,
        "sections": sections,
        "tools": {
            "count": len(tool_specs),
            "schema_hash": tools_hash,
        },
        "assembled": {
            "sha256": assembled_hash,
            "prompt_tokens": prompt_tokens,
        },
        "omissions": [dict(item) for item in NATIVE_OMISSIONS],
    }


def write_receipt(path: Path, receipt: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def record_prompt_tokens(path: Path, prompt_tokens: int | None) -> None:
    """Join provider prompt_tokens onto an existing first-turn receipt."""
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    assembled = payload.get("assembled")
    if not isinstance(assembled, dict):
        assembled = {}
        payload["assembled"] = assembled
    assembled["prompt_tokens"] = prompt_tokens
    write_receipt(path, payload)
