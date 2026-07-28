"""Runtime write-scope enforcement + MutationReceipt (Wave 4).

Prompts are not enforcement. Runtime classifies changed paths against
effective allowed roots (allowed_roots minus denied_roots; deny wins),
restores out-of-scope mutations where technically possible, emits a
MutationReceipt, and transitions Binding/Approval terminal authority.

Does NOT claim atomic rollback.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .app_paths import (
    ensure_parent_dir,
    write_scope_events_dir,
    write_scope_receipts_dir,
)
from .write_scope_approvals import (
    STATUS_APPROVED,
    STATUS_CONSUMED,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    WriteScopeApprovalError,
    append_scope_event,
    is_approval_active,
    load_approval,
    revoke_approval,
    transition_approval_status,
    validate_approval_body,
)
from .write_scope_bindings import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_SUSPENDED,
    WriteScopeBindingError,
    git_rev_parse_head,
    load_binding,
    resolve_denied_roots,
    resolve_writable_roots,
    transition_binding_status,
)
from .write_scope_proposals import utc_now_iso

RECEIPT_KIND = "mutation_receipt"
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_ID_PREFIX = "wmr-"

COMPLIANCE_WITHIN_SCOPE = "within_scope"
COMPLIANCE_SCOPE_EXCEEDED = "scope_exceeded"
COMPLIANCE_BASE_SHA_MISMATCH = "base_sha_mismatch"
COMPLIANCE_NO_MUTATION = "no_mutation"
COMPLIANCE_PARTIAL = "partial"

COMPLIANCE_VALUES = frozenset(
    {
        COMPLIANCE_WITHIN_SCOPE,
        COMPLIANCE_SCOPE_EXCEEDED,
        COMPLIANCE_BASE_SHA_MISMATCH,
        COMPLIANCE_NO_MUTATION,
        COMPLIANCE_PARTIAL,
    }
)

RUNTIME_BREACH_REVOKER = "runtime:write-scope-enforcement"
STOP_SCOPE_EXCEEDED = "write_scope_exceeded"
STOP_BASE_SHA_MISMATCH = "write_scope_base_sha_mismatch"
STOP_EXPIRED = "write_scope_expired"
STOP_REVOKED = "write_scope_revoked"
STOP_PARTIAL = "write_scope_partial"

_STORE_LOCK = threading.RLock()


class WriteScopeEnforcementError(ValueError):
    """Raised when enforcement cannot evaluate truthfully."""


@dataclass(frozen=True)
class ScopeRoots:
    """Absolute allowed/denied roots for path-prefix checks."""

    workspace_root: Path
    allowed_roots: list[Path]
    denied_roots: list[Path]


@dataclass(frozen=True)
class EnforcementOutcome:
    """Result of terminal write-scope enforcement."""

    receipt: dict[str, Any]
    binding: dict[str, Any]
    approval: dict[str, Any]
    run_failed: bool
    stop_reason: str | None
    remaining_changed_paths: list[str]


def compute_receipt_id(
    *,
    binding_id: str,
    run_id: str,
    evaluated_at: str,
    compliance: str,
) -> str:
    material = f"{binding_id}:{run_id}:{evaluated_at}:{compliance}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"{RECEIPT_ID_PREFIX}{digest}"


def receipt_path(receipt_id: str, *, base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else write_scope_receipts_dir()
    return root / f"{receipt_id}.json"


def load_scope_roots_for_binding(
    binding: dict[str, Any],
    *,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
) -> ScopeRoots:
    """Load absolute allowed/denied roots for a Binding from its Approval body."""
    try:
        approval = load_approval(
            binding["approval_id"],
            approvals_dir=approvals_dir,
            events_dir=events_dir,
            evaluate_ttl=True,
            persist_expiry=True,
        )
    except WriteScopeApprovalError as exc:
        raise WriteScopeEnforcementError(str(exc)) from exc
    body = validate_approval_body(approval["body"])
    workspace = Path(str(binding.get("workspace_root") or "")).expanduser().resolve(
        strict=False
    )
    try:
        allowed = [
            Path(p)
            for p in resolve_writable_roots(
                list(body["allowed_roots"]),
                workspace_root=workspace,
            )
        ]
        denied = [
            Path(p)
            for p in resolve_denied_roots(
                list(body.get("denied_roots") or []),
                workspace_root=workspace,
            )
        ]
    except WriteScopeBindingError as exc:
        raise WriteScopeEnforcementError(str(exc)) from exc
    # Prefer binding-stored writable_roots when present (bind-time freeze).
    stored = binding.get("writable_roots")
    if isinstance(stored, list) and stored:
        allowed = [Path(str(p)).resolve(strict=False) for p in stored]
    stored_denied = binding.get("denied_roots")
    if isinstance(stored_denied, list):
        denied = [Path(str(p)).resolve(strict=False) for p in stored_denied]
    return ScopeRoots(
        workspace_root=workspace,
        allowed_roots=allowed,
        denied_roots=denied,
    )


def _path_matches_root(abs_path: Path, root: Path) -> bool:
    """Path-prefix match: exact root or descendant."""
    try:
        abs_path.relative_to(root)
        return True
    except ValueError:
        return False


def normalize_workspace_path(
    path_text: str,
    *,
    workspace_root: Path,
) -> tuple[Path | None, str | None, str | None]:
    """Normalize a changed path against traversal/symlink escape.

    Returns (absolute_resolved, repo_relative, error_reason).
    On escape/traversal, absolute/repo_relative are None and error_reason set.
    """
    raw = str(path_text or "").strip()
    if not raw:
        return None, None, "empty_path"
    workspace = Path(workspace_root).expanduser().resolve(strict=False)
    if raw.startswith("/"):
        candidate = Path(raw)
    else:
        # Reject explicit .. segments before join (path traversal).
        parts = Path(raw).parts
        if ".." in parts:
            return None, None, "path_traversal"
        candidate = workspace / raw
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None, None, "path_resolve_failed"
    try:
        rel = resolved.relative_to(workspace)
    except ValueError:
        return None, None, "symlink_or_escape_outside_workspace"
    # Symlink escape: if any parent of the unre solved path is a symlink that
    # leaves the workspace, resolve already caught it via relative_to failure.
    # Extra check: when the path exists as a symlink, ensure the symlink target
    # stays inside the workspace.
    probe = candidate
    try:
        if probe.is_symlink():
            target = probe.resolve(strict=False)
            target.relative_to(workspace)
    except (OSError, ValueError):
        return None, None, "symlink_escape"
    return resolved, str(rel).replace("\\", "/"), None


def path_is_within_effective_scope(
    path_text: str,
    *,
    scope: ScopeRoots,
) -> tuple[bool, str | None, str | None]:
    """Return (in_scope, repo_relative_or_raw, reject_reason)."""
    absolute, rel, err = normalize_workspace_path(
        path_text, workspace_root=scope.workspace_root
    )
    if err is not None or absolute is None:
        return False, (rel or str(path_text or "").strip() or None), err
    under_allowed = any(
        _path_matches_root(absolute, root) for root in scope.allowed_roots
    )
    if not under_allowed:
        return False, rel, "outside_allowed_roots"
    under_denied = any(
        _path_matches_root(absolute, root) for root in scope.denied_roots
    )
    if under_denied:
        return False, rel, "denied_root"
    return True, rel, None


def classify_changed_paths(
    changed_paths: list[str],
    *,
    scope: ScopeRoots,
) -> tuple[list[str], list[str]]:
    """Split changed paths into (in_scope, out_of_scope) repo-relative lists."""
    in_scope: list[str] = []
    out_of_scope: list[str] = []
    seen: set[str] = set()
    for raw in changed_paths or []:
        ok, rel, _err = path_is_within_effective_scope(raw, scope=scope)
        key = rel or str(raw).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        if ok:
            in_scope.append(key)
        else:
            out_of_scope.append(key)
    return in_scope, out_of_scope


def restore_paths(workspace_root: Path, paths: list[str]) -> list[str]:
    """Best-effort restore of repo-relative paths (not atomic rollback).

    Reuses the read-only restore pattern: git restore for tracked files;
    unlink/rmtree for untracked paths.
    """
    restored: list[str] = []
    root = Path(workspace_root).expanduser().resolve(strict=False)
    if not paths:
        return restored
    for rel_path in sorted({str(item).strip() for item in paths if str(item).strip()}):
        target = root / rel_path
        tracked = (
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", rel_path],
                cwd=str(root),
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            ).returncode
            == 0
        )
        if tracked:
            dirty = subprocess.run(
                [
                    "git",
                    "status",
                    "--short",
                    "--untracked-files=all",
                    "--",
                    rel_path,
                ],
                cwd=str(root),
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            ).stdout.strip()
            if not dirty:
                continue
            subprocess.run(
                ["git", "restore", "--staged", "--worktree", "--", rel_path],
                cwd=str(root),
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


def verify_receipt_record(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise WriteScopeEnforcementError("mutation receipt is not an object")
    if record.get("kind") != RECEIPT_KIND:
        raise WriteScopeEnforcementError("mutation receipt kind mismatch")
    if record.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise WriteScopeEnforcementError("unsupported mutation receipt schema_version")
    receipt_id = str(record.get("receipt_id") or "").strip()
    run_id = str(record.get("run_id") or "").strip()
    binding_id = str(record.get("binding_id") or "").strip()
    approval_id = str(record.get("approval_id") or "").strip()
    target_id = str(record.get("target_id") or "").strip()
    base_sha = str(record.get("base_sha") or "").strip().lower()
    compliance = str(record.get("compliance") or "").strip()
    binding_status = str(record.get("binding_status") or "").strip()
    approval_status = str(record.get("approval_status") or "").strip()
    created_at = str(record.get("created_at") or "").strip()
    evaluated_at = str(record.get("evaluated_at") or "").strip()
    if not receipt_id or not run_id or not binding_id or not approval_id:
        raise WriteScopeEnforcementError("mutation receipt identity fields required")
    if not target_id or not created_at or not evaluated_at:
        raise WriteScopeEnforcementError("mutation receipt metadata fields required")
    if compliance not in COMPLIANCE_VALUES:
        raise WriteScopeEnforcementError(f"unsupported compliance: {compliance!r}")
    if len(base_sha) != 40 or any(ch not in "0123456789abcdef" for ch in base_sha):
        raise WriteScopeEnforcementError(f"invalid base_sha: {base_sha!r}")
    expected = compute_receipt_id(
        binding_id=binding_id,
        run_id=run_id,
        evaluated_at=evaluated_at,
        compliance=compliance,
    )
    if receipt_id != expected:
        raise WriteScopeEnforcementError(
            f"receipt_id mismatch: expected {expected}, got {receipt_id}"
        )

    def _str_list(key: str) -> list[str]:
        value = record.get(key)
        if not isinstance(value, list):
            raise WriteScopeEnforcementError(f"{key} must be a list")
        return [str(item).strip() for item in value if str(item).strip()]

    verified: dict[str, Any] = {
        "kind": RECEIPT_KIND,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "run_id": run_id,
        "binding_id": binding_id,
        "approval_id": approval_id,
        "target_id": target_id,
        "base_sha": base_sha,
        "changed_paths": _str_list("changed_paths"),
        "in_scope_paths": _str_list("in_scope_paths"),
        "out_of_scope_paths": _str_list("out_of_scope_paths"),
        "restored_paths": _str_list("restored_paths"),
        "compliance": compliance,
        "binding_status": binding_status,
        "approval_status": approval_status,
        "created_at": created_at,
        "evaluated_at": evaluated_at,
        "rollback_atomic": False,
    }
    if record.get("workspace_root") is not None:
        verified["workspace_root"] = str(record.get("workspace_root") or "").strip()
    if record.get("stop_reason") is not None:
        verified["stop_reason"] = (
            None
            if record.get("stop_reason") is None
            else str(record.get("stop_reason") or "")
        )
    if record.get("failure_reason") is not None:
        verified["failure_reason"] = (
            None
            if record.get("failure_reason") is None
            else str(record.get("failure_reason") or "")
        )
    if record.get("revocation_id") is not None:
        verified["revocation_id"] = (
            None
            if record.get("revocation_id") is None
            else str(record.get("revocation_id") or "")
        )
    if record.get("effective_allowed_roots") is not None:
        verified["effective_allowed_roots"] = _str_list("effective_allowed_roots")
    if record.get("denied_roots") is not None:
        verified["denied_roots"] = _str_list("denied_roots")
    return verified


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


def save_receipt(
    record: dict[str, Any],
    *,
    receipts_dir: Path | None = None,
    events_dir: Path | None = None,
) -> dict[str, Any]:
    verified = verify_receipt_record(record)
    path = receipt_path(verified["receipt_id"], base_dir=receipts_dir)
    with _STORE_LOCK:
        _atomic_write_json(path, verified)
    append_scope_event(
        "write_scope.mutation_receipt",
        {
            "receipt_id": verified["receipt_id"],
            "binding_id": verified["binding_id"],
            "approval_id": verified["approval_id"],
            "run_id": verified["run_id"],
            "compliance": verified["compliance"],
            "binding_status": verified["binding_status"],
            "approval_status": verified["approval_status"],
            "stop_reason": verified.get("stop_reason"),
        },
        events_dir=events_dir if events_dir is not None else write_scope_events_dir(),
        at=verified["evaluated_at"],
    )
    return verified


def load_receipt(
    receipt_id: str,
    *,
    receipts_dir: Path | None = None,
) -> dict[str, Any]:
    """Load and verify one MutationReceipt (fail closed on corrupt/missing)."""
    ref = str(receipt_id or "").strip()
    if not ref:
        raise WriteScopeEnforcementError("receipt_id is required")
    path = receipt_path(ref, base_dir=receipts_dir)
    if not path.is_file():
        raise WriteScopeEnforcementError(f"mutation receipt not found: {ref}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WriteScopeEnforcementError(f"corrupt mutation receipt: {ref}") from exc
    return verify_receipt_record(raw)


def list_receipts(
    *,
    binding_id: str | None = None,
    approval_id: str | None = None,
    run_id: str | None = None,
    receipts_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """List verified MutationReceipt records (skips corrupt files)."""
    root = receipts_dir if receipts_dir is not None else write_scope_receipts_dir()
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.glob("wmr-*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            record = verify_receipt_record(raw)
        except (OSError, json.JSONDecodeError, WriteScopeEnforcementError):
            continue
        if binding_id is not None and record["binding_id"] != binding_id:
            continue
        if approval_id is not None and record["approval_id"] != approval_id:
            continue
        if run_id is not None and record["run_id"] != run_id:
            continue
        items.append(record)
    items.sort(key=lambda item: (item.get("evaluated_at") or "", item["receipt_id"]))
    return items


def consume_approval(
    approval_id: str,
    *,
    reason: str,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
) -> dict[str, Any]:
    """Mark Approval consumed after successful in-scope mutation (single-use)."""
    updated = transition_approval_status(
        approval_id,
        STATUS_CONSUMED,
        approvals_dir=approvals_dir,
        events_dir=events_dir,
        allow_consumed=True,
    )
    append_scope_event(
        "write_scope.consumed",
        {
            "approval_id": updated["approval_id"],
            "reason": str(reason or "").strip() or "successful_in_scope_mutation",
        },
        events_dir=events_dir if events_dir is not None else write_scope_events_dir(),
    )
    return updated


def revoke_approval_for_breach(
    approval_id: str,
    *,
    reason: str,
    approvals_dir: Path | None = None,
    revocations_dir: Path | None = None,
    events_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Revoke Approval after scope breach — no residual mutation authority."""
    approval, revocation, _already = revoke_approval(
        approval_id,
        reason=str(reason or "").strip() or "scope_exceeded",
        revoked_by=RUNTIME_BREACH_REVOKER,
        approvals_dir=approvals_dir,
        revocations_dir=revocations_dir,
        events_dir=events_dir,
    )
    return approval, revocation


def check_authority_checkpoint(
    binding: dict[str, Any],
    *,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
    workspace_root: Path | None = None,
    check_base_sha: bool = True,
    now: datetime | None = None,
) -> tuple[str | None, str | None]:
    """Fail-closed authority checkpoint.

    Returns (stop_reason, detail) or (None, None) when still valid.
    """
    if binding.get("status") not in {STATUS_ACTIVE, STATUS_SUSPENDED}:
        return (
            "write_scope_binding_not_active",
            f"binding status={binding.get('status')!r}",
        )
    try:
        approval = load_approval(
            binding["approval_id"],
            approvals_dir=approvals_dir,
            events_dir=events_dir,
            now=now,
            evaluate_ttl=True,
            persist_expiry=True,
        )
    except WriteScopeApprovalError as exc:
        return STOP_REVOKED, str(exc)
    status = approval["status"]
    if status == STATUS_EXPIRED:
        return STOP_EXPIRED, f"approval expired: {approval['approval_id']}"
    if status == STATUS_REVOKED:
        return STOP_REVOKED, f"approval revoked: {approval['approval_id']}"
    if status == STATUS_CONSUMED:
        return "write_scope_approval_consumed", f"approval consumed: {approval['approval_id']}"
    if status != STATUS_APPROVED or not is_approval_active(approval):
        return STOP_REVOKED, f"approval not active: status={status}"
    if check_base_sha:
        workspace = Path(
            workspace_root
            if workspace_root is not None
            else binding.get("workspace_root") or ""
        )
        try:
            current = git_rev_parse_head(workspace)
        except WriteScopeBindingError as exc:
            return STOP_BASE_SHA_MISMATCH, str(exc)
        expected = str(binding.get("base_sha") or "").lower()
        if current != expected:
            return (
                STOP_BASE_SHA_MISMATCH,
                f"base_sha mismatch: approval={expected} workspace_head={current}",
            )
    return None, None


def enforce_write_scope_mutations(
    binding_id: str,
    *,
    changed_paths: list[str] | None,
    runner_failed: bool = False,
    runner_stop_reason: str | None = None,
    workspace_root: Path | None = None,
    restore_out_of_scope: bool = True,
    approvals_dir: Path | None = None,
    bindings_dir: Path | None = None,
    revocations_dir: Path | None = None,
    events_dir: Path | None = None,
    receipts_dir: Path | None = None,
    evaluated_at: str | None = None,
    now: datetime | None = None,
) -> EnforcementOutcome:
    """Terminal enforcement for an active WriteScopeBinding.

    Classifies mutations, restores out-of-scope paths when possible, emits
    MutationReceipt, and transitions Binding/Approval authority.
    """
    binding = load_binding(binding_id, bindings_dir=bindings_dir)
    evaluated_ts = evaluated_at or utc_now_iso()
    workspace = Path(
        workspace_root
        if workspace_root is not None
        else binding.get("workspace_root") or ""
    ).expanduser().resolve(strict=False)

    checkpoint_stop, checkpoint_detail = check_authority_checkpoint(
        binding,
        approvals_dir=approvals_dir,
        events_dir=events_dir,
        workspace_root=workspace,
        check_base_sha=True,
        now=now,
    )

    # Scope roots can be loaded from Binding-frozen writable/denied roots even
    # when the Approval has since expired/revoked.
    try:
        scope = load_scope_roots_for_binding(
            binding,
            approvals_dir=approvals_dir,
            events_dir=events_dir,
        )
    except WriteScopeEnforcementError:
        workspace_path = Path(str(binding.get("workspace_root") or workspace))
        scope = ScopeRoots(
            workspace_root=workspace_path,
            allowed_roots=[
                Path(p) for p in (binding.get("writable_roots") or [])
            ],
            denied_roots=[Path(p) for p in (binding.get("denied_roots") or [])],
        )
    raw_changed = [str(p).strip() for p in (changed_paths or []) if str(p).strip()]
    in_scope, out_of_scope = classify_changed_paths(raw_changed, scope=scope)
    # Preserve classification order but keep a stable union for changed_paths.
    classified_changed = list(dict.fromkeys([*in_scope, *out_of_scope]))

    restored: list[str] = []
    failure_reason: str | None = None
    stop_reason: str | None = None
    compliance: str
    run_failed = False
    revocation_id: str | None = None

    if checkpoint_stop == STOP_BASE_SHA_MISMATCH:
        compliance = COMPLIANCE_BASE_SHA_MISMATCH
        stop_reason = STOP_BASE_SHA_MISMATCH
        failure_reason = checkpoint_detail
        run_failed = True
        if out_of_scope and restore_out_of_scope:
            restored = restore_paths(workspace, out_of_scope)
    elif checkpoint_stop in {STOP_EXPIRED, STOP_REVOKED, "write_scope_approval_consumed", "write_scope_binding_not_active"}:
        # Authority died mid-flight: fail closed. Classify mutations honestly.
        if out_of_scope:
            compliance = COMPLIANCE_SCOPE_EXCEEDED
        elif classified_changed:
            compliance = COMPLIANCE_PARTIAL
        else:
            compliance = COMPLIANCE_NO_MUTATION
        stop_reason = checkpoint_stop
        failure_reason = checkpoint_detail
        run_failed = True
        if out_of_scope and restore_out_of_scope:
            restored = restore_paths(workspace, out_of_scope)
    elif out_of_scope:
        compliance = COMPLIANCE_SCOPE_EXCEEDED
        stop_reason = STOP_SCOPE_EXCEEDED
        failure_reason = (
            "out_of_scope_paths=" + ",".join(out_of_scope[:20])
        )
        run_failed = True
        if restore_out_of_scope:
            restored = restore_paths(workspace, out_of_scope)
    elif runner_failed:
        if classified_changed:
            compliance = COMPLIANCE_PARTIAL
            stop_reason = runner_stop_reason or STOP_PARTIAL
            failure_reason = runner_stop_reason or "runner_failed_with_mutations"
            run_failed = True
        else:
            compliance = COMPLIANCE_NO_MUTATION
            stop_reason = runner_stop_reason or "runner_failed"
            failure_reason = runner_stop_reason
            run_failed = True
    elif not classified_changed:
        compliance = COMPLIANCE_NO_MUTATION
        stop_reason = None
        run_failed = False
    else:
        compliance = COMPLIANCE_WITHIN_SCOPE
        stop_reason = None
        run_failed = False

    # Authority transitions.
    approval = load_approval(
        binding["approval_id"],
        approvals_dir=approvals_dir,
        events_dir=events_dir,
        now=now,
        evaluate_ttl=True,
        persist_expiry=True,
    )
    binding_status = binding["status"]
    approval_status = approval["status"]

    try:
        if compliance == COMPLIANCE_WITHIN_SCOPE:
            binding = transition_binding_status(
                binding_id,
                STATUS_COMPLETED,
                reason="within_scope_mutation",
                bindings_dir=bindings_dir,
                events_dir=events_dir,
                terminal_at=evaluated_ts,
            )
            if in_scope:
                approval = consume_approval(
                    binding["approval_id"],
                    reason="successful_in_scope_mutation",
                    approvals_dir=approvals_dir,
                    events_dir=events_dir,
                )
            binding_status = binding["status"]
            approval_status = approval["status"]
        elif compliance == COMPLIANCE_NO_MUTATION:
            # Prefer completed with no_mutation; Approval remains reusable.
            target_binding = STATUS_FAILED if run_failed else STATUS_COMPLETED
            reason = (
                failure_reason
                or stop_reason
                or ("runner_failed_no_mutation" if run_failed else "no_mutation")
            )
            if binding["status"] in {STATUS_ACTIVE, STATUS_SUSPENDED}:
                binding = transition_binding_status(
                    binding_id,
                    target_binding,
                    reason=reason,
                    bindings_dir=bindings_dir,
                    events_dir=events_dir,
                    terminal_at=evaluated_ts,
                )
            binding_status = binding["status"]
            approval_status = approval["status"]
        elif compliance == COMPLIANCE_SCOPE_EXCEEDED:
            if binding["status"] in {STATUS_ACTIVE, STATUS_SUSPENDED}:
                binding = transition_binding_status(
                    binding_id,
                    STATUS_FAILED,
                    reason=failure_reason or STOP_SCOPE_EXCEEDED,
                    bindings_dir=bindings_dir,
                    events_dir=events_dir,
                    terminal_at=evaluated_ts,
                )
            if approval_status == STATUS_APPROVED:
                approval, revocation = revoke_approval_for_breach(
                    binding["approval_id"],
                    reason=failure_reason or STOP_SCOPE_EXCEEDED,
                    approvals_dir=approvals_dir,
                    revocations_dir=revocations_dir,
                    events_dir=events_dir,
                )
                revocation_id = revocation["revocation_id"]
            binding_status = binding["status"]
            approval_status = approval["status"]
        elif compliance == COMPLIANCE_BASE_SHA_MISMATCH:
            if binding["status"] in {STATUS_ACTIVE, STATUS_SUSPENDED}:
                binding = transition_binding_status(
                    binding_id,
                    STATUS_FAILED,
                    reason=failure_reason or STOP_BASE_SHA_MISMATCH,
                    bindings_dir=bindings_dir,
                    events_dir=events_dir,
                    terminal_at=evaluated_ts,
                )
            # Approval left as-is (still sha-bound; rebind fails closed).
            binding_status = binding["status"]
            approval_status = approval["status"]
        elif compliance == COMPLIANCE_PARTIAL:
            if binding["status"] in {STATUS_ACTIVE, STATUS_SUSPENDED}:
                binding = transition_binding_status(
                    binding_id,
                    STATUS_FAILED,
                    reason=failure_reason or STOP_PARTIAL,
                    bindings_dir=bindings_dir,
                    events_dir=events_dir,
                    terminal_at=evaluated_ts,
                )
            # Any mutation under partial: no silent resume.
            if approval_status == STATUS_APPROVED and classified_changed:
                if out_of_scope:
                    approval, revocation = revoke_approval_for_breach(
                        binding["approval_id"],
                        reason=failure_reason or STOP_PARTIAL,
                        approvals_dir=approvals_dir,
                        revocations_dir=revocations_dir,
                        events_dir=events_dir,
                    )
                    revocation_id = revocation["revocation_id"]
                else:
                    approval = consume_approval(
                        binding["approval_id"],
                        reason=failure_reason or "partial_execution_with_mutations",
                        approvals_dir=approvals_dir,
                        events_dir=events_dir,
                    )
            binding_status = binding["status"]
            approval_status = approval["status"]
        else:
            raise WriteScopeEnforcementError(f"unhandled compliance: {compliance}")
    except (WriteScopeBindingError, WriteScopeApprovalError) as exc:
        # If Approval already expired/revoked, still fail Binding if active.
        failure_reason = f"{failure_reason or ''}; authority_transition: {exc}".strip(
            "; "
        )
        if binding.get("status") in {STATUS_ACTIVE, STATUS_SUSPENDED}:
            try:
                binding = transition_binding_status(
                    binding_id,
                    STATUS_FAILED,
                    reason=failure_reason,
                    bindings_dir=bindings_dir,
                    events_dir=events_dir,
                    terminal_at=evaluated_ts,
                )
            except WriteScopeBindingError:
                binding = load_binding(binding_id, bindings_dir=bindings_dir)
        binding_status = binding["status"]
        try:
            approval = load_approval(
                binding["approval_id"],
                approvals_dir=approvals_dir,
                events_dir=events_dir,
                evaluate_ttl=True,
                persist_expiry=True,
            )
            approval_status = approval["status"]
        except WriteScopeApprovalError:
            approval_status = approval_status
        run_failed = True

    remaining = list(in_scope) if compliance in {
        COMPLIANCE_WITHIN_SCOPE,
        COMPLIANCE_PARTIAL,
        COMPLIANCE_NO_MUTATION,
    } else list(in_scope)

    receipt = verify_receipt_record(
        {
            "kind": RECEIPT_KIND,
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "receipt_id": compute_receipt_id(
                binding_id=binding["binding_id"],
                run_id=binding["run_id"],
                evaluated_at=evaluated_ts,
                compliance=compliance,
            ),
            "run_id": binding["run_id"],
            "binding_id": binding["binding_id"],
            "approval_id": binding["approval_id"],
            "target_id": binding["target_id"],
            "base_sha": str(binding["base_sha"]).lower(),
            "workspace_root": str(workspace),
            "changed_paths": classified_changed,
            "in_scope_paths": in_scope,
            "out_of_scope_paths": out_of_scope,
            "restored_paths": restored,
            "compliance": compliance,
            "binding_status": binding_status,
            "approval_status": approval_status,
            "created_at": evaluated_ts,
            "evaluated_at": evaluated_ts,
            "stop_reason": stop_reason,
            "failure_reason": failure_reason,
            "revocation_id": revocation_id,
            "effective_allowed_roots": [str(p) for p in scope.allowed_roots],
            "denied_roots": [str(p) for p in scope.denied_roots],
            "rollback_atomic": False,
        }
    )
    receipt = save_receipt(
        receipt,
        receipts_dir=receipts_dir,
        events_dir=events_dir,
    )

    return EnforcementOutcome(
        receipt=receipt,
        binding=binding,
        approval=approval,
        run_failed=run_failed,
        stop_reason=stop_reason,
        remaining_changed_paths=remaining,
    )


def apply_enforcement_to_result(
    result: dict[str, Any],
    *,
    binding: dict[str, Any] | None,
    workspace_root: Path | None = None,
    runner_failed: bool | None = None,
    approvals_dir: Path | None = None,
    bindings_dir: Path | None = None,
    revocations_dir: Path | None = None,
    events_dir: Path | None = None,
    receipts_dir: Path | None = None,
) -> dict[str, Any]:
    """Embed MutationReceipt on an AgentRunResult dict and align terminal status.

    No-op when binding is None. Idempotent when mutation_receipt already present.
    """
    if binding is None:
        return result
    if not isinstance(result, dict):
        raise WriteScopeEnforcementError("result must be a dict")
    if isinstance(result.get("mutation_receipt"), dict):
        result.setdefault("write_scope_binding_id", binding["binding_id"])
        result.setdefault("write_scope_approval_id", binding["approval_id"])
        return result

    status_text = str(result.get("status") or "").strip().lower()
    exit_code = result.get("exit_code")
    inferred_failed = (
        runner_failed
        if runner_failed is not None
        else (
            status_text in {"failed", "blocked", "error"}
            or (isinstance(exit_code, int) and exit_code != 0)
        )
    )
    changed = list(result.get("changed_paths") or [])
    outcome = enforce_write_scope_mutations(
        binding["binding_id"],
        changed_paths=changed,
        runner_failed=bool(inferred_failed),
        runner_stop_reason=str(result.get("stop_reason") or "") or None,
        workspace_root=workspace_root
        or Path(str(binding.get("workspace_root") or "")),
        approvals_dir=approvals_dir,
        bindings_dir=bindings_dir,
        revocations_dir=revocations_dir,
        events_dir=events_dir,
        receipts_dir=receipts_dir,
    )
    updated = dict(result)
    updated["mutation_receipt"] = outcome.receipt
    updated["write_scope_binding_id"] = binding["binding_id"]
    updated["write_scope_approval_id"] = binding["approval_id"]
    updated["changed_paths"] = outcome.remaining_changed_paths
    if outcome.run_failed:
        updated["status"] = "failed"
        updated["exit_code"] = 1 if not isinstance(exit_code, int) or exit_code == 0 else exit_code
        updated["stop_reason"] = (
            outcome.stop_reason
            or str(updated.get("stop_reason") or "")
            or STOP_SCOPE_EXCEEDED
        )
        if not str(updated.get("final_text") or "").strip():
            updated["final_text"] = (
                outcome.receipt.get("failure_reason")
                or outcome.stop_reason
                or "write-scope enforcement failed"
            )
        elif outcome.stop_reason and outcome.stop_reason not in str(
            updated.get("final_text") or ""
        ):
            updated["final_text"] = (
                f"{updated['final_text']}\n[write-scope] {outcome.stop_reason}: "
                f"{outcome.receipt.get('failure_reason') or ''}"
            ).strip()
    return updated


def guardrail_write_allowed(
    path: str,
    *,
    writable_roots: list[Path] | list[str] | None,
    denied_roots: list[Path] | list[str] | None = None,
    workspace_root: Path | str | None = None,
) -> str | None:
    """Tool-layer check: None if allowed, else error message.

    Uses path-prefix semantics with deny-wins. When workspace_root is omitted,
    roots are treated as absolute prefixes only (legacy Guardrails behavior + deny).
    """
    roots = [Path(r).resolve(strict=False) for r in (writable_roots or [])]
    if not roots:
        return None
    denied = [Path(r).resolve(strict=False) for r in (denied_roots or [])]
    if workspace_root is not None:
        scope = ScopeRoots(
            workspace_root=Path(workspace_root).expanduser().resolve(strict=False),
            allowed_roots=roots,
            denied_roots=denied,
        )
        ok, _rel, err = path_is_within_effective_scope(path, scope=scope)
        if ok:
            return None
        if err == "denied_root":
            return f"Path '{path}' matches denied write-scope root"
        return f"Path '{path}' is outside writable roots"
    abs_path = Path(path).resolve(strict=False)
    if denied and any(_path_matches_root(abs_path, root) for root in denied):
        return f"Path '{path}' matches denied write-scope root"
    if not any(_path_matches_root(abs_path, root) for root in roots):
        roots_text = ", ".join(str(root) for root in roots)
        return f"Path '{path}' is outside writable roots: {roots_text}"
    return None


__all__ = [
    "COMPLIANCE_BASE_SHA_MISMATCH",
    "COMPLIANCE_NO_MUTATION",
    "COMPLIANCE_PARTIAL",
    "COMPLIANCE_SCOPE_EXCEEDED",
    "COMPLIANCE_VALUES",
    "COMPLIANCE_WITHIN_SCOPE",
    "EnforcementOutcome",
    "RECEIPT_ID_PREFIX",
    "RECEIPT_KIND",
    "RECEIPT_SCHEMA_VERSION",
    "RUNTIME_BREACH_REVOKER",
    "STOP_BASE_SHA_MISMATCH",
    "STOP_EXPIRED",
    "STOP_PARTIAL",
    "STOP_REVOKED",
    "STOP_SCOPE_EXCEEDED",
    "ScopeRoots",
    "WriteScopeEnforcementError",
    "apply_enforcement_to_result",
    "check_authority_checkpoint",
    "classify_changed_paths",
    "compute_receipt_id",
    "consume_approval",
    "enforce_write_scope_mutations",
    "guardrail_write_allowed",
    "list_receipts",
    "load_receipt",
    "load_scope_roots_for_binding",
    "normalize_workspace_path",
    "path_is_within_effective_scope",
    "receipt_path",
    "restore_paths",
    "revoke_approval_for_breach",
    "save_receipt",
    "verify_receipt_record",
]
