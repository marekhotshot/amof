"""Canonical runtime usage helpers — amof.runtime_usage/v1.

Truth rules:
- null/unavailable means the source did not expose the field
- 0 means an authoritative source reported zero
- never estimate tokens from text length
- never coerce null → 0 for UI convenience
"""

from __future__ import annotations

import math
from typing import Any

RUNTIME_USAGE_SCHEMA = "amof.runtime_usage/v1"

USAGE_SOURCE_PROVIDER = "AUTHORITATIVE_PROVIDER_USAGE"
USAGE_SOURCE_SUBSTRATE = "AUTHORITATIVE_SUBSTRATE_USAGE"
USAGE_SOURCE_NORMALIZED = "AMOF_NORMALIZED_USAGE"
USAGE_SOURCE_DERIVED = "DERIVED_AGGREGATE"
USAGE_SOURCE_DIAGNOSTIC = "DIAGNOSTIC_ONLY"
USAGE_SOURCE_UNAVAILABLE = "UNAVAILABLE"


def finite_int(value: Any) -> int | None:
    """Return non-negative int when value is a finite number; else None.

    Does not coerce missing/None/"" to 0.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def empty_usage_accumulator() -> dict[str, Any]:
    return {
        "prompt_tokens": None,
        "completion_tokens": None,
        "cache_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "model_calls": 0,
        "tool_calls": 0,
        "agent_calls": None,
        "estimated_cost_usd": None,
        "cost_status": None,
        "calls": [],
        "saw_authoritative_tokens": False,
    }


def add_token_field(acc: dict[str, Any], field: str, value: Any) -> None:
    parsed = finite_int(value)
    if parsed is None:
        return
    current = acc.get(field)
    acc[field] = (int(current) if current is not None else 0) + parsed
    if field in {"prompt_tokens", "completion_tokens", "cache_tokens", "total_tokens"}:
        acc["saw_authoritative_tokens"] = True


def token_telemetry_status(
    *,
    saw_tokens: bool,
    model_calls: int | None,
    partial_dimensions: bool,
) -> str:
    if saw_tokens and not partial_dimensions:
        return "available"
    if saw_tokens and partial_dimensions:
        return "partial"
    if model_calls and model_calls > 0:
        return "unavailable"
    return "unavailable"


def build_agent_run_usage(
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cache_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    total_tokens: int | None = None,
    model_calls: int | None = None,
    tool_calls: int | None = None,
    agent_calls: int | None = None,
    billing_model: str,
    token_telemetry: str,
    subagent_telemetry: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_tokens": cache_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "agent_calls": agent_calls,
        "billing_model": billing_model,
        "token_telemetry": token_telemetry,
        "subagent_telemetry": subagent_telemetry,
    }
    if extra:
        usage.update(extra)
    return usage


def build_runtime_usage_v1(
    *,
    run_id: str,
    backend: str,
    billing_model: str,
    telemetry_status: str,
    usage_source: str,
    aggregates: dict[str, Any],
    by_model: list[dict[str, Any]] | None = None,
    spans: list[dict[str, Any]] | None = None,
    receipt_refs: list[str] | None = None,
    raw_usage_refs: list[str] | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": RUNTIME_USAGE_SCHEMA,
        "run_id": run_id,
        "candidate_id": candidate_id,
        "backend": backend,
        "billing_model": billing_model,
        "telemetry_status": telemetry_status,
        "aggregates": {
            "input_tokens": aggregates.get("prompt_tokens"),
            "output_tokens": aggregates.get("completion_tokens"),
            "cached_tokens": aggregates.get("cache_tokens"),
            "reasoning_tokens": aggregates.get("reasoning_tokens"),
            "total_tokens": aggregates.get("total_tokens"),
            "model_calls": aggregates.get("model_calls"),
            "agent_calls": aggregates.get("agent_calls"),
            "tool_calls": aggregates.get("tool_calls"),
        },
        "by_model": list(by_model or []),
        "spans": list(spans or []),
        "provenance": {
            "usage_source": usage_source,
            "receipt_refs": list(receipt_refs or []),
            "raw_usage_refs": list(raw_usage_refs or []),
        },
    }


def normalize_cursor_sdk_usage(sdk_usage: dict[str, Any] | None) -> dict[str, Any]:
    """Map Cursor TokenUsage fields → governed usage (proven semantics only)."""
    raw = dict(sdk_usage or {})
    input_tokens = finite_int(raw.get("input_tokens"))
    output_tokens = finite_int(raw.get("output_tokens"))
    cache_read = finite_int(raw.get("cache_read_tokens"))
    cache_write = finite_int(raw.get("cache_write_tokens"))
    total_tokens = finite_int(raw.get("total_tokens"))
    reasoning_tokens = finite_int(raw.get("reasoning_tokens"))

    cache_tokens: int | None = None
    if cache_read is not None or cache_write is not None:
        cache_tokens = int(cache_read or 0) + int(cache_write or 0)

    saw = any(
        v is not None
        for v in (input_tokens, output_tokens, total_tokens, cache_tokens, reasoning_tokens)
    )
    return {
        "raw": {
            key: raw[key]
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "total_tokens",
                "reasoning_tokens",
            )
            if key in raw and raw[key] is not None
        },
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "saw_authoritative_tokens": saw,
        # Cumulative per-run aggregate from SDK; not per model-call.
        "granularity": "per_run_aggregate",
        "semantic_notes": {
            "input_tokens": "Prompt tokens sent to the model (Cursor TokenUsage).",
            "output_tokens": "Tokens generated; includes reasoning when reported.",
            "total_tokens": "input+output+cache_read+cache_write; excludes reasoning_tokens.",
            "reasoning_tokens": "Subset of output_tokens; omitted from total to avoid double-count.",
        },
    }


def remote_ial_tokens_from_body(remote: dict[str, Any]) -> tuple[int | None, int | None]:
    """Extract IAL tokens.input/output without null→0 coercion."""
    tokens = remote.get("tokens") if isinstance(remote.get("tokens"), dict) else {}
    if not tokens:
        return None, None
    # Prefer key presence: missing key → None; present 0 → 0.
    input_tokens = finite_int(tokens["input"]) if "input" in tokens else None
    output_tokens = finite_int(tokens["output"]) if "output" in tokens else None
    return input_tokens, output_tokens
