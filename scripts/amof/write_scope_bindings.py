"""Durable WriteScopeBinding + execution bind gate (Wave 3).

Runtime binds exactly one valid Approval to exactly one mutating execution
attempt. Binding is created by Runtime before worker mutation authority.

Wave 3 semantics
----------------
- Binding creation does NOT consume the Approval (consumption is Wave 4).
- Active Binding reserves the Approval: a second bind fails.
- Approval with any Binding in active|completed cannot re-bind.
- Failed/revoked/suspended bindings do not permanently exhaust the Approval
  for retry (still subject to Approval TTL/revoke and single-active-target).
- On bind failure the Approval stays approved if unused.
- base_sha is checked with real ``git rev-parse HEAD`` (no silent refresh).
- Repo-relative allowed_roots resolve to absolute writable roots.
- Legacy ``--approve-writable-root`` never mints Approval/Binding evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .app_paths import ensure_parent_dir, write_scope_bindings_dir, write_scope_events_dir
from .write_scope_approvals import (
    STATUS_APPROVED,
    STATUS_CONSUMED,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    WriteScopeApprovalError,
    append_scope_event,
    compute_body_hash,
    is_approval_active,
    load_approval,
    validate_approval_body,
    verify_approval_record,
)
from .write_scope_proposals import utc_now_iso

BINDING_KIND = "write_scope_binding"
BINDING_SCHEMA_VERSION = 1
BINDING_ID_PREFIX = "wsb-"

STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_REVOKED = "revoked"
STATUS_SUSPENDED = "suspended"

BINDING_STATUSES = frozenset(
    {
        STATUS_ACTIVE,
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_REVOKED,
        STATUS_SUSPENDED,
    }
)
TERMINAL_BINDING_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_REVOKED}
)
# Approvals with these binding statuses cannot create another binding.
# Suspended (crash/recover) also blocks until recover terminalizes the Binding.
APPROVAL_BLOCKING_BINDING_STATUSES = frozenset(
    {STATUS_ACTIVE, STATUS_COMPLETED, STATUS_SUSPENDED}
)

BINDING_LIFECYCLE_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({STATUS_ACTIVE}),
    STATUS_ACTIVE: frozenset(
        {STATUS_COMPLETED, STATUS_FAILED, STATUS_REVOKED, STATUS_SUSPENDED}
    ),
    STATUS_SUSPENDED: frozenset(
        {STATUS_COMPLETED, STATUS_FAILED, STATUS_REVOKED}
    ),
    STATUS_COMPLETED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_REVOKED: frozenset(),
}

LEGACY_PATH_ELEVATION_WARNING = (
    "WARNING: --approve-writable-root is a deprecated path-elevation "
    "compatibility shim. It does NOT create WriteScopeApproval or "
    "WriteScopeBinding evidence and is not the happy path. "
    "Prefer --write-scope-approval <approval-id>."
)

_STORE_LOCK = threading.RLock()


class WriteScopeBindingError(ValueError):
    """Raised when a binding cannot be created or loaded truthfully."""


@dataclass(frozen=True)
class BindGateResult:
    """Result of a successful Runtime bind before mutation authority."""

    binding: dict[str, Any]
    approval: dict[str, Any]
    writable_roots: list[str]
    denied_roots: list[str]
    target_id: str
    base_sha: str
    workspace_root: str


def emit_legacy_path_elevation_warning(*, stream: TextIO | None = None) -> None:
    """Emit the deprecated naked-root warning (never mints authority records)."""
    handle = stream if stream is not None else sys.stderr
    handle.write(LEGACY_PATH_ELEVATION_WARNING + "\n")
    handle.flush()


def compute_binding_id(
    *,
    approval_id: str,
    run_id: str,
    bound_at: str,
    target_id: str,
) -> str:
    material = f"{approval_id}:{run_id}:{bound_at}:{target_id}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"{BINDING_ID_PREFIX}{digest}"


def binding_path(binding_id: str, *, base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else write_scope_bindings_dir()
    return root / f"{binding_id}.json"


def _assert_binding_transition(from_status: str | None, to_status: str) -> None:
    allowed = BINDING_LIFECYCLE_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise WriteScopeBindingError(
            f"illegal binding lifecycle transition: {from_status!r} -> {to_status!r}"
        )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent_dir(path)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(path)
        os.chmod(path, 0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def git_rev_parse_head(workspace_root: Path) -> str:
    """Return current HEAD as 40-hex via real git. Fail closed on error."""
    root = Path(workspace_root).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise WriteScopeBindingError(f"workspace_root is not a directory: {root}")
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise WriteScopeBindingError(
            f"git rev-parse failed for workspace {root}: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise WriteScopeBindingError(
            f"git rev-parse HEAD failed in {root}: {detail or 'unknown error'}"
        )
    sha = (completed.stdout or "").strip().lower()
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise WriteScopeBindingError(
            f"git rev-parse HEAD returned non-40-hex value: {sha!r}"
        )
    return sha


def _resolve_repo_relative_roots(
    roots: list[str],
    *,
    workspace_root: Path,
    field_name: str,
    require_non_empty: bool,
) -> list[str]:
    """Resolve repo-relative roots to absolute paths under workspace_root."""
    root = Path(workspace_root).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise WriteScopeBindingError(f"workspace_root is not a directory: {root}")
    if require_non_empty and not roots:
        raise WriteScopeBindingError(f"{field_name} must be non-empty for bind")

    resolved: list[str] = []
    for raw in roots or []:
        rel = str(raw or "").strip()
        if not rel:
            raise WriteScopeBindingError(f"{field_name} entry must be non-empty")
        if rel.startswith("/") or rel.startswith("~"):
            raise WriteScopeBindingError(
                f"{field_name} must be repo-relative at bind time; got {rel!r}"
            )
        if ".." in Path(rel).parts:
            raise WriteScopeBindingError(
                f"{field_name} path traversal rejected: {rel!r}"
            )
        absolute = (root / rel).resolve(strict=False)
        try:
            absolute.relative_to(root)
        except ValueError as exc:
            raise WriteScopeBindingError(
                f"{field_name} escape workspace_root: {rel!r}"
            ) from exc
        resolved.append(str(absolute))
    return resolved


def resolve_writable_roots(
    allowed_roots: list[str],
    *,
    workspace_root: Path,
) -> list[str]:
    """Resolve repo-relative allowed_roots to absolute writable roots.

    Fail closed on empty roots, absolute inputs, or traversal outside workspace.
    """
    return _resolve_repo_relative_roots(
        allowed_roots,
        workspace_root=workspace_root,
        field_name="allowed_roots",
        require_non_empty=True,
    )


def resolve_denied_roots(
    denied_roots: list[str],
    *,
    workspace_root: Path,
) -> list[str]:
    """Resolve repo-relative denied_roots to absolute paths (may be empty)."""
    return _resolve_repo_relative_roots(
        denied_roots,
        workspace_root=workspace_root,
        field_name="denied_roots",
        require_non_empty=False,
    )


def resolve_workspace_for_target(
    target_id: str,
    *,
    workspace_root: Path | None = None,
    manifest: dict[str, Any] | None = None,
) -> Path:
    """Locate the workspace directory for a target_id.

    Preference: matching manifest repo path, else explicit workspace_root.
    """
    wanted = str(target_id or "").strip()
    if not wanted:
        raise WriteScopeBindingError("target_id is required")

    if isinstance(manifest, dict):
        repos = manifest.get("repos")
        if isinstance(repos, list):
            matches: list[Path] = []
            for repo in repos:
                if not isinstance(repo, dict):
                    continue
                if str(repo.get("target_id") or "").strip() != wanted:
                    continue
                path_text = str(repo.get("path") or "").strip()
                if path_text:
                    matches.append(Path(path_text).expanduser().resolve(strict=False))
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise WriteScopeBindingError(
                    f"ambiguous workspace for target_id {wanted!r}"
                )

    if workspace_root is not None:
        path = Path(workspace_root).expanduser().resolve(strict=False)
        if path.is_dir():
            return path
        raise WriteScopeBindingError(f"workspace_root is not a directory: {path}")

    raise WriteScopeBindingError(
        f"cannot resolve workspace for target_id {wanted!r}; "
        "pass workspace_root or a manifest repo with matching target_id"
    )


def verify_binding_record(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise WriteScopeBindingError("binding record is not an object")
    if record.get("kind") != BINDING_KIND:
        raise WriteScopeBindingError("binding kind mismatch")
    if record.get("schema_version") != BINDING_SCHEMA_VERSION:
        raise WriteScopeBindingError("unsupported binding schema_version")

    binding_id = str(record.get("binding_id") or "").strip()
    approval_id = str(record.get("approval_id") or "").strip()
    run_id = str(record.get("run_id") or "").strip()
    target_id = str(record.get("target_id") or "").strip()
    bound_at = str(record.get("bound_at") or "").strip()
    status = str(record.get("status") or "").strip()
    body_hash = str(record.get("body_hash") or "").strip()
    base_sha = str(record.get("base_sha") or "").strip().lower()
    workspace_root = str(record.get("workspace_root") or "").strip()
    writable_roots = record.get("writable_roots")

    if not binding_id or not approval_id or not run_id or not target_id:
        raise WriteScopeBindingError("binding identity fields are required")
    if not bound_at:
        raise WriteScopeBindingError("bound_at is required")
    if status not in BINDING_STATUSES:
        raise WriteScopeBindingError(f"unsupported binding status: {status!r}")
    if not body_hash.startswith("sha256:") or len(body_hash) != len("sha256:") + 64:
        raise WriteScopeBindingError(f"invalid body_hash: {body_hash!r}")
    if len(base_sha) != 40 or any(ch not in "0123456789abcdef" for ch in base_sha):
        raise WriteScopeBindingError(f"invalid base_sha: {base_sha!r}")
    if not workspace_root:
        raise WriteScopeBindingError("workspace_root is required")
    if not isinstance(writable_roots, list) or not writable_roots:
        raise WriteScopeBindingError("writable_roots must be a non-empty list")
    abs_roots: list[str] = []
    for item in writable_roots:
        text = str(item or "").strip()
        if not text.startswith("/"):
            raise WriteScopeBindingError(
                f"writable_roots must be absolute paths; got {text!r}"
            )
        abs_roots.append(text)
    denied_roots = record.get("denied_roots")
    if denied_roots is None:
        denied_roots = []
    if not isinstance(denied_roots, list):
        raise WriteScopeBindingError("denied_roots must be a list")
    abs_denied: list[str] = []
    for item in denied_roots:
        text = str(item or "").strip()
        if not text.startswith("/"):
            raise WriteScopeBindingError(
                f"denied_roots must be absolute paths; got {text!r}"
            )
        abs_denied.append(text)

    expected_id = compute_binding_id(
        approval_id=approval_id,
        run_id=run_id,
        bound_at=bound_at,
        target_id=target_id,
    )
    if binding_id != expected_id:
        raise WriteScopeBindingError(
            f"binding_id mismatch: expected {expected_id}, got {binding_id}"
        )

    verified: dict[str, Any] = {
        "kind": BINDING_KIND,
        "schema_version": BINDING_SCHEMA_VERSION,
        "binding_id": binding_id,
        "approval_id": approval_id,
        "run_id": run_id,
        "target_id": target_id,
        "bound_at": bound_at,
        "status": status,
        "body_hash": body_hash,
        "base_sha": base_sha,
        "workspace_root": workspace_root,
        "writable_roots": abs_roots,
        "denied_roots": abs_denied,
    }
    runner_id = record.get("runner_id")
    if runner_id is not None:
        runner_text = str(runner_id).strip()
        verified["runner_id"] = runner_text or None
    else:
        verified["runner_id"] = None
    if record.get("terminal_at") is not None:
        verified["terminal_at"] = str(record.get("terminal_at") or "").strip()
    if record.get("terminal_reason") is not None:
        verified["terminal_reason"] = str(record.get("terminal_reason") or "")
    return verified


def list_bindings(
    *,
    approval_id: str | None = None,
    run_id: str | None = None,
    target_id: str | None = None,
    status: str | None = None,
    bindings_dir: Path | None = None,
) -> list[dict[str, Any]]:
    root = bindings_dir if bindings_dir is not None else write_scope_bindings_dir()
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.glob("wsb-*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            record = verify_binding_record(raw)
        except (OSError, json.JSONDecodeError, WriteScopeBindingError):
            continue
        if approval_id is not None and record["approval_id"] != approval_id:
            continue
        if run_id is not None and record["run_id"] != run_id:
            continue
        if target_id is not None and record["target_id"] != target_id:
            continue
        if status is not None and record["status"] != status:
            continue
        items.append(record)
    items.sort(key=lambda item: (item.get("bound_at") or "", item["binding_id"]))
    return items


def load_binding(
    binding_id: str,
    *,
    bindings_dir: Path | None = None,
) -> dict[str, Any]:
    ref = str(binding_id or "").strip()
    if not ref:
        raise WriteScopeBindingError("binding_id is required")
    path = binding_path(ref, base_dir=bindings_dir)
    if not path.is_file():
        raise WriteScopeBindingError(f"binding not found: {ref}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WriteScopeBindingError(f"corrupt binding record: {ref}") from exc
    return verify_binding_record(raw)


def save_binding(
    record: dict[str, Any],
    *,
    bindings_dir: Path | None = None,
    events_dir: Path | None = None,
    emit_event: bool = True,
) -> dict[str, Any]:
    verified = verify_binding_record(record)
    path = binding_path(verified["binding_id"], base_dir=bindings_dir)
    with _STORE_LOCK:
        if path.exists():
            existing = load_binding(verified["binding_id"], bindings_dir=bindings_dir)
            if (
                existing["approval_id"] == verified["approval_id"]
                and existing["run_id"] == verified["run_id"]
                and existing["bound_at"] == verified["bound_at"]
                and existing["target_id"] == verified["target_id"]
            ):
                return existing
            raise WriteScopeBindingError(
                f"binding_id collision with different content: {verified['binding_id']}"
            )
        _atomic_write_json(path, verified)
    if emit_event:
        append_scope_event(
            "write_scope.bound",
            {
                "binding_id": verified["binding_id"],
                "approval_id": verified["approval_id"],
                "run_id": verified["run_id"],
                "target_id": verified["target_id"],
                "status": verified["status"],
                "base_sha": verified["base_sha"],
            },
            events_dir=events_dir if events_dir is not None else write_scope_events_dir(),
            at=verified["bound_at"],
        )
    return verified


def approval_has_blocking_binding(
    approval_id: str,
    *,
    bindings_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return active/completed binding for approval if single-use is exhausted."""
    for record in list_bindings(approval_id=approval_id, bindings_dir=bindings_dir):
        if record["status"] in APPROVAL_BLOCKING_BINDING_STATUSES:
            return record
    return None


def active_binding_for_target(
    target_id: str,
    *,
    bindings_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return active or suspended mutating Binding for target (blocks new bind)."""
    for status in (STATUS_ACTIVE, STATUS_SUSPENDED):
        for record in list_bindings(
            target_id=target_id,
            status=status,
            bindings_dir=bindings_dir,
        ):
            return record
    return None


def transition_binding_status(
    binding_id: str,
    to_status: str,
    *,
    reason: str | None = None,
    bindings_dir: Path | None = None,
    events_dir: Path | None = None,
    terminal_at: str | None = None,
) -> dict[str, Any]:
    """Move an active/suspended binding to a later status (Wave 3 terminal hook)."""
    target = str(to_status or "").strip()
    if target not in BINDING_STATUSES:
        raise WriteScopeBindingError(f"unsupported binding status: {target!r}")
    with _STORE_LOCK:
        record = load_binding(binding_id, bindings_dir=bindings_dir)
        _assert_binding_transition(record["status"], target)
        updated = dict(record)
        updated["status"] = target
        if target != STATUS_ACTIVE:
            updated["terminal_at"] = terminal_at or utc_now_iso()
        if reason is not None:
            updated["terminal_reason"] = str(reason)
        verified = verify_binding_record(updated)
        _atomic_write_json(
            binding_path(verified["binding_id"], base_dir=bindings_dir),
            verified,
        )
    append_scope_event(
        "write_scope.binding_status",
        {
            "binding_id": verified["binding_id"],
            "approval_id": verified["approval_id"],
            "from_status": record["status"],
            "to_status": verified["status"],
            "reason": reason,
        },
        events_dir=events_dir if events_dir is not None else write_scope_events_dir(),
        at=verified.get("terminal_at") or utc_now_iso(),
    )
    return verified


def finalize_binding(
    binding_id: str,
    *,
    success: bool,
    reason: str | None = None,
    bindings_dir: Path | None = None,
    events_dir: Path | None = None,
) -> dict[str, Any]:
    """Mark binding completed (success) or failed. Does not consume Approval."""
    return transition_binding_status(
        binding_id,
        STATUS_COMPLETED if success else STATUS_FAILED,
        reason=reason,
        bindings_dir=bindings_dir,
        events_dir=events_dir,
    )


def create_binding(
    approval_id: str,
    *,
    run_id: str,
    workspace_root: Path | None = None,
    manifest: dict[str, Any] | None = None,
    execution_target_id: str | None = None,
    requested_capabilities: list[str] | None = None,
    runner_id: str | None = None,
    require_bounded_write: bool = True,
    bound_at: str | None = None,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
    bindings_dir: Path | None = None,
) -> BindGateResult:
    """Create an active Binding after fail-closed re-checks.

    Runtime-only. Does not consume the Approval (Wave 4).
    """
    approval_ref = str(approval_id or "").strip()
    run_ref = str(run_id or "").strip()
    if not approval_ref:
        raise WriteScopeBindingError("approval_id is required")
    if not run_ref:
        raise WriteScopeBindingError("run_id is required")

    try:
        approval = load_approval(
            approval_ref,
            approvals_dir=approvals_dir,
            events_dir=events_dir,
            evaluate_ttl=True,
            persist_expiry=True,
        )
    except WriteScopeApprovalError as exc:
        raise WriteScopeBindingError(str(exc)) from exc

    # Re-verify integrity and active grant.
    try:
        verified_approval = verify_approval_record(approval, evaluate_ttl=True)
    except WriteScopeApprovalError as exc:
        raise WriteScopeBindingError(str(exc)) from exc

    status = verified_approval["status"]
    if status == STATUS_EXPIRED:
        raise WriteScopeBindingError(
            f"approval expired; cannot bind: {approval_ref}"
        )
    if status == STATUS_REVOKED:
        raise WriteScopeBindingError(
            f"approval revoked; cannot bind: {approval_ref}"
        )
    if status == STATUS_CONSUMED:
        raise WriteScopeBindingError(
            f"approval consumed; cannot bind: {approval_ref}"
        )
    if status != STATUS_APPROVED or not is_approval_active(verified_approval):
        raise WriteScopeBindingError(
            f"approval is not active for bind: {approval_ref} status={status}"
        )

    body = validate_approval_body(verified_approval["body"])
    body_hash = compute_body_hash(body)
    if body_hash != verified_approval["body_hash"]:
        raise WriteScopeBindingError(
            f"approval body_hash integrity failure: {approval_ref}"
        )

    target_id = str(body["target_id"])
    expected_target = (
        str(execution_target_id).strip()
        if execution_target_id is not None
        else target_id
    )
    if not expected_target:
        raise WriteScopeBindingError("execution target_id is required")
    if expected_target != target_id:
        raise WriteScopeBindingError(
            f"target_id mismatch: approval={target_id!r} execution={expected_target!r}"
        )

    workspace = resolve_workspace_for_target(
        target_id,
        workspace_root=workspace_root,
        manifest=manifest,
    )
    current_sha = git_rev_parse_head(workspace)
    approved_sha = str(body["base_sha"]).lower()
    if current_sha != approved_sha:
        raise WriteScopeBindingError(
            f"base_sha mismatch: approval={approved_sha} "
            f"workspace_head={current_sha} (no silent retarget)"
        )

    caps = [
        str(item).strip()
        for item in (requested_capabilities or [])
        if str(item).strip()
    ]
    if require_bounded_write and "bounded_write" not in caps:
        raise WriteScopeBindingError(
            "bounded_write capability approval is required to bind a mutating write scope"
        )

    with _STORE_LOCK:
        blocking = approval_has_blocking_binding(
            approval_ref, bindings_dir=bindings_dir
        )
        if blocking is not None:
            raise WriteScopeBindingError(
                f"approval already bound ({blocking['status']}): "
                f"approval_id={approval_ref} binding_id={blocking['binding_id']}"
            )
        active = active_binding_for_target(target_id, bindings_dir=bindings_dir)
        if active is not None:
            raise WriteScopeBindingError(
                f"active mutating binding already exists for target_id={target_id!r}: "
                f"binding_id={active['binding_id']}"
            )

        writable_roots = resolve_writable_roots(
            list(body["allowed_roots"]),
            workspace_root=workspace,
        )
        denied_roots = resolve_denied_roots(
            list(body.get("denied_roots") or []),
            workspace_root=workspace,
        )
        bound_ts = bound_at or utc_now_iso()
        _assert_binding_transition(None, STATUS_ACTIVE)
        binding_id = compute_binding_id(
            approval_id=approval_ref,
            run_id=run_ref,
            bound_at=bound_ts,
            target_id=target_id,
        )
        record = {
            "kind": BINDING_KIND,
            "schema_version": BINDING_SCHEMA_VERSION,
            "binding_id": binding_id,
            "approval_id": approval_ref,
            "run_id": run_ref,
            "target_id": target_id,
            "runner_id": (str(runner_id).strip() or None) if runner_id else None,
            "bound_at": bound_ts,
            "status": STATUS_ACTIVE,
            "body_hash": body_hash,
            "base_sha": approved_sha,
            "workspace_root": str(workspace),
            "writable_roots": writable_roots,
            "denied_roots": denied_roots,
        }
        saved = save_binding(
            record,
            bindings_dir=bindings_dir,
            events_dir=events_dir,
            emit_event=True,
        )

    return BindGateResult(
        binding=saved,
        approval=verified_approval,
        writable_roots=list(saved["writable_roots"]),
        denied_roots=list(saved.get("denied_roots") or []),
        target_id=target_id,
        base_sha=approved_sha,
        workspace_root=str(workspace),
    )


def prepare_execution_write_scope(
    *,
    write_scope_approval: str | None,
    approve_writable_roots: list[str] | None,
    run_id: str,
    workspace_root: Path | None = None,
    manifest: dict[str, Any] | None = None,
    execution_target_id: str | None = None,
    requested_capabilities: list[str] | None = None,
    runner_id: str | None = None,
    legacy_path_elevation: bool = False,
    require_bounded_write: bool = True,
    warn_stream: TextIO | None = None,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
    bindings_dir: Path | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Resolve mutation roots for an execution attempt.

    Returns (writable_roots, binding_or_none).

    - ``--write-scope-approval``: bind gate (creates Binding).
    - naked ``--approve-writable-root``: deprecated warning; never mints
      Approval/Binding. Allowed with warning (compatibility). Optional
      ``legacy_path_elevation`` documents explicit opt-in but is not required
      for Wave 3 to avoid breaking existing callers.
    - neither: empty roots (read-only).
    """
    approval_ref = str(write_scope_approval or "").strip() or None
    naked_roots = [
        str(item).strip()
        for item in (approve_writable_roots or [])
        if str(item).strip()
    ]

    if approval_ref and naked_roots:
        raise WriteScopeBindingError(
            "refuse mixed authority: pass --write-scope-approval OR "
            "--approve-writable-root, not both"
        )

    if approval_ref:
        result = create_binding(
            approval_ref,
            run_id=run_id,
            workspace_root=workspace_root,
            manifest=manifest,
            execution_target_id=execution_target_id,
            requested_capabilities=requested_capabilities,
            runner_id=runner_id,
            require_bounded_write=require_bounded_write,
            approvals_dir=approvals_dir,
            events_dir=events_dir,
            bindings_dir=bindings_dir,
        )
        return list(result.writable_roots), result.binding

    if naked_roots:
        # Deprecated compatibility: warn, allow, never fabricate authority.
        _ = legacy_path_elevation  # explicit flag reserved; warning always emitted
        emit_legacy_path_elevation_warning(stream=warn_stream)
        return naked_roots, None

    return [], None


__all__ = [
    "APPROVAL_BLOCKING_BINDING_STATUSES",
    "BINDING_ID_PREFIX",
    "BINDING_KIND",
    "BINDING_LIFECYCLE_TRANSITIONS",
    "BINDING_SCHEMA_VERSION",
    "BINDING_STATUSES",
    "BindGateResult",
    "LEGACY_PATH_ELEVATION_WARNING",
    "STATUS_ACTIVE",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_REVOKED",
    "STATUS_SUSPENDED",
    "TERMINAL_BINDING_STATUSES",
    "WriteScopeBindingError",
    "active_binding_for_target",
    "approval_has_blocking_binding",
    "binding_path",
    "compute_binding_id",
    "create_binding",
    "emit_legacy_path_elevation_warning",
    "finalize_binding",
    "git_rev_parse_head",
    "list_bindings",
    "load_binding",
    "prepare_execution_write_scope",
    "resolve_denied_roots",
    "resolve_writable_roots",
    "resolve_workspace_for_target",
    "save_binding",
    "transition_binding_status",
    "verify_binding_record",
]
