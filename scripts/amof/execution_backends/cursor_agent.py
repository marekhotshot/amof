"""Cursor Agent backend contract for governed AMOF handoffs.

Worker / delegated implementation backend only. Runtime Authority, IAL
gateway, and operator-console core must never import ``cursor_agent`` /
``@cursor/sdk``. This module is the sole AMOF-owned import boundary; the
Python package is loaded lazily inside the invoke path.

Same governed selection (capabilities, writable roots, timeout) and the
same AgentRunResult contract as hermes_opensandbox / claude_code. Cursor
``agent_id`` / run ``id`` are substrate refs recorded in evidence — never
substituted for AMOF ``run_id``.

``CURSOR_API_KEY`` is a substrate secret (env only). It is not an RA config
surface. Service use defaults ``setting_sources`` to empty (inline config
only; no IDE ambient bleed).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..app_paths import runs_dir
from . import hermes_opensandbox as _shared
from .hermes_opensandbox import (
    HermesBackendSelection,
    WRITE_SCOPE_PROPOSAL_REQUIRED,
)

BACKEND_TYPE = "cursor_agent"
BACKEND_CONTRACT_VERSION = "cursor-agent-v1"
RUNTIME_CONTRACT = "Cursor Agent (local Agent.create/send) + CURSOR_API_KEY"
ISOLATION_MODEL = "runtime_owner_workspace"
FUTURE_ISOLATION_MODELS = tuple(_shared.FUTURE_ISOLATION_MODELS)
PROVIDER = "cursor"
TRANSPORT = "cursor_agent"
SUPPORTED_CAPABILITIES = tuple(_shared.SUPPORTED_CAPABILITIES)
DEFAULT_MODEL = "composer-2.5"
AGENT_LABEL = "Cursor Agent"
# Service default: no ambient IDE/project/user/team/MDM settings.
DEFAULT_SETTING_SOURCES: tuple[str, ...] = ()

# Never forward alternate provider credentials into the Cursor process env.
_STRIPPED_ENV_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "AMOF_REMOTE_IAL_API_KEY",
)


class CursorAgentBackendError(RuntimeError):
    """Raised when the Cursor Agent backend cannot be dispatched truthfully."""


@dataclass(frozen=True)
class CursorAgentInvokeResult:
    """Normalized substrate invoke outcome (mocked in unit tests)."""

    status: str  # finished | error | cancelled | expired | startup_failed
    result_text: str
    substrate_agent_id: str | None = None
    substrate_run_id: str | None = None
    error_message: str | None = None
    is_retryable: bool | None = None
    usage: dict[str, Any] | None = None


def _api_key() -> str:
    return str(os.environ.get("CURSOR_API_KEY") or "").strip()


def _requested_model(model_override: str | None = None) -> str:
    return (
        str(model_override or os.environ.get("AMOF_CURSOR_AGENT_MODEL") or "").strip()
        or DEFAULT_MODEL
    )


def _setting_sources() -> tuple[str, ...]:
    raw = str(os.environ.get("AMOF_CURSOR_AGENT_SETTING_SOURCES") or "").strip()
    if not raw:
        return DEFAULT_SETTING_SOURCES
    if raw.lower() in {"none", "empty", "-"}:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def is_cursor_agent_runner(record: dict[str, Any]) -> bool:
    return _shared.runner_backend_type(record) == BACKEND_TYPE


def _probe_sdk_import() -> dict[str, Any]:
    try:
        import cursor_sdk  # noqa: F401
    except ImportError as exc:
        return {
            "status": "unavailable",
            "exit_code": 127,
            "stdout": "",
            "stderr": f"cursor-sdk package not importable: {exc}",
            "probe_command": ["python", "-c", "import cursor_sdk"],
            "dispatch_command_preview": ["cursor_sdk.Agent.create", "agent.send", "run.wait"],
        }
    return {
        "status": "ready",
        "exit_code": 0,
        "stdout": "cursor_sdk importable",
        "stderr": "",
        "probe_command": ["python", "-c", "import cursor_sdk"],
        "dispatch_command_preview": ["cursor_sdk.Agent.create", "agent.send", "run.wait"],
    }


def runtime_health() -> dict[str, Any]:
    dispatch_probe = _probe_sdk_import()
    api_key_present = bool(_api_key())
    sdk_ready = dispatch_probe["status"] == "ready"
    # Optional allowlist gate: when set to 0/false/off, advertise blocked even
    # if the package+key are present (discoverable but not live-dispatched).
    flag = str(os.environ.get("AMOF_CURSOR_AGENT_ENABLED") or "1").strip().lower()
    flag_enabled = flag not in {"0", "false", "off", "no"}
    dispatch_available = sdk_ready and api_key_present and flag_enabled
    return {
        "backend_type": BACKEND_TYPE,
        "backend_contract_version": BACKEND_CONTRACT_VERSION,
        "runtime_contract": RUNTIME_CONTRACT,
        "isolation_model": ISOLATION_MODEL,
        "future_isolation_models": list(FUTURE_ISOLATION_MODELS),
        "dispatch_available": dispatch_available,
        "runtime_health": "ready" if sdk_ready else "unavailable",
        "cursor_agent_runtime": "ready" if sdk_ready else "unavailable",
        "inference_transport": TRANSPORT,
        "inference_health": "ready" if api_key_present else "blocked",
        "requested_provider": PROVIDER,
        "effective_provider": PROVIDER if api_key_present else "unverified",
        "requested_model": _requested_model(),
        "effective_model": _requested_model() if api_key_present else "unverified",
        "direct_provider_fallback": "disabled",
        "execution_endpoint": "cursor_agent.Agent",
        "feature_flag_enabled": flag_enabled,
        "default_setting_sources": list(DEFAULT_SETTING_SOURCES),
        "cancellation_support": True,
        "log_event_support": True,
        "process_identity": {
            "backend_id": BACKEND_TYPE,
            "backend_contract_version": BACKEND_CONTRACT_VERSION,
            "runtime_contract": RUNTIME_CONTRACT,
            "isolation_model": ISOLATION_MODEL,
            "dispatch_probe": dispatch_probe,
            "setting_sources": list(_setting_sources()),
            "substrate_secret": "CURSOR_API_KEY",
            "substrate_secret_configured": api_key_present,
        },
        "supported_capabilities": list(SUPPORTED_CAPABILITIES),
    }


def doctor_record(record: dict[str, Any]) -> dict[str, Any]:
    health = runtime_health()
    capabilities = [
        str(item) for item in record.get("capabilities", []) if str(item).strip()
    ]
    mutation_modes = [
        str(item)
        for item in record.get("allowed_mutation_modes", [])
        if str(item).strip()
    ]
    return {
        "runner_id": str(record.get("runner_id") or ""),
        "backend_type": _shared.runner_backend_type(record),
        "backend_contract_version": health.get("backend_contract_version"),
        "runtime_contract": health.get("runtime_contract"),
        "isolation_model": health.get("isolation_model"),
        "dispatch_available": bool(health["dispatch_available"]),
        "runtime_health": health["runtime_health"],
        "dispatch": "available" if health["dispatch_available"] else "blocked",
        "cursor_agent_runtime": health.get(
            "cursor_agent_runtime", health["runtime_health"]
        ),
        "inference_transport": health.get("inference_transport", TRANSPORT),
        "inference_health": health.get("inference_health", "blocked"),
        "requested_provider": health.get("requested_provider", PROVIDER),
        "effective_provider": health.get("effective_provider", "unverified"),
        "requested_model": health.get("requested_model", "unconfigured"),
        "effective_model": health.get("effective_model", "unverified"),
        "direct_provider_fallback": health.get("direct_provider_fallback", "disabled"),
        "execution_endpoint": health["execution_endpoint"],
        "process_identity": health["process_identity"],
        "supported_capabilities": list(SUPPORTED_CAPABILITIES),
        "registered_capabilities": capabilities,
        "registered_mutation_modes": mutation_modes,
        "writable_root_required": True,
        "cancellation_support": health["cancellation_support"],
        "log_event_support": health["log_event_support"],
    }


def build_selection(
    *,
    runner_id: str,
    requested_capabilities: list[str],
    approve_writable_roots: list[str],
    timeout_seconds: int,
    readable_root: str | None,
    write_scope_binding_id: str | None = None,
) -> HermesBackendSelection:
    return _shared.build_selection(
        runner_id=runner_id,
        requested_capabilities=requested_capabilities,
        approve_writable_roots=approve_writable_roots,
        timeout_seconds=timeout_seconds,
        readable_root=readable_root,
        write_scope_binding_id=write_scope_binding_id,
    )


def _run_dir(run_id: str) -> Path:
    path = runs_dir() / "cursor-agent" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def invoke_cursor_local(
    *,
    prompt: str,
    cwd: str,
    model: str,
    api_key: str,
    setting_sources: Sequence[str] = DEFAULT_SETTING_SOURCES,
) -> CursorAgentInvokeResult:
    """Invoke Cursor Agent local runtime. Patchable for unit tests (no live key)."""
    try:
        from cursor_sdk import (  # type: ignore[import-not-found]
            Agent,
            CursorAgentError,
            LocalAgentOptions,
        )
    except ImportError as exc:
        return CursorAgentInvokeResult(
            status="startup_failed",
            result_text="",
            error_message=f"cursor-sdk not installed: {exc}",
            is_retryable=False,
        )

    try:
        with Agent.create(
            model=model,
            api_key=api_key,
            local=LocalAgentOptions(
                cwd=cwd,
                setting_sources=list(setting_sources),
            ),
        ) as agent:
            substrate_agent_id = str(
                getattr(agent, "agent_id", None) or getattr(agent, "agentId", None) or ""
            ).strip() or None
            run = agent.send(prompt)
            substrate_run_id = str(getattr(run, "id", None) or "").strip() or None
            result = run.wait()
            status = str(getattr(result, "status", "") or "").strip() or "error"
            result_text = str(getattr(result, "result", None) or "").strip()
            usage_obj = getattr(result, "usage", None)
            usage: dict[str, Any] | None = None
            if usage_obj is not None:
                if isinstance(usage_obj, dict):
                    usage = dict(usage_obj)
                else:
                    usage = {
                        key: getattr(usage_obj, key, None)
                        for key in (
                            "input_tokens",
                            "output_tokens",
                            "total_tokens",
                        )
                        if getattr(usage_obj, key, None) is not None
                    } or None
            return CursorAgentInvokeResult(
                status=status,
                result_text=result_text,
                substrate_agent_id=substrate_agent_id,
                substrate_run_id=substrate_run_id
                or (str(getattr(result, "id", None) or "").strip() or None),
                usage=usage,
            )
    except CursorAgentError as exc:
        return CursorAgentInvokeResult(
            status="startup_failed",
            result_text="",
            error_message=str(getattr(exc, "message", None) or exc),
            is_retryable=bool(getattr(exc, "is_retryable", None)),
        )


def _blocked_result(
    *,
    run_id: str,
    stop_reason: str,
    final_text: str,
    studio_session_id: str | None,
    event_log_path: Path,
    runtime_log_path: Path,
    result_path: Path,
    selection: HermesBackendSelection,
    health: dict[str, Any],
    dispatch_probe: dict[str, Any],
    requested_model: str,
    started_at: str,
    reason: str,
    extra_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = _result_payload(
        run_id=run_id,
        status="blocked",
        exit_code=1,
        stop_reason=stop_reason,
        final_text=final_text,
        studio_session_id=studio_session_id,
        event_log_path=event_log_path,
        runtime_log_path=runtime_log_path,
        changed_paths=[],
        selection=selection,
        health=health,
        dispatch_probe=dispatch_probe,
        requested_model=requested_model,
        effective_model="unverified",
    )
    if extra_evidence:
        result["evidence_refs"].update(extra_evidence)
    _shared._append_event(event_log_path, "run_blocked", reason=reason)
    return _shared._write_terminal_result(
        result_path=result_path,
        event_log_path=event_log_path,
        runtime_log_path=runtime_log_path,
        result=result,
        reason=reason,
        started_at=started_at,
    )


def run(
    *,
    manifest: dict[str, Any],
    goal: str,
    request_id: str,
    studio_session_id: str | None,
    selection: HermesBackendSelection,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    health = runtime_health()
    # AMOF-owned run id — never reuse Cursor agent/run ids here.
    run_id = (
        f"cursor-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        f"-{_shared._safe_id(request_id)}"
    )
    run_dir = _run_dir(run_id)
    event_log_path = run_dir / "events.jsonl"
    runtime_log_path = run_dir / "runtime.log"
    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    result_path = run_dir / "result.json"
    started_at = _shared._now_iso()
    workspace = _shared._workspace_for(selection, manifest)
    preexisting_changed_paths = _shared._changed_paths(workspace)
    requested_model = _requested_model(model)
    setting_sources = _setting_sources()
    _shared._append_event(
        event_log_path,
        "run_created",
        run_id=run_id,
        session_id=run_id,
        runner_id=selection.runner_id,
        backend=BACKEND_TYPE,
        studio_session_id=studio_session_id,
    )
    _shared._attach_studio_run(
        studio_session_id=studio_session_id,
        run_id=run_id,
        event_log_path=event_log_path,
        run_dir=run_dir,
        result_path=result_path,
        status="running",
    )
    dispatch_probe = dict(health.get("process_identity", {}).get("dispatch_probe") or {})
    if not dispatch_probe:
        dispatch_probe = _probe_sdk_import()
    _shared._append_event(event_log_path, "cursor_agent_dispatch_probe", **dispatch_probe)

    if provider and provider not in {PROVIDER, "cursor-agent", "cursor"}:
        return _blocked_result(
            run_id=run_id,
            stop_reason="inference_transport_unavailable",
            final_text="Provider override is not allowed for the AMOF-managed Cursor Agent runner.",
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result_path=result_path,
            selection=selection,
            health=health,
            dispatch_probe=dispatch_probe,
            requested_model=requested_model,
            started_at=started_at,
            reason="direct_provider_override_rejected",
        )
    if not _api_key():
        return _blocked_result(
            run_id=run_id,
            stop_reason="inference_transport_unavailable",
            final_text="Cursor API credential (CURSOR_API_KEY) is not configured for the Cursor Agent runner.",
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result_path=result_path,
            selection=selection,
            health=health,
            dispatch_probe=dispatch_probe,
            requested_model=requested_model,
            started_at=started_at,
            reason="inference_transport_unavailable",
        )
    if not health.get("feature_flag_enabled", True):
        return _blocked_result(
            run_id=run_id,
            stop_reason="cursor_agent_dispatch_unavailable",
            final_text="Cursor Agent backend is disabled by AMOF_CURSOR_AGENT_ENABLED.",
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result_path=result_path,
            selection=selection,
            health=health,
            dispatch_probe=dispatch_probe,
            requested_model=requested_model,
            started_at=started_at,
            reason="feature_flag_disabled",
        )
    if dispatch_probe["status"] != "ready":
        return _blocked_result(
            run_id=run_id,
            stop_reason="cursor_agent_dispatch_unavailable",
            final_text="Cursor Agent package is unavailable; selected runner failed closed.",
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result_path=result_path,
            selection=selection,
            health=health,
            dispatch_probe=dispatch_probe,
            requested_model=requested_model,
            started_at=started_at,
            reason="dispatch_unavailable",
        )
    if not selection.writable_roots and preexisting_changed_paths:
        return _blocked_result(
            run_id=run_id,
            stop_reason="read_only_workspace_not_clean",
            final_text=_shared._read_only_unclean_workspace_message(
                list(preexisting_changed_paths)
            ),
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result_path=result_path,
            selection=selection,
            health=health,
            dispatch_probe=dispatch_probe,
            requested_model=requested_model,
            started_at=started_at,
            reason="read_only_workspace_not_clean",
            extra_evidence={
                "preexisting_changed_paths": list(preexisting_changed_paths)
            },
        )

    read_only_replan_used = False
    proposal_replan_used = False
    prompt = _shared._build_prompt(
        goal,
        selection,
        workspace,
        manifest,
        agent_label=AGENT_LABEL,
        backend_name=BACKEND_TYPE,
    )
    proposal_required = (
        _shared._goal_requests_write_scope_proposal(goal)
        and not selection.writable_roots
    )
    expected_proposal_paths = _shared._explicit_required_proposal_paths(goal)
    write_scope_proposals: list[dict[str, Any]] = []
    proposal_missing_reason: str | None = None
    task_findings = ""
    runtime_detail = ""
    validation_status = "not_run"
    changed: list[str] = []
    substrate_agent_id: str | None = None
    substrate_run_id: str | None = None
    sdk_envelope: dict[str, Any] | None = None

    while True:
        (run_dir / "request.json").write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "runner_id": selection.runner_id,
                    "backend": BACKEND_TYPE,
                    "backend_contract_version": BACKEND_CONTRACT_VERSION,
                    "runtime_contract": RUNTIME_CONTRACT,
                    "isolation_model": ISOLATION_MODEL,
                    "studio_session_id": studio_session_id,
                    "capabilities": selection.capabilities,
                    "writable_roots": selection.writable_roots,
                    "workspace": str(workspace),
                    "requested_provider": PROVIDER,
                    "requested_model": requested_model,
                    "transport": TRANSPORT,
                    "setting_sources": list(setting_sources),
                    "fallback_used": False,
                    "note": "Cursor agent_id/run.id are substrate refs only; AMOF run_id is authoritative.",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        status = "completed"
        stop_reason = "completed"
        exit_code = 0
        try:
            # Strip alternate provider keys from process env for the invoke window.
            stripped = {name: os.environ.pop(name) for name in _STRIPPED_ENV_NAMES if name in os.environ}
            try:
                invoke = invoke_cursor_local(
                    prompt=prompt,
                    cwd=str(workspace),
                    model=requested_model,
                    api_key=_api_key(),
                    setting_sources=setting_sources,
                )
            finally:
                os.environ.update(stripped)

            stdout_path.write_text(invoke.result_text or "", encoding="utf-8")
            err_text = invoke.error_message or ""
            stderr_path.write_text(err_text, encoding="utf-8")
            runtime_log_path.write_text(
                (invoke.result_text or "")
                + (f"\n--- STDERR ---\n{err_text}" if err_text else ""),
                encoding="utf-8",
            )
            substrate_agent_id = invoke.substrate_agent_id or substrate_agent_id
            substrate_run_id = invoke.substrate_run_id or substrate_run_id
            sdk_envelope = {
                "status": invoke.status,
                "substrate_agent_id": invoke.substrate_agent_id,
                "substrate_run_id": invoke.substrate_run_id,
                "is_retryable": invoke.is_retryable,
                "usage": invoke.usage,
            }
            _shared._append_event(
                event_log_path,
                "cursor_agent_invoke",
                amof_run_id=run_id,
                substrate_agent_id=invoke.substrate_agent_id,
                substrate_run_id=invoke.substrate_run_id,
                status=invoke.status,
            )
            if invoke.status == "startup_failed":
                status = "blocked"
                stop_reason = "cursor_agent_dispatch_unavailable"
                exit_code = 1
            elif invoke.status == "finished":
                status = "completed"
                stop_reason = "completed"
                exit_code = 0
            elif invoke.status == "cancelled":
                status = "failed"
                stop_reason = "cancelled"
                exit_code = 130
            elif invoke.status == "expired":
                status = "failed"
                stop_reason = "timeout"
                exit_code = 124
            else:
                status = "failed"
                stop_reason = "cursor_agent_run_failed"
                exit_code = 2
        except Exception as exc:
            status = "failed"
            stop_reason = "cursor_agent_runtime_exception"
            exit_code = 1
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            _shared._write_runtime_log(runtime_log_path, f"{type(exc).__name__}: {exc}")

        raw_task_findings = stdout_path.read_text(encoding="utf-8").strip()
        runtime_detail = stderr_path.read_text(encoding="utf-8").strip()
        write_scope_proposals, task_findings = (
            _shared._extract_write_scope_proposal_outputs(
                raw_task_findings,
                expected_allowed_roots=expected_proposal_paths,
            )
        )
        proposal_missing_reason = (
            _shared._proposal_missing_reason(task_findings, runtime_detail)
            if proposal_required and not write_scope_proposals
            else None
        )
        validation_status = _shared._infer_validation_status(
            task_findings or runtime_detail
        )
        if status == "completed" and validation_status == "failed":
            status = "failed"
            stop_reason = "validation_failed"
            exit_code = 1
        changed = _shared._changed_paths_delta(
            preexisting_changed_paths, _shared._changed_paths(workspace)
        )
        if status == "completed" and not selection.writable_roots and changed:
            restored_paths = _shared._restore_read_only_paths(workspace, changed)
            if read_only_replan_used:
                status = "failed"
                stop_reason = "read_only_mutation_detected"
                exit_code = 1
                _shared._append_event(
                    event_log_path,
                    "read_only_mutation_blocked",
                    changed_paths=list(changed),
                    restored_paths=list(restored_paths),
                )
                changed = []
                break
            _shared._append_event(
                event_log_path,
                "read_only_mutation_replan",
                changed_paths=list(changed),
                restored_paths=list(restored_paths),
            )
            read_only_replan_used = True
            prompt = _shared._build_prompt(
                goal,
                selection,
                workspace,
                manifest,
                read_only_replan=True,
                agent_label=AGENT_LABEL,
                backend_name=BACKEND_TYPE,
            )
            continue
        if status == "completed" and proposal_required and not write_scope_proposals:
            if not proposal_replan_used:
                _shared._append_event(
                    event_log_path,
                    "proposal_contract_replan",
                    reason=proposal_missing_reason or "structured proposal missing",
                )
                proposal_replan_used = True
                prompt = _shared._build_prompt(
                    goal,
                    selection,
                    workspace,
                    manifest,
                    proposal_replan=True,
                    agent_label=AGENT_LABEL,
                    backend_name=BACKEND_TYPE,
                )
                continue
            status = "blocked"
            stop_reason = WRITE_SCOPE_PROPOSAL_REQUIRED
            exit_code = 1
            validation_status = "failed"
        break

    final_text = _runtime_summary_text(
        status=status,
        stop_reason=stop_reason,
        run_id=run_id,
        task_findings_available=bool(task_findings),
        substrate_agent_id=substrate_agent_id,
        substrate_run_id=substrate_run_id,
    )
    if not task_findings and runtime_detail:
        task_findings = runtime_detail

    result = _result_payload(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        stop_reason=stop_reason,
        final_text=final_text,
        task_findings=task_findings or None,
        studio_session_id=studio_session_id,
        event_log_path=event_log_path,
        runtime_log_path=runtime_log_path,
        changed_paths=changed,
        selection=selection,
        health=health,
        dispatch_probe=dispatch_probe,
        validation_status=validation_status,
        requested_model=requested_model,
        effective_model=requested_model if status != "blocked" else "unverified",
        write_scope_proposals=write_scope_proposals,
        proposal_missing_reason=proposal_missing_reason,
        substrate_agent_id=substrate_agent_id,
        substrate_run_id=substrate_run_id,
        sdk_envelope=sdk_envelope,
    )
    result = _shared._apply_write_scope_enforcement_if_bound(
        result,
        selection=selection,
        run_id=run_id,
        workspace=workspace,
    )
    stop_reason = str(result.get("stop_reason") or stop_reason)
    status = str(result.get("status") or status)
    _shared._write_terminal_result(
        result_path=result_path,
        event_log_path=event_log_path,
        runtime_log_path=runtime_log_path,
        result=result,
        reason=stop_reason,
        started_at=started_at,
    )
    _shared._attach_studio_run(
        studio_session_id=studio_session_id,
        run_id=run_id,
        event_log_path=event_log_path,
        run_dir=run_dir,
        result_path=result_path,
        status=status,
    )
    return result


def _result_payload(
    *,
    run_id: str,
    status: str,
    exit_code: int,
    stop_reason: str,
    final_text: str,
    studio_session_id: str | None,
    event_log_path: Path,
    runtime_log_path: Path,
    changed_paths: list[str],
    selection: HermesBackendSelection,
    health: dict[str, Any],
    dispatch_probe: dict[str, Any],
    validation_status: str = "not_run",
    requested_model: str = "unconfigured",
    effective_model: str = "unverified",
    task_findings: str | None = None,
    write_scope_proposal: dict[str, Any] | None = None,
    write_scope_proposals: list[dict[str, Any]] | None = None,
    proposal_missing_reason: str | None = None,
    substrate_agent_id: str | None = None,
    substrate_run_id: str | None = None,
    sdk_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if write_scope_proposal is None and write_scope_proposals:
        write_scope_proposal = write_scope_proposals[0]
    spent = 0.0
    if isinstance(sdk_envelope, dict) and isinstance(sdk_envelope.get("usage"), dict):
        # Cursor Agent usage is token-shaped; dollar spend is not always present.
        # Keep budget_summary.spent at 0 unless a numeric cost field appears later.
        spent = float(sdk_envelope.get("usage", {}).get("total_cost_usd") or 0.0)
    return {
        "result_kind": "agent_run_result",
        "contract_version": "agent-run-v1",
        "schema_version": 1,
        "status": status,
        "session_id": run_id,
        "exit_code": exit_code,
        "stop_reason": stop_reason,
        "final_text": final_text,
        "task_findings": task_findings,
        **(
            {"write_scope_proposal": write_scope_proposal}
            if write_scope_proposal is not None
            else {}
        ),
        **(
            {"write_scope_proposals": write_scope_proposals}
            if write_scope_proposals
            else {}
        ),
        **(
            {"proposal_missing_reason": proposal_missing_reason}
            if proposal_missing_reason is not None
            else {}
        ),
        "runner_id": selection.runner_id,
        "backend": BACKEND_TYPE,
        "requested_provider": PROVIDER,
        "effective_provider": PROVIDER
        if effective_model != "unverified"
        else "unverified",
        "requested_model": requested_model,
        "effective_model": effective_model,
        "transport": TRANSPORT,
        "fallback_used": False,
        "studio_session_id": studio_session_id,
        "plan_path": None,
        "checkpoint_path": None,
        "event_log_path": str(event_log_path),
        "runtime_log_path": str(runtime_log_path),
        "journal_path": None,
        "changed_paths": changed_paths,
        "validation_summary": {
            "status": validation_status,
            "reason": (
                "Cursor Agent backend returns process status; focused validation "
                "must be requested in mission text. Evidence density is weaker "
                "than Hermes/Claude CLI unless normalized."
            ),
        },
        "approved_capabilities": list(selection.capabilities),
        "effective_capabilities": list(selection.capabilities),
        "evidence_refs": {
            "backend_contract_version": BACKEND_CONTRACT_VERSION,
            "runtime_contract": RUNTIME_CONTRACT,
            "isolation_model": ISOLATION_MODEL,
            "event_log_path": str(event_log_path),
            "runtime_log_path": str(runtime_log_path),
            "process_identity": health.get("process_identity"),
            "dispatch_probe": dict(dispatch_probe),
            "amof_run_id": run_id,
            "substrate_agent_id": substrate_agent_id,
            "substrate_run_id": substrate_run_id,
            **(
                {"sdk_envelope_summary": dict(sdk_envelope)}
                if isinstance(sdk_envelope, dict)
                else {}
            ),
            "inference": {
                "requested_provider": PROVIDER,
                "effective_provider": PROVIDER
                if effective_model != "unverified"
                else "unverified",
                "requested_model": requested_model,
                "effective_model": effective_model,
                "transport": TRANSPORT,
                "fallback_used": False,
                "direct_provider_fallback": "disabled",
                "substrate_secret": "CURSOR_API_KEY",
                "setting_sources": list(
                    (health.get("process_identity") or {}).get("setting_sources")
                    or DEFAULT_SETTING_SOURCES
                ),
            },
        },
        "budget_summary": {"limit": None, "spent": spent, "remaining": None},
        "warnings": [],
        "usage": {
            "prompt_tokens": None,
            "completion_tokens": None,
            "cache_tokens": None,
            "reasoning_tokens": None,
            "model_calls": None,
            "tool_calls": None,
            "agent_calls": 1,
            "billing_model": "subscription",
            "token_telemetry": "unavailable",
            "subagent_telemetry": "unavailable",
        },
    }


def _runtime_summary_text(
    *,
    status: str,
    stop_reason: str,
    run_id: str,
    task_findings_available: bool,
    substrate_agent_id: str | None = None,
    substrate_run_id: str | None = None,
) -> str:
    findings_state = (
        "task findings captured"
        if task_findings_available
        else "no task findings captured"
    )
    substrate = ""
    if substrate_agent_id or substrate_run_id:
        substrate = (
            f" substrate_agent_id={substrate_agent_id or 'none'}"
            f" substrate_run_id={substrate_run_id or 'none'};"
        )
    return (
        f"AMOF Cursor Agent run {run_id} finished with status={status}, "
        f"stop_reason={stop_reason};{substrate} {findings_state}. "
        "Authoritative runtime metadata is recorded in this AgentRunResult envelope."
    )
