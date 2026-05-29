"""Local profile config rendering and doctor checks."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

from ..app_config import get_current_context_name, get_provider_profile_refs, load_provider_profile


def _profile_credential_env(profile: dict[str, Any], key: str) -> str | None:
    credential_refs = profile.get("credential_refs")
    if isinstance(credential_refs, dict):
        value = credential_refs.get(key)
        if value:
            return str(value).strip()
    value = profile.get(key)
    if value:
        return str(value).strip()
    return None


def _selected_profile_name() -> tuple[str | None, str | None]:
    refs = get_provider_profile_refs()
    if not refs:
        return None, (
            "No active provider profile is configured. "
            "Run: amof profile init remote-ial-openrouter && amof profile use remote-ial-openrouter"
        )
    if len(refs) > 1:
        joined = ", ".join(refs)
        return None, (
            f"Multiple active provider profiles are configured: {joined}. "
            "Run `amof profile use <name>` to select exactly one profile."
        )
    return refs[0], None


def _effective_profile_render(profile: dict[str, Any], *, redacted: bool) -> dict[str, Any]:
    name = str(profile.get("name") or "").strip() or None
    provider = str(profile.get("provider") or "").strip() or None
    model_env = str(profile.get("model_env") or "").strip() or None
    base_url_env = _profile_credential_env(profile, "base_url_env")
    api_key_env = _profile_credential_env(profile, "api_key_env")
    timeout = profile.get("timeout_seconds")
    timeout_seconds = float(timeout) if isinstance(timeout, (int, float)) else 90.0
    payload: dict[str, Any] = {
        "selected_profile": name,
        "selected_context": get_current_context_name(),
        "provider": provider,
        "model_env": model_env,
        "default_model": str(profile.get("default_model") or "").strip() or None,
        "base_url_env": base_url_env,
        "api_key_env": api_key_env,
        "timeout_seconds": timeout_seconds,
        "cost_truth": {
            "missing_provider_cost": "unknown",
            "never_zero_fallback": True,
        },
    }
    if redacted:
        payload["env_presence"] = {
            "model_env_set": bool(model_env and str(os.environ.get(model_env, "")).strip()),
            "base_url_env_set": bool(base_url_env and str(os.environ.get(base_url_env, "")).strip()),
            "api_key_env_set": bool(api_key_env and str(os.environ.get(api_key_env, "")).strip()),
        }
    return payload


def _doctor_validate_base_url(base_url_value: str) -> str | None:
    parsed = urlparse(base_url_value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "must be an http(s) URL (example: https://ial.example.internal)"
    return None


def _doctor(profile: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    provider = str(profile.get("provider") or "").strip()
    model_env = str(profile.get("model_env") or "").strip()
    base_url_env = _profile_credential_env(profile, "base_url_env")
    api_key_env = _profile_credential_env(profile, "api_key_env")
    checks: list[dict[str, Any]] = []

    if model_env:
        has_model = bool(str(os.environ.get(model_env, "")).strip())
        checks.append(
            {
                "check": "model_env",
                "env_var": model_env,
                "status": "ok" if has_model else "warn",
                "message": (
                    f"{model_env} is set."
                    if has_model
                    else f"{model_env} is not set; default_model from profile will be used."
                ),
            }
        )

    if base_url_env:
        base_url_value = str(os.environ.get(base_url_env, "")).strip()
        if not base_url_value:
            checks.append(
                {
                    "check": "base_url_env",
                    "env_var": base_url_env,
                    "status": "error",
                    "message": f"{base_url_env} is missing. Export it before remote IAL planning.",
                    "action": f"export {base_url_env}=https://<your-remote-ial-host>",
                }
            )
        else:
            shape_error = _doctor_validate_base_url(base_url_value)
            if shape_error:
                checks.append(
                    {
                        "check": "base_url_env",
                        "env_var": base_url_env,
                        "status": "error",
                        "message": f"{base_url_env} {shape_error}",
                    }
                )
            else:
                checks.append(
                    {
                        "check": "base_url_env",
                        "env_var": base_url_env,
                        "status": "ok",
                        "message": f"{base_url_env} is set and has a valid URL shape.",
                    }
                )

    if api_key_env:
        has_api_key = bool(str(os.environ.get(api_key_env, "")).strip())
        checks.append(
            {
                "check": "api_key_env",
                "env_var": api_key_env,
                "status": "ok" if has_api_key else "error",
                "message": (
                    f"{api_key_env} is set."
                    if has_api_key
                    else f"{api_key_env} is missing. Export it before remote IAL planning."
                ),
                "action": None if has_api_key else f"export {api_key_env}=<secret>",
            }
        )

    has_error = any(item.get("status") == "error" for item in checks)
    payload = {
        "selected_profile": str(profile.get("name") or "").strip() or None,
        "provider": provider or None,
        "status": "fail" if has_error else "ok",
        "checks": checks,
    }
    return (1 if has_error else 0), payload


def cmd_config(args: Any) -> int:
    config_cmd = str(getattr(args, "config_cmd", "") or "").strip()
    if config_cmd not in {"render", "doctor"}:
        sys.stderr.write("Usage: amof config {render|doctor} [options]\n")
        return 1

    profile_name, error = _selected_profile_name()
    if error:
        sys.stderr.write(f"[config] {error}\n")
        return 1
    assert profile_name is not None
    try:
        profile = load_provider_profile(profile_name)
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"[config] {exc}\n")
        return 1

    if config_cmd == "render":
        if not bool(getattr(args, "redacted", False)):
            sys.stderr.write("[config] render currently requires --redacted.\n")
            return 1
        print(json.dumps(_effective_profile_render(profile, redacted=True), indent=2))
        return 0

    exit_code, doctor_payload = _doctor(profile)
    print(json.dumps(doctor_payload, indent=2))
    return exit_code


__all__ = ["cmd_config"]
