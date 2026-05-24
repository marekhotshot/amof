"""Guided AMOF setup commands."""

from __future__ import annotations

from copy import deepcopy
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from ..app_config import activate_provider_profile_ref, get_current_context_name
from ..app_paths import ensure_parent_dir, provider_profiles_dir


PROVIDER_TEMPLATE_ORDER = ("openrouter", "local-qwen", "openai", "anthropic", "bedrock", "remote-ial", "xai", "runpod")

PROVIDER_TEMPLATES: dict[str, dict[str, Any]] = {
    "openrouter": {
        "name": "openrouter-default",
        "provider": "openrouter",
        "lane": "planner",
        "model_family": "openai-compatible",
        "model_env": "AMOF_OPENROUTER_MODEL",
        "default_model": "openrouter/openai/gpt-4o-mini",
        "credential_refs": {
            "api_key_env": "OPENROUTER_API_KEY",
            "base_url_env": "OPENROUTER_OPENAI_BASE_URL",
        },
        "redaction_policy": {"record_secret_names_only": True},
        "allow_direct_git_write": False,
        "status": "setup_profile_only",
        "notes": [
            "Stores environment variable names only; export OPENROUTER_API_KEY before live agent calls.",
            "Current CLI execution also accepts --provider openrouter.",
        ],
    },
    "local-qwen": {
        "name": "local-qwen",
        "provider": "local",
        "lane": "worker",
        "model_family": "openai-compatible",
        "model_env": "AMOF_LOCAL_QWEN_MODEL",
        "default_model": "qwen2.5-coder:7b",
        "credential_refs": {},
        "base_url_env": "AMOF_LOCAL_OPENAI_BASE_URL",
        "default_base_url": "http://localhost:11434/v1",
        "timeout_seconds": 60.0,
        "redaction_policy": {"record_secret_names_only": True},
        "allow_direct_git_write": False,
        "status": "template_only_until_runner_profile_is_configured",
        "notes": [
            "No API key is required by default for local Ollama-compatible endpoints.",
            "Start the local OpenAI-compatible server before using this profile for live inference.",
            "Local SDK retries are disabled; tune timeout_seconds for slower local generations.",
        ],
    },
    "openai": {
        "name": "openai-default",
        "provider": "openai",
        "lane": "planner",
        "model_family": "openai-compatible",
        "model_env": "AMOF_OPENAI_MODEL",
        "default_model": "gpt-4o",
        "credential_refs": {"api_key_env": "OPENAI_API_KEY"},
        "redaction_policy": {"record_secret_names_only": True},
        "allow_direct_git_write": False,
        "status": "setup_profile_only",
        "notes": [
            "Stores the OPENAI_API_KEY environment variable name only.",
            "Current CLI execution also accepts --provider openai.",
        ],
    },
    "anthropic": {
        "name": "anthropic-default",
        "provider": "anthropic",
        "lane": "planner",
        "model_family": "anthropic",
        "model_env": "AMOF_ANTHROPIC_MODEL",
        "default_model": "claude-sonnet-4-5",
        "credential_refs": {"api_key_env": "ANTHROPIC_API_KEY"},
        "redaction_policy": {"record_secret_names_only": True},
        "allow_direct_git_write": False,
        "status": "setup_profile_only",
        "notes": [
            "Stores the ANTHROPIC_API_KEY environment variable name only.",
            "Anthropic remains the default provider when no agent provider is configured.",
        ],
    },
    "bedrock": {
        "name": "bedrock-default",
        "provider": "bedrock",
        "lane": "planner",
        "model_family": "anthropic-bedrock",
        "model_env": "AMOF_BEDROCK_STANDARD_MODEL_ID",
        "default_model": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        "credential_refs": {},
        "redaction_policy": {"record_secret_names_only": True},
        "allow_direct_git_write": False,
        "status": "setup_profile_only",
        "notes": [
            "Stores environment variable names only; Bedrock credentials stay in AWS_PROFILE/AWS_REGION or AMOF_BEDROCK_REGION.",
            "Corporate TLS environments may need SSL_CERT_FILE or REQUESTS_CA_BUNDLE plus AWS_CA_BUNDLE.",
            "Current CLI execution also accepts --provider bedrock and does not call AWS during setup.",
        ],
    },
    "remote-ial": {
        "name": "remote-ial-default",
        "provider": "remote-ial",
        "lane": "planner",
        "model_family": "remote-ial",
        "model_env": "AMOF_REMOTE_IAL_MODEL",
        "default_model": "remote-ial/default",
        "credential_refs": {
            "api_key_env": "AMOF_REMOTE_IAL_API_KEY",
            "base_url_env": "AMOF_REMOTE_IAL_BASE_URL",
        },
        "timeout_seconds": 90.0,
        "redaction_policy": {"record_secret_names_only": True},
        "allow_direct_git_write": False,
        "status": "private_gateway_contract_only",
        "notes": [
            "Routes model calls through a private remote IAL gateway; public AMOF stores only env var names and local evidence metadata.",
            "Set AMOF_REMOTE_IAL_BASE_URL and AMOF_REMOTE_IAL_API_KEY before live calls.",
        ],
    },
    "xai": {
        "name": "xai-default",
        "provider": "xai",
        "lane": "planner",
        "model_family": "openai-compatible",
        "model_env": "AMOF_XAI_MODEL",
        "credential_refs": {
            "api_key_env": "XAI_API_KEY",
            "base_url_env": "XAI_OPENAI_BASE_URL",
        },
        "redaction_policy": {"record_secret_names_only": True},
        "allow_direct_git_write": False,
        "status": "template_only_until_provider_resolver_support",
        "notes": [
            "xAI profile metadata is available for planning/bootstrap records.",
            "Current public agent execution may require provider resolver support before xAI can be used directly.",
        ],
    },
    "runpod": {
        "name": "runpod-heavy",
        "provider": "runpod",
        "lane": "heavy-lane",
        "model_family": "openai-compatible",
        "model_env": "AMOF_RUNPOD_MODEL",
        "credential_refs": {
            "api_key_env": "RUNPOD_API_KEY",
            "base_url_env": "RUNPOD_OPENAI_BASE_URL",
        },
        "redaction_policy": {"record_secret_names_only": True},
        "allow_direct_git_write": False,
        "status": "template_only_until_runner_profile_is_configured",
        "notes": [
            "Runpod uses an OpenAI-compatible endpoint URL from RUNPOD_OPENAI_BASE_URL.",
            "This setup command records references only and does not call Runpod.",
        ],
    },
}

ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_PREFIXES = ("sk-", "sk_", "xox", "ghp_", "github_pat_", "pat_", "eyJ")


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, sort_keys=False)


def _template_for(provider_name: str) -> dict[str, Any]:
    normalized = str(provider_name or "").strip()
    if normalized not in PROVIDER_TEMPLATES:
        options = ", ".join(PROVIDER_TEMPLATE_ORDER)
        raise ValueError(f"unknown provider template: {normalized or '<empty>'}. Expected one of: {options}")
    return deepcopy(PROVIDER_TEMPLATES[normalized])


def _looks_like_raw_secret(value: str) -> bool:
    stripped = str(value or "").strip()
    lowered = stripped.lower()
    if any(lowered.startswith(prefix.lower()) for prefix in SECRET_PREFIXES):
        return True
    if len(stripped) >= 32 and not ENV_VAR_RE.fullmatch(stripped):
        return True
    return False


def _validate_env_var_name(value: str, *, flag_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{flag_name} expects an environment variable name")
    if _looks_like_raw_secret(normalized) or not ENV_VAR_RE.fullmatch(normalized):
        raise ValueError(f"{flag_name} expects an environment variable name, not a secret value")
    return normalized


def _validate_timeout_seconds(value: Any, *, flag_name: str) -> float:
    try:
        timeout_seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{flag_name} expects a positive number of seconds") from None
    if timeout_seconds <= 0:
        raise ValueError(f"{flag_name} expects a positive number of seconds")
    return timeout_seconds


def _profile_target_path(profile_name: str) -> Path:
    return provider_profiles_dir() / f"{profile_name}.yaml"


def _apply_overrides(template: dict[str, Any], args: Any) -> dict[str, Any]:
    profile = deepcopy(template)
    profile_name = str(getattr(args, "profile_name", None) or profile.get("name") or "").strip()
    if not profile_name:
        raise ValueError("provider profile name is required")
    profile["name"] = profile_name
    if getattr(args, "lane", None):
        profile["lane"] = str(args.lane).strip()
    if getattr(args, "model", None):
        profile["model"] = str(args.model).strip()
    if getattr(args, "model_env", None):
        profile["model_env"] = _validate_env_var_name(args.model_env, flag_name="--model-env")
    credential_refs = profile.setdefault("credential_refs", {})
    if not isinstance(credential_refs, dict):
        credential_refs = {}
        profile["credential_refs"] = credential_refs
    if getattr(args, "api_key_env", None):
        credential_refs["api_key_env"] = _validate_env_var_name(args.api_key_env, flag_name="--api-key-env")
    if getattr(args, "base_url", None):
        profile["base_url"] = str(args.base_url).strip()
    if getattr(args, "base_url_env", None):
        credential_refs["base_url_env"] = _validate_env_var_name(args.base_url_env, flag_name="--base-url-env")
    if getattr(args, "timeout_seconds", None) is not None:
        profile["timeout_seconds"] = _validate_timeout_seconds(args.timeout_seconds, flag_name="--timeout-seconds")
    return profile


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n[setup] Cancelled.\n")
        return False


def _list_templates() -> int:
    print("Available provider profile templates:")
    for provider_name in PROVIDER_TEMPLATE_ORDER:
        template = PROVIDER_TEMPLATES[provider_name]
        print(f"  - {provider_name}: provider={template['provider']} lane={template['lane']}")
    return 0


def cmd_setup(args: Any) -> int:
    setup_cmd = str(getattr(args, "setup_cmd", "") or "").strip()
    if setup_cmd != "provider":
        sys.stderr.write("Usage: amof setup provider [provider] [options]\n")
        return 1

    if bool(getattr(args, "list_templates", False)):
        return _list_templates()

    provider_name = str(getattr(args, "provider_template", "") or "").strip()
    if not provider_name:
        sys.stderr.write(
            "Usage: amof setup provider <openrouter|local-qwen|openai|anthropic|bedrock|xai|runpod> [options]\n"
        )
        return 1

    try:
        profile = _apply_overrides(_template_for(provider_name), args)
    except ValueError as exc:
        sys.stderr.write(f"[setup] {exc}\n")
        return 1

    yaml_text = _dump_yaml(profile)
    if bool(getattr(args, "print_template", False)):
        print(yaml_text, end="")
        return 0

    target = _profile_target_path(str(profile["name"]))
    if bool(getattr(args, "dry_run", False)):
        print(f"[setup] Provider profile: {profile['name']}")
        print(f"[setup] Target path: {target}")
        print("[setup] Dry run only; no files written.")
        print()
        print(yaml_text, end="")
        return 0

    if not bool(getattr(args, "yes", False)):
        if not _confirm(f"Write provider profile {profile['name']} to {target}? [y/N] "):
            return 1

    ensure_parent_dir(target).write_text(yaml_text, encoding="utf-8")
    print(f"[setup] Wrote provider profile: {target}")
    print("[setup] Stored secret references only; no raw API keys were written.")

    if bool(getattr(args, "activate", False)):
        context_name = get_current_context_name()
        activate_provider_profile_ref(str(profile["name"]), context_name=context_name)
        print(f"[setup] Activated provider profile '{profile['name']}' for context '{context_name}'.")

    return 0


__all__ = ["PROVIDER_TEMPLATE_ORDER", "PROVIDER_TEMPLATES", "cmd_setup"]
