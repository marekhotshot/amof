"""AMOF-owned RunPod heavy inference lane status.

This module does not create pods or execute inference. It turns the existing
RunPod substrate into a narrow, opt-in lane that can be health-checked before
AMOF tries to route heavy work to it.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from amof.api.services.runpod import (
    RunpodClient,
    RunpodClientError,
    RunpodNotConfigured,
    RunpodProfileError,
    load_profile,
    load_runpod_settings,
)


CANONICAL_PROFILE_ID = "runner_code_heavy"
CANONICAL_MODEL = "MiniMaxAI/MiniMax-M2.5"
CANONICAL_ROLE_MAPPING = ("runner_code_heavy", "long_context_reviewer")
MAX_CONTEXT_TOKENS = 32768
HARD_TIMEOUT_SECONDS = 7200
MAX_COST_PER_RUN_USD = 5.0
IDLE_TTL_MINUTES = 120


@dataclass(frozen=True)
class HeavyLaneProfile:
    profile_id: str = CANONICAL_PROFILE_ID
    provider: str = "runpod"
    model: str = CANONICAL_MODEL
    roles: tuple[str, ...] = CANONICAL_ROLE_MAPPING
    endpoint_env: str = "RUNPOD_OPENAI_BASE_URL"
    api_key_env: str = "RUNPOD_API_KEY"
    health_path: str = "/models"
    max_context_tokens: int = MAX_CONTEXT_TOKENS
    hard_timeout_seconds: int = HARD_TIMEOUT_SECONDS
    max_cost_per_run_usd: float = MAX_COST_PER_RUN_USD
    idle_ttl_minutes: int = IDLE_TTL_MINUTES
    allow_master: bool = False
    allow_direct_git_write: bool = False


def _fallback_profile_dict() -> Dict[str, Any]:
    profile = HeavyLaneProfile()
    return {
        "profile_id": profile.profile_id,
        "provider": profile.provider,
        "model": profile.model,
        "roles": list(profile.roles),
        "endpoint_env": profile.endpoint_env,
        "api_key_env": profile.api_key_env,
        "health_path": profile.health_path,
        "max_context_tokens": profile.max_context_tokens,
        "hard_timeout_seconds": profile.hard_timeout_seconds,
        "max_cost_per_run_usd": profile.max_cost_per_run_usd,
        "idle_ttl_minutes": profile.idle_ttl_minutes,
        "allow_master": profile.allow_master,
        "allow_direct_git_write": profile.allow_direct_git_write,
        "source": "embedded_fallback",
    }


def resolve_profile() -> Dict[str, Any]:
    """Return the heavy-lane profile as a dict.

    Prefers ``.amof/runpod-profiles/runner_code_heavy.yaml`` if present;
    falls back to the embedded :class:`HeavyLaneProfile` defaults so the
    lane stays introspectable even before the profile file is
    materialized.
    """

    settings = load_runpod_settings()
    if settings is None:
        return _fallback_profile_dict()
    try:
        profile = load_profile(CANONICAL_PROFILE_ID, settings)
    except RunpodProfileError:
        return _fallback_profile_dict()
    except RunpodNotConfigured:
        return _fallback_profile_dict()
    return {
        "profile_id": profile.name,
        "provider": "runpod",
        "model": profile.model or CANONICAL_MODEL,
        "roles": list(profile.intended_roles) or list(CANONICAL_ROLE_MAPPING),
        "endpoint_env": "RUNPOD_OPENAI_BASE_URL",
        "api_key_env": "RUNPOD_API_KEY",
        "health_path": profile.health_path or "/models",
        "max_context_tokens": profile.max_context_tokens or MAX_CONTEXT_TOKENS,
        "hard_timeout_seconds": profile.hard_timeout_seconds or HARD_TIMEOUT_SECONDS,
        "max_cost_per_run_usd": (
            profile.max_cost_per_run_usd
            if profile.max_cost_per_run_usd is not None
            else MAX_COST_PER_RUN_USD
        ),
        "idle_ttl_minutes": profile.idle_ttl_minutes or IDLE_TTL_MINUTES,
        "allow_master": profile.allow_master,
        "allow_direct_git_write": profile.allow_direct_git_write,
        "source": "yaml_catalog",
    }


def evaluate_heavy_lane_status(*, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Return a truthful readiness projection for the canonical heavy lane."""
    env = env if env is not None else os.environ
    profile = resolve_profile()
    api_key = str(env.get("RUNPOD_API_KEY") or "").strip()
    base_url = str(env.get("RUNPOD_OPENAI_BASE_URL") or "").strip().rstrip("/")
    missing: list[str] = []
    reasons: list[str] = ["canonical_profile_selected"]
    failure_class: Optional[str] = None
    endpoint_host = None
    model_count: Optional[int] = None
    latency_ms: Optional[int] = None
    pod_count: Optional[int] = None
    amof_managed_pod_count: Optional[int] = None
    template_count: Optional[int] = None

    if not api_key:
        missing.append("RUNPOD_API_KEY")
    if not base_url:
        missing.append("RUNPOD_OPENAI_BASE_URL")
    if base_url:
        endpoint_host = urlparse(base_url).netloc or None

    account = _inspect_runpod_account()
    pod_count = account.get("pod_count")
    amof_managed_pod_count = account.get("amof_managed_pod_count")
    template_count = account.get("template_count")
    if account.get("api_key_configured"):
        reasons.append("runpod_api_key_configured")
    if template_count:
        reasons.append("runpod_template_available")
    if pod_count == 0:
        reasons.append("no_active_pods")
    # A usable RunPod heavy lane needs either a Serverless OpenAI
    # endpoint OR an AMOF-managed pod that serves the same /v1/models
    # contract. Pod-based lanes were proven in UP11-4; require
    # ``runpod_openai_endpoint`` only when neither path exists.
    if (
        account.get("endpoint_count") == 0
        and not (amof_managed_pod_count or 0)
    ):
        missing.append("runpod_openai_endpoint")
    if amof_managed_pod_count:
        reasons.append("amof_managed_pod_present")

    if api_key and base_url:
        started = time.monotonic()
        try:
            response = requests.get(
                f"{base_url}{profile['health_path']}",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                timeout=10,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            if response.status_code in (401, 403):
                failure_class = "provider_unauthorized"
            elif response.status_code == 404:
                failure_class = "provider_model_endpoint_not_found"
            elif response.status_code == 429:
                failure_class = "provider_rate_limited"
            elif response.status_code >= 500:
                failure_class = "provider_unreachable"
            elif response.ok:
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, list):
                    model_count = len(data)
                    served_ids = [str(row.get("id") or "") for row in data if isinstance(row, dict)]
                    if profile["model"] in served_ids:
                        reasons.append("model_listed")
                    else:
                        missing.append("canonical_model_listed")
                        failure_class = "provider_model_not_listed"
                else:
                    failure_class = "provider_invalid_response"
            else:
                failure_class = "provider_unreachable"
        except requests.Timeout:
            failure_class = "provider_timeout"
        except requests.RequestException:
            failure_class = "provider_unreachable"

    usable = not missing and failure_class is None and model_count is not None
    status = "usable" if usable else "unusable"
    return {
        "lane_id": "runpod_heavy",
        "status": status,
        "usable": usable,
        "profile": profile,
        "endpoint_host": endpoint_host,
        "model_count": model_count,
        "latency_ms": latency_ms,
        "failure_class": failure_class,
        "missing_prerequisites": sorted(set(missing)),
        "reasons": sorted(set(reasons)),
        "substrate": {
            "api_key_configured": bool(api_key),
            "template_count": template_count,
            "pod_count": pod_count,
            "amof_managed_pod_count": amof_managed_pod_count,
            "endpoint_count": account.get("endpoint_count"),
            "account_probe_error": account.get("error"),
        },
        "guardrails": {
            "opt_in_only": True,
            "heavy_only": True,
            "allow_master": False,
            "allow_direct_git_write": False,
            "hard_timeout_seconds": HARD_TIMEOUT_SECONDS,
            "max_cost_per_run_usd": MAX_COST_PER_RUN_USD,
            "idle_ttl_minutes": IDLE_TTL_MINUTES,
        },
    }


def runpod_probe(*, env: Optional[Dict[str, str]] = None, timeout_seconds: int = 10) -> Dict[str, Any]:
    """Structured RunPod reachability probe for the release validate gate.

    Implements the shape required by
    ``repos/amof/contracts/release-runtime-validation-gate.md`` §21-§23:

    - Reads ``RUNPOD_OPENAI_BASE_URL`` and ``RUNPOD_API_KEY`` from ``env``.
    - Issues an authenticated ``GET ${base}/models`` with a bounded
      timeout (default 10s).
    - Treats HTTP 200 + JSON ``data: [...]`` as PASS and records the
      served model count.
    - Any non-200 / mismatch / timeout / DNS failure is FAIL with the
      truthful ``failure_class``.
    - Never hits ``/chat/completions`` (no quota consumed).

    The return payload is the exact ``runpod_probe`` block the release
    validate gate should embed in its evidence.
    """

    env = env if env is not None else os.environ
    api_key = str(env.get("RUNPOD_API_KEY") or "").strip()
    base_url = str(env.get("RUNPOD_OPENAI_BASE_URL") or "").strip().rstrip("/")
    endpoint_host = urlparse(base_url).netloc or None if base_url else None
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if not api_key or not base_url:
        return {
            "status": "fail",
            "endpoint_host": endpoint_host,
            "model_count": None,
            "failure_class": "provider_not_configured",
            "latency_ms": None,
            "checked_at": checked_at,
            "missing": [
                name
                for name, present in (
                    ("RUNPOD_API_KEY", bool(api_key)),
                    ("RUNPOD_OPENAI_BASE_URL", bool(base_url)),
                )
                if not present
            ],
        }

    started = time.monotonic()
    failure_class: Optional[str] = None
    status = "fail"
    model_count: Optional[int] = None
    latency_ms: Optional[int] = None
    try:
        response = requests.get(
            f"{base_url}/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=timeout_seconds,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code in (401, 403):
            failure_class = "provider_unauthorized"
        elif response.status_code == 429:
            failure_class = "provider_rate_limited"
        elif response.status_code == 404:
            failure_class = "provider_model_endpoint_not_found"
        elif response.status_code >= 500:
            failure_class = "provider_unreachable"
        elif response.ok:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, list):
                model_count = len(data)
                status = "pass"
            else:
                failure_class = "provider_invalid_response"
        else:
            failure_class = "provider_unreachable"
    except requests.Timeout:
        failure_class = "provider_timeout"
        latency_ms = int((time.monotonic() - started) * 1000)
    except requests.RequestException:
        failure_class = "provider_unreachable"
        latency_ms = int((time.monotonic() - started) * 1000)

    return {
        "status": status,
        "endpoint_host": endpoint_host,
        "model_count": model_count,
        "failure_class": failure_class,
        "latency_ms": latency_ms,
        "checked_at": checked_at,
    }


def _inspect_runpod_account() -> Dict[str, Any]:
    try:
        client = RunpodClient()
        pods = client.list_pods()
        endpoints = client._request("GET", "/endpoints")  # Existing service owns REST auth/session.
        templates = client._request("GET", "/templates")
        return {
            "api_key_configured": True,
            "pod_count": len(pods),
            "amof_managed_pod_count": len(client.list_amof_pods()),
            "endpoint_count": len(endpoints) if isinstance(endpoints, list) else None,
            "template_count": len(templates) if isinstance(templates, list) else None,
        }
    except RunpodNotConfigured:
        return {"api_key_configured": False, "error": "RUNPOD_API_KEY missing"}
    except RunpodClientError as exc:
        return {"api_key_configured": bool(load_runpod_settings()), "error": str(exc)}
