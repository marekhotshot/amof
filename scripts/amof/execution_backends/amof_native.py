"""AMOF Native Agent Runtime — first-party governed execution backend.

Own agent loop, tools, write enforcement, and model adapter. Reuses shared
Hermes helpers only for result envelope writing and changed_paths accounting.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..app_paths import runs_dir
from . import hermes_opensandbox as _shared
from .hermes_opensandbox import (
    _manifest_repo_targets,
    _normalize_repository_relative_scope_path,
)

BACKEND_TYPE = "amof_native"
BACKEND_CONTRACT_VERSION = "amof-native-agent-runtime-v1"
RUNTIME_CONTRACT = "AMOF Native Agent Runtime (first-party) + model adapter"
ISOLATION_MODEL = "runtime_owner_workspace"
FUTURE_ISOLATION_MODELS = tuple(_shared.FUTURE_ISOLATION_MODELS)
SUPPORTED_CAPABILITIES = tuple(_shared.SUPPORTED_CAPABILITIES)
DEFAULT_MODEL = "gpt-4o-mini"
AGENT_LABEL = "AMOF Native Agent"
SCRIPT_PROVIDER = "amof_native_script"
TRANSPORT_OPENAI = "openai_compatible"
TRANSPORT_REMOTE_IAL = "remote_ial"

_SHELL_ESCAPE_RE = re.compile(
    r"(?:\.\./|/\.\.|^/|;\s*cd\s+/\s|>\s*/|`\s*cd\s+/\s)"
)


class AmofNativeBackendError(RuntimeError):
    """Raised when the AMOF Native backend cannot be dispatched truthfully."""


@dataclass(frozen=True)
class AmofNativeBackendSelection:
    runner_id: str
    capabilities: list[str]
    writable_roots_relative: list[str]
    writable_roots_resolved: tuple[str, ...]
    timeout_seconds: int
    readable_root: str | None
    write_scope_binding_id: str | None = None
    accepted_base_sha: str | None = None
    target_id: str | None = None

    @property
    def writable_roots(self) -> list[str]:
        return list(self.writable_roots_resolved)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    return _shared._safe_id(value)


def is_amof_native_runner(record: dict[str, Any]) -> bool:
    return _shared.runner_backend_type(record) == BACKEND_TYPE


def _script_path() -> Path | None:
    raw = str(os.environ.get("AMOF_NATIVE_SCRIPT") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_file() else None


def _script_mode_available() -> bool:
    return _script_path() is not None


def _remote_ial_configured() -> bool:
    base = str(os.environ.get("AMOF_REMOTE_IAL_BASE_URL") or "").strip()
    key = str(os.environ.get("AMOF_REMOTE_IAL_API_KEY") or "").strip()
    model = str(os.environ.get("AMOF_REMOTE_IAL_MODEL") or "").strip()
    if not base or not key or not model:
        return False
    try:
        parsed = urlparse(base)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def _openai_compatible_configured() -> bool:
    return bool(
        str(os.environ.get("OPENAI_API_KEY") or "").strip()
        or str(os.environ.get("OPENROUTER_API_KEY") or "").strip()
    )


def _inference_transport() -> str | None:
    if _script_mode_available():
        return "scripted"
    if _remote_ial_configured():
        return TRANSPORT_REMOTE_IAL
    if _openai_compatible_configured():
        return TRANSPORT_OPENAI
    return None


def _requested_model(model_override: str | None = None) -> str:
    if _script_mode_available():
        path = _script_path()
        if path is not None:
            try:
                script = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(script, dict) and str(script.get("model") or "").strip():
                    return str(script["model"]).strip()
            except json.JSONDecodeError:
                pass
        return "script-v1"
    return (
        str(model_override or os.environ.get("AMOF_NATIVE_MODEL") or "").strip()
        or str(os.environ.get("AMOF_REMOTE_IAL_MODEL") or "").strip()
        or DEFAULT_MODEL
    )


def _effective_provider(model_override: str | None = None) -> str:
    if _script_mode_available():
        path = _script_path()
        if path is not None:
            try:
                script = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(script, dict) and str(script.get("provider") or "").strip():
                    return str(script["provider"]).strip()
            except json.JSONDecodeError:
                pass
        return SCRIPT_PROVIDER
    if _remote_ial_configured():
        return "remote-ial"
    if str(os.environ.get("OPENROUTER_API_KEY") or "").strip():
        return "openrouter"
    if str(os.environ.get("OPENAI_API_KEY") or "").strip():
        return "openai"
    return "unverified"


def runtime_health() -> dict[str, Any]:
    transport = _inference_transport()
    dispatch_available = transport is not None
    provider = _effective_provider()
    model = _requested_model()
    return {
        "backend_type": BACKEND_TYPE,
        "backend_contract_version": BACKEND_CONTRACT_VERSION,
        "runtime_contract": RUNTIME_CONTRACT,
        "isolation_model": ISOLATION_MODEL,
        "future_isolation_models": list(FUTURE_ISOLATION_MODELS),
        "dispatch_available": dispatch_available,
        "runtime_health": "ready",
        "amof_native_runtime": "ready",
        "inference_transport": transport or "blocked",
        "inference_health": "ready" if dispatch_available else "blocked",
        "requested_provider": provider if dispatch_available else "unverified",
        "effective_provider": provider if dispatch_available else "unverified",
        "requested_model": model if dispatch_available else "unconfigured",
        "effective_model": model if dispatch_available else "unverified",
        "direct_provider_fallback": "disabled",
        "execution_endpoint": "amof_native.run",
        "process_identity": {
            "backend_id": BACKEND_TYPE,
            "backend_contract_version": BACKEND_CONTRACT_VERSION,
            "runtime_contract": RUNTIME_CONTRACT,
            "isolation_model": ISOLATION_MODEL,
            "script_mode": _script_mode_available(),
            "script_path": str(_script_path() or ""),
        },
        "supported_capabilities": list(SUPPORTED_CAPABILITIES),
        "writable_root_required": True,
        "cancellation_support": "timeout_deadline",
        "log_event_support": "events_jsonl",
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
        "amof_native_runtime": health.get("amof_native_runtime", health["runtime_health"]),
        "inference_transport": health.get("inference_transport", "blocked"),
        "inference_health": health.get("inference_health", "blocked"),
        "requested_provider": health.get("requested_provider", "unverified"),
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


def _coerce_relative_grant(
    raw: str,
    *,
    workspace: Path | None,
    repo_roots: list[Path],
) -> str:
    text = str(raw or "").strip()
    if not text:
        raise AmofNativeBackendError("writable root cannot be empty")
    normalized = _normalize_repository_relative_scope_path(text)
    if normalized is not None:
        return normalized
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise AmofNativeBackendError(
            f"writable root must be repository-relative or an absolute path under the workspace: {text}"
        )
    resolved = path.resolve(strict=False)
    candidates: list[Path] = []
    if workspace is not None:
        candidates.append(workspace.resolve(strict=False))
    candidates.extend(repo_roots)
    for root in candidates:
        try:
            if resolved.is_relative_to(root):
                rel = resolved.relative_to(root).as_posix()
                normalized = _normalize_repository_relative_scope_path(rel)
                if normalized is not None:
                    return normalized
        except ValueError:
            continue
    raise AmofNativeBackendError(
        f"absolute writable root {text!r} is outside readable workspace/repo roots; fail closed"
    )


def build_selection(
    *,
    runner_id: str,
    requested_capabilities: list[str],
    approve_writable_roots: list[str],
    timeout_seconds: int,
    readable_root: str | None,
    write_scope_binding_id: str | None = None,
    accepted_base_sha: str | None = None,
    target_id: str | None = None,
) -> AmofNativeBackendSelection:
    normalized_caps = [str(item).strip() for item in requested_capabilities if str(item).strip()]
    _shared._assert_no_dangerous_caps(normalized_caps)
    workspace = (
        Path(readable_root).expanduser().resolve(strict=False)
        if readable_root
        else None
    )
    if workspace is not None and not workspace.is_dir():
        raise AmofNativeBackendError(f"readable root is not a directory: {readable_root}")
    repo_roots = _shared._workspace_repo_roots(workspace) if workspace is not None else []
    relative_grants = [
        _coerce_relative_grant(item, workspace=workspace, repo_roots=repo_roots)
        for item in approve_writable_roots
        if str(item or "").strip()
    ]
    effective_caps = ["read"]
    if relative_grants:
        if "bounded_write" not in normalized_caps:
            raise AmofNativeBackendError(
                "bounded_write capability approval is required when writable roots are approved"
            )
        effective_caps.extend(["bounded_write", "shell_limited", "focused_tests"])
    elif any(cap in {"bounded_write", "shell_limited", "focused_tests"} for cap in normalized_caps):
        raise AmofNativeBackendError(
            "bounded write/test capabilities require at least one explicit writable root"
        )
    return AmofNativeBackendSelection(
        runner_id=runner_id,
        capabilities=effective_caps,
        writable_roots_relative=relative_grants,
        writable_roots_resolved=(),
        timeout_seconds=timeout_seconds,
        readable_root=readable_root,
        write_scope_binding_id=(
            str(write_scope_binding_id).strip() or None
            if write_scope_binding_id is not None
            else None
        ),
        accepted_base_sha=(
            str(accepted_base_sha).strip().lower() or None
            if accepted_base_sha is not None
            else None
        ),
        target_id=str(target_id).strip() or None if target_id is not None else None,
    )


def _resolve_grants_at_runtime(
    selection: AmofNativeBackendSelection,
    workspace: Path,
) -> AmofNativeBackendSelection:
    repo_roots = _shared._workspace_repo_roots(workspace)
    if not repo_roots:
        repo_roots = [workspace]
    resolved: list[str] = []
    for rel in selection.writable_roots_relative:
        matched = False
        for repo_root in repo_roots:
            candidate = (repo_root / rel).resolve(strict=False)
            try:
                real = candidate.resolve(strict=False)
            except OSError:
                real = candidate
            try:
                if not real.is_relative_to(repo_root.resolve(strict=False)):
                    continue
            except ValueError:
                continue
            resolved.append(str(real))
            matched = True
            break
        if not matched:
            raise AmofNativeBackendError(
                f"writable root {rel!r} could not be resolved inside workspace git roots"
            )
    return replace(selection, writable_roots_resolved=tuple(resolved))


def _validate_target_sha(
    selection: AmofNativeBackendSelection,
    manifest: dict[str, Any],
) -> str | None:
    if not selection.target_id and not selection.accepted_base_sha:
        return None
    targets = _manifest_repo_targets(manifest)
    if not targets:
        return "manifest has no repo targets for target/sha binding"
    if selection.target_id:
        match = next((t for t in targets if t.get("target_id") == selection.target_id), None)
        if match is None:
            return f"target_id {selection.target_id!r} not found in manifest"
    if selection.accepted_base_sha:
        sha_ok = any(
            t.get("base_sha") == selection.accepted_base_sha for t in targets if t.get("base_sha")
        )
        if not sha_ok:
            return f"accepted_base_sha {selection.accepted_base_sha!r} not matched in manifest"
    return None


def _run_dir(run_id: str) -> Path:
    path = runs_dir() / "amof-native" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_script(selection: AmofNativeBackendSelection | None = None) -> dict[str, Any] | None:
    path = _script_path()
    if path is None:
        return None
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise AmofNativeBackendError("AMOF_NATIVE_SCRIPT must contain a JSON object")
    return parsed


class _GrantEnforcer:
    def __init__(
        self,
        *,
        workspace: Path,
        repo_roots: list[Path],
        grant_roots_resolved: list[Path],
        writable: bool,
    ) -> None:
        self.workspace = workspace.resolve(strict=False)
        self.repo_roots = [root.resolve(strict=False) for root in repo_roots]
        self.grant_roots = [root.resolve(strict=False) for root in grant_roots_resolved]
        self.writable = writable

    def _repo_for_relative(self, rel_path: str) -> Path:
        rel = self._normalize_relative(rel_path)
        for repo_root in self.repo_roots:
            candidate = (repo_root / rel).resolve(strict=False)
            try:
                if candidate.is_relative_to(repo_root):
                    return repo_root
            except ValueError:
                continue
        raise AmofNativeBackendError(f"path {rel_path!r} is outside workspace repositories")

    def _normalize_relative(self, rel_path: str) -> str:
        normalized = _normalize_repository_relative_scope_path(str(rel_path or "").strip())
        if normalized is None:
            raise AmofNativeBackendError(f"invalid repository-relative path: {rel_path!r}")
        return normalized.rstrip("/")

    def resolve_read_path(self, rel_path: str) -> Path:
        rel = self._normalize_relative(rel_path)
        repo_root = self._repo_for_relative(rel)
        target = (repo_root / rel).resolve(strict=False)
        real = target.resolve(strict=False)
        if not real.is_relative_to(repo_root):
            raise AmofNativeBackendError(f"read path escapes repository root: {rel_path!r}")
        if not real.is_relative_to(self.workspace):
            raise AmofNativeBackendError(f"read path escapes readable workspace: {rel_path!r}")
        return real

    def resolve_write_path(self, rel_path: str) -> Path:
        if not self.writable:
            raise AmofNativeBackendError("write_file denied: run is read-only")
        rel = self._normalize_relative(rel_path)
        repo_root = self._repo_for_relative(rel)
        target = (repo_root / rel).resolve(strict=False)
        real = target.resolve(strict=False)
        if not real.is_relative_to(repo_root):
            raise AmofNativeBackendError(f"write path escapes repository root: {rel_path!r}")
        if not any(self._path_within_grant(real, grant) for grant in self.grant_roots):
            raise AmofNativeBackendError(
                f"write path {rel_path!r} is outside approved writable roots"
            )
        return real

    @staticmethod
    def _path_within_grant(path: Path, grant_root: Path) -> bool:
        try:
            real_path = path.resolve(strict=False)
            real_grant = grant_root.resolve(strict=False)
            if real_path == real_grant:
                return True
            return real_path.is_relative_to(real_grant)
        except (OSError, ValueError):
            return False


class NativeAgentTools:
    def __init__(self, enforcer: _GrantEnforcer) -> None:
        self.enforcer = enforcer
        self.repo_root = enforcer.repo_roots[0]

    def read_file(self, path: str) -> str:
        target = self.enforcer.resolve_read_path(path)
        if not target.is_file():
            raise AmofNativeBackendError(f"read_file: not a file: {path}")
        return target.read_text(encoding="utf-8")

    def list_dir(self, path: str = ".") -> list[str]:
        rel = path if path not in {".", ""} else "."
        if rel == ".":
            base = self.repo_root
        else:
            base = self.enforcer.resolve_read_path(rel)
        if not base.is_dir():
            raise AmofNativeBackendError(f"list_dir: not a directory: {path}")
        return sorted(item.name for item in base.iterdir())

    def glob(self, pattern: str) -> list[str]:
        if not pattern or "\x00" in pattern:
            raise AmofNativeBackendError("glob pattern is invalid")
        matches: list[str] = []
        for repo_root in self.enforcer.repo_roots:
            for path in repo_root.rglob("*"):
                rel = path.relative_to(repo_root).as_posix()
                if fnmatch.fnmatch(rel, pattern):
                    matches.append(rel)
        return sorted(set(matches))[:500]

    def write_file(self, path: str, content: str) -> str:
        target = self.enforcer.resolve_write_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        normalized = _normalize_repository_relative_scope_path(path)
        return normalized or path

    def run_shell(self, command: str) -> str:
        text = str(command or "").strip()
        if not text:
            raise AmofNativeBackendError("run_shell: empty command")
        if _SHELL_ESCAPE_RE.search(text):
            raise AmofNativeBackendError("run_shell: command rejected (escape pattern)")
        completed = subprocess.run(
            text,
            shell=True,
            cwd=str(self.repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        return f"exit_code={completed.returncode}\n{output}".strip()

    def git_status(self) -> str:
        completed = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=str(self.repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        return (completed.stdout or completed.stderr or "").strip()

    def git_diff(self) -> str:
        completed = subprocess.run(
            ["git", "diff", "--"],
            cwd=str(self.repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        return (completed.stdout or completed.stderr or "").strip()

    def dispatch_tool(self, name: str, arguments: dict[str, Any]) -> str:
        args = arguments if isinstance(arguments, dict) else {}
        if name == "read_file":
            return self.read_file(str(args.get("path") or ""))
        if name == "list_dir":
            return "\n".join(self.list_dir(str(args.get("path") or ".")))
        if name == "glob":
            return "\n".join(self.glob(str(args.get("pattern") or "*")))
        if name == "write_file":
            self.write_file(str(args.get("path") or ""), str(args.get("content") or ""))
            return f"wrote {args.get('path')}"
        if name == "run_shell":
            return self.run_shell(str(args.get("command") or ""))
        if name == "git_status":
            return self.git_status()
        if name == "git_diff":
            return self.git_diff()
        raise AmofNativeBackendError(f"unknown tool: {name}")


def _execute_scripted_loop(
    *,
    script: dict[str, Any],
    tools: NativeAgentTools,
    event_log_path: Path,
    deadline: float | None,
) -> tuple[str, str, str]:
    steps = script.get("steps")
    if not isinstance(steps, list):
        raise AmofNativeBackendError("script must include steps[]")
    findings: list[str] = []
    for index, step in enumerate(steps):
        if deadline is not None and time.monotonic() >= deadline:
            return "failed", "timeout", ""
        if not isinstance(step, dict):
            raise AmofNativeBackendError(f"script step {index} must be an object")
        step_type = str(step.get("type") or "").strip()
        if step_type == "tool":
            name = str(step.get("name") or "").strip()
            arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
            output = tools.dispatch_tool(name, arguments)
            findings.append(output)
            _shared._append_event(
                event_log_path,
                "tool_call",
                name=name,
                arguments=arguments,
                output_preview=output[:500],
            )
            continue
        if step_type == "final":
            text = str(step.get("text") or "").strip()
            return "completed", "completed", text or "\n".join(findings)
        raise AmofNativeBackendError(f"unsupported script step type: {step_type!r}")
    return "completed", "completed", "\n".join(findings)


_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a repository-relative file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a repository-relative path inside approved grants",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a repository-relative directory",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Glob repository-relative paths",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]


def _chat_endpoint_and_headers() -> tuple[str, dict[str, str], str]:
    if _remote_ial_configured():
        base = str(os.environ.get("AMOF_REMOTE_IAL_BASE_URL") or "").strip().rstrip("/")
        key = str(os.environ.get("AMOF_REMOTE_IAL_API_KEY") or "").strip()
        # Remote IAL owns /v1/ial/chat — not OpenAI /v1/chat/completions (404).
        return f"{base}/v1/ial/chat", {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }, TRANSPORT_REMOTE_IAL
    openrouter = str(os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if openrouter:
        return "https://openrouter.ai/api/v1/chat/completions", {
            "Authorization": f"Bearer {openrouter}",
            "Content-Type": "application/json",
        }, TRANSPORT_OPENAI
    openai_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    base = str(os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
    return f"{base}/chat/completions", {
        "Authorization": f"Bearer {openai_key}",
        "Content-Type": "application/json",
    }, TRANSPORT_OPENAI


def _openai_compatible_from_remote_ial(remote: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Normalize Remote IAL /v1/ial/chat into an OpenAI-like chat.completion object."""
    tool_calls = [
        _shared._remote_ial_tool_to_openai(item, index)
        for index, item in enumerate(remote.get("tool_calls") or [], start=1)
        if isinstance(item, dict)
    ]
    message: dict[str, Any] = {"role": "assistant", "content": remote.get("text") or ""}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": str(remote.get("request_id") or "chatcmpl-amof-native"),
        "object": "chat.completion",
        "model": str(remote.get("model") or remote.get("upstream_model") or model),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _shared._finish_reason(remote.get("stop_reason"), tool_calls),
            }
        ],
        "usage": {
            "prompt_tokens": int(((remote.get("tokens") or {}) if isinstance(remote.get("tokens"), dict) else {}).get("input") or 0),
            "completion_tokens": int(((remote.get("tokens") or {}) if isinstance(remote.get("tokens"), dict) else {}).get("output") or 0),
        },
    }


def _chat_completion(
    *,
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    url, headers, transport = _chat_endpoint_and_headers()
    if transport == TRANSPORT_REMOTE_IAL:
        system, remote_messages = _shared._extract_remote_ial_messages(list(messages))
        payload = {
            "system": system,
            "messages": remote_messages,
            "tools": tools or [],
            "model": model,
            "max_tokens": 8192,
            "temperature": 0.0,
        }
    else:
        payload = {"model": model, "messages": messages, "temperature": 0}
        if tools:
            payload["tools"] = tools
    request = Request(url, headers=headers, data=json.dumps(payload).encode("utf-8"), method="POST")
    try:
        with urlopen(request, timeout=90) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = str(exc)
        raise AmofNativeBackendError(
            f"model transport HTTP {exc.code} for {url}: {detail or exc.reason}"
        ) from exc
    except (OSError, URLError) as exc:
        raise AmofNativeBackendError(f"model transport request failed for {url}: {exc}") from exc
    if not isinstance(body, dict):
        raise AmofNativeBackendError("chat completion returned non-object response")
    if transport == TRANSPORT_REMOTE_IAL:
        return _openai_compatible_from_remote_ial(body, model=model)
    return body


def _run_model_loop(
    *,
    goal: str,
    tools: NativeAgentTools,
    model: str,
    writable: bool,
    event_log_path: Path,
    deadline: float | None,
) -> tuple[str, str, str]:
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "You are AMOF Native Agent. Use tools for repository work; stay within approved grants.",
        },
        {"role": "user", "content": goal},
    ]
    tool_specs = _TOOL_SPECS if writable else [spec for spec in _TOOL_SPECS if spec["function"]["name"] != "write_file"]
    findings: list[str] = []
    for _turn in range(12):
        if deadline is not None and time.monotonic() >= deadline:
            return "failed", "timeout", "\n".join(findings)
        response = _chat_completion(messages=messages, model=model, tools=tool_specs)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AmofNativeBackendError("chat completion missing choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        if not isinstance(message, dict):
            raise AmofNativeBackendError("chat completion missing message")
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            messages.append(message)
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or "").strip()
                try:
                    arguments = json.loads(str(function.get("arguments") or "{}"))
                except json.JSONDecodeError:
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                output = tools.dispatch_tool(name, arguments)
                findings.append(output)
                _shared._append_event(
                    event_log_path,
                    "tool_call",
                    name=name,
                    arguments=arguments,
                    output_preview=output[:500],
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": output,
                    }
                )
            continue
        content = str(message.get("content") or "").strip()
        if content:
            findings.append(content)
        return "completed", "completed", content or "\n".join(findings)
    return "failed", "amof_native_max_turns", "\n".join(findings)


def _changed_paths_outside_grants(
    changed: list[str],
    selection: AmofNativeBackendSelection,
    workspace: Path,
) -> list[str]:
    if not changed or not selection.writable_roots_relative:
        return list(changed) if changed and not selection.writable_roots_relative else []
    repo_roots = _shared._workspace_repo_roots(workspace) or [workspace]
    outside: list[str] = []
    grant_rel = set(selection.writable_roots_relative)
    for item in changed:
        rel = item.rstrip("/")
        allowed = False
        for grant in grant_rel:
            if rel == grant.rstrip("/") or rel.startswith(grant.rstrip("/") + "/"):
                allowed = True
                break
        if not allowed:
            outside.append(item)
    return outside


def _blocked_result(
    *,
    run_id: str,
    stop_reason: str,
    final_text: str,
    studio_session_id: str | None,
    event_log_path: Path,
    runtime_log_path: Path,
    result_path: Path,
    selection: AmofNativeBackendSelection,
    health: dict[str, Any],
    requested_model: str,
    effective_provider: str,
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
        requested_model=requested_model,
        effective_model="unverified",
        effective_provider=effective_provider,
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
    selection: AmofNativeBackendSelection,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    health = runtime_health()
    run_id = (
        f"amof-native-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        f"-{_safe_id(request_id)}"
    )
    run_dir = _run_dir(run_id)
    event_log_path = run_dir / "events.jsonl"
    runtime_log_path = run_dir / "runtime.log"
    result_path = run_dir / "result.json"
    started_at = _now_iso()
    workspace = _shared._workspace_for(selection, manifest)
    requested_model = _requested_model(model)
    effective_provider = _effective_provider(model)
    transport = _inference_transport()

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

    if transport is None:
        return _blocked_result(
            run_id=run_id,
            stop_reason="inference_transport_unavailable",
            final_text="No inference transport or AMOF_NATIVE_SCRIPT is configured for the AMOF Native runner.",
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result_path=result_path,
            selection=selection,
            health=health,
            requested_model=requested_model,
            effective_provider=effective_provider,
            started_at=started_at,
            reason="inference_transport_unavailable",
        )

    try:
        selection = _resolve_grants_at_runtime(selection, workspace)
    except AmofNativeBackendError as exc:
        return _blocked_result(
            run_id=run_id,
            stop_reason="writable_root_resolution_failed",
            final_text=str(exc),
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result_path=result_path,
            selection=selection,
            health=health,
            requested_model=requested_model,
            effective_provider=effective_provider,
            started_at=started_at,
            reason="writable_root_resolution_failed",
        )

    target_error = _validate_target_sha(selection, manifest)
    if target_error:
        return _blocked_result(
            run_id=run_id,
            stop_reason="target_binding_rejected",
            final_text=target_error,
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result_path=result_path,
            selection=selection,
            health=health,
            requested_model=requested_model,
            effective_provider=effective_provider,
            started_at=started_at,
            reason="target_binding_rejected",
        )

    preexisting_changed_paths = _shared._changed_paths(workspace)
    if not selection.writable_roots_relative and preexisting_changed_paths:
        return _blocked_result(
            run_id=run_id,
            stop_reason="read_only_workspace_not_clean",
            final_text=_shared._read_only_unclean_workspace_message(list(preexisting_changed_paths)),
            studio_session_id=studio_session_id,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result_path=result_path,
            selection=selection,
            health=health,
            requested_model=requested_model,
            effective_provider=effective_provider,
            started_at=started_at,
            reason="read_only_workspace_not_clean",
            extra_evidence={"preexisting_changed_paths": list(preexisting_changed_paths)},
        )

    repo_roots = _shared._workspace_repo_roots(workspace) or [workspace]
    grant_paths = [Path(item) for item in selection.writable_roots_resolved]
    enforcer = _GrantEnforcer(
        workspace=workspace,
        repo_roots=repo_roots,
        grant_roots_resolved=grant_paths,
        writable=bool(selection.writable_roots_relative),
    )
    tools = NativeAgentTools(enforcer)

    deadline: float | None = None
    if selection.timeout_seconds is not None:
        if selection.timeout_seconds <= 0:
            deadline = time.monotonic()
        else:
            deadline = time.monotonic() + float(selection.timeout_seconds)

    (run_dir / "request.json").write_text(
        json.dumps(
            {
                "request_id": request_id,
                "runner_id": selection.runner_id,
                "backend": BACKEND_TYPE,
                "writable_roots_relative": selection.writable_roots_relative,
                "workspace": str(workspace),
                "requested_provider": effective_provider,
                "requested_model": requested_model,
                "transport": transport,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    status = "failed"
    stop_reason = "amof_native_runtime_exception"
    exit_code = 1
    task_findings = ""
    validation_status = "not_run"

    try:
        script = _load_script(selection) if transport == "scripted" else None
        if script is not None:
            effective_provider = str(script.get("provider") or SCRIPT_PROVIDER)
            requested_model = str(script.get("model") or requested_model)
            status, stop_reason, task_findings = _execute_scripted_loop(
                script=script,
                tools=tools,
                event_log_path=event_log_path,
                deadline=deadline,
            )
            exit_code = 0 if status == "completed" else (124 if stop_reason == "timeout" else 1)
        else:
            status, stop_reason, task_findings = _run_model_loop(
                goal=goal,
                tools=tools,
                model=requested_model,
                writable=bool(selection.writable_roots_relative),
                event_log_path=event_log_path,
                deadline=deadline,
            )
            exit_code = 0 if status == "completed" else (124 if stop_reason == "timeout" else 1)
    except AmofNativeBackendError as exc:
        status = "failed"
        stop_reason = "grant_enforcement_failed"
        exit_code = 1
        task_findings = str(exc)
        _shared._append_event(event_log_path, "grant_enforcement_failed", error=str(exc))

    changed = _shared._changed_paths_delta(preexisting_changed_paths, _shared._changed_paths(workspace))
    outside = _changed_paths_outside_grants(changed, selection, workspace)
    if outside and status == "completed":
        status = "failed"
        stop_reason = "write_outside_grant"
        exit_code = 1
        task_findings = f"Modified paths outside grant: {', '.join(outside)}"
        _shared._append_event(
            event_log_path,
            "write_outside_grant",
            changed_paths=list(changed),
            outside_grant=list(outside),
        )

    validation_status = _shared._infer_validation_status(task_findings)
    if status == "completed" and validation_status == "failed":
        status = "failed"
        stop_reason = "validation_failed"
        exit_code = 1

    final_text = _runtime_summary_text(
        status=status,
        stop_reason=stop_reason,
        run_id=run_id,
        task_findings_available=bool(task_findings),
    )
    _shared._write_runtime_log(runtime_log_path, task_findings or final_text)

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
        validation_status=validation_status,
        requested_model=requested_model,
        effective_model=requested_model if status != "blocked" else "unverified",
        effective_provider=effective_provider,
        transport=transport or "blocked",
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
    selection: AmofNativeBackendSelection,
    health: dict[str, Any],
    validation_status: str = "not_run",
    requested_model: str = "unconfigured",
    effective_model: str = "unverified",
    effective_provider: str = "unverified",
    transport: str = "blocked",
    task_findings: str | None = None,
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
        "runner_id": selection.runner_id,
        "backend": BACKEND_TYPE,
        "requested_provider": effective_provider,
        "effective_provider": effective_provider if effective_model != "unverified" else "unverified",
        "requested_model": requested_model,
        "effective_model": effective_model,
        "transport": transport,
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
            "reason": "AMOF Native backend enforces grants and records git delta.",
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
            "writable_roots_relative": list(selection.writable_roots_relative),
            "inference": {
                "requested_provider": effective_provider,
                "effective_provider": effective_provider
                if effective_model != "unverified"
                else "unverified",
                "requested_model": requested_model,
                "effective_model": effective_model,
                "transport": transport,
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
    findings_state = (
        "task findings captured" if task_findings_available else "no task findings captured"
    )
    return (
        f"AMOF Native run {run_id} finished with status={status}, "
        f"stop_reason={stop_reason}; {findings_state}. "
        "Authoritative runtime metadata is recorded in this AgentRunResult envelope."
    )
