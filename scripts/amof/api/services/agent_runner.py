import os
import sys
import logging
import uuid
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path

from amof.api.command_builder import get_workspace_root
from amof.api.run_manager import (
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCESS,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
)

logger = logging.getLogger(__name__)


def _resolve_api_runner_model(cfg: Dict[str, Any]) -> str:
    profile_selection = cfg.get("llm_profile_selection") if isinstance(cfg, dict) else None
    if isinstance(profile_selection, dict):
        standard_profile = profile_selection.get("standard")
        if isinstance(standard_profile, str) and standard_profile.strip():
            return standard_profile.strip()
    llm_ladder = cfg.get("llm_ladder") or {}
    roles = llm_ladder.get("roles") if isinstance(llm_ladder, dict) else {}
    orchestrator = roles.get("orchestrator") if isinstance(roles, dict) else {}
    cascade = orchestrator.get("cascade") if isinstance(orchestrator, dict) else None
    if isinstance(cascade, list):
        for model_id in cascade:
            if isinstance(model_id, str) and model_id.strip():
                return model_id.strip()

    provider = str(cfg.get("default_provider", "anthropic") or "anthropic").strip().lower()
    if provider == "openrouter":
        return "openrouter/anthropic/claude-4.6-sonnet"
    if provider == "openai":
        return os.environ.get("AMOF_OPENAI_MODEL", "gpt-4o")
    if provider == "bedrock":
        return os.environ.get(
            "AMOF_BEDROCK_STANDARD_MODEL_ID",
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        )
    return os.environ.get("AMOF_ANTHROPIC_MODEL", "claude-sonnet-4-5")


def _is_supported_api_runner_model(model_id: str) -> bool:
    normalized = (model_id or "").strip().lower()
    if not normalized:
        return False
    # The API runner uses the standard chat-completions path, so skip
    # provider-specific custom-tools slugs that are not valid model IDs there.
    if "custom-tools" in normalized:
        return False
    return True


def _build_api_runner_profile_router(cfg: Dict[str, Any]) -> Optional[Tuple[Any, Any, Any, str]]:
    if not isinstance(cfg.get("llm_profile_selection"), dict):
        return None

    from amof.orchestrator.context.summarizer import ContextSummarizer
    from amof.orchestrator.llm.profile_catalog import (
        build_clients_from_selection,
        get_profile_selection,
    )
    from amof.orchestrator.model_router import ModelRouter

    selection = get_profile_selection(cfg)
    models = build_clients_from_selection(selection)
    cascade = [slot for slot in ("fast", "standard", "strong") if slot in models]
    if not cascade:
        return None

    default_tier = "standard" if "standard" in models else cascade[0]
    model_router = ModelRouter(
        models=models,
        default_tier=default_tier,
        cascade=cascade,
    )
    summarizer_llm = models.get("fast") or models[default_tier]
    context_summarizer = ContextSummarizer(
        summarizer_llm=summarizer_llm,
        threshold_pct=60.0,
        keep_recent=6,
    )
    return model_router, model_router, context_summarizer, models[default_tier].model_name()


# IAL Phase 2a — sources eligible for local-inference routing.
#
# Master is intentionally excluded; per the Phase 2a contract we only let
# off-funnel non-master callers resolve to a local OpenAI-compatible
# endpoint. ``runner:<name>`` (delegated workers) are excluded by default —
# they share the master cloud path so their cost remains comparable to
# master. Mini Ultra Plan 2 / Phase L2 adds an explicit opt-in path via
# ``local_inference.runner_overrides`` that is gated on the runtime profile
# (``"local_qwen"``); see :func:`_resolve_runner_local_clients`. Default
# behaviour is unchanged when no opt-in is configured.
LOCAL_INFERENCE_ALLOWED_SOURCES = ("summarizer", "indexer")

# Default per-source fallback policy when ``local_inference`` is enabled
# but a registered local client raises. ``"cloud"`` falls through to the
# underlying ModelRouter; ``"none"`` re-raises. Operators can override
# globally or per source via ``agent.yaml``.
DEFAULT_LOCAL_FALLBACK_POLICY = "cloud"

# Mini Ultra Plan 2 / Phase L2: the runtime profile name that activates
# local-Qwen runner overrides. Holding the string in one constant makes
# the gating check greppable and avoids stringly-typed drift across
# agent_runner.py, the queue dispatcher, and tests.
LOCAL_QWEN_RUNTIME_PROFILE = "local_qwen"


def _resolve_env_reference(value: Any, env_lookup: Optional[Dict[str, str]] = None) -> Any:
    """Resolve ``${VAR}`` placeholders against the supplied environment map."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("${") and text.endswith("}"):
        lookup = env_lookup if env_lookup is not None else os.environ
        return lookup.get(text[2:-1], "")
    return value


def _register_local_inference_clients(
    authority: Any,
    cfg: Dict[str, Any],
    *,
    log: Any = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Register Phase 2a local clients on the shared inference authority.

    Reads the ``local_inference`` block out of ``agent.yaml``::

        local_inference:
          enabled: true                # master switch (default false)
          base_url: "http://host:port/v1"
          api_key: "local"             # optional; many local servers ignore
          api_key_env: "RUNPOD_API_KEY"
          provider_id: "local"         # or "runpod" for RunPod-backed sources
          timeout: 60                  # seconds
          fallback: "cloud"            # default per-source fallback policy
          sources:
            summarizer:
              model: "qwen2.5-coder:7b"
              # base_url / api_key / api_key_env / provider_id / fallback may be
              # overridden per source
            indexer:
              model: "qwen2.5-coder:7b"

    Behaviour:
      * If ``local_inference.enabled`` is falsy or absent → no-op. Phase 1/1b
        cloud-only behaviour is preserved exactly.
      * Only sources listed under ``sources`` AND in
        :data:`LOCAL_INFERENCE_ALLOWED_SOURCES` get a local client. Master
        and ``runner:*`` are silently skipped.
      * Each source needs at minimum ``model`` and an inherited or
        per-source ``base_url``; otherwise it is skipped with a warning.

    Returns a mapping ``{source: model_label}`` describing what was actually
    registered. The caller logs this so operators can see in the run output
    which sources were diverted to local inference.
    """

    registered: Dict[str, str] = {}
    block = cfg.get("local_inference") if isinstance(cfg, dict) else None
    if not isinstance(block, dict):
        return registered
    if not block.get("enabled"):
        return registered

    env_lookup = env if env is not None else os.environ

    default_base_url = _resolve_env_reference(block.get("base_url"), env_lookup)
    default_api_key = _resolve_env_reference(block.get("api_key"), env_lookup)
    default_api_key_env = block.get("api_key_env")
    default_provider_id = str(block.get("provider_id") or "local").strip() or "local"
    default_timeout = block.get("timeout")
    default_fallback = block.get("fallback") or DEFAULT_LOCAL_FALLBACK_POLICY

    sources_cfg = block.get("sources") or {}
    if not isinstance(sources_cfg, dict):
        if log is not None:
            try:
                log("[agent] local_inference.sources must be a mapping; skipping")
            except Exception:
                pass
        return registered

    try:
        from amof.orchestrator.llm.local_openai_compatible import (
            LocalOpenAICompatibleClient,
            DEFAULT_LOCAL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if log is not None:
            try:
                log(f"[agent] local_inference disabled: import failed ({exc})")
            except Exception:
                pass
        return registered

    skipped_unresolved: List[str] = []
    for source, source_cfg in sources_cfg.items():
        if source not in LOCAL_INFERENCE_ALLOWED_SOURCES:
            if log is not None:
                try:
                    log(
                        f"[agent] local_inference: source '{source}' is not "
                        f"eligible for Phase 2a routing; skipping"
                    )
                except Exception:
                    pass
            continue
        if not isinstance(source_cfg, dict):
            continue

        model = _resolve_env_reference(source_cfg.get("model"), env_lookup)
        base_url = _resolve_env_reference(
            source_cfg.get("base_url") or default_base_url,
            env_lookup,
        )
        api_key = _resolve_env_reference(
            source_cfg.get("api_key") or default_api_key,
            env_lookup,
        )
        api_key_env = source_cfg.get("api_key_env") or default_api_key_env
        if not api_key and api_key_env:
            api_key = env_lookup.get(str(api_key_env), "")
        timeout = source_cfg.get("timeout") or default_timeout or DEFAULT_LOCAL_TIMEOUT_SECONDS
        fallback = source_cfg.get("fallback") or default_fallback
        provider_id = str(
            source_cfg.get("provider_id") or default_provider_id or "local"
        ).strip() or "local"

        if not model or not base_url:
            missing = []
            if not model:
                missing.append("model")
            if not base_url:
                missing.append("base_url")
            skipped_unresolved.append(f"{source} missing {'/'.join(missing)}")
            continue

        try:
            client = LocalOpenAICompatibleClient(
                base_url=base_url,
                model=model,
                api_key=api_key,
                timeout=float(timeout),
                provider_id=provider_id,
            )
            authority.register_source_client(source, client, fallback=fallback)
            registered[source] = client.model_name()
        except Exception as exc:
            if log is not None:
                try:
                    log(
                        f"[agent] local_inference: failed to register "
                        f"source '{source}' ({exc})"
                    )
                except Exception:
                    pass
            continue

    if skipped_unresolved and log is not None:
        try:
            log(
                "[agent] local_inference disabled for unresolved source config: "
                f"{'; '.join(skipped_unresolved)}. Cloud routing remains active; "
                "set local_inference.base_url/source base_url and model to enable."
            )
        except Exception:
            pass

    return registered


def _resolve_runner_runpod_clients(
    cfg: Dict[str, Any],
    runtime_profile: Optional[str],
    *,
    log: Any = None,
    env: Optional[Dict[str, str]] = None,
    strict: bool = False,
    health_status: Optional[Dict[str, Any]] = None,
    profile_loader: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build the runner-name → Runpod LLMClient map for Phase R1 opt-in.

    Reads the top-level ``runpod`` block::

        runpod:
          enabled: false
          base_url: "${RUNPOD_OPENAI_BASE_URL}"
          api_key_env: "RUNPOD_API_KEY"
          model: "qwen2.5-coder-32b-instruct"
          timeout: 180
          fallback: none
          runner_overrides:
            enabled_for_profile: ["clouddev_runpod_runner"]
            allowed_runners: ["code"]

    Behaviour mirrors :func:`_resolve_runner_local_clients` but routes
    through the same :class:`LocalOpenAICompatibleClient` shape with
    ``provider_id="runpod"``. The empty map is returned when:

      * ``runpod.enabled`` is falsy (default in canonical agent.yaml),
      * no ``runtime_profile`` is supplied,
      * the profile is not in ``runner_overrides.enabled_for_profile``,
      * ``base_url`` cannot be resolved from env (`${RUNPOD_OPENAI_BASE_URL}`
        interpolation), or
      * ``RUNPOD_API_KEY`` is not present in the environment.

    T7 adds three extra refusal gates under ``strict=True``:

      * (a) heavy-lane health is not ``usable`` (live ``/models`` probe),
      * (b) any runner name in ``allowed_runners`` is not in the profile
            YAML's ``intended_roles`` (if the YAML exists and declares
            non-empty ``intended_roles``),
      * (c) the profile YAML sets ``allow_master: true`` somehow (never
            honored for this lane).

    Production call sites pass ``strict=True``; existing backward-compat
    tests default to ``strict=False`` which preserves the Phase R1 gates
    only. Returning ``{}`` is the safe default in both modes.
    """

    overrides_clients: Dict[str, Any] = {}
    if not runtime_profile:
        return overrides_clients

    block = cfg.get("runpod") if isinstance(cfg, dict) else None
    if not isinstance(block, dict):
        return overrides_clients
    if not block.get("enabled"):
        return overrides_clients

    overrides_cfg = block.get("runner_overrides")
    if not isinstance(overrides_cfg, dict):
        return overrides_clients

    enabled_profiles = overrides_cfg.get("enabled_for_profile") or []
    if not isinstance(enabled_profiles, list) or runtime_profile not in enabled_profiles:
        return overrides_clients

    allowed_runners = overrides_cfg.get("allowed_runners") or []
    if not isinstance(allowed_runners, list) or not allowed_runners:
        return overrides_clients

    import os
    env_lookup = env if env is not None else os.environ

    raw_base_url = overrides_cfg.get("base_url") or block.get("base_url") or ""
    if isinstance(raw_base_url, str) and raw_base_url.startswith("${") and raw_base_url.endswith("}"):
        var_name = raw_base_url[2:-1]
        base_url = env_lookup.get(var_name, "")
    else:
        base_url = raw_base_url

    api_key_env = block.get("api_key_env") or "RUNPOD_API_KEY"
    api_key = env_lookup.get(api_key_env, "")

    model = overrides_cfg.get("model") or block.get("model")
    timeout = overrides_cfg.get("timeout") or block.get("timeout")

    if not base_url or not api_key or not model:
        if log is not None:
            try:
                log(
                    f"[agent] runpod.runner_overrides skipped: "
                    f"base_url_set={bool(base_url)} api_key_set={bool(api_key)} "
                    f"model_set={bool(model)} (no silent fallback)"
                )
            except Exception:
                pass
        return overrides_clients

    try:
        from amof.orchestrator.llm.local_openai_compatible import (
            LocalOpenAICompatibleClient,
            DEFAULT_LOCAL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if log is not None:
            try:
                log(
                    f"[agent] runpod.runner_overrides disabled: import failed ({exc})"
                )
            except Exception:
                pass
        return overrides_clients

    effective_timeout = float(timeout or DEFAULT_LOCAL_TIMEOUT_SECONDS)

    # T7 strict mode: consult the profile YAML catalog and the heavy-lane
    # health endpoint before handing out any Runpod clients. Falls open
    # (permissive) when strict=False so existing Phase R1 tests keep
    # their behaviour.
    profile_catalog: Optional[Any] = None
    if strict:
        try:
            if profile_loader is not None:
                profile_catalog = profile_loader(runtime_profile)
            else:
                from amof.api.services.runpod import (
                    RunpodNotConfigured,
                    RunpodProfileError,
                    load_profile,
                )
                try:
                    profile_catalog = load_profile(runtime_profile)
                except (RunpodProfileError, RunpodNotConfigured):
                    profile_catalog = None
        except Exception as exc:  # noqa: BLE001
            if log is not None:
                try:
                    log(
                        f"[agent] runpod.runner_overrides: profile catalog "
                        f"lookup failed for '{runtime_profile}' ({exc})"
                    )
                except Exception:
                    pass
            profile_catalog = None

        if profile_catalog is not None and getattr(profile_catalog, "allow_master", False):
            if log is not None:
                try:
                    log(
                        f"[agent] runpod.runner_overrides refused: profile "
                        f"'{runtime_profile}' declares allow_master=True; "
                        f"RunPod must never be master."
                    )
                except Exception:
                    pass
            return overrides_clients

        intended_roles = tuple(getattr(profile_catalog, "intended_roles", ()) or ())
        if intended_roles:
            runner_names_kept: list[str] = []
            for runner_name in allowed_runners:
                if runner_name in intended_roles:
                    runner_names_kept.append(runner_name)
                else:
                    if log is not None:
                        try:
                            log(
                                f"[agent] runpod.runner_overrides dropped "
                                f"runner '{runner_name}' not in intended_roles "
                                f"{list(intended_roles)}"
                            )
                        except Exception:
                            pass
            if not runner_names_kept:
                return overrides_clients
            allowed_runners = runner_names_kept

        if health_status is None:
            try:
                from amof.api.services.runpod_heavy_lane import (
                    evaluate_heavy_lane_status,
                )
                health_status = evaluate_heavy_lane_status(env=dict(env_lookup))
            except Exception as exc:  # noqa: BLE001
                if log is not None:
                    try:
                        log(
                            f"[agent] runpod.runner_overrides refused: heavy-lane "
                            f"health probe failed ({exc})"
                        )
                    except Exception:
                        pass
                return overrides_clients
        if not (isinstance(health_status, dict) and health_status.get("usable")):
            if log is not None:
                try:
                    reasons = (
                        ",".join(health_status.get("missing_prerequisites") or [])
                        if isinstance(health_status, dict)
                        else "health_probe_failed"
                    )
                    log(
                        f"[agent] runpod.runner_overrides refused: heavy-lane "
                        f"not usable ({reasons})"
                    )
                except Exception:
                    pass
            return overrides_clients

    for runner_name in allowed_runners:
        if not isinstance(runner_name, str) or not runner_name:
            continue
        try:
            client = LocalOpenAICompatibleClient(
                base_url=base_url,
                model=model,
                api_key=api_key,
                timeout=effective_timeout,
                provider_id="runpod",
            )
        except Exception as exc:
            if log is not None:
                try:
                    log(
                        f"[agent] runpod.runner_overrides: failed to build client "
                        f"for runner '{runner_name}' ({exc})"
                    )
                except Exception:
                    pass
            continue
        overrides_clients[runner_name] = client

    return overrides_clients


def _resolve_runner_local_clients(
    cfg: Dict[str, Any],
    runtime_profile: Optional[str],
    *,
    log: Any = None,
) -> Dict[str, Any]:
    """Build the runner-name → local LLMClient map for Phase L2 opt-in.

    Reads the ``local_inference.runner_overrides`` block::

        local_inference:
          runner_overrides:
            enabled_for_profile: ["local_qwen"]
            allowed_runners: ["code"]
            model: "qwen2.5-coder:7b-instruct"
            # base_url / api_key / timeout / fallback inherit the
            # surrounding local_inference block when omitted; an
            # explicit value here overrides the inherited default.

    Behaviour:
      * If the block is absent, ``runtime_profile`` is missing, the
        profile is not in ``enabled_for_profile``, or the surrounding
        ``local_inference.enabled`` is falsy → returns ``{}``. The
        default cloud cascade is preserved exactly. This is the
        guarantee that Phase L2 never silently flips runner routing.
      * Otherwise, build one
        :class:`~amof.orchestrator.llm.local_openai_compatible.LocalOpenAICompatibleClient`
        per runner name in ``allowed_runners`` (skipping any that
        the runner factory does not know about — but that filtering
        happens at the caller, not here). The same model/base_url is
        used for every override unless an inner per-runner mapping is
        supplied.

    Returns a mapping ``{runner_name: LocalOpenAICompatibleClient}``.
    The caller is responsible for handing this map to ``RunnerFactory``
    and for emitting a single, truthful operator log line summarising
    which runners were diverted.
    """

    overrides_clients: Dict[str, Any] = {}
    if not runtime_profile:
        return overrides_clients

    block = cfg.get("local_inference") if isinstance(cfg, dict) else None
    if not isinstance(block, dict):
        return overrides_clients
    if not block.get("enabled"):
        return overrides_clients

    overrides_cfg = block.get("runner_overrides")
    if not isinstance(overrides_cfg, dict):
        return overrides_clients

    enabled_profiles = overrides_cfg.get("enabled_for_profile") or []
    if not isinstance(enabled_profiles, list) or runtime_profile not in enabled_profiles:
        return overrides_clients

    allowed_runners = overrides_cfg.get("allowed_runners") or []
    if not isinstance(allowed_runners, list) or not allowed_runners:
        return overrides_clients

    base_url = overrides_cfg.get("base_url") or block.get("base_url")
    api_key = overrides_cfg.get("api_key") or block.get("api_key")
    timeout = overrides_cfg.get("timeout") or block.get("timeout")
    model = overrides_cfg.get("model")

    if not base_url or not model:
        if log is not None:
            try:
                log(
                    "[agent] local_inference.runner_overrides: missing base_url "
                    "or model; runner override skipped (no silent flip)"
                )
            except Exception:
                pass
        return overrides_clients

    try:
        from amof.orchestrator.llm.local_openai_compatible import (
            LocalOpenAICompatibleClient,
            DEFAULT_LOCAL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if log is not None:
            try:
                log(
                    f"[agent] local_inference.runner_overrides disabled: import failed ({exc})"
                )
            except Exception:
                pass
        return overrides_clients

    effective_timeout = float(timeout or DEFAULT_LOCAL_TIMEOUT_SECONDS)

    for runner_name in allowed_runners:
        if not isinstance(runner_name, str) or not runner_name:
            continue
        try:
            client = LocalOpenAICompatibleClient(
                base_url=base_url,
                model=model,
                api_key=api_key,
                timeout=effective_timeout,
                provider_id="local",
            )
        except Exception as exc:
            if log is not None:
                try:
                    log(
                        f"[agent] local_inference.runner_overrides: failed to build "
                        f"client for runner '{runner_name}' ({exc})"
                    )
                except Exception:
                    pass
            continue
        overrides_clients[runner_name] = client

    return overrides_clients


def _build_api_runner_llm(cfg: Dict[str, Any]) -> Tuple[Any, str]:
    from amof.orchestrator.llm.anthropic import AnthropicClient
    from amof.orchestrator.llm.bedrock_anthropic import BedrockAnthropicClient
    from amof.orchestrator.llm.openai_client import OpenAIClient

    model_id = _resolve_api_runner_model(cfg)
    if not _is_supported_api_runner_model(model_id):
        llm_ladder = cfg.get("llm_ladder") or {}
        roles = llm_ladder.get("roles") if isinstance(llm_ladder, dict) else {}
        orchestrator = roles.get("orchestrator") if isinstance(roles, dict) else {}
        cascade = orchestrator.get("cascade") if isinstance(orchestrator, dict) else None
        if isinstance(cascade, list):
            for candidate in cascade:
                if isinstance(candidate, str) and _is_supported_api_runner_model(candidate):
                    model_id = candidate.strip()
                    break
        if not _is_supported_api_runner_model(model_id):
            model_id = "openrouter/anthropic/claude-4.6-sonnet"
    provider = str(cfg.get("default_provider", "anthropic") or "anthropic").strip().lower()

    if model_id.startswith("openrouter/"):
        client = OpenAIClient(api_key=os.environ.get("OPENROUTER_API_KEY", ""), model=model_id)
    elif provider == "openai" or model_id.startswith(("gpt-", "o1", "o3")):
        client = OpenAIClient(api_key=os.environ.get("OPENAI_API_KEY", ""), model=model_id)
    elif provider == "bedrock" or model_id.startswith("anthropic.claude-"):
        client = BedrockAnthropicClient(model=model_id)
    else:
        client = AnthropicClient(api_key=os.environ.get("ANTHROPIC_API_KEY", ""), model=model_id)

    return client, model_id

def run_agent_for_ui(
    run_manager,
    run_id: str,
    ecosystem: str,
    prompt: str,
    mode: str = "build",
    runtime_profile: Optional[str] = None,
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    arena_mode: Optional[str] = None,
    team_id: Optional[str] = None,
    delegation_id: Optional[str] = None,
    backlog_item_id: Optional[str] = None,
    trigger_kind: Optional[str] = None,
    parent_run_id: Optional[str] = None,
) -> None:
    """
    Run the AMOF agent natively within the API process, but optimized for UI consumption.
    Unlike `cmd_agent`, this avoids CLI prompts, handles events cleanly, and 
    separates chat from internal logs.

    Mini Ultra Plan 2 / Phase L2: ``runtime_profile`` is now a first-class
    argument. The queue dispatcher already passed it as the 6th positional
    argument; previously the function signature only declared 7 params and
    the dispatcher call would have raised ``TypeError`` at runtime. The
    widened signature also accepts the trailing kwargs the dispatcher
    surfaces (thread_id, arena_mode, ...) so call-site truth and function
    truth agree. ``runtime_profile`` is the opt-in switch for L2's
    per-runner local-Qwen routing — see :func:`_resolve_runner_local_clients`.
    Other kwargs are accepted for call-site compatibility but are not
    consumed here yet.
    """
    run_manager.update_status(run_id, RUN_STATUS_RUNNING)
    
    try:
        import yaml
        
        root = get_workspace_root()
        eco_dir = root / "ecosystems" / ecosystem
        manifest_path = eco_dir / "ecosystem.yaml"
        
        if not manifest_path.exists():
            run_manager.append_log(run_id, f"Error: Manifest not found for {ecosystem}")
            run_manager.update_status(run_id, RUN_STATUS_FAILED)
            return
            
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
            
        manifest["ecosystem"] = ecosystem
        
        # We need to construct the agent and run it directly, bypassing the CLI wrapper.
        from amof.commands.agent_cmd import _auto_load_env
        from amof.orchestrator.tools import create_default_registry, Guardrails
        from amof.orchestrator.tools.base import GuardrailConfig
        from amof.orchestrator.agent import Agent
        from amof.orchestrator.session import Session
        from amof.orchestrator.telemetry import SessionTelemetry
        from amof.orchestrator.events import EventLog
        from amof.orchestrator.trust_boundary import create_trust_state
        from amof.orchestrator.context.builder import ContextBuilder
        from amof.orchestrator.runners import RunnerFactory
        from amof.orchestrator.llm.inference_authority import InferenceAuthority
        from amof.state import get_active_ticket
        from amof.worktree_manager import get_ticket_worktree_base
        from amof.api.services.settings_service import get_agent_config, resolve_agent_dry_run

        # 1. Load configuration and environment.
        # Read through settings_service so the runner sees the same operator-managed
        # `.amof/agent.yaml` that the Settings UI reads/writes (workspace-rooted).
        # Previously this called `_load_agent_config()` which falls back to a
        # cwd-relative path when the absolute path is missing, hiding
        # operator-edited keys (notably `dry_run`) from the runner.
        cfg = get_agent_config()
        effective_dry_run, dry_run_source = resolve_agent_dry_run(cfg)
        _auto_load_env(root / ".env")
        run_manager.append_log(
            run_id,
            f"[agent] dry_run={str(effective_dry_run).lower()} ({dry_run_source})",
        )
        
        # Resolve ticket workspace
        active_ticket = get_active_ticket()
        ticket_cwd = get_ticket_worktree_base(root, active_ticket) if active_ticket else None
        
        default_model = _resolve_api_runner_model(cfg)
        try:
            profile_router = _build_api_runner_profile_router(cfg)
            if profile_router is not None:
                primary_llm, model_router, context_summarizer, default_model = profile_router
            else:
                base_llm, default_model = _build_api_runner_llm(cfg)
                from amof.orchestrator.model_router import ModelRouter
                from amof.orchestrator.context.summarizer import ContextSummarizer

                model_router = ModelRouter(
                    models={"standard": base_llm, "fast": base_llm},
                    default_tier="standard",
                    cascade=["standard", "fast"],
                )
                primary_llm = model_router
                context_summarizer = ContextSummarizer(
                    summarizer_llm=base_llm,
                    threshold_pct=60.0,
                    keep_recent=6,
                )
        except ValueError as exc:
            run_manager.append_log(run_id, f"Error: {exc}")
            run_manager.update_status(run_id, RUN_STATUS_FAILED)
            return
            
        # 2. Setup Guardrails
        no_touch = manifest.get("guardrails", {}).get("no_touch_paths", [])
        readonly_repos = {}
        for r in manifest.get("repos", []):
            if r.get("readonly"):
                readonly_repos[r["name"]] = Path(r["path"])
                
        guardrail_config = GuardrailConfig.load(root / ".amof" / "rules" / "guardrails.yaml")
        
        # For UI, we auto-deny interactive confirmations to prevent hanging
        def _ui_confirm(command: str, reason: str) -> str:
            run_manager.append_log(run_id, f"Guardrail blocked command: {command} (Reason: {reason})")
            return "no"
            
        guardrails = Guardrails(
            no_touch_paths=no_touch,
            readonly_repos=readonly_repos,
            mode="build",
            config=guardrail_config,
            confirm_fn=_ui_confirm,
        )
        
        # 3. Session (conversation) vs Run (this invocation). Sessions live under .amof/sessions/.
        if not session_id:
            session_id = str(uuid.uuid4())
            run_manager.append_log(run_id, f"[agent] New session {session_id[:8]}...")
        session = Session(session_id=session_id, mode=mode)
        session.ecosystem = ecosystem
        sessions_dir = root / ".amof" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # Resume: load messages and telemetry from session dir
        session_dir = sessions_dir / session_id
        if session_dir.exists():
            messages_path = session_dir / "messages.jsonl"
            if messages_path.exists():
                from amof.commands.agent_cmd import _load_session_messages
                _load_session_messages(session, messages_path)
                run_manager.append_log(run_id, f"[agent] Resumed session ({session.turn_count} turns)")
        
        max_cost = float(cfg.get("default_max_cost", 5.0))
        telemetry = SessionTelemetry(max_cost=max_cost)
        if session_dir.exists():
            telemetry_path = session_dir / "telemetry.json"
            if telemetry_path.exists():
                from amof.commands.agent_cmd import _load_telemetry
                _load_telemetry(telemetry, telemetry_path)
                run_manager.append_log(run_id, f"[agent] Restored telemetry (${telemetry.total_cost:.4f} spent)")
        
        class UIRunEventLog(EventLog):
            def __init__(self, session_id: Optional[str] = None, runs_dir: Optional[Path] = None):
                super().__init__(session_id, runs_dir)
                
            def log(self, event_type: str, **payload: Any) -> Dict[str, Any]:
                event = super().log(event_type, **payload)

                if event_type == "agent_response" and "content_preview" in payload:
                    pass
                elif event_type == "tool_call":
                    tool = payload.get("tool")
                    success = "✓" if payload.get("success") else "✗"
                    duration = payload.get("duration_ms", 0)
                    output_preview = payload.get("output_preview", "")
                    run_manager.append_event(
                        run_id, level="info", type="tool_call",
                        message=f"[{success}] Tool {tool} ({duration}ms)",
                        payload={"tool": tool, "success": payload.get("success"), "duration_ms": duration,
                                 "args": payload.get("args"), "output_preview": output_preview},
                    )
                elif event_type == "llm_call":
                    model = payload.get("model", "")
                    tokens_dict = payload.get("tokens", {})
                    tok_in = tokens_dict.get("in", 0) if isinstance(tokens_dict, dict) else 0
                    tok_out = tokens_dict.get("out", 0) if isinstance(tokens_dict, dict) else 0
                    cost = payload.get("cost", 0)
                    latency = payload.get("latency_ms", 0)
                    # IAL Phase 1 / 1b: surface source attribution
                    # (master / summarizer / indexer / runner:<name>) for
                    # every llm_call rebroadcast. After Phase 1b the master
                    # path also goes through the authority and supplies
                    # source="master" explicitly, so this default only
                    # applies to legacy / CLI emissions that bypass the
                    # authority entirely (e.g. `amof agent` invocations
                    # constructing Agent without an authority).
                    source = payload.get("source") or "master"
                    # Mini Ultra Plan 2 / Phase L2: forward the truthful
                    # provider id (anthropic/openrouter/local/runpod/...)
                    # the authority extracted from the resolved client.
                    # Empty string is preserved (don't fabricate) so the
                    # UI can distinguish "authority did not record one"
                    # from any specific provider.
                    provider = payload.get("provider", "") or ""
                    provider_bit = f" via {provider}" if provider else ""
                    run_manager.append_event(
                        run_id, level="info", type="llm_call",
                        message=f"LLM call [{source}{provider_bit}] ({model}, {tok_in}+{tok_out} tokens, ${cost:.4f})",
                        payload={"model": model, "prompt_tokens": tok_in, "completion_tokens": tok_out,
                                 "cost": cost, "latency_ms": latency, "source": source,
                                 "provider": provider,
                                 "upstream_provider": payload.get("upstream_provider"),
                                 "upstream_model": payload.get("upstream_model"),
                                 "request_id": payload.get("request_id"),
                                 "policy_decision": payload.get("policy_decision"),
                                 "input_hash": payload.get("input_hash"),
                                 "output_hash": payload.get("output_hash")},
                    )
                elif event_type in ("session_start", "session_end"):
                    run_manager.append_event(
                        run_id, level="info", type=event_type,
                        message=str(payload.get("mode", "") or payload.get("goal", "")),
                    )
                    # C5: re-emit a top-level `telemetry` SSE event on session_end so
                    # the right-rail (which gates Section A on event.type == "telemetry")
                    # stops being structurally hidden. Do not collapse the payload —
                    # ship the full SessionTelemetry.to_dict() shape that the UI reads.
                    if event_type == "session_end":
                        telemetry_payload = payload.get("telemetry")
                        if isinstance(telemetry_payload, dict) and telemetry_payload:
                            run_manager.append_event(
                                run_id, level="info", type="telemetry",
                                message="Session telemetry snapshot",
                                payload=telemetry_payload,
                            )
                elif event_type == "error":
                    # Mini Ultra Plan 2 / Phase L1 truthfulness fix:
                    # forward structured provider attribution to SSE so
                    # the right-rail and run detail can show the actual
                    # provider/model/failure_class instead of a
                    # message-only blob. Without this, `events.jsonl`
                    # carried truthful fields (provider, failure_class,
                    # failure_provider, status_code, resumable, model,
                    # source) but the SSE rebroadcast collapsed to a
                    # message-only string and the UI lost attribution.
                    structured_payload: Dict[str, Any] = {
                        "message": payload.get("message"),
                    }
                    for key in (
                        "provider",
                        "failure_class",
                        "failure_provider",
                        "status_code",
                        "resumable",
                        "model",
                        "source",
                        "stop_reason",
                    ):
                        value = payload.get(key)
                        if value is not None:
                            structured_payload[key] = value
                    run_manager.append_event(
                        run_id, level="error", type="error",
                        message=f"Error: {payload.get('message')}",
                        payload=structured_payload,
                    )
                else:
                    run_manager.append_event(run_id, level="info", type=event_type, message=str(payload))

                return event
                
        events = UIRunEventLog(session_id=session_id, runs_dir=sessions_dir)
        trust_state = create_trust_state(prompt)

        # IAL Phase 1 / 1b — shared runtime chokepoint.
        #
        # D1 audit (`docs/audit/runtime-truth-ial-reality.md`) verdict: the
        # master Agent loop is already a chokepoint, but DelegateTool's batch
        # summarizer and the API-side CodebaseIndexer call `LLMClient.chat`
        # directly so their tokens / cost / latency disappear from operator
        # telemetry. Construct one `InferenceAuthority` per run, bound to this
        # run's SessionTelemetry + UI event sink, and route off-funnel callers
        # through it via `with_source(...)` adapters (Phase 1).
        #
        # Phase 1b: also pass this authority into the Agent below
        # (`inference_authority=...`) so the master path's per-step telemetry
        # recording and `llm_call` event emission are owned by the authority
        # instead of `Agent.run`. Raw events.jsonl master entries then carry
        # `source="master"` and there is exactly one telemetry recording per
        # call. Provider cascade, failover and circuit-breaker state still
        # live in `primary_llm` (ModelRouter); the authority remains a thin
        # shim whose only job is uniform source attribution + emission.
        inference_authority = InferenceAuthority(
            primary_llm,
            telemetry=telemetry,
            events=events,
            default_source="master",
        )

        # IAL Phase 2a — opt-in local inference for non-master sources.
        #
        # Reads `local_inference` out of agent.yaml and, when enabled,
        # registers a LocalOpenAICompatibleClient per allowlisted source
        # (currently only `summarizer` and `indexer`) on the authority. The
        # authority then routes those sources to the local endpoint while
        # master continues to use `primary_llm` (cloud ModelRouter). On
        # local failure, the per-source `fallback` policy decides whether
        # to fall through to the cloud client or re-raise. When the block
        # is absent or `enabled: false`, this is a no-op and Phase 1/1b
        # cloud-only behaviour is preserved exactly.
        try:
            _registered_local = _register_local_inference_clients(
                inference_authority,
                cfg,
                log=lambda msg: run_manager.append_log(run_id, msg),
            )
            if _registered_local:
                _summary = ", ".join(
                    f"{src}->{model}" for src, model in _registered_local.items()
                )
                run_manager.append_log(
                    run_id,
                    f"[agent] local_inference active: {_summary}",
                )
        except Exception as exc:
            run_manager.append_log(
                run_id,
                f"[agent] local_inference setup failed (non-fatal): {exc}",
            )

        # 4. Setup Context and Tools
        def _queue_stop_requested() -> bool:
            try:
                queue_store = getattr(run_manager, "queue_store", None)
                if queue_store is None:
                    return False
                item = queue_store.load(run_id)
                return bool(item and item.control.get("stop_requested"))
            except Exception:
                return False
        
        # Setup Vector Store
        vector_store = None
        try:
            from amof.orchestrator.memory import VectorStore
            vector_store = VectorStore(persist_directory=root / ".amof" / "vector_store")
        except Exception as e:
            logger.warning("Vector memory unavailable: %s", e)

        # ---- Auto-index codebase (Merkle tree + incremental LLM index) ----
        codebase_index = None
        index_dir = root / "ecosystems" / ecosystem / "index" if ecosystem else root / ".amof" / "index"

        repos_root = root / "repos"
        if repos_root.exists() and cfg.get("auto_index", True):
            try:
                from amof.orchestrator.indexer import CodebaseIndexer, MAX_FILES_FOR_INDEXING
                from amof.orchestrator.manifest_scope import resolve_scope

                scope = resolve_scope(manifest, root, ecosystem=ecosystem)
                if scope.is_empty():
                    run_manager.append_log(
                        run_id,
                        f"[agent] Indexing scope empty for ecosystem={ecosystem} "
                        f"(skipped={scope.skipped}); skipping auto-index",
                    )
                else:
                    indexer = CodebaseIndexer(
                        indexer_llm=inference_authority.with_source("indexer"),
                        repos_root=repos_root,
                        index_dir=index_dir,
                        vector_store=vector_store,
                        ecosystem_name=ecosystem,
                        repo_roots=scope.repo_roots,
                    )
                    run_manager.append_log(
                        run_id,
                        f"[agent] Indexing scope: {scope.repo_count} repo(s) "
                        f"({', '.join(p.name for p in scope.repo_roots)})",
                    )

                    if indexer.index_path.exists() and indexer.tree_path.exists():
                        from amof.orchestrator.merkle import MerkleTree
                        current_tree = MerkleTree.build_from_roots(scope.repo_roots)
                        cached_tree = MerkleTree.load(indexer.tree_path)

                        if current_tree.hash == cached_tree.hash:
                            codebase_index = indexer._load_cached()
                            run_manager.append_log(run_id, f"[agent] Index up to date ({codebase_index.file_count} files)")
                        else:
                            diff = MerkleTree.diff(cached_tree, current_tree)
                            is_followup_run = session.turn_count > 0
                            if is_followup_run and diff.total_changes > MAX_FILES_FOR_INDEXING:
                                codebase_index = indexer._load_cached()
                                run_manager.append_log(
                                    run_id,
                                    "[agent] Index stale "
                                    f"({diff.summary()}); follow-up run reusing cached index "
                                    f"because refresh exceeds {MAX_FILES_FOR_INDEXING} changes",
                                )
                            else:
                                run_manager.append_log(run_id, f"[agent] Index stale ({diff.summary()}), updating...")
                                codebase_index = indexer.index(force=False)
                                run_manager.append_log(run_id, f"[agent] Index updated ({codebase_index.file_count} files)")
                    else:
                        run_manager.append_log(run_id, "[agent] No codebase index found, creating...")
                        codebase_index = indexer.index(force=True)
                        run_manager.append_log(run_id, f"[agent] Indexed {codebase_index.file_count} files")
            except Exception as e:
                run_manager.append_log(run_id, f"[agent] Auto-index failed (non-fatal): {e}")

        prompt_filename = "master.md"
        if agent_id and agent_id != "default":
            candidate = root / "prompts" / f"{agent_id}.md"
            if candidate.exists():
                prompt_filename = f"{agent_id}.md"
                run_manager.append_event(run_id, level="info", type="log", message=f"[agent] Using prompt: {prompt_filename}")
            else:
                run_manager.append_event(run_id, level="info", type="log", message=f"[agent] Prompt {agent_id}.md not found, falling back to master.md")

        context_builder = ContextBuilder(
            workspace_root=root,
            manifest=manifest,
            base_prompt_path=root / "prompts" / prompt_filename,
            codebase_index=codebase_index,
        )
        system_prompt = context_builder.build(mode=mode)

        sys_tokens = len(system_prompt) // 4
        run_manager.append_event(
            run_id, level="info", type="context_debug",
            message=f"Context built: ~{sys_tokens} tokens, prompt={prompt_filename}",
            payload={
                "system_prompt_tokens": sys_tokens,
                "system_prompt_chars": len(system_prompt),
                "prompt_file": prompt_filename,
                "agent_id": agent_id or "master",
                "system_prompt_full": system_prompt,
            },
        )

        # Setup Runners (match agent_cmd: jenkins_jobs, deploy_presets, max_cost_per_runner, cascade)
        runner_factory = None
        runners_config_path = root / ".amof" / "rules" / "runners.yaml"
        runner_cost_fraction = float(cfg.get("runner_cost_fraction", 0.3))
        runner_max_cost = max_cost * runner_cost_fraction
        llm_ladder_cfg = cfg.get("llm_ladder", {}).get("roles", {})
        worker_cascade = (llm_ladder_cfg.get("worker") or {}).get("cascade")

        # Mini Ultra Plan 2 / Phase L2: build the per-runner local-client
        # map BEFORE constructing the RunnerFactory so it can be threaded
        # into runner Agents at spawn time. The map is empty when the
        # opt-in conditions aren't met (no runtime_profile, profile not
        # in enabled_for_profile, runner_overrides absent, ...). When
        # populated, RunnerFactory.run_runner swaps the matching runner's
        # ``model_clients["standard"]`` / ``["fast"]`` for the local
        # client before constructing the worker Agent. Default routing
        # is preserved exactly when the map is empty — no silent flip.
        runner_local_clients = _resolve_runner_local_clients(
            cfg,
            runtime_profile,
            log=lambda msg: run_manager.append_log(run_id, msg),
        )

        # Mini Ultra Plan 2 / Phase R1: parallel Runpod resolver. The two
        # profiles are mutually exclusive in canonical config (each opt-in
        # profile is registered against exactly one provider block), so
        # in practice at most one of these returns a non-empty map. We
        # union them with Runpod taking precedence for the same runner
        # name only when an operator explicitly mis-configures the same
        # profile in both places — which would be a configuration error
        # we surface via the operator log line below rather than a silent
        # collision. Default behaviour: empty map → cloud cascade
        # preserved exactly.
        runner_runpod_clients = _resolve_runner_runpod_clients(
            cfg,
            runtime_profile,
            log=lambda msg: run_manager.append_log(run_id, msg),
            strict=True,
        )
        if runner_runpod_clients:
            collisions = set(runner_local_clients) & set(runner_runpod_clients)
            if collisions:
                run_manager.append_log(
                    run_id,
                    f"[agent] WARNING runner_overrides collision for "
                    f"{sorted(collisions)} — Runpod wins (config error: same "
                    f"profile registered against both local and runpod)",
                )
            runner_local_clients = {**runner_local_clients, **runner_runpod_clients}

        if runners_config_path.exists():
            try:
                from amof.orchestrator.runners import RunnerFactory
                _base_tools = create_default_registry(
                    guardrails=guardrails,
                    ops_tools=cfg.get("ops_tools", True),
                    workspace_root=root,
                    jenkins_jobs=cfg.get("jenkins_jobs"),
                    deploy_presets=cfg.get("deploy_presets"),
                    role="worker",
                    ticket_cwd=ticket_cwd,
                    stop_checker=_queue_stop_requested,
                    events=events,
                    trust_state=trust_state,
                    policy_source="runner",
                )
                runner_factory = RunnerFactory.from_config(
                    config_path=runners_config_path,
                    model_clients={"standard": primary_llm},
                    parent_tools=_base_tools,
                    guardrails=guardrails,
                    workspace_root=root,
                    max_cost_per_runner=runner_max_cost,
                    verbose=False,
                    cascade=worker_cascade,
                    inference_authority=inference_authority,
                    runner_local_clients=runner_local_clients,
                )
                run_manager.append_log(run_id, f"[agent] Runners loaded: {', '.join(runner_factory.runner_names)}")
                if runner_local_clients:
                    overridden = ", ".join(
                        f"{name}->{client.model_name()}"
                        for name, client in runner_local_clients.items()
                        if name in runner_factory.runner_names
                    ) or "(none of the configured runners matched)"
                    run_manager.append_log(
                        run_id,
                        f"[agent] runtime_profile='{runtime_profile}' active local "
                        f"runner overrides: {overridden}",
                    )
            except Exception as e:
                logger.warning("Runner factory init failed: %s", e)
                run_manager.append_log(run_id, f"[agent] Runner factory init failed (non-fatal): {e}")

        # Pass parent_telemetry + summarizer_llm so DelegateTool can roll
        # child/runner costs (and batch-summarizer costs) into this run's
        # SessionTelemetry. Without this, RunnerFactory.run_runner() receives
        # parent_telemetry=None and skips _rollup_telemetry — leaving the
        # visible "Agent costs" surface showing only the master agent.
        #
        # IAL Phase 1: route the batch summarizer through the shared inference
        # authority with `source="summarizer"`. The authority records cost +
        # an `llm_call` event tagged by source, eliminating the previous
        # blackhole where DelegateTool summarizer chats vanished from the
        # operator-visible telemetry/event stream.
        tools = create_default_registry(
            guardrails=guardrails,
            ops_tools=cfg.get("ops_tools", True),
            workspace_root=root,
            jenkins_jobs=cfg.get("jenkins_jobs"),
            deploy_presets=cfg.get("deploy_presets"),
            role="orchestrator",
            ecosystem_name=ecosystem,
            runner_factory=runner_factory,
            parent_telemetry=telemetry,
            summarizer_llm=inference_authority.with_source("summarizer"),
            vector_store=vector_store,
            ticket_cwd=ticket_cwd,
            stop_checker=_queue_stop_requested,
            events=events,
            trust_state=trust_state,
            policy_source="master",
        )
        
        # 5. Create Agent
        #
        # IAL Phase 1b: pass the same `inference_authority` so the master
        # path's per-step `record_from_usage` + `record_agent_cost("master")`
        # + `events.llm_call` are owned by the authority instead of the
        # Agent. This makes raw events.jsonl master entries carry
        # `source="master"` and exercises the same emission path as for
        # summarizer / indexer. Underlying ModelRouter, retry, cache, and
        # circuit-breaker bookkeeping stay in the Agent unchanged.
        agent = Agent(
            llm=primary_llm,
            tools=tools,
            system_prompt=system_prompt,
            session=session,
            telemetry=telemetry,
            events=events,
            verbose=False,
            dry_run=effective_dry_run,
            model_router=model_router,
            context_summarizer=context_summarizer,
            inference_authority=inference_authority,
        )
        
        events.session_start(mode=mode, goal=prompt, ecosystem=ecosystem)
        run_manager.append_log(run_id, f"Agent started. Goal: {prompt}")
        
        # 6. Run Agent
        original_cwd = os.getcwd()
        os.chdir(str(root))
        
        try:
            # Override stdout/stderr temporarily just in case tools print directly
            class LogCapturer:
                def __init__(self, run_manager, run_id):
                    self.rm = run_manager
                    self.rid = run_id
                    self.buf = ""
                def write(self, text):
                    if text:
                        self.buf += text
                        while "\n" in self.buf:
                            line, self.buf = self.buf.split("\n", 1)
                            if line: self.rm.append_log(self.rid, line)
                def flush(self):
                    if self.buf:
                        self.rm.append_log(self.rid, self.buf)
                        self.buf = ""
            
            old_stdout, old_stderr = sys.stdout, sys.stderr
            capturer = LogCapturer(run_manager, run_id)
            sys.stdout = sys.stderr = capturer
            
            try:
                response = agent.run(prompt)
                capturer.flush()
                
                # Send the final response as a pure chat event
                if response:
                    for line in response.splitlines():
                        run_manager.append_event(run_id, level="info", type="chat", message=line)
                
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
                
            events.session_end(telemetry.to_dict())
            
            # Persist session to .amof/sessions/<session_id>/ for conversation resume
            from amof.commands.agent_cmd import _save_session, _generate_journal
            session_dir_path = _save_session(session, telemetry, events, root, session_subdir="sessions")
            _generate_journal(session, prompt, getattr(agent, "stop_reason", "completed"), telemetry, events, manifest, root)

            # Resolve stop reason once — used by both the C4 snapshot and the
            # terminal-status classification below so they cannot disagree.
            stop_reason_val = getattr(agent, "stop_reason", "completed") or "completed"
            iterations_used_val = int(getattr(agent, "iteration_count", 0) or 0)
            max_iterations_val = getattr(agent, "max_iterations", None)

            # C4: project session truth onto the run record so `session_state`,
            # `session_message`, `session_has_telemetry`, `session_has_messages`
            # in /runs/{id} stop being structurally null. Keep the messages slice
            # bounded — only role+timestamp metadata; the full content lives at
            # `messages_path` on disk. Also surface `iterations_used` and
            # `max_iterations` so /runs/{id}/session can return real loop truth
            # for kind=agent runs without UI changes.
            try:
                run_manager.update_session_snapshot(
                    run_id,
                    {
                        "session_state": stop_reason_val,
                        "message": f"Session ended: {stop_reason_val}",
                        "telemetry": telemetry.to_dict(),
                        "messages": [
                            {"role": m.role, "timestamp": m.timestamp}
                            for m in session.messages
                        ],
                        "stop_reason": stop_reason_val,
                        "iterations_used": iterations_used_val,
                        "max_iterations": max_iterations_val,
                        "messages_path": str(session_dir_path / "messages.jsonl"),
                    },
                )
            except Exception as snap_exc:
                logger.warning("update_session_snapshot failed for run %s: %s", run_id, snap_exc)
            
            # Use raw string since run_manager takes a string
            summary_lines = telemetry.summary().splitlines()
            for line in summary_lines:
                if line.strip():
                    run_manager.append_log(run_id, line)

            # Terminal classification: only "completed" is a truthful success.
            # Other stop reasons (cost_exceeded, circuit_breaker, max_iterations,
            # pending) mean the main execution path did not complete truthfully —
            # e.g. provider 402 / credit exhaustion lands here as circuit_breaker
            # after retries, and must not be reported as success.
            if stop_reason_val == "completed":
                run_manager.update_status(run_id, RUN_STATUS_SUCCESS, exit_code=0)
            elif stop_reason_val == "cancelled":
                run_manager.append_log(
                    run_id,
                    "Run cancelled after stop request interrupted the active Shell tool.",
                )
                run_manager.update_status(run_id, RUN_STATUS_CANCELLED, exit_code=130)
            else:
                run_manager.append_log(
                    run_id,
                    f"Run failed: stop_reason={stop_reason_val}",
                )
                run_manager.update_status(run_id, RUN_STATUS_FAILED, exit_code=1)

            # Project agent loop truth into loop_state AFTER terminal status so
            # the persistence/queue fields written by _finalize_terminal_loop_state
            # are preserved and we only overlay agent-specific fields. The UI
            # bounded-loop card reads `loop_state.loop_step`, so populating that
            # alongside `iterations_used` and `max_iterations` removes the
            # remaining false `n/a` for kind=agent runs. We keep the existing
            # `stop_reason` (queue/terminal classification) and add a separate
            # `agent_stop_reason` so downstream readers don't lose the truthful
            # agent exit (completed / cost_exceeded / max_iterations / ...).
            try:
                terminal_run = run_manager.get_run(run_id)
                if terminal_run is not None:
                    merged_loop_state = dict(terminal_run.loop_state or {})
                    merged_loop_state["loop_step"] = iterations_used_val
                    merged_loop_state["iterations_used"] = iterations_used_val
                    if max_iterations_val is not None:
                        merged_loop_state["max_iterations"] = max_iterations_val
                    merged_loop_state["agent_stop_reason"] = stop_reason_val
                    if stop_reason_val == "cancelled":
                        merged_loop_state["cancel_requested"] = True
                    # Mini Ultra Plan 2 / Phase L2: surface the runtime
                    # profile that drove this run (e.g. "local_qwen") so
                    # /runs/{id}.loop_state truthfully reports which
                    # provider/runner override was in effect. Persisted
                    # only when explicitly set by the caller — don't
                    # invent a default.
                    if runtime_profile:
                        merged_loop_state["runtime_profile"] = runtime_profile

                    # Project truthful provider failure attribution onto the
                    # run record so /runs/{id}.loop_state surfaces the actual
                    # provider, HTTP status code, structured failure class and
                    # whether the run is resumable. This is what lets the
                    # dashboard show "OpenRouter 402 payment_required
                    # (resumable)" instead of a generic api_error /
                    # circuit_breaker.
                    failure_class = getattr(agent, "failure_class", None)
                    if failure_class:
                        merged_loop_state["failure_class"] = failure_class
                        merged_loop_state["failure_provider"] = getattr(
                            agent, "failure_provider", None
                        )
                        merged_loop_state["failure_status_code"] = getattr(
                            agent, "failure_status_code", None
                        )
                        merged_loop_state["resumable"] = bool(
                            getattr(agent, "resumable", False)
                        )

                    run_manager.update_loop_state(run_id, merged_loop_state)
            except Exception as proj_exc:
                logger.warning(
                    "loop_state projection failed for run %s: %s", run_id, proj_exc
                )

        finally:
            os.chdir(original_cwd)
            
    except Exception as e:
        logger.exception("Agent run failed")
        run_manager.append_log(run_id, f"Agent failed: {str(e)}")
        run_manager.update_status(run_id, RUN_STATUS_FAILED)
