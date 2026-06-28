"""Hermes compatibility backend contract for governed AMOF handoffs."""

from __future__ import annotations

import json
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    result.setdefault("result_path", str(result_path))
    result.setdefault("runtime_log_unavailable_reason", None)
    result.setdefault("failure_classification", reason if result.get("status") != "completed" else None)
    if not runtime_log_path.exists():
        _write_runtime_log(
            runtime_log_path,
            result.get("final_text") or result.get("stop_reason") or "terminal result written",
        )
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
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


def _primary_manifest_target(manifest: dict[str, Any]) -> dict[str, str]:
    repos = manifest.get("repos")
    if not isinstance(repos, list) or not repos:
        return {}
    first = repos[0]
    if not isinstance(first, dict):
        return {}
    base_sha = str(first.get("sha") or first.get("branch") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        base_sha = ""
    return {
        "target_id": str(first.get("target_id") or "").strip(),
        "base_sha": base_sha,
        "repository_url": str(first.get("url") or "").strip(),
        "workspace_path": str(first.get("path") or "").strip(),
    }


def _normalize_write_scope_proposal(value: Any) -> dict[str, Any] | None:
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
        values = [str(item).strip() for item in raw]
        if any(not item for item in values):
            return None
        return values

    allowed_roots = _string_list("allowed_roots")
    denied_roots = _string_list("denied_roots")
    expected_checks = _string_list("expected_checks")
    docs_only = proposal.get("docs_only")
    source_mutation = proposal.get("source_mutation")
    if (
        allowed_roots is None
        or denied_roots is None
        or expected_checks is None
        or not isinstance(docs_only, bool)
        or not isinstance(source_mutation, bool)
    ):
        return None
    proposal["target_id"] = target_id
    proposal["base_sha"] = base_sha
    proposal["reason"] = reason
    proposal["allowed_roots"] = allowed_roots
    proposal["denied_roots"] = denied_roots
    proposal["expected_checks"] = expected_checks
    proposal["docs_only"] = docs_only
    proposal["source_mutation"] = source_mutation
    return proposal


def _extract_write_scope_proposal_output(
    text: str,
) -> tuple[dict[str, Any] | None, str]:
    pattern = re.compile(
        rf"{WRITE_SCOPE_PROPOSAL_START}\s*(\{{.*?\}})\s*{WRITE_SCOPE_PROPOSAL_END}",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return None, text.strip()
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        parsed = None
    proposal = _normalize_write_scope_proposal(parsed)
    summary = (text[: match.start()] + text[match.end() :]).strip()
    return proposal, summary
def _build_prompt(
    goal: str,
    selection: HermesBackendSelection,
    workspace: Path,
    manifest: dict[str, Any] | None = None,
    *,
    read_only_replan: bool = False,
) -> str:
    lines = [
        "You are executing as Hermes under AMOF authority.",
        f"AMOF runner_id: {selection.runner_id}",
        f"AMOF backend: {BACKEND_TYPE}",
        f"Workspace root: {workspace}",
        f"Approved capabilities: {', '.join(selection.capabilities)}",
        "Denied: Kubernetes mutation, deployment, secrets, unrestricted network, push, promotion, tags, releases.",
        "",
        "Truth domains:",
        "- Agent-observed task findings: report only facts you inspect in the workspace through approved commands/tools.",
        "- AMOF runtime envelope: handoff ID, run ID, Studio Session ID, runner/backend, provider/model/transport, fallback, capabilities, changed paths, status, stop reason, and evidence paths are supplied by AMOF outside your answer.",
        "Do not search the repository for AMOF runtime-envelope field names such as runner_id, backend, transport, studio_session_id, result_path, runtime_log_path, or event_log_path.",
        "If asked for AMOF runtime-envelope fields, state that AMOF will provide them in the runtime envelope; do not treat absent metadata files as blockers.",
        "Use explicit commands when the mission asks for command-derived repository facts, and include command exit codes in your task findings.",
    ]
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
    if _goal_requests_write_scope_proposal(goal):
        target = _primary_manifest_target(manifest or {})
        lines.extend(
            [
                "",
                "Structured write-scope contract:",
                "If the mission asks for a structured write_scope_proposal, emit exactly one JSON object between these markers before any human-readable summary.",
                WRITE_SCOPE_PROPOSAL_START,
                '{"target_id":"","base_sha":"","allowed_roots":[],"denied_roots":[],"reason":"","expected_checks":[],"docs_only":false,"source_mutation":false}',
                WRITE_SCOPE_PROPOSAL_END,
                "Use exactly those JSON field names. Do not wrap them in another object.",
                "Keep allowed_roots and denied_roots repository-relative.",
                "After the JSON block, emit a Markdown summary for humans. Do not restate the JSON block in prose.",
                "If evidence does not justify a bounded follow-up, omit the JSON block and emit only the Markdown summary.",
            ]
        )
        if target:
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


def _changed_paths_delta(before: list[str], after: list[str]) -> list[str]:
    before_set = {item for item in before if item}
    after_set = {item for item in after if item}
    return sorted(after_set - before_set)


def _restore_read_only_paths(workspace: Path, paths: list[str]) -> list[str]:
    restored: list[str] = []
    if not paths:
        return restored
    for rel_path in sorted({item for item in paths if item}):
        target = workspace / rel_path
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel_path],
            cwd=str(workspace),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        ).returncode == 0
        if tracked:
            subprocess.run(
                ["git", "restore", "--staged", "--worktree", "--", rel_path],
                cwd=str(workspace),
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
    return sorted(restored)


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
        final_text = "Read-only run blocked before execution because workspace has pre-existing tracked changes."
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
    prompt = _build_prompt(goal, selection, workspace, manifest)
    write_scope_proposal: dict[str, Any] | None = None
    task_findings = ""
    runtime_detail = ""
    validation_status = "not_run"
    changed: list[str] = []
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
        try:
            with _RemoteIALOpenAIAdapter(remote_ial) as adapter:
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

        raw_task_findings = stdout_path.read_text(encoding="utf-8").strip()
        runtime_detail = stderr_path.read_text(encoding="utf-8").strip()
        write_scope_proposal, task_findings = _extract_write_scope_proposal_output(
            raw_task_findings
        )
        validation_status = _infer_validation_status(task_findings or runtime_detail)
        if status == "completed" and validation_status == "failed":
            status = "failed"
            stop_reason = "validation_failed"
            exit_code = 1
        changed = _changed_paths_delta(preexisting_changed_paths, _changed_paths(workspace))
        if status == "completed" and not selection.writable_roots and changed:
            if read_only_replan_used:
                status = "failed"
                stop_reason = "read_only_mutation_detected"
                exit_code = 1
                break
            restored_paths = _restore_read_only_paths(workspace, changed)
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
        requested_model=remote_ial.model,
        effective_model=remote_ial.model,
        write_scope_proposal=write_scope_proposal,
    )
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
        "task_findings": task_findings,
        **(
            {"write_scope_proposal": write_scope_proposal}
            if write_scope_proposal is not None
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
        "validation_summary": {
            "status": validation_status,
            "reason": "Hermes backend returns process status; focused validation must be requested in mission text.",
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
            "inference": {
                "requested_provider": REMOTE_IAL_PROVIDER,
                "effective_provider": REMOTE_IAL_PROVIDER if effective_model != "unverified" else "unverified",
                "requested_model": requested_model,
                "effective_model": effective_model,
                "transport": "remote_ial",
                "fallback_used": False,
                "direct_provider_fallback": "disabled",
            },
        },
        "budget_summary": {"limit": None, "spent": 0.0, "remaining": None},
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
