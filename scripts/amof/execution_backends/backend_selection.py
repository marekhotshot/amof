"""Governed capability and workspace selection shared by CLI adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .runtime_governance import assert_no_dangerous_caps


@dataclass(frozen=True)
class BackendSelection:
    runner_id: str
    capabilities: list[str]
    writable_roots: list[str]
    timeout_seconds: int
    readable_root: str | None
    write_scope_binding_id: str | None = None


def _resolve_roots(
    values: list[str], *, readable_root: str | None, error_type: type[Exception],
) -> list[Path]:
    roots: list[Path] = []
    workspace = Path(readable_root).expanduser().resolve(strict=True) if readable_root else None
    if workspace is not None and not workspace.is_dir():
        raise error_type(f"readable root is not a directory: {readable_root}")
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            path = candidate.resolve(strict=False)
        else:
            if workspace is None:
                raise error_type(f"relative writable root requires readable workspace: {text}")
            path = (workspace / candidate).resolve(strict=False)
        if workspace is not None and not path.is_relative_to(workspace):
            raise error_type(f"approved writable root is outside the readable workspace: {text}")
        if path.exists() and not (path.is_dir() or path.is_file()):
            raise error_type(f"approved writable root is not a file or directory: {text}")
        roots.append(path)
    return roots


def build_selection(
    *, runner_id: str, requested_capabilities: list[str], approve_writable_roots: list[str],
    timeout_seconds: int, readable_root: str | None, write_scope_binding_id: str | None = None,
    backend_name: str, error_type: type[Exception] = RuntimeError,
) -> BackendSelection:
    normalized_caps = [str(item).strip() for item in requested_capabilities if str(item).strip()]
    assert_no_dangerous_caps(normalized_caps, backend_name=backend_name, error_type=error_type)
    writable_roots = [str(path) for path in _resolve_roots(
        approve_writable_roots, readable_root=readable_root, error_type=error_type,
    )]
    effective_caps = ["read"]
    if writable_roots:
        if "bounded_write" not in normalized_caps:
            raise error_type("bounded_write capability approval is required when writable roots are approved")
        effective_caps.extend(["bounded_write", "shell_limited", "focused_tests"])
    elif any(cap in {"bounded_write", "shell_limited", "focused_tests"} for cap in normalized_caps):
        raise error_type("bounded write/test capabilities require at least one explicit writable root")
    return BackendSelection(
        runner_id=runner_id, capabilities=effective_caps, writable_roots=writable_roots,
        timeout_seconds=timeout_seconds, readable_root=readable_root,
        write_scope_binding_id=(str(write_scope_binding_id).strip() or None
                                if write_scope_binding_id is not None else None),
    )
