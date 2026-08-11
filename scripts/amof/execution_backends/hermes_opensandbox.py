"""Hermes compatibility backend contract for governed AMOF handoffs."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..app_paths import get_app_paths, runs_dir
from ..commands.studio import attach_run_reference, require_active_studio_session
from ..write_scope_proposals import (
    _normalize_repository_relative_scope_path,
    classify_repository_relative_scope_path,
    persist_write_scope_proposals_from_result,
)
from .validation_closure import build_validation_summary, derive_validation_closure

BACKEND_TYPE = "hermes_opensandbox"
BACKEND_CONTRACT_VERSION = "hermes-cli-remote-ial-v1"
RUNTIME_CONTRACT = "Hermes CLI + Remote IAL"
ISOLATION_MODEL = "runtime_owner_workspace"
FUTURE_ISOLATION_MODELS = ("session_execution_environment", "run_execution_environment")
REMOTE_IAL_PROVIDER = "remote-ial"
SUPPORTED_CAPABILITIES = ("read", "bounded_write", "shell_limited", "focused_tests")
DIRECT_PROVIDER_ENV_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
)
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
WRITE_SCOPE_PROPOSAL_START = "AMOF_WRITE_SCOPE_PROPOSAL_JSON_START"
WRITE_SCOPE_PROPOSAL_END = "AMOF_WRITE_SCOPE_PROPOSAL_JSON_END"
WRITE_SCOPE_PROPOSAL_REQUIRED = "WRITE_SCOPE_PROPOSAL_REQUIRED"
WRITE_SCOPE_PROPOSAL_FIELDS = (
    "target_id",
    "base_sha",
    "allowed_roots",
    "denied_roots",
    "reason",
    "expected_checks",
    "docs_only",
    "source_mutation",
)
SECRET_LIKE_TEXT_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._-]+|"
    r"(?:token|secret|password|authorization|api[_-]?key)\s*[:=]\s*['\"]?[^\s,'\"]+|"
    r"(?:ghp|github_pat|sk|xoxb|xoxp|xoxs|xoxa)-[A-Za-z0-9._-]+)"
)


class HermesBackendError(RuntimeError):
    """Raised when the Hermes compatibility backend cannot be dispatched truthfully."""


@dataclass(frozen=True)
class RemoteIALConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float


@dataclass(frozen=True)
class HermesBackendSelection:
    runner_id: str
    capabilities: list[str]
    writable_roots: list[str]
    timeout_seconds: int
    readable_root: str | None
    write_scope_binding_id: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _duration_ms_from_timestamps(started_at: Any, completed_at: Any) -> int | None:
    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        return None
    try:
        duration_ms = int(
            (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)).total_seconds()
            * 1000
        )
    except ValueError:
        return None
    return duration_ms if duration_ms >= 0 else None


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return normalized[:96] or "hermes-run"


def _runtime_root_from_env(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, "") or default).expanduser().resolve(strict=False)


def hermes_runtime_root() -> Path:
    default = get_app_paths().data_root / "runners" / "hermes-agent" / "v2026.6.5"
    return _runtime_root_from_env("AMOF_HERMES_RUNTIME_ROOT", default)


def hermes_executable() -> Path:
    return hermes_runtime_root() / "venv" / "bin" / "hermes"


def _normalize_remote_ial_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HermesBackendError("Remote IAL base URL is not configured as a valid http(s) URL")
    return normalized


def _remote_ial_timeout_seconds() -> float:
    raw = str(os.environ.get("AMOF_REMOTE_IAL_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 90.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise HermesBackendError("Remote IAL timeout must be a positive number") from exc
    if value <= 0:
        raise HermesBackendError("Remote IAL timeout must be a positive number")
    return value


def _remote_ial_config(model_override: str | None = None) -> RemoteIALConfig:
    base_url = _normalize_remote_ial_base_url(str(os.environ.get("AMOF_REMOTE_IAL_BASE_URL") or ""))
    api_key = str(os.environ.get("AMOF_REMOTE_IAL_API_KEY") or "").strip()
    if not api_key:
        raise HermesBackendError("Remote IAL API key is not configured")
    model = str(model_override or os.environ.get("AMOF_REMOTE_IAL_MODEL") or "").strip()
    if not model:
        raise HermesBackendError("Remote IAL model is not configured")
    return RemoteIALConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=_remote_ial_timeout_seconds(),
    )


def _remote_ial_headers(config: RemoteIALConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }


def _remote_ial_health(config: RemoteIALConfig) -> dict[str, Any]:
    request = Request(
        f"{config.base_url}/v1/ial/healthz",
        headers=_remote_ial_headers(config),
        method="GET",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        return {"inference_health": "blocked", "status_code": exc.code}
    except (OSError, URLError, ValueError) as exc:
        return {"inference_health": "blocked", "error_class": type(exc).__name__}
    return {
        "inference_health": "ready" if body.get("status") == "ok" else "blocked",
        "selected_provider": body.get("selected_provider"),
        "selected_model": body.get("selected_model"),
        "provider_configured": bool(body.get("provider_configured")),
    }


def runner_backend_type(record: dict[str, Any]) -> str:
    explicit = str(record.get("backend") or record.get("backend_type") or "").strip()
    if explicit:
        return explicit
    if str(record.get("driver") or "").strip().lower() == "hermes":
        return BACKEND_TYPE
    return "planning_only"


def is_hermes_runner(record: dict[str, Any]) -> bool:
    return runner_backend_type(record) == BACKEND_TYPE


def _requested_model(model_override: str | None = None) -> str:
    return str(model_override or os.environ.get("AMOF_REMOTE_IAL_MODEL") or "").strip() or "unconfigured"


def hermes_dispatch_command(*, model: str, prompt: str) -> list[str]:
    return [
        str(hermes_executable()),
        "chat",
        "--cli",
        "--quiet",
        "--model",
        model,
        "--query",
        prompt,
    ]


def _probe_hermes_cli_contract(model: str) -> dict[str, Any]:
    executable = hermes_executable()
    dispatch_preview = hermes_dispatch_command(model=model, prompt="<amof-contract-probe>")
    if not executable.is_file():
        return {
            "status": "unavailable",
            "exit_code": 127,
            "stdout": "",
            "stderr": "hermes executable not found",
            "probe_command": [str(executable), "chat", "--help"],
            "dispatch_command_preview": dispatch_preview,
        }
    if not os.access(executable, os.X_OK):
        return {
            "status": "unavailable",
            "exit_code": 126,
            "stdout": "",
            "stderr": "hermes executable is not executable",
            "probe_command": [str(executable), "chat", "--help"],
            "dispatch_command_preview": dispatch_preview,
        }
    completed = subprocess.run(
        [str(executable), "chat", "--help"],
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
        "probe_command": [str(executable), "chat", "--help"],
        "dispatch_command_preview": dispatch_preview,
    }


def runtime_health() -> dict[str, Any]:
    hermes = hermes_executable()
    dispatch_probe = _probe_hermes_cli_contract(_requested_model())
    receipt_path = hermes_runtime_root() / "receipts" / "install-receipt.json"
    receipt: dict[str, Any] = {}
    if receipt_path.is_file():
        try:
            parsed = json.loads(receipt_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                receipt = parsed
        except json.JSONDecodeError:
            receipt = {}
    health = {
        "backend_type": BACKEND_TYPE,
        "backend_contract_version": BACKEND_CONTRACT_VERSION,
        "runtime_contract": RUNTIME_CONTRACT,
        "isolation_model": ISOLATION_MODEL,
        "future_isolation_models": list(FUTURE_ISOLATION_MODELS),
        "dispatch_available": False,
        "runtime_health": "ready" if dispatch_probe["status"] == "ready" else "unavailable",
        "hermes_runtime": "ready" if dispatch_probe["status"] == "ready" else "unavailable",
        "inference_transport": "remote_ial",
        "inference_health": "blocked",
        "requested_provider": REMOTE_IAL_PROVIDER,
        "effective_provider": "unverified",
        "requested_model": _requested_model(),
        "effective_model": "unverified",
        "direct_provider_fallback": "disabled",
        "execution_endpoint": str(hermes),
        "process_identity": {
            "backend_id": BACKEND_TYPE,
            "backend_contract_version": BACKEND_CONTRACT_VERSION,
            "runtime_contract": RUNTIME_CONTRACT,
            "isolation_model": ISOLATION_MODEL,
            "future_isolation_models": list(FUTURE_ISOLATION_MODELS),
            "hermes_executable": str(hermes),
            "hermes_runtime_root": str(hermes_runtime_root()),
            "dispatch_probe": dict(dispatch_probe),
            "runner_source_sha": str((receipt.get("upstream") or {}).get("commit") or ""),
            "runner_version": str((receipt.get("upstream") or {}).get("package_version") or ""),
        },
        "supported_capabilities": list(SUPPORTED_CAPABILITIES),
        "writable_root_required": True,
        "cancellation_support": "timeout_process_termination",
        "log_event_support": "stdout_stderr_event_jsonl",
    }
    try:
        config = _remote_ial_config()
        remote_health = _remote_ial_health(config)
        health.update(
            {
                "dispatch_available": dispatch_probe["status"] == "ready"
                and remote_health.get("inference_health") == "ready",
                "inference_health": remote_health.get("inference_health", "blocked"),
                "requested_model": config.model,
                "effective_model": str(remote_health.get("selected_model") or "unverified"),
                "effective_provider": REMOTE_IAL_PROVIDER
                if remote_health.get("inference_health") == "ready"
                else "unverified",
                "upstream_provider": remote_health.get("selected_provider"),
                "upstream_model": remote_health.get("selected_model"),
            }
        )
    except HermesBackendError:
        pass
    return health


def doctor_record(record: dict[str, Any]) -> dict[str, Any]:
    health = runtime_health()
    capabilities = [str(item) for item in record.get("capabilities", []) if str(item).strip()]
    mutation_modes = [str(item) for item in record.get("allowed_mutation_modes", []) if str(item).strip()]
    return {
        "runner_id": str(record.get("runner_id") or ""),
        "backend_type": runner_backend_type(record),
        "backend_contract_version": health.get("backend_contract_version"),
        "runtime_contract": health.get("runtime_contract"),
        "isolation_model": health.get("isolation_model"),
        "dispatch_available": bool(health["dispatch_available"]),
        "runtime_health": health["runtime_health"],
        "dispatch": "available" if health["dispatch_available"] else "blocked",
        "hermes_runtime": health.get("hermes_runtime", health["runtime_health"]),
        "inference_transport": health.get("inference_transport", "remote_ial"),
        "inference_health": health.get("inference_health", "blocked"),
        "requested_provider": health.get("requested_provider", REMOTE_IAL_PROVIDER),
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


def _run_dir(run_id: str) -> Path:
    path = runs_dir() / "hermes-opensandbox" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append_event(path: Path, event: str, **payload: Any) -> None:
    record = {"timestamp": _now_iso(), "event": event, "event_type": event, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _write_runtime_log(path: Path, message: str) -> None:
    path.write_text(message if message.endswith("\n") else f"{message}\n", encoding="utf-8")


def _redact_secret_like_text(text: str) -> str:
    return SECRET_LIKE_TEXT_RE.sub("[redacted]", text)


def _preview_kind(path: str, text: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".json"):
        return "json"
    if lowered.endswith(".jsonl"):
        return "jsonl"
    if lowered.endswith(".log"):
        return "log"
    if re.search(r"^\s*#{1,6}\s+", text, re.MULTILINE) or re.search(
        r"^\s*[-*+]\s+", text, re.MULTILINE
    ):
        return "markdown"
    return "text"


def _truncate_preview(text: str, limit: int = 16000) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[truncated {len(text) - limit} chars]"


def _preview_payload(*, path: str, raw: str, load_error: str | None = None) -> dict[str, Any]:
    sanitized = _truncate_preview(_redact_secret_like_text(raw))
    kind = _preview_kind(path, sanitized)
    preview_text = sanitized
    if kind == "json":
        try:
            preview_text = json.dumps(json.loads(sanitized), indent=2)
        except json.JSONDecodeError:
            preview_text = sanitized
    elif kind == "jsonl":
        lines: list[str] = []
        for line in sanitized.splitlines():
            if not line.strip():
                continue
            try:
                lines.append(json.dumps(json.loads(line), indent=2))
            except json.JSONDecodeError:
                lines.append(line)
            if len(lines) >= 80:
                break
        preview_text = "\n\n".join(lines)
    return {
        "path": path,
        "title": Path(path).stem,
        "kind": kind,
        "preview_text": preview_text,
        "raw_text": sanitized,
        "load_error": load_error,
    }


def _build_evidence_previews(
    *,
    result: dict[str, Any],
    result_path: Path,
    event_log_path: Path,
    runtime_log_path: Path,
) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    result_snapshot = dict(result)
    result_snapshot.pop("evidence_previews", None)
    previews.append(
        _preview_payload(
            path=str(result_path),
            raw=json.dumps(result_snapshot, indent=2) + "\n",
        )
    )
    for artifact_path in (event_log_path, runtime_log_path):
        if not artifact_path.exists():
            continue
        previews.append(
            _preview_payload(
                path=str(artifact_path),
                raw=artifact_path.read_text(encoding="utf-8"),
            )
        )
    return previews


def _proposal_missing_reason(task_findings: str, runtime_detail: str) -> str:
    detail = task_findings or runtime_detail
    for line in detail.splitlines():
        text = line.strip()
        if text:
            return text[:500]
    return "structured write_scope_proposal was requested but the runner did not emit one"


def _write_terminal_result(
    *,
    result_path: Path,
    event_log_path: Path,
    runtime_log_path: Path,
    result: dict[str, Any],
    reason: str,
    started_at: str | None = None,
) -> dict[str, Any]:
    if started_at is not None:
        result.setdefault("started_at", started_at)
    result.setdefault("completed_at", _now_iso())
    duration_ms = _duration_ms_from_timestamps(
        result.get("started_at"),
        result.get("completed_at"),
    )
    if duration_ms is not None:
        result.setdefault("duration_ms", duration_ms)
    result.setdefault("result_path", str(result_path))
    result.setdefault("runtime_log_unavailable_reason", None)
    result.setdefault("failure_classification", reason if result.get("status") != "completed" else None)
    if not runtime_log_path.exists():
        _write_runtime_log(
            runtime_log_path,
            result.get("final_text") or result.get("stop_reason") or "terminal result written",
        )
    result["evidence_previews"] = _build_evidence_previews(
        result=result,
        result_path=result_path,
        event_log_path=event_log_path,
        runtime_log_path=runtime_log_path,
    )
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    # Wave 1: persist validated proposal evidence only. Never creates authority.
    # Backends may emit write_scope_proposals[]; singular write_scope_proposal
    # remains for compatibility. Prose-only / hostile approval claims are rejected.
    # Intentionally does not mutate AgentRunResult fields (additionalProperties=false).
    persist_write_scope_proposals_from_result(result)
    _append_event(
        event_log_path,
        "run_finished",
        run_id=str(result.get("session_id") or ""),
        session_id=str(result.get("session_id") or ""),
        studio_session_id=result.get("studio_session_id"),
        status=str(result.get("status") or "failed"),
        exit_code=result.get("exit_code"),
        stop_reason=str(result.get("stop_reason") or reason),
        failure_classification=reason,
        result_path=str(result_path),
        runtime_log_path=str(runtime_log_path),
    )
    return result


def _attach_studio_run(
    *,
    studio_session_id: str | None,
    run_id: str,
    event_log_path: Path,
    run_dir: Path,
    result_path: Path,
    status: str,
) -> None:
    if studio_session_id is None:
        return
    require_active_studio_session(studio_session_id)
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


def _resolve_roots(values: list[str], *, readable_root: str | None) -> list[Path]:
    """Resolve approved writable roots against the readable workspace.

    Repository-relative grants (the Autopilot / Job contract) must be joined to
    ``readable_root`` before absolutizing. Execution Jobs often start with
    CWD=/, so ``Path(rel).resolve()`` would otherwise escape the workspace and
    false-fail Cursor/Claude/Hermes bounded-write dispatch.
    """
    roots: list[Path] = []
    workspace = (
        Path(readable_root).expanduser().resolve(strict=True)
        if readable_root
        else None
    )
    if workspace is not None and not workspace.is_dir():
        raise HermesBackendError(f"readable root is not a directory: {readable_root}")
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            path = candidate.resolve(strict=False)
        else:
            if workspace is None:
                raise HermesBackendError(
                    f"relative writable root requires readable workspace: {text}"
                )
            path = (workspace / candidate).resolve(strict=False)
        if workspace is not None and not path.is_relative_to(workspace):
            raise HermesBackendError(
                f"approved writable root is outside the readable workspace: {text}"
            )
        if path.exists() and not (path.is_dir() or path.is_file()):
            raise HermesBackendError(f"approved writable root is not a file or directory: {text}")
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
    write_scope_binding_id: str | None = None,
) -> HermesBackendSelection:
    normalized_caps = [str(item).strip() for item in requested_capabilities if str(item).strip()]
    _assert_no_dangerous_caps(normalized_caps)
    writable_roots = [
        str(path)
        for path in _resolve_roots(
            approve_writable_roots,
            readable_root=readable_root,
        )
    ]
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
        write_scope_binding_id=(
            str(write_scope_binding_id).strip() or None
            if write_scope_binding_id is not None
            else None
        ),
    )


def _workspace_for(selection: HermesBackendSelection, manifest: dict[str, Any]) -> Path:
    if selection.readable_root:
        path = Path(selection.readable_root).expanduser().resolve(strict=False)
        if path.is_dir():
            return path
    if selection.writable_roots:
        first_scope = Path(selection.writable_roots[0]).resolve(strict=False)
        if first_scope.is_dir():
            return first_scope
        for parent in first_scope.parents:
            if parent.is_dir():
                return parent
    repos = manifest.get("repos")
    if isinstance(repos, list):
        for item in repos:
            if isinstance(item, dict):
                path = Path(str(item.get("path") or "")).expanduser().resolve(strict=False)
                if path.is_dir():
                    return path
    return Path.cwd().resolve(strict=False)


def _extract_remote_ial_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    remote_messages: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role == "system":
            content = item.get("content")
            if content:
                system_parts.append(str(content))
            continue
        if role == "assistant":
            message: dict[str, Any] = {"role": "assistant", "content": item.get("content")}
            tool_calls = []
            for tool_call in item.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                name = str(function.get("name") or "").strip()
                raw_args = function.get("arguments")
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else {}
                except json.JSONDecodeError:
                    arguments = {}
                if name:
                    tool_calls.append(
                        {
                            "id": str(tool_call.get("id") or ""),
                            "name": name,
                            "arguments": arguments if isinstance(arguments, dict) else {},
                        }
                    )
            if tool_calls:
                message["tool_calls"] = tool_calls
            remote_messages.append(message)
            continue
        if role == "tool":
            remote_messages.append(
                {
                    "role": "tool",
                    "results": [
                        {
                            "id": str(item.get("tool_call_id") or ""),
                            "tool_call_id": str(item.get("tool_call_id") or ""),
                            "content": item.get("content"),
                        }
                    ],
                }
            )
            continue
        remote_messages.append({"role": role or "user", "content": item.get("content")})
    return "\n\n".join(system_parts), remote_messages


def _remote_ial_tool_to_openai(item: dict[str, Any], index: int) -> dict[str, Any]:
    arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    return {
        "id": str(item.get("id") or f"remote-tool-{index}"),
        "type": "function",
        "function": {
            "name": str(item.get("name") or ""),
            "arguments": json.dumps(arguments, sort_keys=True),
        },
    }


def _finish_reason(stop_reason: Any, tool_calls: list[dict[str, Any]]) -> str:
    if tool_calls:
        return "tool_calls"
    normalized = str(stop_reason or "").strip().lower()
    if normalized in {"max_tokens", "length"}:
        return "length"
    return "stop"


class _RemoteIALOpenAIAdapter:
    def __init__(self, config: RemoteIALConfig) -> None:
        self.config = config
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated_cost_usd: float | None = None
        self.chat_calls = 0

    def __enter__(self) -> "_RemoteIALOpenAIAdapter":
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/v1/models":
                    self._json(200, {"object": "list", "data": [{"id": adapter.config.model, "object": "model"}]})
                    return
                self._json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self._json(404, {"error": {"message": "not found"}})
                    return
                length = int(self.headers.get("Content-Length") or "0")
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._json(400, {"error": {"message": "invalid json"}})
                    return
                system, messages = _extract_remote_ial_messages(list(body.get("messages") or []))
                payload = {
                    "system": system,
                    "messages": messages,
                    "tools": body.get("tools") or [],
                    "model": adapter.config.model,
                    "max_tokens": int(body.get("max_tokens") or 8192),
                    "temperature": float(body.get("temperature") or 0.0),
                }
                request = Request(
                    f"{adapter.config.base_url}/v1/ial/chat",
                    headers=_remote_ial_headers(adapter.config),
                    data=json.dumps(payload).encode("utf-8"),
                    method="POST",
                )
                try:
                    with urlopen(request, timeout=adapter.config.timeout_seconds) as response:
                        remote = json.loads(response.read().decode("utf-8") or "{}")
                except HTTPError as exc:
                    self._json(exc.code, {"error": {"message": "remote IAL request failed"}})
                    return
                except (OSError, URLError, ValueError):
                    self._json(502, {"error": {"message": "remote IAL request failed"}})
                    return
                remote_tokens = remote.get("tokens")
                if isinstance(remote_tokens, dict):
                    prompt_tokens = _finite_number(remote_tokens.get("input"))
                    completion_tokens = _finite_number(remote_tokens.get("output"))
                    if prompt_tokens is not None and prompt_tokens >= 0:
                        adapter.prompt_tokens += int(prompt_tokens)
                    if completion_tokens is not None and completion_tokens >= 0:
                        adapter.completion_tokens += int(completion_tokens)
                estimated_cost = _finite_number(remote.get("estimated_cost"))
                if estimated_cost is not None and estimated_cost > 0:
                    adapter.estimated_cost_usd = (adapter.estimated_cost_usd or 0.0) + estimated_cost
                adapter.chat_calls += 1
                tool_calls = [
                    _remote_ial_tool_to_openai(item, index)
                    for index, item in enumerate(remote.get("tool_calls") or [], start=1)
                    if isinstance(item, dict)
                ]
                message: dict[str, Any] = {"role": "assistant", "content": remote.get("text") or ""}
                if tool_calls:
                    message["tool_calls"] = tool_calls
                choice = {
                    "index": 0,
                    "message": message,
                    "finish_reason": _finish_reason(remote.get("stop_reason"), tool_calls),
                }
                response_payload = {
                    "id": str(remote.get("request_id") or f"chatcmpl-{int(time.time())}"),
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": str(remote.get("model") or remote.get("upstream_model") or adapter.config.model),
                    "choices": [choice],
                    "usage": {
                        "prompt_tokens": int((remote.get("tokens") or {}).get("input") or 0),
                        "completion_tokens": int((remote.get("tokens") or {}).get("output") or 0),
                        "total_tokens": int((remote.get("tokens") or {}).get("input") or 0)
                        + int((remote.get("tokens") or {}).get("output") or 0),
                    },
                }
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    chunk = {
                        "id": response_payload["id"],
                        "object": "chat.completion.chunk",
                        "created": response_payload["created"],
                        "model": response_payload["model"],
                        "choices": [{"index": 0, "delta": message, "finish_reason": None}],
                    }
                    final = {
                        "id": response_payload["id"],
                        "object": "chat.completion.chunk",
                        "created": response_payload["created"],
                        "model": response_payload["model"],
                        "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}],
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                    self.wfile.write(f"data: {json.dumps(final)}\n\n".encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self._json(200, response_payload)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            host, port = probe.getsockname()
        self.server = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{port}/v1"
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


def _goal_requests_write_scope_proposal(goal: str) -> bool:
    lowered = goal.lower()
    return "write_scope_proposal" in lowered or "write scope proposal" in lowered


# Path classification/normalization authority lives in write_scope_proposals
# (classify_repository_relative_scope_path). Re-exported above for backends.


def _explicit_required_proposal_paths(goal: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"\bexactly\s*:?\s*`?([^\s,;`]+)", goal, re.IGNORECASE):
        candidate = match.group(1).rstrip(".'\"),:")
        normalized = _normalize_repository_relative_scope_path(candidate)
        if normalized and "/" in normalized and normalized not in paths:
            paths.append(normalized)
    return paths


def _manifest_repo_targets(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Every manifest repo as canonical proposal target context, in order."""
    repos = manifest.get("repos")
    if not isinstance(repos, list):
        return []
    targets: list[dict[str, str]] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        base_sha = str(repo.get("sha") or repo.get("branch") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
            base_sha = ""
        targets.append(
            {
                "target_id": str(repo.get("target_id") or "").strip(),
                "base_sha": base_sha,
                "repository_url": str(repo.get("url") or "").strip(),
                "workspace_path": str(repo.get("path") or "").strip(),
                "name": str(repo.get("name") or "").strip(),
            }
        )
    return targets


def _primary_manifest_target(manifest: dict[str, Any]) -> dict[str, str]:
    targets = _manifest_repo_targets(manifest)
    return targets[0] if targets else {}


def _normalize_write_scope_proposal(
    value: Any,
    *,
    expected_allowed_roots: list[str] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    proposal = dict(value)
    required = set(WRITE_SCOPE_PROPOSAL_FIELDS)
    if not required.issubset(proposal):
        return None
    target_id = str(proposal.get("target_id") or "").strip()
    base_sha = str(proposal.get("base_sha") or "").strip().lower()
    reason = str(proposal.get("reason") or "").strip()
    if not target_id or not re.fullmatch(r"[0-9a-f]{40}", base_sha) or not reason:
        return None

    def _string_list(name: str) -> list[str] | None:
        raw = proposal.get(name)
        if not isinstance(raw, list):
            return None
        if any(not isinstance(item, str) for item in raw):
            return None
        values = [item.strip() for item in raw]
        if any(not item for item in values):
            return None
        return values

    raw_allowed_roots = _string_list("allowed_roots")
    raw_denied_roots = _string_list("denied_roots")
    expected_checks = _string_list("expected_checks")
    allowed_roots = (
        [_normalize_repository_relative_scope_path(item) for item in raw_allowed_roots]
        if raw_allowed_roots is not None
        else None
    )
    denied_roots = (
        [_normalize_repository_relative_scope_path(item) for item in raw_denied_roots]
        if raw_denied_roots is not None
        else None
    )
    docs_only = proposal.get("docs_only")
    source_mutation = proposal.get("source_mutation")
    if (
        allowed_roots is None
        or not allowed_roots
        or any(item is None for item in allowed_roots)
        or denied_roots is None
        or any(item is None for item in denied_roots)
        or expected_checks is None
        or not isinstance(docs_only, bool)
        or not isinstance(source_mutation, bool)
    ):
        return None
    normalized_allowed_roots = [str(item) for item in allowed_roots]
    normalized_denied_roots = [str(item) for item in denied_roots]
    if expected_allowed_roots and not set(normalized_allowed_roots).issubset(
        set(expected_allowed_roots)
    ):
        # Multi-target missions partition the explicitly required paths across
        # per-repository proposals, so each block may carry a subset. Any root
        # outside the mission's explicit requirement still fails closed.
        return None
    proposal["target_id"] = target_id
    proposal["base_sha"] = base_sha
    proposal["reason"] = reason
    proposal["allowed_roots"] = normalized_allowed_roots
    proposal["denied_roots"] = normalized_denied_roots
    proposal["expected_checks"] = expected_checks
    proposal["docs_only"] = docs_only
    proposal["source_mutation"] = source_mutation
    return proposal


def _extract_write_scope_proposal_outputs(
    text: str,
    *,
    expected_allowed_roots: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Extract every valid proposal block (multi-target missions emit one
    block per target repository). Duplicate target_ids keep the first block.
    Returns (proposals, prose summary with the blocks removed)."""
    pattern = re.compile(
        rf"{WRITE_SCOPE_PROPOSAL_START}\s*(\{{.*?\}})\s*{WRITE_SCOPE_PROPOSAL_END}",
        re.DOTALL,
    )
    proposals: list[dict[str, Any]] = []
    seen_target_ids: set[str] = set()
    summary_parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        summary_parts.append(text[cursor : match.start()])
        cursor = match.end()
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            parsed = None
        proposal = _normalize_write_scope_proposal(
            parsed,
            expected_allowed_roots=expected_allowed_roots,
        )
        if proposal is None:
            continue
        target_id = str(proposal.get("target_id") or "")
        if target_id in seen_target_ids:
            continue
        seen_target_ids.add(target_id)
        proposals.append(proposal)
    summary_parts.append(text[cursor:])
    summary = "".join(summary_parts).strip()
    return proposals, summary


def _extract_write_scope_proposal_output(
    text: str,
    *,
    expected_allowed_roots: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    proposals, summary = _extract_write_scope_proposal_outputs(
        text,
        expected_allowed_roots=expected_allowed_roots,
    )
    return (proposals[0] if proposals else None), summary
def _build_prompt(
    goal: str,
    selection: HermesBackendSelection,
    workspace: Path,
    manifest: dict[str, Any] | None = None,
    *,
    read_only_replan: bool = False,
    proposal_replan: bool = False,
    agent_label: str = "Hermes",
    backend_name: str = BACKEND_TYPE,
) -> str:
    proposal_requested = (
        _goal_requests_write_scope_proposal(goal) and not selection.writable_roots
    )
    manifest_targets = _manifest_repo_targets(manifest or {})
    lines = [
        f"You are executing as {agent_label} under AMOF authority.",
        f"AMOF runner_id: {selection.runner_id}",
        f"AMOF backend: {backend_name}",
        f"Workspace root: {workspace}",
        f"Approved capabilities: {', '.join(selection.capabilities)}",
        "Denied: Kubernetes mutation, deployment, secrets, unrestricted network, push, promotion, tags, releases.",
    ]
    if len(manifest_targets) > 1:
        lines.extend(
            [
                "",
                f"Target repositories ({len(manifest_targets)}): the workspace root contains one materialized checkout per target. Inspect EVERY target repository relevant to the mission, not only the first.",
            ]
        )
        for index, target in enumerate(manifest_targets, start=1):
            lines.append(
                f"- target {index}: {target.get('name') or target.get('target_id') or 'unknown'} at {target.get('workspace_path') or 'unknown'}"
                f" (target_id: {target.get('target_id') or 'unknown'}, base_sha: {target.get('base_sha') or 'unknown'})"
            )
    lines.extend(
        [
            "",
            "Truth domains:",
            "- Agent-observed task findings: report only facts you inspect in the workspace through approved commands/tools.",
            "- AMOF runtime envelope: handoff ID, run ID, Studio Session ID, runner/backend, provider/model/transport, fallback, capabilities, changed paths, status, stop reason, and evidence paths are supplied by AMOF outside your answer.",
            "Do not search the repository for AMOF runtime-envelope field names such as runner_id, backend, transport, studio_session_id, result_path, runtime_log_path, or event_log_path.",
            "If asked for AMOF runtime-envelope fields, state that AMOF will provide them in the runtime envelope; do not treat absent metadata files as blockers.",
            "Use explicit commands when the mission asks for command-derived repository facts, and include command exit codes in your task findings.",
        ]
    )
    if selection.writable_roots:
        roots = ", ".join(selection.writable_roots)
        lines.append(f"Writable roots: {roots}")
        lines.append("Modify files only inside the listed writable roots. Do not commit, push, promote, deploy, tag, or release.")
    else:
        lines.extend(
            [
                "Read-only run: this repository is already materialized and must be inspected in place.",
                f"Read-only workspace boundary (exact path): {workspace}",
                "Do not run git clone, git init, git worktree, or create nested repositories.",
                "Do not create, modify, or delete files anywhere in this workspace.",
            ]
        )
        if read_only_replan:
            lines.append(
                "Read-only mutation was detected once; this constrained replan must remain read-only within the same workspace boundary."
            )
    if proposal_requested:
        target = _primary_manifest_target(manifest or {})
        multi_target = len(manifest_targets) > 1
        expected_allowed_roots = _explicit_required_proposal_paths(goal)
        docs_only = bool(expected_allowed_roots) and all(
            root == "docs" or root.startswith("docs/")
            for root in expected_allowed_roots
        )
        proposal_example = {
            "target_id": target.get("target_id") or "",
            "base_sha": target.get("base_sha") or "",
            "allowed_roots": expected_allowed_roots
            or ["<repository-relative-path-your-evidence-justifies>"],
            "denied_roots": [],
            "reason": (
                "bounded write proof artifact"
                if expected_allowed_roots == ["docs/amof-bounded-write-proof.md"]
                else "bounded follow-up justified by inspected evidence"
            ),
            "expected_checks": ["git diff --check"],
            "docs_only": docs_only,
            "source_mutation": not docs_only,
        }
        lines.extend(
            [
                "",
                "Required structured write-scope contract:",
                "This mission requires machine-readable structured write_scope_proposal output. A prose-only answer is a contract failure.",
                (
                    "You MUST emit one non-empty JSON object between these markers for EACH target repository your evidence justifies changing (repeat the marker pair per target), before any human-readable summary."
                    if multi_target
                    else "You MUST emit exactly one non-empty JSON object between these markers before any human-readable summary."
                ),
                WRITE_SCOPE_PROPOSAL_START,
                json.dumps(proposal_example, separators=(",", ":")),
                WRITE_SCOPE_PROPOSAL_END,
                "Use exactly those JSON field names. Do not wrap them in another object.",
                "Populate target_id and base_sha from the canonical target context. Empty or partial proposal objects are invalid.",
                "allowed_roots must list the exact repository-relative file or directory paths your inspected evidence justifies changing; an empty allowed_roots array is invalid.",
                "Keep allowed_roots and denied_roots repository-relative.",
                "Wildcard roots and additional unrequested roots are forbidden.",
                "The proposal may describe a future create_or_update operation; do not perform that operation now and do not include approved_write_scope or any approval claim.",
                "After the JSON block, emit a Markdown summary for humans. Do not restate the JSON block in prose.",
            ]
        )
        if multi_target:
            lines.extend(
                [
                    "Each proposal block covers exactly one target repository: use that target's target_id and base_sha from the target list above, and keep allowed_roots relative to that repository's own root (never prefix them with the checkout directory name).",
                    "Do not emit a proposal block for a target that needs no changes; explain why in the summary instead.",
                ]
            )
        if expected_allowed_roots:
            lines.append(
                (
                    "Required allowed_roots across ALL proposal blocks combined (no additional paths; each block lists only the paths that belong to its own repository): "
                    if multi_target
                    else "Required allowed_roots (exact; no additional paths): "
                )
                + json.dumps(expected_allowed_roots)
            )
        if multi_target:
            lines.append("Canonical proposal target context (one entry per target):")
            for entry in manifest_targets:
                lines.append(
                    f"- target_id: {entry.get('target_id') or 'unknown'} | base_sha: {entry.get('base_sha') or 'unknown'}"
                    f" | repository_url: {entry.get('repository_url') or 'unknown'} | workspace_path: {entry.get('workspace_path') or 'unknown'}"
                )
        elif target:
            lines.extend(
                [
                    "Canonical proposal target context:",
                    f"- target_id: {target.get('target_id') or 'unknown'}",
                    f"- base_sha: {target.get('base_sha') or 'unknown'}",
                    f"- repository_url: {target.get('repository_url') or 'unknown'}",
                    f"- workspace_path: {target.get('workspace_path') or workspace}",
                ]
            )
    lines.extend(["", "Mission:", goal])
    if proposal_requested:
        lines.extend(
            [
                "",
                "CURRENT PHASE OVERRIDE — PROPOSAL ONLY:",
                "Any mission instruction to create, update, or output a file is conditional on later operator approval and MUST NOT be executed in this run.",
                "Inspect read-only. Do not create, modify, rename, or delete any file.",
                f"Your final answer MUST begin with {WRITE_SCOPE_PROPOSAL_START}, followed by the required non-empty JSON object and {WRITE_SCOPE_PROPOSAL_END}.",
                "Prose-only output is invalid.",
            ]
        )
        if read_only_replan:
            lines.append(
                "A prior mutation attempt was restored. Do not repeat it; return only the required proposal block and human-readable findings."
            )
        if proposal_replan:
            lines.append(
                "CONTRACT RETRY: your previous answer omitted the required JSON block or its fields were invalid (for example an empty allowed_roots array). "
                "Re-run the inspection conclusion and emit the JSON block again with every required field populated and allowed_roots listing the exact repository-relative paths your evidence justifies."
            )
    elif selection.writable_roots:
        lines.extend(
            [
                "",
                "CURRENT PHASE OVERRIDE — APPROVED BOUNDED WRITE:",
                "AMOF has already validated explicit operator approval for the listed writable roots.",
                "Execute the mission's requested create_or_update operation now. Create missing parent directories when required.",
                "Do not ask for another confirmation and do not emit a write-scope proposal.",
                "The approval remains bounded: do not modify any path outside Writable roots.",
            ]
        )
    return "\n".join(lines)


def _workspace_repo_roots(workspace: Path) -> list[Path]:
    """Git roots governed by a workspace: the workspace itself when it is a
    repository, else its direct child repositories (multi-target dispatch
    workspaces materialize one pinned checkout per target)."""
    if (workspace / ".git").exists():
        return [workspace]
    if not workspace.is_dir():
        return []
    return sorted(
        child for child in workspace.iterdir() if (child / ".git").exists()
    )


def _changed_paths(workspace: Path) -> list[str]:
    paths: list[str] = []
    for repo_root in _workspace_repo_roots(workspace):
        completed = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines():
            item = line[3:].strip()
            if item:
                paths.append(item)
    return list(dict.fromkeys(paths))


def _changed_paths_delta(before: list[str], after: list[str]) -> list[str]:
    before_set = {item for item in before if item}
    after_set = {item for item in after if item}
    return sorted(after_set - before_set)


def _read_only_unclean_workspace_message(preexisting_changed_paths: list[str]) -> str:
    """Describe the unclean workspace without claiming tracked-only when untracked may be present.

    `_changed_paths` uses `git status --short --untracked-files=all`, so the sample
    may include modified tracked files and/or untracked paths.
    """
    paths = [str(item).strip() for item in preexisting_changed_paths if str(item).strip()]
    if not paths:
        return (
            "Read-only run blocked before execution because the workspace is not clean."
        )
    sample = ", ".join(paths[:5])
    more = f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""
    return (
        "Read-only run blocked before execution because the workspace has pre-existing "
        f"changes (modified and/or untracked): {sample}{more}."
    )


def _restore_read_only_paths(workspace: Path, paths: list[str]) -> list[str]:
    restored: list[str] = []
    if not paths:
        return restored
    for repo_root in _workspace_repo_roots(workspace):
        for rel_path in sorted({item for item in paths if item}):
            target = repo_root / rel_path
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", rel_path],
                cwd=str(repo_root),
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            ).returncode == 0
            if tracked:
                dirty = subprocess.run(
                    ["git", "status", "--short", "--untracked-files=all", "--", rel_path],
                    cwd=str(repo_root),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                ).stdout.strip()
                if not dirty:
                    continue
                subprocess.run(
                    ["git", "restore", "--staged", "--worktree", "--", rel_path],
                    cwd=str(repo_root),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                restored.append(rel_path)
                continue
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
                restored.append(rel_path)
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                restored.append(rel_path)
    return sorted(dict.fromkeys(restored))


def _write_run_hermes_config(run_dir: Path, adapter: _RemoteIALOpenAIAdapter, model: str) -> Path:
    hermes_home = run_dir / "hermes-home"
    hermes_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "model:",
                "  provider: custom",
                f"  model: {model}",
                f"  base_url: {adapter.base_url}",
                "  api_key: amof-local-remote-ial-adapter",
                "  api_mode: chat_completions",
                "fallback_providers: []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    env_path = hermes_home / ".env"
    env_path.write_text("", encoding="utf-8")
    os.chmod(env_path, 0o600)
    return hermes_home


def _base_env(adapter: _RemoteIALOpenAIAdapter | None = None, run_dir: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    state_home = hermes_runtime_root() / "state" / "home"
    if state_home.is_dir():
        env["HOME"] = str(state_home)
    if adapter is not None and run_dir is not None:
        env["HERMES_HOME"] = str(_write_run_hermes_config(run_dir, adapter, adapter.config.model))
    else:
        env.setdefault("HERMES_HOME", str(state_home / ".hermes"))
    env["HERMES_QUIET"] = "1"
    env["HERMES_ACCEPT_HOOKS"] = "1"
    for name in DIRECT_PROVIDER_ENV_NAMES:
        env.pop(name, None)
    return env


def run(
    *,
    manifest: dict[str, Any],
    goal: str,
    request_id: str,
    studio_session_id: str | None,
    selection: HermesBackendSelection,
    provider: str | None = None,
    model: str | None = None,
    validation_gates: list[str] | None = None,
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
    preexisting_changed_paths = _changed_paths(workspace)
    _append_event(
        event_log_path,
        "run_created",
        run_id=run_id,
        session_id=run_id,
        runner_id=selection.runner_id,
        backend=BACKEND_TYPE,
        studio_session_id=studio_session_id,
    )
    _attach_studio_run(
        studio_session_id=studio_session_id,
        run_id=run_id,
        event_log_path=event_log_path,
        run_dir=run_dir,
        result_path=result_path,
        status="running",
    )
    dispatch_probe = dict(health.get("process_identity", {}).get("dispatch_probe") or {})
    if not dispatch_probe:
        dispatch_probe = _probe_hermes_cli_contract(_requested_model(model))
    _append_event(event_log_path, "hermes_dispatch_probe", **dispatch_probe)
    try:
        remote_ial = _remote_ial_config(model)
        remote_health = _remote_ial_health(remote_ial)
    except HermesBackendError as exc:
        final_text = "Remote IAL inference transport is unavailable."
        result = _result_payload(
            run_id=run_id,
            status="blocked",
            exit_code=1,
            stop_reason="inference_transport_unavailable",
            final_text=final_text,
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            changed_paths=[],
            selection=selection,
            health=health,
            dispatch_probe=dispatch_probe,
            requested_model=str(model or os.environ.get("AMOF_REMOTE_IAL_MODEL") or "unconfigured"),
            effective_model="unverified",
        )
        result["evidence_refs"]["inference_transport_error"] = type(exc).__name__
        _append_event(event_log_path, "run_blocked", reason="inference_transport_unavailable")
        return _write_terminal_result(
            result_path=result_path,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result=result,
            reason="inference_transport_unavailable",
            started_at=started_at,
        )
    if remote_health.get("inference_health") != "ready":
        final_text = "Remote IAL inference transport is not ready."
        result = _result_payload(
            run_id=run_id,
            status="blocked",
            exit_code=1,
            stop_reason="inference_transport_unavailable",
            final_text=final_text,
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            changed_paths=[],
            selection=selection,
            health=health,
            dispatch_probe=dispatch_probe,
            requested_model=remote_ial.model,
            effective_model="unverified",
        )
        _append_event(event_log_path, "run_blocked", reason="inference_transport_unavailable")
        return _write_terminal_result(
            result_path=result_path,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result=result,
            reason="inference_transport_unavailable",
            started_at=started_at,
        )
    if provider and provider != REMOTE_IAL_PROVIDER:
        final_text = "Direct provider override is not allowed for the AMOF-managed Hermes runner."
        result = _result_payload(
            run_id=run_id,
            status="blocked",
            exit_code=1,
            stop_reason="inference_transport_unavailable",
            final_text=final_text,
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            changed_paths=[],
            selection=selection,
            health=health,
            dispatch_probe=dispatch_probe,
            requested_model=remote_ial.model,
            effective_model="unverified",
        )
        _append_event(event_log_path, "run_blocked", reason="direct_provider_override_rejected")
        return _write_terminal_result(
            result_path=result_path,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result=result,
            reason="direct_provider_override_rejected",
            started_at=started_at,
        )

    if not bool(health["dispatch_available"]):
        final_text = "Hermes CLI + Remote IAL dispatch is unavailable; selected runner failed closed."
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
            dispatch_probe=dispatch_probe,
        )
        _append_event(event_log_path, "run_blocked", reason="dispatch_unavailable")
        return _write_terminal_result(
            result_path=result_path,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result=result,
            reason="dispatch_unavailable",
            started_at=started_at,
        )

    if not selection.writable_roots and preexisting_changed_paths:
        final_text = _read_only_unclean_workspace_message(list(preexisting_changed_paths))
        result = _result_payload(
            run_id=run_id,
            status="blocked",
            exit_code=1,
            stop_reason="read_only_workspace_not_clean",
            final_text=final_text,
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            changed_paths=[],
            selection=selection,
            health=health,
            dispatch_probe=dispatch_probe,
            requested_model=remote_ial.model,
            effective_model="unverified",
        )
        result["evidence_refs"]["preexisting_changed_paths"] = list(preexisting_changed_paths)
        _append_event(
            event_log_path,
            "run_blocked",
            reason="read_only_workspace_not_clean",
            preexisting_changed_paths=list(preexisting_changed_paths),
        )
        return _write_terminal_result(
            result_path=result_path,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result=result,
            reason="read_only_workspace_not_clean",
            started_at=started_at,
        )

    read_only_replan_used = False
    proposal_replan_used = False
    prompt = _build_prompt(goal, selection, workspace, manifest)
    proposal_required = (
        _goal_requests_write_scope_proposal(goal) and not selection.writable_roots
    )
    expected_proposal_paths = _explicit_required_proposal_paths(goal)
    write_scope_proposals: list[dict[str, Any]] = []
    proposal_missing_reason: str | None = None
    task_findings = ""
    runtime_detail = ""
    validation_status = "not_run"
    changed: list[str] = []
    usage: dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "estimated_cost_usd": None,
        "chat_calls": 0,
    }
    while True:
        command = hermes_dispatch_command(model=remote_ial.model, prompt=prompt)
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
                    "requested_provider": REMOTE_IAL_PROVIDER,
                    "requested_model": remote_ial.model,
                    "transport": "remote_ial",
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
        adapter = _RemoteIALOpenAIAdapter(remote_ial)
        try:
            with adapter:
                completed = subprocess.run(
                    command,
                    cwd=str(workspace),
                    env=_base_env(adapter, run_dir),
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
            _write_runtime_log(runtime_log_path, "Hermes process timed out.")
        except Exception as exc:
            status = "failed"
            stop_reason = "hermes_runtime_exception"
            exit_code = 1
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            _write_runtime_log(runtime_log_path, f"{type(exc).__name__}: {exc}")
        finally:
            usage["prompt_tokens"] += int(getattr(adapter, "prompt_tokens", 0) or 0)
            usage["completion_tokens"] += int(getattr(adapter, "completion_tokens", 0) or 0)
            usage["chat_calls"] += int(getattr(adapter, "chat_calls", 0) or 0)
            adapter_cost = getattr(adapter, "estimated_cost_usd", None)
            if adapter_cost is not None:
                usage["estimated_cost_usd"] = (
                    float(usage["estimated_cost_usd"] or 0.0) + float(adapter_cost)
                )

        raw_task_findings = stdout_path.read_text(encoding="utf-8").strip()
        runtime_detail = stderr_path.read_text(encoding="utf-8").strip()
        write_scope_proposals, task_findings = _extract_write_scope_proposal_outputs(
            raw_task_findings,
            expected_allowed_roots=expected_proposal_paths,
        )
        proposal_missing_reason = (
            _proposal_missing_reason(task_findings, runtime_detail)
            if proposal_required and not write_scope_proposals
            else None
        )
        validation_status = _infer_validation_status(task_findings or runtime_detail)
        if status == "completed" and validation_status == "failed":
            status = "failed"
            stop_reason = "validation_failed"
            exit_code = 1
        changed = _changed_paths_delta(preexisting_changed_paths, _changed_paths(workspace))
        if status == "completed" and not selection.writable_roots and changed:
            restored_paths = _restore_read_only_paths(workspace, changed)
            if read_only_replan_used:
                status = "failed"
                stop_reason = "read_only_mutation_detected"
                exit_code = 1
                _append_event(
                    event_log_path,
                    "read_only_mutation_blocked",
                    changed_paths=list(changed),
                    restored_paths=list(restored_paths),
                )
                changed = []
                break
            _append_event(
                event_log_path,
                "read_only_mutation_replan",
                changed_paths=list(changed),
                restored_paths=list(restored_paths),
            )
            read_only_replan_used = True
            prompt = _build_prompt(
                goal,
                selection,
                workspace,
                manifest,
                read_only_replan=True,
            )
            continue
        if status == "completed" and proposal_required and not write_scope_proposals:
            if not proposal_replan_used:
                # One bounded corrective retry: most misses are formatting
                # (missing markers, empty allowed_roots), not judgment.
                _append_event(
                    event_log_path,
                    "proposal_contract_replan",
                    reason=proposal_missing_reason or "structured proposal missing",
                )
                proposal_replan_used = True
                prompt = _build_prompt(
                    goal,
                    selection,
                    workspace,
                    manifest,
                    proposal_replan=True,
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
        validation_gates=validation_gates,
        requested_model=remote_ial.model,
        effective_model=remote_ial.model,
        write_scope_proposals=write_scope_proposals,
        proposal_missing_reason=proposal_missing_reason,
        usage=usage,
    )
    result = _apply_write_scope_enforcement_if_bound(
        result,
        selection=selection,
        run_id=run_id,
        workspace=workspace,
    )
    stop_reason = str(result.get("stop_reason") or stop_reason)
    status = str(result.get("status") or status)
    _write_terminal_result(
        result_path=result_path,
        event_log_path=event_log_path,
        runtime_log_path=runtime_log_path,
        result=result,
        reason=stop_reason,
        started_at=started_at,
    )
    _attach_studio_run(
        studio_session_id=studio_session_id,
        run_id=run_id,
        event_log_path=event_log_path,
        run_dir=run_dir,
        result_path=result_path,
        status=status,
    )
    return result


def _apply_write_scope_enforcement_if_bound(
    result: dict[str, Any],
    *,
    selection: HermesBackendSelection,
    run_id: str,
    workspace: Path,
) -> dict[str, Any]:
    """Wave 4: when a Binding is active for this run, enforce + attach MutationReceipt."""
    from ..write_scope_bindings import list_bindings, load_binding
    from ..write_scope_enforcement import apply_enforcement_to_result

    binding = None
    binding_id = getattr(selection, "write_scope_binding_id", None)
    if binding_id:
        try:
            binding = load_binding(str(binding_id))
        except Exception:
            binding = None
    if binding is None:
        active = list_bindings(run_id=run_id, status="active")
        binding = active[0] if active else None
    if binding is None:
        return result
    return apply_enforcement_to_result(
        result,
        binding=binding,
        workspace_root=workspace,
    )


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
    validation_gates: list[str] | None = None,
    requested_model: str = "unconfigured",
    effective_model: str = "unverified",
    task_findings: str | None = None,
    write_scope_proposal: dict[str, Any] | None = None,
    write_scope_proposals: list[dict[str, Any]] | None = None,
    proposal_missing_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    tests_executed: list[str] | None = None,
) -> dict[str, Any]:
    if write_scope_proposal is None and write_scope_proposals:
        write_scope_proposal = write_scope_proposals[0]
    usage = usage or {}
    prompt_tokens = int(_finite_number(usage.get("prompt_tokens")) or 0)
    completion_tokens = int(_finite_number(usage.get("completion_tokens")) or 0)
    chat_calls = int(_finite_number(usage.get("chat_calls")) or 0)
    estimated_cost_usd = _finite_number(usage.get("estimated_cost_usd"))
    if estimated_cost_usd is not None and estimated_cost_usd <= 0:
        estimated_cost_usd = None
    cost_status = (
        "observed"
        if estimated_cost_usd is not None
        else "tokens_only"
        if prompt_tokens or completion_tokens
        else "unknown"
    )
    remote_ial_usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "chat_calls": chat_calls,
        "cost_status": cost_status,
    }
    closure = derive_validation_closure(
        execution_status=status,
        validation_gates=validation_gates,
        heuristic_status=validation_status,
        tests_executed=tests_executed,
    )
    validation_summary = build_validation_summary(
        closure,
        reason=(
            "Hermes backend returns process status; focused validation must be "
            "requested in mission text."
        ),
    )
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
        "requested_provider": REMOTE_IAL_PROVIDER,
        "effective_provider": REMOTE_IAL_PROVIDER if effective_model != "unverified" else "unverified",
        "requested_model": requested_model,
        "effective_model": effective_model,
        "transport": "remote_ial",
        "fallback_used": False,
        "studio_session_id": studio_session_id,
        "plan_path": None,
        "checkpoint_path": None,
        "event_log_path": str(event_log_path),
        "runtime_log_path": str(runtime_log_path),
        "journal_path": None,
        "changed_paths": changed_paths,
        **({"num_turns": chat_calls} if usage else {}),
        "validation_summary": validation_summary,
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
            "inference": {
                "requested_provider": REMOTE_IAL_PROVIDER,
                "effective_provider": REMOTE_IAL_PROVIDER if effective_model != "unverified" else "unverified",
                "requested_model": requested_model,
                "effective_model": effective_model,
                "transport": "remote_ial",
                "fallback_used": False,
                "direct_provider_fallback": "disabled",
            },
            "remote_ial_usage": remote_ial_usage,
        },
        "budget_summary": {
            "limit": None,
            "spent": estimated_cost_usd,
            "remaining": None,
            "cost_status": cost_status,
        },
    }


def _runtime_summary_text(
    *,
    status: str,
    stop_reason: str,
    run_id: str,
    task_findings_available: bool,
) -> str:
    findings_state = "task findings captured" if task_findings_available else "no task findings captured"
    return (
        f"AMOF Hermes run {run_id} finished with status={status}, "
        f"stop_reason={stop_reason}; {findings_state}. "
        "Authoritative runtime metadata is recorded in this AgentRunResult envelope."
    )


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
