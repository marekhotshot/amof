"""Hermes/OpenSandbox execution backend contract for governed AMOF handoffs."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..app_paths import runs_dir
from ..commands.studio import attach_run_reference, require_active_studio_session

BACKEND_TYPE = "hermes_opensandbox"
DEFAULT_HERMES_RUNTIME_ROOT = Path.home() / ".local" / "share" / "amof" / "runners" / "hermes-agent" / "v2026.6.5"
DEFAULT_OPENSANDBOX_RUNTIME_ROOT = Path.home() / ".local" / "share" / "amof" / "runners" / "opensandbox" / "0.1.14"
SUPPORTED_CAPABILITIES = ("read", "bounded_write", "shell_limited", "focused_tests")
DANGEROUS_CAPABILITIES = {
    "kubernetes_mutation",
    "deployment",
    "deploy",
    "secrets",
    "secret_access",
    "network_unrestricted",
    "unrestricted_network",
    "push",
    "promotion",
    "promote",
    "tags",
    "releases",
}


class HermesBackendError(RuntimeError):
    """Raised when Hermes/OpenSandbox cannot be dispatched truthfully."""


@dataclass(frozen=True)
class HermesBackendSelection:
    runner_id: str
    capabilities: list[str]
    writable_roots: list[str]
    timeout_seconds: int
    readable_root: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return normalized[:96] or "hermes-run"


def _runtime_root_from_env(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, "") or default).expanduser().resolve(strict=False)


def hermes_runtime_root() -> Path:
    return _runtime_root_from_env("AMOF_HERMES_RUNTIME_ROOT", DEFAULT_HERMES_RUNTIME_ROOT)


def opensandbox_runtime_root() -> Path:
    return _runtime_root_from_env("AMOF_OPENSANDBOX_RUNTIME_ROOT", DEFAULT_OPENSANDBOX_RUNTIME_ROOT)


def hermes_executable() -> Path:
    return hermes_runtime_root() / "venv" / "bin" / "hermes"


def opensandbox_executable() -> Path:
    return opensandbox_runtime_root() / "venv" / "bin" / "opensandbox"


def runner_backend_type(record: dict[str, Any]) -> str:
    explicit = str(record.get("backend") or record.get("backend_type") or "").strip()
    if explicit:
        return explicit
    if str(record.get("driver") or "").strip().lower() == "hermes":
        return BACKEND_TYPE
    return "planning_only"


def is_hermes_runner(record: dict[str, Any]) -> bool:
    return runner_backend_type(record) == BACKEND_TYPE


def runtime_health() -> dict[str, Any]:
    hermes = hermes_executable()
    opensandbox = opensandbox_executable()
    receipt_path = hermes_runtime_root() / "receipts" / "install-receipt.json"
    receipt: dict[str, Any] = {}
    if receipt_path.is_file():
        try:
            parsed = json.loads(receipt_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                receipt = parsed
        except json.JSONDecodeError:
            receipt = {}
    return {
        "backend_type": BACKEND_TYPE,
        "dispatch_available": hermes.is_file() and os.access(hermes, os.X_OK) and opensandbox.is_file(),
        "runtime_health": "ready" if hermes.is_file() and opensandbox.is_file() else "unavailable",
        "execution_endpoint": str(hermes),
        "process_identity": {
            "hermes_executable": str(hermes),
            "hermes_runtime_root": str(hermes_runtime_root()),
            "opensandbox_executable": str(opensandbox),
            "opensandbox_runtime_root": str(opensandbox_runtime_root()),
            "runner_source_sha": str((receipt.get("upstream") or {}).get("commit") or ""),
            "runner_version": str((receipt.get("upstream") or {}).get("package_version") or ""),
        },
        "supported_capabilities": list(SUPPORTED_CAPABILITIES),
        "writable_root_required": True,
        "cancellation_support": "timeout_process_termination",
        "log_event_support": "stdout_stderr_event_jsonl",
    }


def doctor_record(record: dict[str, Any]) -> dict[str, Any]:
    health = runtime_health()
    capabilities = [str(item) for item in record.get("capabilities", []) if str(item).strip()]
    mutation_modes = [str(item) for item in record.get("allowed_mutation_modes", []) if str(item).strip()]
    return {
        "runner_id": str(record.get("runner_id") or ""),
        "backend_type": runner_backend_type(record),
        "dispatch_available": bool(health["dispatch_available"]),
        "runtime_health": health["runtime_health"],
        "execution_endpoint": health["execution_endpoint"],
        "process_identity": health["process_identity"],
        "supported_capabilities": list(SUPPORTED_CAPABILITIES),
        "registered_capabilities": capabilities,
        "registered_mutation_modes": mutation_modes,
        "writable_root_required": True,
        "cancellation_support": health["cancellation_support"],
        "log_event_support": health["log_event_support"],
    }


def _run_dir(run_id: str) -> Path:
    path = runs_dir() / "hermes-opensandbox" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append_event(path: Path, event: str, **payload: Any) -> None:
    record = {"timestamp": _now_iso(), "event": event, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _resolve_roots(values: list[str]) -> list[Path]:
    roots: list[Path] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser().resolve(strict=False)
        if not path.is_dir():
            raise HermesBackendError(f"approved writable root is not a directory: {text}")
        roots.append(path)
    return roots


def _assert_no_dangerous_caps(capabilities: list[str]) -> None:
    dangerous = sorted({cap for cap in capabilities if cap in DANGEROUS_CAPABILITIES})
    if dangerous:
        raise HermesBackendError(f"dangerous capabilities are not available for Hermes backend: {', '.join(dangerous)}")


def build_selection(
    *,
    runner_id: str,
    requested_capabilities: list[str],
    approve_writable_roots: list[str],
    timeout_seconds: int,
    readable_root: str | None,
) -> HermesBackendSelection:
    normalized_caps = [str(item).strip() for item in requested_capabilities if str(item).strip()]
    _assert_no_dangerous_caps(normalized_caps)
    writable_roots = [str(path) for path in _resolve_roots(approve_writable_roots)]
    effective_caps = ["read"]
    if writable_roots:
        if "bounded_write" not in normalized_caps:
            raise HermesBackendError("bounded_write capability approval is required when writable roots are approved")
        effective_caps.extend(["bounded_write", "shell_limited", "focused_tests"])
    elif any(cap in {"bounded_write", "shell_limited", "focused_tests"} for cap in normalized_caps):
        raise HermesBackendError("bounded write/test capabilities require at least one explicit writable root")
    return HermesBackendSelection(
        runner_id=runner_id,
        capabilities=effective_caps,
        writable_roots=writable_roots,
        timeout_seconds=timeout_seconds,
        readable_root=readable_root,
    )


def _workspace_for(selection: HermesBackendSelection, manifest: dict[str, Any]) -> Path:
    if selection.writable_roots:
        return Path(selection.writable_roots[0]).resolve(strict=True)
    if selection.readable_root:
        path = Path(selection.readable_root).expanduser().resolve(strict=False)
        if path.is_dir():
            return path
    repos = manifest.get("repos")
    if isinstance(repos, list):
        for item in repos:
            if isinstance(item, dict):
                path = Path(str(item.get("path") or "")).expanduser().resolve(strict=False)
                if path.is_dir():
                    return path
    return Path.cwd().resolve(strict=False)


def _build_prompt(goal: str, selection: HermesBackendSelection, workspace: Path) -> str:
    lines = [
        "You are executing as Hermes under AMOF authority.",
        f"AMOF runner_id: {selection.runner_id}",
        f"AMOF backend: {BACKEND_TYPE}",
        f"Workspace root: {workspace}",
        f"Approved capabilities: {', '.join(selection.capabilities)}",
        "Denied: Kubernetes mutation, deployment, secrets, unrestricted network, push, promotion, tags, releases.",
    ]
    if selection.writable_roots:
        roots = ", ".join(selection.writable_roots)
        lines.append(f"Writable roots: {roots}")
        lines.append("Modify files only inside the listed writable roots. Do not commit, push, promote, deploy, tag, or release.")
    else:
        lines.append("Read-only run: do not modify files.")
    lines.extend(["", "Mission:", goal])
    return "\n".join(lines)


def _changed_paths(workspace: Path) -> list[str]:
    if not (workspace / ".git").exists():
        return []
    completed = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(workspace),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        return []
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        item = line[3:].strip()
        if item:
            paths.append(item)
    return paths


def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    state_home = hermes_runtime_root() / "state" / "home"
    if state_home.is_dir():
        env["HOME"] = str(state_home)
    env.setdefault("HERMES_HOME", str(state_home / ".hermes"))
    env["HERMES_QUIET"] = "1"
    env["HERMES_ACCEPT_HOOKS"] = "1"
    return env


def _probe_opensandbox() -> dict[str, Any]:
    executable = opensandbox_executable()
    if not executable.is_file():
        return {"status": "unavailable", "exit_code": 127, "stdout": "", "stderr": "opensandbox executable not found"}
    completed = subprocess.run(
        [str(executable), "--version"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return {
        "status": "ready" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "").strip(),
        "stderr": (completed.stderr or "").strip(),
    }


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
    run_id = f"hermes-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{_safe_id(request_id)}"
    run_dir = _run_dir(run_id)
    event_log_path = run_dir / "events.jsonl"
    runtime_log_path = run_dir / "runtime.log"
    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    result_path = run_dir / "result.json"
    started_at = _now_iso()
    workspace = _workspace_for(selection, manifest)
    _append_event(event_log_path, "run_created", runner_id=selection.runner_id, backend=BACKEND_TYPE, studio_session_id=studio_session_id)
    opensandbox_probe = _probe_opensandbox()
    _append_event(event_log_path, "opensandbox_probe", **opensandbox_probe)

    if studio_session_id is not None:
        require_active_studio_session(studio_session_id)
        attach_run_reference(
            studio_session_id=studio_session_id,
            run_id=run_id,
            session_id=run_id,
            surface="agent",
            mode="execute",
            status="running",
            events_path=str(event_log_path),
            session_path=str(run_dir),
            output_path=str(result_path),
        )

    if not bool(health["dispatch_available"]):
        final_text = "Hermes/OpenSandbox dispatch is unavailable; selected runner failed closed."
        result = _result_payload(
            run_id=run_id,
            status="blocked",
            exit_code=1,
            stop_reason="hermes_dispatch_unavailable",
            final_text=final_text,
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            changed_paths=[],
            selection=selection,
            health=health,
            opensandbox_probe=opensandbox_probe,
        )
        result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        _append_event(event_log_path, "run_blocked", reason="dispatch_unavailable")
        return result

    prompt = _build_prompt(goal, selection, workspace)
    command = [str(hermes_executable()), "chat", "--cli", "--quiet"]
    if provider:
        command.extend(["--provider", provider])
    if model:
        command.extend(["--model", model])
    command.extend(["--query", prompt])
    (run_dir / "request.json").write_text(
        json.dumps(
            {
                "request_id": request_id,
                "runner_id": selection.runner_id,
                "backend": BACKEND_TYPE,
                "studio_session_id": studio_session_id,
                "capabilities": selection.capabilities,
                "writable_roots": selection.writable_roots,
                "workspace": str(workspace),
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
            env=_base_env(),
            text=True,
            capture_output=True,
            check=False,
            timeout=selection.timeout_seconds,
        )
        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")
        runtime_log_path.write_text((completed.stdout or "") + ("\n--- STDERR ---\n" + completed.stderr if completed.stderr else ""), encoding="utf-8")
        exit_code = int(completed.returncode)
        if exit_code != 0:
            status = "failed"
            stop_reason = "hermes_process_failed"
    except subprocess.TimeoutExpired as exc:
        status = "failed"
        stop_reason = "timeout"
        exit_code = 124
        stdout_path.write_text(exc.stdout or "", encoding="utf-8")
        stderr_path.write_text(exc.stderr or "", encoding="utf-8")
        runtime_log_path.write_text("Hermes process timed out.\n", encoding="utf-8")

    final_text = stdout_path.read_text(encoding="utf-8").strip()
    if not final_text:
        final_text = stderr_path.read_text(encoding="utf-8").strip() or stop_reason
    validation_status = _infer_validation_status(final_text)
    if status == "completed" and validation_status == "failed":
        status = "failed"
        stop_reason = "validation_failed"
        exit_code = 1
    changed = _changed_paths(workspace)
    if status == "completed" and not selection.writable_roots and changed:
        status = "failed"
        stop_reason = "read_only_mutation_detected"
        exit_code = 1

    result = _result_payload(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        stop_reason=stop_reason,
        final_text=final_text,
        studio_session_id=studio_session_id,
        event_log_path=event_log_path,
        runtime_log_path=runtime_log_path,
        changed_paths=changed,
        selection=selection,
        health=health,
        opensandbox_probe=opensandbox_probe,
        validation_status=validation_status,
    )
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    _append_event(event_log_path, "run_finished", status=status, exit_code=exit_code, stop_reason=stop_reason)
    if studio_session_id is not None:
        attach_run_reference(
            studio_session_id=studio_session_id,
            run_id=run_id,
            session_id=run_id,
            surface="agent",
            mode="execute",
            status=status,
            events_path=str(event_log_path),
            session_path=str(run_dir),
            output_path=str(result_path),
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
    opensandbox_probe: dict[str, Any],
    validation_status: str = "not_run",
) -> dict[str, Any]:
    return {
        "result_kind": "agent_run_result",
        "contract_version": "agent-run-v1",
        "schema_version": 1,
        "status": status,
        "session_id": run_id,
        "exit_code": exit_code,
        "stop_reason": stop_reason,
        "final_text": final_text,
        "runner_id": selection.runner_id,
        "backend": BACKEND_TYPE,
        "studio_session_id": studio_session_id,
        "plan_path": None,
        "checkpoint_path": None,
        "event_log_path": str(event_log_path),
        "runtime_log_path": str(runtime_log_path),
        "journal_path": None,
        "changed_paths": changed_paths,
        "validation_summary": {
            "status": validation_status,
            "reason": "Hermes backend returns process status; focused validation must be requested in mission text.",
        },
        "approved_capabilities": list(selection.capabilities),
        "effective_capabilities": list(selection.capabilities),
        "evidence_refs": {
            "event_log_path": str(event_log_path),
            "runtime_log_path": str(runtime_log_path),
            "process_identity": health.get("process_identity"),
            "opensandbox_probe": dict(opensandbox_probe),
        },
        "budget_summary": {"limit": None, "spent": 0.0, "remaining": None},
    }


def _infer_validation_status(final_text: str) -> str:
    lowered = final_text.lower()
    failure_markers = (
        "failed (failures=",
        "failed (errors=",
        "traceback (most recent call last)",
        "assertionerror",
        "\nfail:",
        "\nerror:",
        "the test ran, but it did not",
        "resulting in a failure",
    )
    if any(marker in lowered for marker in failure_markers):
        return "failed"
    success_markers = ("ran 1 test", "\nok", "validation_ok")
    if any(marker in lowered for marker in success_markers):
        return "passed"
    return "not_run"
