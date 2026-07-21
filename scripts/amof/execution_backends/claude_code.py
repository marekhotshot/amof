"""Claude Code CLI backend contract for governed AMOF handoffs.

Second agent runtime beside the Hermes backend. The same governed selection
(capabilities, writable roots, timeout) and the same AgentRunResult contract
apply; only the execution engine differs: missions are dispatched to the
Claude Code CLI in headless print mode with an explicit tool allowlist, and
inference goes directly to the Anthropic API (ANTHROPIC_API_KEY).

Shared mission semantics (prompt contract, structured write-scope proposal
extraction, read-only mutation restore + replan, changed-path accounting)
are reused from the Hermes backend module so both runtimes stay contract-
identical from the operator's point of view.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..app_paths import runs_dir
from . import hermes_opensandbox as _shared
from .hermes_opensandbox import (
    HermesBackendSelection,
    WRITE_SCOPE_PROPOSAL_REQUIRED,
)

BACKEND_TYPE = "claude_code"
BACKEND_CONTRACT_VERSION = "claude-code-cli-v1"
RUNTIME_CONTRACT = "Claude Code CLI (headless print mode) + Anthropic API"
ISOLATION_MODEL = "runtime_owner_workspace"
FUTURE_ISOLATION_MODELS = tuple(_shared.FUTURE_ISOLATION_MODELS)
PROVIDER = "anthropic"
TRANSPORT = "anthropic_api"
SUPPORTED_CAPABILITIES = tuple(_shared.SUPPORTED_CAPABILITIES)
DEFAULT_MODEL = "claude-sonnet-4-5"
AGENT_LABEL = "Claude Code"

# Tools the headless CLI may use without interactive permission prompts.
# Read-only runs never receive edit tools; the post-run changed-path guard
# (restore + one replan, then fail closed) still backstops Bash side effects,
# mirroring the Hermes backend's read-only enforcement.
READ_ONLY_ALLOWED_TOOLS = (
    "Read",
    "Grep",
    "Glob",
    "LS",
    "TodoWrite",
    "Task",
    "Bash(git status:*)",
    "Bash(git log:*)",
    "Bash(git diff:*)",
    "Bash(git show:*)",
    "Bash(git ls-files:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(rg:*)",
    "Bash(grep:*)",
    "Bash(find:*)",
)
BOUNDED_WRITE_ALLOWED_TOOLS = READ_ONLY_ALLOWED_TOOLS + (
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
)

# Same governance posture as the Hermes backend: the runner never receives
# alternative provider credentials, and never escalates beyond the workspace.
_STRIPPED_ENV_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "AMOF_REMOTE_IAL_API_KEY",
)


class ClaudeCodeBackendError(RuntimeError):
    """Raised when the Claude Code backend cannot be dispatched truthfully."""


def claude_executable() -> Path:
    override = str(os.environ.get("AMOF_CLAUDE_CODE_BIN") or "").strip()
    if override:
        return Path(override).expanduser().resolve(strict=False)
    found = shutil.which("claude")
    if found:
        return Path(found)
    return Path("/usr/local/bin/claude")


def _requested_model(model_override: str | None = None) -> str:
    return (
        str(model_override or os.environ.get("AMOF_CLAUDE_CODE_MODEL") or "").strip()
        or DEFAULT_MODEL
    )


def _api_key() -> str:
    return str(os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def is_claude_code_runner(record: dict[str, Any]) -> bool:
    return _shared.runner_backend_type(record) == BACKEND_TYPE


def claude_dispatch_command(*, model: str, writable: bool) -> list[str]:
    # The mission prompt is delivered on stdin, never as a positional
    # argument: --allowedTools is variadic in the Claude CLI and would
    # swallow a trailing positional prompt, leaving the run with no input.
    tools = BOUNDED_WRITE_ALLOWED_TOOLS if writable else READ_ONLY_ALLOWED_TOOLS
    command = [
        str(claude_executable()),
        "--print",
        "--output-format",
        "json",
        "--model",
        model,
        "--allowedTools",
        ",".join(tools),
    ]
    if writable:
        command.extend(["--permission-mode", "acceptEdits"])
    return command


def _probe_claude_cli_contract(model: str) -> dict[str, Any]:
    executable = claude_executable()
    dispatch_preview = claude_dispatch_command(model=model, writable=False)
    if not executable.is_file():
        return {
            "status": "unavailable",
            "exit_code": 127,
            "stdout": "",
            "stderr": "claude executable not found",
            "probe_command": [str(executable), "--version"],
            "dispatch_command_preview": dispatch_preview,
        }
    if not os.access(executable, os.X_OK):
        return {
            "status": "unavailable",
            "exit_code": 126,
            "stdout": "",
            "stderr": "claude executable is not executable",
            "probe_command": [str(executable), "--version"],
            "dispatch_command_preview": dispatch_preview,
        }
    completed = subprocess.run(
        [str(executable), "--version"],
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    return {
        "status": "ready" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "").strip(),
        "stderr": (completed.stderr or "").strip(),
        "probe_command": [str(executable), "--version"],
        "dispatch_command_preview": dispatch_preview,
    }


def runtime_health() -> dict[str, Any]:
    executable = claude_executable()
    dispatch_probe = _probe_claude_cli_contract(_requested_model())
    api_key_present = bool(_api_key())
    cli_ready = dispatch_probe["status"] == "ready"
    return {
        "backend_type": BACKEND_TYPE,
        "backend_contract_version": BACKEND_CONTRACT_VERSION,
        "runtime_contract": RUNTIME_CONTRACT,
        "isolation_model": ISOLATION_MODEL,
        "future_isolation_models": list(FUTURE_ISOLATION_MODELS),
        "dispatch_available": cli_ready and api_key_present,
        "runtime_health": "ready" if cli_ready else "unavailable",
        "claude_code_runtime": "ready" if cli_ready else "unavailable",
        "inference_transport": TRANSPORT,
        "inference_health": "ready" if api_key_present else "blocked",
        "requested_provider": PROVIDER,
        "effective_provider": PROVIDER if api_key_present else "unverified",
        "requested_model": _requested_model(),
        "effective_model": _requested_model() if api_key_present else "unverified",
        "direct_provider_fallback": "disabled",
        "execution_endpoint": str(executable),
        "process_identity": {
            "backend_id": BACKEND_TYPE,
            "backend_contract_version": BACKEND_CONTRACT_VERSION,
            "runtime_contract": RUNTIME_CONTRACT,
            "isolation_model": ISOLATION_MODEL,
            "future_isolation_models": list(FUTURE_ISOLATION_MODELS),
            "claude_executable": str(executable),
            "dispatch_probe": dict(dispatch_probe),
            "runner_version": str(dispatch_probe.get("stdout") or ""),
        },
        "supported_capabilities": list(SUPPORTED_CAPABILITIES),
        "writable_root_required": True,
        "cancellation_support": "timeout_process_termination",
        "log_event_support": "stdout_stderr_event_jsonl",
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
        "claude_code_runtime": health.get(
            "claude_code_runtime", health["runtime_health"]
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
) -> HermesBackendSelection:
    # Selection semantics (capability gating, writable-root containment) are
    # backend-independent; reuse the shared governed builder.
    return _shared.build_selection(
        runner_id=runner_id,
        requested_capabilities=requested_capabilities,
        approve_writable_roots=approve_writable_roots,
        timeout_seconds=timeout_seconds,
        readable_root=readable_root,
    )


def _run_dir(run_id: str) -> Path:
    path = runs_dir() / "claude-code" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _base_env(run_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    config_dir = run_dir / "claude-home"
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_ERROR_REPORTING"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    for name in _STRIPPED_ENV_NAMES:
        env.pop(name, None)
    return env


def _parse_cli_envelope(stdout: str) -> tuple[dict[str, Any] | None, str]:
    """Parse the --output-format json envelope; fall back to raw stdout."""
    text = stdout.strip()
    if not text:
        return None, ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None, text
    if not isinstance(parsed, dict):
        return None, text
    result_text = str(parsed.get("result") or "").strip()
    return parsed, result_text


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
    run_id = f"claude-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{_shared._safe_id(request_id)}"
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
    dispatch_probe = dict(
        health.get("process_identity", {}).get("dispatch_probe") or {}
    )
    if not dispatch_probe:
        dispatch_probe = _probe_claude_cli_contract(requested_model)
    _shared._append_event(event_log_path, "claude_code_dispatch_probe", **dispatch_probe)

    if provider and provider not in {PROVIDER, "claude-code"}:
        return _blocked_result(
            run_id=run_id,
            stop_reason="inference_transport_unavailable",
            final_text="Provider override is not allowed for the AMOF-managed Claude Code runner.",
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
            final_text="Anthropic API credential (ANTHROPIC_API_KEY) is not configured for the Claude Code runner.",
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
    if dispatch_probe["status"] != "ready":
        return _blocked_result(
            run_id=run_id,
            stop_reason="claude_code_dispatch_unavailable",
            final_text="Claude Code CLI dispatch is unavailable; selected runner failed closed.",
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
            final_text="Read-only run blocked before execution because workspace has pre-existing tracked changes.",
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
    write_scope_proposal: dict[str, Any] | None = None
    proposal_missing_reason: str | None = None
    task_findings = ""
    runtime_detail = ""
    validation_status = "not_run"
    changed: list[str] = []
    cli_envelope: dict[str, Any] | None = None
    while True:
        command = claude_dispatch_command(
            model=requested_model,
            writable=bool(selection.writable_roots),
        )
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
                    "fallback_used": False,
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
            completed = subprocess.run(
                command,
                cwd=str(workspace),
                env=_base_env(run_dir),
                input=prompt,
                text=True,
                capture_output=True,
                check=False,
                timeout=selection.timeout_seconds,
            )
            stdout_path.write_text(completed.stdout or "", encoding="utf-8")
            stderr_path.write_text(completed.stderr or "", encoding="utf-8")
            runtime_log_path.write_text(
                (completed.stdout or "")
                + ("\n--- STDERR ---\n" + completed.stderr if completed.stderr else ""),
                encoding="utf-8",
            )
            exit_code = int(completed.returncode)
            if exit_code != 0:
                status = "failed"
                stop_reason = "claude_process_failed"
        except subprocess.TimeoutExpired as exc:
            status = "failed"
            stop_reason = "timeout"
            exit_code = 124
            stdout_path.write_text(str(exc.stdout or ""), encoding="utf-8")
            stderr_path.write_text(str(exc.stderr or ""), encoding="utf-8")
            _shared._write_runtime_log(runtime_log_path, "Claude Code process timed out.")
        except Exception as exc:
            status = "failed"
            stop_reason = "claude_runtime_exception"
            exit_code = 1
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            _shared._write_runtime_log(runtime_log_path, f"{type(exc).__name__}: {exc}")

        cli_envelope, raw_task_findings = _parse_cli_envelope(
            stdout_path.read_text(encoding="utf-8")
        )
        if cli_envelope is not None and bool(cli_envelope.get("is_error")):
            status = "failed"
            stop_reason = "claude_process_failed"
            exit_code = exit_code or 1
        runtime_detail = stderr_path.read_text(encoding="utf-8").strip()
        write_scope_proposal, task_findings = (
            _shared._extract_write_scope_proposal_output(
                raw_task_findings,
                expected_allowed_roots=expected_proposal_paths,
            )
        )
        proposal_missing_reason = (
            _shared._proposal_missing_reason(task_findings, runtime_detail)
            if proposal_required and write_scope_proposal is None
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
        if status == "completed" and proposal_required and write_scope_proposal is None:
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
        effective_model=requested_model,
        write_scope_proposal=write_scope_proposal,
        proposal_missing_reason=proposal_missing_reason,
        cli_envelope=cli_envelope,
    )
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
    proposal_missing_reason: str | None = None,
    cli_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spent = 0.0
    if isinstance(cli_envelope, dict):
        try:
            spent = float(cli_envelope.get("total_cost_usd") or 0.0)
        except (TypeError, ValueError):
            spent = 0.0
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
            "reason": "Claude Code backend returns process status; focused validation must be requested in mission text.",
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
            **(
                {
                    "cli_envelope_summary": {
                        "session_id": cli_envelope.get("session_id"),
                        "num_turns": cli_envelope.get("num_turns"),
                        "duration_ms": cli_envelope.get("duration_ms"),
                        "total_cost_usd": cli_envelope.get("total_cost_usd"),
                        "is_error": cli_envelope.get("is_error"),
                    }
                }
                if isinstance(cli_envelope, dict)
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
            },
        },
        "budget_summary": {"limit": None, "spent": spent, "remaining": None},
    }


def _runtime_summary_text(
    *,
    status: str,
    stop_reason: str,
    run_id: str,
    task_findings_available: bool,
) -> str:
    findings_state = (
        "task findings captured"
        if task_findings_available
        else "no task findings captured"
    )
    return (
        f"AMOF Claude Code run {run_id} finished with status={status}, "
        f"stop_reason={stop_reason}; {findings_state}. "
        "Authoritative runtime metadata is recorded in this AgentRunResult envelope."
    )
