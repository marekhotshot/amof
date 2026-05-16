"""Canonical LLM ladder profile catalog for the meeting slice."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Optional

from amof.api.services.runpod_heavy_lane import resolve_profile as resolve_runpod_heavy_profile

PROFILE_SLOTS = ("fast", "standard", "strong")

DEFAULT_BEDROCK_HAIKU_INFERENCE_PROFILE = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_BEDROCK_FAST_MODEL = DEFAULT_BEDROCK_HAIKU_INFERENCE_PROFILE
DEFAULT_BEDROCK_STANDARD_MODEL = DEFAULT_BEDROCK_HAIKU_INFERENCE_PROFILE
DEFAULT_BEDROCK_STRONG_MODEL = "anthropic.claude-opus-4-6-v1:0"

RECOMMENDED_DEMO_SELECTION = {
    "fast": "bedrock_claude_haiku",
    "standard": "bedrock_claude_haiku",
    "strong": "bedrock_claude_haiku",
}


@dataclass(frozen=True)
class LLMProfile:
    id: str
    label: str
    provider: str
    model_id: str
    description: str
    aws_profile_env: Optional[str] = None
    aws_region_env: Optional[str] = None
    base_url_env: Optional[str] = None
    api_key_env: Optional[str] = None
    source: str = "builtin"


def _bedrock_model_from_env(var_name: str, fallback: str) -> str:
    return str(os.environ.get(var_name) or fallback).strip()


def get_profile_catalog() -> Dict[str, LLMProfile]:
    runpod_profile = resolve_runpod_heavy_profile()
    return {
        "bedrock_claude_haiku": LLMProfile(
            id="bedrock_claude_haiku",
            label="Bedrock Claude Haiku",
            provider="bedrock",
            model_id=_bedrock_model_from_env("AMOF_BEDROCK_FAST_MODEL_ID", DEFAULT_BEDROCK_FAST_MODEL),
            description="Fast, low-latency Bedrock path for lightweight agent turns.",
            aws_profile_env="AWS_PROFILE",
            aws_region_env="AWS_REGION",
        ),
        "bedrock_claude_sonnet": LLMProfile(
            id="bedrock_claude_sonnet",
            label="Bedrock Claude Sonnet",
            provider="bedrock",
            model_id=_bedrock_model_from_env("AMOF_BEDROCK_STANDARD_MODEL_ID", DEFAULT_BEDROCK_STANDARD_MODEL),
            description="Primary Bedrock profile for the meeting demo.",
            aws_profile_env="AWS_PROFILE",
            aws_region_env="AWS_REGION",
        ),
        "bedrock_claude_opus": LLMProfile(
            id="bedrock_claude_opus",
            label="Bedrock Claude Opus",
            provider="bedrock",
            model_id=_bedrock_model_from_env("AMOF_BEDROCK_STRONG_MODEL_ID", DEFAULT_BEDROCK_STRONG_MODEL),
            description="Strong/high-reasoning Bedrock profile for premium meeting turns.",
            aws_profile_env="AWS_PROFILE",
            aws_region_env="AWS_REGION",
        ),
        "runpod_heavy": LLMProfile(
            id="runpod_heavy",
            label="RunPod Heavy",
            provider="runpod",
            model_id=str(runpod_profile.get("model") or "").strip(),
            description="Optional heavy/alternate path backed by the canonical RunPod heavy lane profile.",
            base_url_env=str(runpod_profile.get("endpoint_env") or "RUNPOD_OPENAI_BASE_URL"),
            api_key_env=str(runpod_profile.get("api_key_env") or "RUNPOD_API_KEY"),
            source=str(runpod_profile.get("source") or "runpod_profile"),
        ),
    }


def list_profile_catalog() -> List[Dict[str, Any]]:
    return [asdict(profile) for profile in get_profile_catalog().values()]


def get_profile_selection(cfg: Mapping[str, Any]) -> Dict[str, str]:
    raw = cfg.get("llm_profile_selection") if isinstance(cfg, Mapping) else None
    selection = dict(RECOMMENDED_DEMO_SELECTION)
    if isinstance(raw, Mapping):
        for slot in PROFILE_SLOTS:
            candidate = raw.get(slot)
            if isinstance(candidate, str) and candidate.strip():
                selection[slot] = candidate.strip()
    return selection


def validate_profile_selection(selection: Mapping[str, Any]) -> Dict[str, str]:
    catalog = get_profile_catalog()
    normalized: Dict[str, str] = {}
    for slot in PROFILE_SLOTS:
        candidate = selection.get(slot)
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        profile_id = candidate.strip()
        if profile_id not in catalog:
            raise ValueError(f"Unknown LLM profile id for {slot}: {profile_id}")
        normalized[slot] = profile_id
    return normalized


def build_clients_from_selection(selection: Mapping[str, str]) -> Dict[str, Any]:
    catalog = get_profile_catalog()
    clients: Dict[str, Any] = {}
    for slot in PROFILE_SLOTS:
        profile_id = selection.get(slot)
        if not profile_id:
            continue
        clients[slot] = build_client_for_profile(catalog[profile_id])
    return clients


def build_client_for_profile(profile: LLMProfile) -> Any:
    if profile.provider == "bedrock":
        from amof.orchestrator.llm.bedrock_anthropic import BedrockAnthropicClient

        return BedrockAnthropicClient(model=profile.model_id)
    if profile.provider == "runpod":
        from amof.orchestrator.llm.local_openai_compatible import LocalOpenAICompatibleClient

        base_url = str(os.environ.get(profile.base_url_env or "RUNPOD_OPENAI_BASE_URL") or "").strip()
        api_key = str(os.environ.get(profile.api_key_env or "RUNPOD_API_KEY") or "").strip()
        if not base_url:
            raise ValueError(
                f"{profile.id} requires {profile.base_url_env or 'RUNPOD_OPENAI_BASE_URL'} to be set."
            )
        if not api_key:
            raise ValueError(
                f"{profile.id} requires {profile.api_key_env or 'RUNPOD_API_KEY'} to be set."
            )
        return LocalOpenAICompatibleClient(
            base_url=base_url,
            model=profile.model_id,
            api_key=api_key,
            provider_id="runpod",
        )
    raise ValueError(f"Unsupported LLM profile provider: {profile.provider}")
