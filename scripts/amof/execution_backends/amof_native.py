"""AMOF Native Agent Runtime — first-party governed execution backend.

Own agent loop, tools, write enforcement, and model adapter. Reuses shared
Hermes helpers only for result envelope writing and changed_paths accounting.
"""

from __future__ import annotations

import fnmatch
import hashlib
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
from ..write_scope_proposals import (
    PATH_CLASS_EXPLICIT_REPOSITORY_ROOT,
    PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE,
    PATH_CLASS_MISSING_OR_INVALID,
    RepositoryRelativePathClassification,
    _normalize_repository_relative_scope_path,
    classify_repository_relative_scope_path,
)
from . import hermes_opensandbox as _shared
from .hermes_opensandbox import (
    WRITE_SCOPE_PROPOSAL_REQUIRED,
    _manifest_repo_targets,
)
from . import context_assembly_receipt as _context_receipt
from . import native_loop_budget as _loop_budget
from . import runtime_usage as _runtime_usage
from .validation_closure import build_validation_summary, derive_validation_closure

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
# Per-call Remote IAL execution budget (see NATIVE-IAL-EXECUTION-BUDGET-CONTRACT).
DEFAULT_NATIVE_IAL_TIMEOUT_SECONDS = 180.0
DEFAULT_NATIVE_IAL_MAX_TOKENS = 4096
STOP_REASON_REMOTE_IAL_TOTAL_TIMEOUT = "remote_ial_total_timeout"

_SHELL_ESCAPE_RE = re.compile(
    r"(?:\.\./|/\.\.|^/|;\s*cd\s+/\s|>\s*/|`\s*cd\s+/\s)"
)


class AmofNativeBackendError(RuntimeError):
    """Raised when the AMOF Native backend cannot be dispatched truthfully."""


class AmofNativeTimeoutError(AmofNativeBackendError):
    """Raised when a Native model HTTP call exceeds its execution budget."""

    def __init__(
        self,
        message: str,
        *,
        timeout_kind: str = "REMOTE_IAL_TOTAL_TIMEOUT",
        timeout_seconds: float | None = None,
        model_turn_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.timeout_kind = timeout_kind
        self.timeout_seconds = timeout_seconds
        self.model_turn_id = model_turn_id
        self.attempt_id = attempt_id


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def native_ial_timeout_seconds() -> float:
    """Per model-call total wait budget for Remote IAL (and other urlopen chats)."""
    value = _env_float("AMOF_NATIVE_IAL_TIMEOUT_SECONDS", DEFAULT_NATIVE_IAL_TIMEOUT_SECONDS)
    return max(1.0, value)


def native_ial_max_tokens() -> int:
    value = _env_int("AMOF_NATIVE_IAL_MAX_TOKENS", DEFAULT_NATIVE_IAL_MAX_TOKENS)
    return max(256, value)


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
        "ial_execution_budget": {
            "per_call_timeout_seconds": native_ial_timeout_seconds(),
            "max_tokens": native_ial_max_tokens(),
            "timeout_semantics": "urlopen_socket_timeout_effectively_total_when_gateway_non_streaming",
            "stop_reason_on_timeout": STOP_REASON_REMOTE_IAL_TOTAL_TIMEOUT,
        },
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


def _reject_non_relative_grant(classified: RepositoryRelativePathClassification) -> None:
    if classified.path_class == PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE:
        return
    if classified.path_class == PATH_CLASS_EXPLICIT_REPOSITORY_ROOT:
        raise AmofNativeBackendError(
            "writable root rejects EXPLICIT_REPOSITORY_ROOT_SCOPE; "
            "repository-root write authority is not granted by default "
            f"(input={classified.raw!r})"
        )
    raise AmofNativeBackendError(
        f"writable root is {PATH_CLASS_MISSING_OR_INVALID} "
        f"(detail={classified.detail}, input={classified.raw!r})"
    )


def _coerce_relative_grant(
    raw: str,
    *,
    workspace: Path | None,
    repo_roots: list[Path],
) -> str:
    classified = classify_repository_relative_scope_path(raw)
    if classified.path_class == PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE:
        assert classified.normalized is not None
        return classified.normalized
    if classified.path_class == PATH_CLASS_EXPLICIT_REPOSITORY_ROOT:
        _reject_non_relative_grant(classified)

    text = str(raw or "").strip()
    path = Path(text).expanduser() if text else None
    # Only absolute inputs may be target-bound translated into relative grants.
    if path is None or not path.is_absolute():
        _reject_non_relative_grant(classified)

    resolved = path.resolve(strict=False)
    candidates: list[Path] = []
    if workspace is not None:
        candidates.append(workspace.resolve(strict=False))
    candidates.extend(repo_roots)
    for root in candidates:
        try:
            root_resolved = root.resolve(strict=False)
            if not resolved.is_relative_to(root_resolved):
                continue
            if resolved == root_resolved:
                # relative_to(repo_root) → "." / "" means repository-root scope.
                _reject_non_relative_grant(
                    RepositoryRelativePathClassification(
                        path_class=PATH_CLASS_EXPLICIT_REPOSITORY_ROOT,
                        normalized=None,
                        detail="absolute_path_equals_repository_root",
                        raw=raw,
                    )
                )
            rel = resolved.relative_to(root_resolved).as_posix()
            rel_classified = classify_repository_relative_scope_path(rel)
            if rel_classified.path_class == PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE:
                assert rel_classified.normalized is not None
                return rel_classified.normalized
            if rel_classified.path_class == PATH_CLASS_EXPLICIT_REPOSITORY_ROOT:
                _reject_non_relative_grant(rel_classified)
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


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
GIF_MAGIC = b"GIF8"
WEBP_MAGIC = b"WEBP"
RIFF_MAGIC = b"RIFF"
PDF_MAGIC = b"%PDF"
BINARY_SUFFIX_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def classify_native_artifact(path: str, data: bytes) -> dict[str, Any]:
    """Classify artifact bytes. Binary stays a ref; only UTF-8 text is decoded."""
    suffix = Path(path).suffix.lower()
    if data.startswith(PNG_MAGIC):
        return {"is_binary": True, "media_type": "image/png", "reason": "png_magic"}
    if data.startswith(JPEG_MAGIC):
        return {"is_binary": True, "media_type": "image/jpeg", "reason": "jpeg_magic"}
    if data.startswith(GIF_MAGIC):
        return {"is_binary": True, "media_type": "image/gif", "reason": "gif_magic"}
    if data.startswith(RIFF_MAGIC) and WEBP_MAGIC in data[:16]:
        return {"is_binary": True, "media_type": "image/webp", "reason": "webp_magic"}
    if data.startswith(PDF_MAGIC):
        return {"is_binary": True, "media_type": "application/pdf", "reason": "pdf_magic"}
    if suffix in BINARY_SUFFIX_MEDIA:
        return {
            "is_binary": True,
            "media_type": BINARY_SUFFIX_MEDIA[suffix],
            "reason": "binary_suffix",
        }
    return {"is_binary": False, "media_type": "text/plain", "reason": "utf8_candidate"}


def render_binary_artifact_ref(*, path: str, data: bytes, media_type: str) -> str:
    return json.dumps(
        {
            "kind": "binary_artifact",
            "path": path,
            "media_type": media_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        },
        indent=2,
    ) + "\n"


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

    def _normalize_relative(self, rel_path: str, *, missing: bool = False) -> str:
        classified = classify_repository_relative_scope_path(rel_path, missing=missing)
        if classified.path_class == PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE:
            assert classified.normalized is not None
            return classified.normalized.rstrip("/")
        if classified.path_class == PATH_CLASS_EXPLICIT_REPOSITORY_ROOT:
            raise AmofNativeBackendError(
                "path rejects EXPLICIT_REPOSITORY_ROOT_SCOPE; "
                "use an explicit repository-relative file or directory path "
                f"(input={rel_path!r})"
            )
        raise AmofNativeBackendError(
            f"path is {PATH_CLASS_MISSING_OR_INVALID} "
            f"(detail={classified.detail}, input={rel_path!r})"
        )

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
        data = target.read_bytes()
        classified = classify_native_artifact(path, data)
        if classified["is_binary"]:
            return render_binary_artifact_ref(
                path=path,
                data=data,
                media_type=str(classified["media_type"]),
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AmofNativeBackendError(
                f"read_file: {path} is not valid UTF-8 text: {exc}"
            ) from exc

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

    def write_file(self, path: str, content: str = "", source_path: str | None = None) -> str:
        if source_path:
            if str(content or ""):
                raise AmofNativeBackendError(
                    "write_file: source_path cannot be combined with text content"
                )
            src = self.enforcer.resolve_read_path(source_path)
            if not src.is_file():
                raise AmofNativeBackendError(
                    f"write_file: source_path is not a file: {source_path}"
                )
            data = src.read_bytes()
            classified = classify_native_artifact(source_path, data)
            if not classified["is_binary"]:
                raise AmofNativeBackendError(
                    "write_file: source_path is not a binary artifact; "
                    "use content for UTF-8 text"
                )
            target = self.enforcer.resolve_write_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return render_binary_artifact_ref(
                path=path,
                data=data,
                media_type=str(classified["media_type"]),
            )
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

    def _require_tool_path(self, args: dict[str, Any], *, tool: str) -> str:
        if "path" not in args:
            raise AmofNativeBackendError(
                f"{tool}: path is {PATH_CLASS_MISSING_OR_INVALID} (detail=missing_path)"
            )
        raw = args.get("path")
        classified = classify_repository_relative_scope_path(raw)
        if classified.path_class == PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE:
            assert classified.normalized is not None
            return classified.normalized
        if classified.path_class == PATH_CLASS_EXPLICIT_REPOSITORY_ROOT:
            raise AmofNativeBackendError(
                f"{tool}: path rejects EXPLICIT_REPOSITORY_ROOT_SCOPE; "
                "refusing to coerce repository-root marker into a broad path "
                f"(input={raw!r})"
            )
        raise AmofNativeBackendError(
            f"{tool}: path is {PATH_CLASS_MISSING_OR_INVALID} "
            f"(detail={classified.detail}, input={raw!r})"
        )

    def dispatch_tool(self, name: str, arguments: dict[str, Any]) -> str:
        args = arguments if isinstance(arguments, dict) else {}
        if name == "read_file":
            return self.read_file(self._require_tool_path(args, tool=name))
        if name == "list_dir":
            # Listing repository root is an explicit read convenience, not a write grant.
            if "path" not in args or args.get("path") in {None, "", ".", "./"}:
                return "\n".join(self.list_dir("."))
            return "\n".join(self.list_dir(self._require_tool_path(args, tool=name)))
        if name == "glob":
            return "\n".join(self.glob(str(args.get("pattern") or "*")))
        if name == "write_file":
            path = self._require_tool_path(args, tool=name)
            raw_source = args.get("source_path")
            if raw_source:
                source_path = self._require_tool_path(
                    {"path": raw_source}, tool="write_file.source_path"
                )
                placed = self.write_file(
                    path,
                    str(args.get("content") or ""),
                    source_path=source_path,
                )
                return placed
            self.write_file(path, str(args.get("content") or ""))
            return f"wrote {path}"
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
            "description": (
                "Write UTF-8 text via content, or place a binary artifact by "
                "copying source_path bytes into an approved grant. "
                "source_path is binary-only and cannot be combined with content. "
                "Not generic shell copy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "source_path": {
                        "type": "string",
                        "description": (
                            "Readable repository-relative binary artifact to place "
                            "at path. PNG/JPEG/GIF/WEBP/PDF or binary suffix only."
                        ),
                    },
                },
                "required": ["path"],
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
    prompt_tokens, completion_tokens = _runtime_usage.remote_ial_tokens_from_body(remote)
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    estimated_cost = _shared._finite_number(remote.get("estimated_cost"))
    if estimated_cost is not None:
        usage["estimated_cost"] = estimated_cost
    if remote.get("cost_status") is not None:
        usage["cost_status"] = remote.get("cost_status")
    if remote.get("request_id") is not None:
        usage["provider_receipt_ref"] = str(remote.get("request_id"))
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
        "usage": usage,
    }


def _is_timeout_exc(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _emit_context_assembly_receipt(
    *,
    assembly_ctx: dict[str, Any] | None,
    call_index: int | None,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    prompt_tokens_reported: int | None,
) -> None:
    """Best-effort per-call receipt. Never raises into the model path."""
    if assembly_ctx is None or call_index is None:
        return
    event_log_path = assembly_ctx.get("event_log_path")
    try:
        path = _context_receipt.persist_call_receipt(
            run_dir=Path(assembly_ctx["run_dir"]),
            run_id=str(assembly_ctx["run_id"]),
            call_index=int(call_index),
            model=model,
            provider=str(assembly_ctx.get("provider") or "unverified"),
            messages=messages,
            tools=tools,
            goal=str(assembly_ctx.get("goal") or ""),
            request_id=assembly_ctx.get("request_id"),
            prompt_tokens_reported=prompt_tokens_reported,
        )
        if event_log_path is not None:
            _shared._append_event(
                Path(event_log_path),
                "context_assembly_receipt_written",
                call_index=int(call_index),
                receipt_path=str(path),
            )
    except Exception as exc:
        if event_log_path is not None:
            try:
                _shared._append_event(
                    Path(event_log_path),
                    "context_assembly_receipt_failed",
                    call_index=int(call_index),
                    error=str(exc),
                )
            except Exception:
                pass


def _chat_completion(
    *,
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]] | None,
    model_turn_id: str | None = None,
    attempt_id: str | None = None,
    abandoned_attempts: set[str] | None = None,
    assembly_ctx: dict[str, Any] | None = None,
    call_index: int | None = None,
) -> dict[str, Any]:
    url, headers, transport = _chat_endpoint_and_headers()
    timeout_seconds = native_ial_timeout_seconds()
    max_tokens = native_ial_max_tokens()
    if transport == TRANSPORT_REMOTE_IAL:
        system, remote_messages = _shared._extract_remote_ial_messages(list(messages))
        payload = {
            "system": system,
            "messages": remote_messages,
            "tools": tools or [],
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
    else:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": min(max_tokens, 4096),
        }
        if tools:
            payload["tools"] = tools
    request = Request(url, headers=headers, data=json.dumps(payload).encode("utf-8"), method="POST")
    prompt_tokens_reported: int | None = None
    try:
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
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
        except (OSError, URLError, TimeoutError) as exc:
            if _is_timeout_exc(exc):
                if attempt_id and abandoned_attempts is not None:
                    abandoned_attempts.add(attempt_id)
                raise AmofNativeTimeoutError(
                    f"model transport timed out after {timeout_seconds:g}s for {url}: {exc}",
                    timeout_kind="REMOTE_IAL_TOTAL_TIMEOUT",
                    timeout_seconds=timeout_seconds,
                    model_turn_id=model_turn_id,
                    attempt_id=attempt_id,
                ) from exc
            raise AmofNativeBackendError(f"model transport request failed for {url}: {exc}") from exc
        if attempt_id and abandoned_attempts is not None and attempt_id in abandoned_attempts:
            # Late body for an already-abandoned attempt must not become authoritative.
            raise AmofNativeTimeoutError(
                f"late model response discarded for abandoned attempt {attempt_id}",
                timeout_kind="REMOTE_IAL_TOTAL_TIMEOUT",
                timeout_seconds=timeout_seconds,
                model_turn_id=model_turn_id,
                attempt_id=attempt_id,
            )
        if not isinstance(body, dict):
            raise AmofNativeBackendError("chat completion returned non-object response")
        if transport == TRANSPORT_REMOTE_IAL:
            body = _openai_compatible_from_remote_ial(body, model=model)
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        prompt_tokens_reported = _runtime_usage.finite_int(usage.get("prompt_tokens"))
        return body
    finally:
        _emit_context_assembly_receipt(
            assembly_ctx=assembly_ctx,
            call_index=call_index,
            model=model,
            messages=messages,
            tools=tools,
            prompt_tokens_reported=prompt_tokens_reported,
        )


def _grant_tree_digest(tools: NativeAgentTools) -> str:
    """Hash contents under approved grant roots (bounded, deterministic)."""
    paths_to_content: dict[str, str] = {}
    for grant in tools.enforcer.grant_roots:
        try:
            root = grant.resolve(strict=False)
        except OSError:
            continue
        if root.is_file():
            try:
                rel = root.relative_to(tools.repo_root).as_posix()
            except ValueError:
                rel = root.name
            try:
                paths_to_content[rel] = root.read_text(encoding="utf-8", errors="replace")
            except OSError:
                paths_to_content[rel] = ""
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            # Keep digest cheap: skip bulky/binary-ish paths.
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".lock"}:
                continue
            try:
                if path.stat().st_size > 256_000:
                    continue
            except OSError:
                continue
            try:
                rel = path.relative_to(tools.repo_root).as_posix()
            except ValueError:
                continue
            try:
                paths_to_content[rel] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                paths_to_content[rel] = ""
    return _loop_budget.digest_grant_tree(paths_to_content)


def _run_model_loop(
    *,
    goal: str,
    tools: NativeAgentTools,
    model: str,
    writable: bool,
    event_log_path: Path,
    deadline: float | None,
    run_id: str | None = None,
    usage_acc: dict[str, Any] | None = None,
    loop_budget_out: dict[str, Any] | None = None,
    assembly_ctx: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    """Run the Native agent loop under amof.native_loop_budget/v1.

    Base turn limit remains 12. Write-capable missions may receive a small
    bounded extension only on MATERIAL machine-observable progress. Read-only
    missions with useful evidence transition to one SYNTHESIS_REQUIRED turn
    instead of an exploration extension. Absolute hard ceiling is always
    enforced. Model self-report never grants extension.
    """
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": _context_receipt.NATIVE_SYSTEM_CONTENT,
        },
        {"role": "user", "content": goal},
    ]
    tool_specs = _TOOL_SPECS if writable else [spec for spec in _TOOL_SPECS if spec["function"]["name"] != "write_file"]
    findings: list[str] = []
    abandoned_attempts: set[str] = set()
    run_key = _safe_id(run_id or "native-run")
    acc = usage_acc if usage_acc is not None else _runtime_usage.empty_usage_accumulator()
    budget_state = _loop_budget.LoopBudgetState(policy=_loop_budget.default_policy())
    absolute = budget_state.policy.absolute_turn_limit

    def _publish_budget(stop: str | None = None) -> None:
        if stop is not None:
            budget_state.last_stop_reason = stop
        if loop_budget_out is not None:
            loop_budget_out.clear()
            loop_budget_out.update(budget_state.to_telemetry())

    for turn_index in range(absolute):
        turn_number = turn_index + 1
        if deadline is not None and time.monotonic() >= deadline:
            _publish_budget("timeout")
            return "failed", "timeout", "\n".join(findings)
        if turn_number > budget_state.effective_turn_limit:
            # Should be unreachable: extension gate runs before exceeding effective.
            stop = _loop_budget.STOP_ABSOLUTE_TURN_LIMIT
            _publish_budget(stop)
            return "failed", stop, "\n".join(findings)

        model_turn_id = f"{run_key}:turn:{turn_number}"
        # Native does not auto-retry timed-out model calls; attempt is always 1 per turn.
        attempt_id = f"{model_turn_id}:attempt:1"
        _shared._append_event(
            event_log_path,
            "model_turn",
            model_turn_id=model_turn_id,
            attempt_id=attempt_id,
            timeout_seconds=native_ial_timeout_seconds(),
            max_tokens=native_ial_max_tokens(),
            loop_budget={
                "turn": turn_number,
                "effective_turn_limit": budget_state.effective_turn_limit,
                "absolute_turn_limit": absolute,
                "extension_count": budget_state.extension_count,
                "synthesis_required": budget_state.synthesis_required,
            },
        )
        call_index = turn_number
        if assembly_ctx is not None:
            call_index = int(assembly_ctx.get("next_call_index") or 1)
            assembly_ctx["next_call_index"] = call_index + 1
        active_tools = tool_specs
        if budget_state.synthesis_required:
            active_tools = []
            if not budget_state.synthesis_consumed:
                messages.append(
                    {
                        "role": "user",
                        "content": _loop_budget.SYNTHESIS_INSTRUCTION,
                    }
                )
                budget_state.synthesis_consumed = True
                _shared._append_event(
                    event_log_path,
                    "loop_budget_synthesis_required",
                    at_turn=turn_number,
                    successful_evidence_count=budget_state.fingerprint.successful_evidence_count,
                    evidence_coverage_digest=budget_state.fingerprint.evidence_coverage_digest,
                )
        try:
            response = _chat_completion(
                messages=messages,
                model=model,
                tools=active_tools,
                model_turn_id=model_turn_id,
                attempt_id=attempt_id,
                abandoned_attempts=abandoned_attempts,
                assembly_ctx=assembly_ctx,
                call_index=call_index,
            )
        except AmofNativeTimeoutError as exc:
            _shared._append_event(
                event_log_path,
                "model_turn_timeout",
                model_turn_id=exc.model_turn_id or model_turn_id,
                attempt_id=exc.attempt_id or attempt_id,
                timeout_kind=exc.timeout_kind,
                timeout_seconds=exc.timeout_seconds,
                error=str(exc),
            )
            _publish_budget(STOP_REASON_REMOTE_IAL_TOTAL_TIMEOUT)
            raise
        call_usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        prompt_tokens = _runtime_usage.finite_int(call_usage.get("prompt_tokens"))
        completion_tokens = _runtime_usage.finite_int(call_usage.get("completion_tokens"))
        _runtime_usage.add_token_field(acc, "prompt_tokens", prompt_tokens)
        _runtime_usage.add_token_field(acc, "completion_tokens", completion_tokens)
        acc["model_calls"] = int(acc.get("model_calls") or 0) + 1
        actual_model = str(response.get("model") or model)
        cost = _shared._finite_number(call_usage.get("estimated_cost"))
        if cost is not None:
            prior = _shared._finite_number(acc.get("estimated_cost_usd"))
            acc["estimated_cost_usd"] = (prior or 0.0) + float(cost)
        if call_usage.get("cost_status") is not None:
            acc["cost_status"] = call_usage.get("cost_status")
        call_record = {
            "model_call_id": model_turn_id,
            "attempt_id": attempt_id,
            "requested_model": model,
            "actual_model": actual_model,
            "provider": "remote_ial",
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "status": "ok",
            "retry_of": None,
            "provider_receipt_ref": call_usage.get("provider_receipt_ref"),
        }
        acc.setdefault("calls", []).append(call_record)
        _shared._append_event(
            event_log_path,
            "model_call_usage",
            model_turn_id=model_turn_id,
            attempt_id=attempt_id,
            requested_model=model,
            actual_model=actual_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            provider_receipt_ref=call_usage.get("provider_receipt_ref"),
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AmofNativeBackendError("chat completion missing choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        if not isinstance(message, dict):
            raise AmofNativeBackendError("chat completion missing message")
        tool_calls = message.get("tool_calls")
        if budget_state.synthesis_required and isinstance(tool_calls, list) and tool_calls:
            stop = _loop_budget.STOP_SYNTHESIS_NOT_COMPLETED
            _shared._append_event(
                event_log_path,
                "loop_budget_synthesis_not_completed",
                at_turn=turn_number,
                reason="tool_calls_during_synthesis",
            )
            _publish_budget(stop)
            return "failed", stop, "\n".join(findings)
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
                tool_error: str | None = None
                try:
                    output = tools.dispatch_tool(name, arguments)
                except AmofNativeBackendError as exc:
                    # Path/grant tool mistakes must not abort the whole run.
                    # Return a structured error to the model and continue;
                    # never coerce missing/empty paths into repository-root scope.
                    tool_error = str(exc)
                    output = f"ERROR: {exc}"
                    findings.append(output)
                    acc["tool_calls"] = int(acc.get("tool_calls") or 0) + 1
                    _shared._append_event(
                        event_log_path,
                        "tool_call",
                        name=name,
                        arguments=arguments,
                        error=str(exc),
                        output_preview=output[:500],
                        model_turn_id=model_turn_id,
                        attempt_id=attempt_id,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(call.get("id") or ""),
                            "content": output,
                        }
                    )
                    _loop_budget.observe_tool_result(
                        budget_state.fingerprint,
                        name=name,
                        arguments=arguments,
                        output=output,
                        error=tool_error,
                        grant_paths_digest=_grant_tree_digest(tools),
                        evidence_keys=budget_state.observed_evidence_keys,
                    )
                    continue
                findings.append(output)
                acc["tool_calls"] = int(acc.get("tool_calls") or 0) + 1
                _shared._append_event(
                    event_log_path,
                    "tool_call",
                    name=name,
                    arguments=arguments,
                    output_preview=output[:500],
                    model_turn_id=model_turn_id,
                    attempt_id=attempt_id,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": output,
                    }
                )
                _loop_budget.observe_tool_result(
                    budget_state.fingerprint,
                    name=name,
                    arguments=arguments,
                    output=output,
                    error=None,
                    grant_paths_digest=_grant_tree_digest(tools),
                    evidence_keys=budget_state.observed_evidence_keys,
                )
            _loop_budget.note_turn_complete(budget_state, turn_number)
            # Model still wants another turn. Gate on progress-aware budget.
            next_turn = turn_number + 1
            if next_turn > absolute:
                stop = _loop_budget.STOP_ABSOLUTE_TURN_LIMIT
                _shared._append_event(
                    event_log_path,
                    "loop_budget_absolute_stop",
                    at_turn=turn_number,
                    absolute_turn_limit=absolute,
                )
                _publish_budget(stop)
                return "failed", stop, "\n".join(findings)
            if next_turn > budget_state.effective_turn_limit:
                if budget_state.synthesis_required:
                    stop = _loop_budget.STOP_SYNTHESIS_NOT_COMPLETED
                    _shared._append_event(
                        event_log_path,
                        "loop_budget_synthesis_not_completed",
                        at_turn=turn_number,
                        reason="synthesis_turn_continued_exploration",
                    )
                    _publish_budget(stop)
                    return "failed", stop, "\n".join(findings)
                if not writable:
                    outcome = _loop_budget.decide_readonly_synthesis(
                        budget_state, at_turn=turn_number
                    )
                    _shared._append_event(
                        event_log_path,
                        "loop_budget_readonly_synthesis_decision",
                        at_turn=turn_number,
                        outcome=outcome,
                        successful_evidence_count=budget_state.fingerprint.successful_evidence_count,
                        synthesis_required=budget_state.synthesis_required,
                        effective_turn_limit=budget_state.effective_turn_limit,
                    )
                    if outcome == _loop_budget.SYNTHESIS_REQUIRED:
                        continue
                    _publish_budget(outcome)
                    return "failed", outcome, "\n".join(findings)
                decision = _loop_budget.decide_extension(budget_state, at_turn=turn_number)
                _shared._append_event(
                    event_log_path,
                    "loop_budget_extension_decision",
                    **{
                        "granted": decision.granted,
                        "at_turn": decision.at_turn,
                        "progress_verdict": decision.progress_verdict,
                        "evidence": list(decision.evidence),
                        "granted_turns": decision.granted_turns,
                        "extension_count_after": decision.extension_count_after,
                        "absolute_limit": decision.absolute_limit,
                        "reason": decision.reason,
                        "effective_turn_limit": budget_state.effective_turn_limit,
                    },
                )
                if not decision.granted:
                    stop = _loop_budget.termination_after_denied_extension(budget_state)
                    _publish_budget(stop)
                    return "failed", stop, "\n".join(findings)
            continue
        content = str(message.get("content") or "").strip()
        if budget_state.synthesis_required and not content:
            stop = _loop_budget.STOP_SYNTHESIS_NOT_COMPLETED
            _shared._append_event(
                event_log_path,
                "loop_budget_synthesis_not_completed",
                at_turn=turn_number,
                reason="empty_synthesis_result",
            )
            _publish_budget(stop)
            return "failed", stop, "\n".join(findings)
        if content:
            findings.append(content)
        # Prose-only / final answer: observe that a turn produced no tool evidence.
        # Self-report text is never treated as progress authority.
        _loop_budget.note_turn_complete(budget_state, turn_number)
        _publish_budget("completed")
        return "completed", "completed", content or "\n".join(findings)

    stop = _loop_budget.STOP_ABSOLUTE_TURN_LIMIT
    _publish_budget(stop)
    return "failed", stop, "\n".join(findings)


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
    validation_gates: list[str] | None = None,
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

    proposal_required = (
        _shared._goal_requests_write_scope_proposal(goal)
        and not selection.writable_roots
    )
    expected_proposal_paths = _shared._explicit_required_proposal_paths(goal)
    write_scope_proposals: list[dict[str, Any]] = []
    proposal_missing_reason: str | None = None
    proposal_replan_used = False
    read_only_replan_used = False
    prompt = _shared._build_prompt(
        goal,
        selection,
        workspace,
        manifest,
        agent_label=AGENT_LABEL,
        backend_name=BACKEND_TYPE,
    )
    assembly_ctx = {
        "run_dir": run_dir,
        "run_id": run_id,
        "request_id": request_id,
        "goal": goal,
        "event_log_path": event_log_path,
        "provider": effective_provider,
        "next_call_index": 1,
    }

    status = "failed"
    stop_reason = "amof_native_runtime_exception"
    exit_code = 1
    task_findings = ""
    validation_status = "not_run"
    changed: list[str] = []
    usage_acc = _runtime_usage.empty_usage_accumulator()
    loop_budget_telemetry: dict[str, Any] = {}

    while True:
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
                    "proposal_required": proposal_required,
                    "proposal_replan_used": proposal_replan_used,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            script = _load_script(selection) if transport == "scripted" else None
            if script is not None:
                effective_provider = str(script.get("provider") or SCRIPT_PROVIDER)
                requested_model = str(script.get("model") or requested_model)
                status, stop_reason, raw_task_findings = _execute_scripted_loop(
                    script=script,
                    tools=tools,
                    event_log_path=event_log_path,
                    deadline=deadline,
                )
                exit_code = 0 if status == "completed" else (124 if stop_reason == "timeout" else 1)
            else:
                status, stop_reason, raw_task_findings = _run_model_loop(
                    goal=prompt,
                    tools=tools,
                    model=requested_model,
                    writable=bool(selection.writable_roots_relative),
                    event_log_path=event_log_path,
                    deadline=deadline,
                    run_id=request_id,
                    usage_acc=usage_acc,
                    loop_budget_out=loop_budget_telemetry,
                    assembly_ctx=assembly_ctx,
                )
                exit_code = 0 if status == "completed" else (124 if stop_reason == "timeout" else 1)
        except AmofNativeTimeoutError as exc:
            status = "failed"
            stop_reason = STOP_REASON_REMOTE_IAL_TOTAL_TIMEOUT
            exit_code = 124
            raw_task_findings = str(exc)
            _shared._append_event(
                event_log_path,
                STOP_REASON_REMOTE_IAL_TOTAL_TIMEOUT,
                error=str(exc),
                timeout_kind=exc.timeout_kind,
                timeout_seconds=exc.timeout_seconds,
                model_turn_id=exc.model_turn_id,
                attempt_id=exc.attempt_id,
            )
        except AmofNativeBackendError as exc:
            status = "failed"
            stop_reason = "grant_enforcement_failed"
            exit_code = 1
            raw_task_findings = str(exc)
            _shared._append_event(event_log_path, "grant_enforcement_failed", error=str(exc))

        write_scope_proposals, task_findings = _shared._extract_write_scope_proposal_outputs(
            raw_task_findings or "",
            expected_allowed_roots=expected_proposal_paths,
        )
        proposal_missing_reason = (
            _shared._proposal_missing_reason(task_findings, "")
            if proposal_required and not write_scope_proposals
            else None
        )

        changed = _shared._changed_paths_delta(
            preexisting_changed_paths, _shared._changed_paths(workspace)
        )
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
            break

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

        validation_status = _shared._infer_validation_status(task_findings)
        if status == "completed" and validation_status == "failed":
            status = "failed"
            stop_reason = "validation_failed"
            exit_code = 1
            break

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
        validation_gates=validation_gates,
        requested_model=requested_model,
        effective_model=requested_model if status != "blocked" else "unverified",
        effective_provider=effective_provider,
        transport=transport or "blocked",
        write_scope_proposals=write_scope_proposals,
        proposal_missing_reason=proposal_missing_reason,
        usage_acc=usage_acc,
        loop_budget=loop_budget_telemetry or None,
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
    validation_gates: list[str] | None = None,
    requested_model: str = "unconfigured",
    effective_model: str = "unverified",
    effective_provider: str = "unverified",
    transport: str = "blocked",
    task_findings: str | None = None,
    write_scope_proposals: list[dict[str, Any]] | None = None,
    proposal_missing_reason: str | None = None,
    usage_acc: dict[str, Any] | None = None,
    loop_budget: dict[str, Any] | None = None,
    tests_executed: list[str] | None = None,
) -> dict[str, Any]:
    write_scope_proposal = write_scope_proposals[0] if write_scope_proposals else None
    acc = usage_acc if isinstance(usage_acc, dict) else _runtime_usage.empty_usage_accumulator()
    loop_budget_payload = dict(loop_budget) if isinstance(loop_budget, dict) and loop_budget else None
    prompt_tokens = _runtime_usage.finite_int(acc.get("prompt_tokens"))
    completion_tokens = _runtime_usage.finite_int(acc.get("completion_tokens"))
    model_calls = int(acc.get("model_calls") or 0)
    tool_calls = int(acc.get("tool_calls") or 0)
    saw_tokens = bool(acc.get("saw_authoritative_tokens"))
    # Counts from the agent loop are authoritative even when provider tokens are absent.
    model_calls_out: int | None = model_calls if model_calls > 0 else None
    tool_calls_out: int | None = tool_calls if tool_calls > 0 else (0 if model_calls > 0 else None)
    token_telemetry = _runtime_usage.token_telemetry_status(
        saw_tokens=saw_tokens,
        model_calls=model_calls_out,
        partial_dimensions=False,
    )
    usage_source = (
        _runtime_usage.USAGE_SOURCE_PROVIDER
        if saw_tokens
        else _runtime_usage.USAGE_SOURCE_UNAVAILABLE
    )
    closure = derive_validation_closure(
        execution_status=status,
        validation_gates=validation_gates,
        heuristic_status=validation_status,
        tests_executed=tests_executed,
    )
    validation_summary = build_validation_summary(
        closure,
        reason="AMOF Native backend enforces grants and records git delta.",
    )
    remote_ial_usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "chat_calls": model_calls_out,
        "tool_calls": tool_calls_out,
        "estimated_cost_usd": acc.get("estimated_cost_usd"),
        "cost_status": acc.get("cost_status"),
        "calls": list(acc.get("calls") or []),
    }
    total_tokens = (
        int(prompt_tokens) + int(completion_tokens)
        if prompt_tokens is not None and completion_tokens is not None
        else None
    )
    runtime_usage = _runtime_usage.build_runtime_usage_v1(
        run_id=run_id,
        backend=BACKEND_TYPE,
        billing_model="metered",
        telemetry_status=token_telemetry,
        usage_source=usage_source,
        aggregates={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": total_tokens,
            "model_calls": model_calls_out,
            "agent_calls": 1 if model_calls_out else None,
            "tool_calls": tool_calls_out,
        },
        by_model=[
            {
                "logical_model": None,
                "actual_model": effective_model,
                "provider_or_substrate": effective_provider,
                "calls": model_calls_out,
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "cached_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": total_tokens,
            }
        ]
        if model_calls_out
        else [],
        spans=[
            {
                "span_id": call.get("model_call_id"),
                "parent_span_id": None,
                "agent_id": run_id,
                "role": "executor",
                "actual_model": call.get("actual_model"),
                "duration_ms": None,
                "usage": {
                    "input_tokens": call.get("input_tokens"),
                    "output_tokens": call.get("output_tokens"),
                },
                "calls": 1,
                "status": call.get("status"),
            }
            for call in list(acc.get("calls") or [])
            if isinstance(call, dict)
        ],
        receipt_refs=[
            str(call.get("provider_receipt_ref"))
            for call in list(acc.get("calls") or [])
            if isinstance(call, dict) and call.get("provider_receipt_ref")
        ],
        raw_usage_refs=["evidence_refs.remote_ial_usage"],
    )
    spent = _shared._finite_number(acc.get("estimated_cost_usd")) or 0.0
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
        "validation_summary": validation_summary,
        "approved_capabilities": list(selection.capabilities),
        "effective_capabilities": list(selection.capabilities),
        "evidence_refs": {
            "backend_contract_version": BACKEND_CONTRACT_VERSION,
            "runtime_contract": RUNTIME_CONTRACT,
            "isolation_model": ISOLATION_MODEL,
            "event_log_path": str(event_log_path),
            "runtime_log_path": str(runtime_log_path),
            **(
                {
                    "context_assembly_dir": str(
                        event_log_path.parent / _context_receipt.RECEIPT_DIRNAME
                    )
                }
                if (event_log_path.parent / _context_receipt.RECEIPT_DIRNAME).is_dir()
                else {}
            ),
            "process_identity": health.get("process_identity"),
            "writable_roots_relative": list(selection.writable_roots_relative),
            "remote_ial_usage": remote_ial_usage,
            "runtime_usage": runtime_usage,
            **(
                {"native_loop_budget": loop_budget_payload}
                if loop_budget_payload is not None
                else {}
            ),
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
        "budget_summary": {
            "limit": (
                loop_budget_payload.get("absolute_turn_limit")
                if loop_budget_payload
                else None
            ),
            "spent": spent,
            "remaining": None,
            **(
                {
                    "native_loop_budget": {
                        "schema": loop_budget_payload.get("schema"),
                        "policy_version": loop_budget_payload.get("policy_version"),
                        "base_turn_limit": loop_budget_payload.get("base_turn_limit"),
                        "turns_used": loop_budget_payload.get("turns_used"),
                        "effective_turn_limit": loop_budget_payload.get(
                            "effective_turn_limit"
                        ),
                        "extension_count": loop_budget_payload.get("extension_count"),
                        "absolute_turn_limit": loop_budget_payload.get(
                            "absolute_turn_limit"
                        ),
                        "stop_reason": loop_budget_payload.get("stop_reason"),
                        "extensions_granted": loop_budget_payload.get(
                            "extensions_granted"
                        ),
                        "extensions_denied": loop_budget_payload.get(
                            "extensions_denied"
                        ),
                    }
                }
                if loop_budget_payload
                else {}
            ),
        },
        "warnings": [],
        "usage": _runtime_usage.build_agent_run_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_tokens=None,
            reasoning_tokens=None,
            total_tokens=total_tokens,
            model_calls=model_calls_out,
            tool_calls=tool_calls_out,
            agent_calls=1 if model_calls_out else None,
            billing_model="metered",
            token_telemetry=token_telemetry,
            subagent_telemetry="partial",
        ),
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
