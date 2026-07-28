"""Crash / restart recovery for WriteScopeBinding (Wave 5).

Recovery never fabricates successful completion and never continues mutation
authority automatically. Allowed outcomes:

- restore_confirmed
- accept_as_partial_failure
- mark_binding_failed
- require_new_approval
- manual_intervention_required
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .write_scope_approvals import (
    STATUS_APPROVED,
    STATUS_CONSUMED,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    WriteScopeApprovalError,
    append_scope_event,
    is_approval_active,
    load_approval,
)
from .write_scope_bindings import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_SUSPENDED,
    WriteScopeBindingError,
    git_rev_parse_head,
    load_binding,
    transition_binding_status,
)
from .write_scope_enforcement import (
    WriteScopeEnforcementError,
    list_receipts,
    load_scope_roots_for_binding,
    restore_paths,
)
from .write_scope_proposals import utc_now_iso

RecoveryDecision = Literal[
    "restore",
    "accept-partial",
    "mark-failed",
    "auto",
]

OUTCOME_RESTORE_CONFIRMED = "restore_confirmed"
OUTCOME_ACCEPT_PARTIAL = "accept_as_partial_failure"
OUTCOME_MARK_FAILED = "mark_binding_failed"
OUTCOME_REQUIRE_NEW_APPROVAL = "require_new_approval"
OUTCOME_MANUAL = "manual_intervention_required"

ALLOWED_OUTCOMES = frozenset(
    {
        OUTCOME_RESTORE_CONFIRMED,
        OUTCOME_ACCEPT_PARTIAL,
        OUTCOME_MARK_FAILED,
        OUTCOME_REQUIRE_NEW_APPROVAL,
        OUTCOME_MANUAL,
    }
)


class WriteScopeRecoveryError(ValueError):
    """Raised when recovery cannot classify or apply a fail-closed outcome."""


@dataclass(frozen=True)
class RecoveryDiagnosis:
    binding_id: str
    binding_status: str
    conditions: list[str]
    dirty_paths: list[str]
    recommended_decision: RecoveryDecision
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "binding_status": self.binding_status,
            "conditions": list(self.conditions),
            "dirty_paths": list(self.dirty_paths),
            "recommended_decision": self.recommended_decision,
            "notes": list(self.notes),
        }


def workspace_dirty_paths(workspace_root: Path) -> list[str]:
    """Return repo-relative dirty paths (modified + untracked), fail closed on git error."""
    root = Path(workspace_root).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise WriteScopeRecoveryError(f"workspace_root missing: {root}")
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=str(root),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        raise WriteScopeRecoveryError(
            f"cannot inspect workspace dirty state: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        # porcelain: XY PATH or XY ORIG -> PATH
        body = line[3:] if len(line) > 3 else line.strip()
        if " -> " in body:
            body = body.split(" -> ", 1)[1]
        rel = body.strip().strip('"')
        if rel:
            paths.append(rel.replace("\\", "/"))
    return sorted(set(paths))


def diagnose_binding_recovery(
    binding_id: str,
    *,
    bindings_dir: Path | None = None,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
    receipts_dir: Path | None = None,
) -> RecoveryDiagnosis:
    """Classify recovery conditions without mutating authority."""
    try:
        binding = load_binding(binding_id, bindings_dir=bindings_dir)
    except WriteScopeBindingError as exc:
        raise WriteScopeRecoveryError(str(exc)) from exc

    conditions: list[str] = []
    notes: list[str] = []
    dirty: list[str] = []
    status = str(binding.get("status") or "")

    if status == STATUS_COMPLETED:
        conditions.append("binding_already_completed")
        notes.append("Recovery will not rewrite completed bindings into success anew.")
        return RecoveryDiagnosis(
            binding_id=binding["binding_id"],
            binding_status=status,
            conditions=conditions,
            dirty_paths=[],
            recommended_decision="mark-failed",
            notes=notes,
        )

    if status not in {STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_FAILED}:
        conditions.append(f"binding_terminal_{status}")
        notes.append("Binding is already terminal; no mutation authority remains.")
        return RecoveryDiagnosis(
            binding_id=binding["binding_id"],
            binding_status=status,
            conditions=conditions,
            dirty_paths=[],
            recommended_decision="mark-failed",
            notes=notes,
        )

    if status == STATUS_ACTIVE:
        conditions.append("active_binding_after_restart")

    approval = None
    try:
        approval = load_approval(
            binding["approval_id"],
            approvals_dir=approvals_dir,
            events_dir=events_dir,
            evaluate_ttl=True,
            persist_expiry=True,
        )
    except WriteScopeApprovalError:
        conditions.append("orphan_binding_missing_approval")
        notes.append("Binding references missing/corrupt Approval.")

    if approval is not None:
        astatus = approval["status"]
        if astatus == STATUS_EXPIRED:
            conditions.append("expired_approval")
        elif astatus == STATUS_REVOKED:
            conditions.append("revoked_approval")
        elif astatus == STATUS_CONSUMED:
            conditions.append("consumed_approval")
        elif astatus != STATUS_APPROVED or not is_approval_active(approval):
            conditions.append("inactive_approval")

    receipts = list_receipts(
        binding_id=binding["binding_id"],
        receipts_dir=receipts_dir,
    )
    if not receipts:
        conditions.append("missing_receipt")
    else:
        conditions.append("receipt_present")

    workspace = Path(str(binding.get("workspace_root") or "")).expanduser()
    try:
        dirty = workspace_dirty_paths(workspace)
    except WriteScopeRecoveryError as exc:
        conditions.append("workspace_unreadable")
        notes.append(str(exc))
        return RecoveryDiagnosis(
            binding_id=binding["binding_id"],
            binding_status=status,
            conditions=conditions,
            dirty_paths=[],
            recommended_decision="auto",
            notes=notes + ["manual_intervention_required for unreadable workspace"],
        )

    if dirty:
        conditions.append("dirty_workspace")
        conditions.append("partial_mutation_suspected")
    else:
        conditions.append("clean_workspace")

    try:
        head = git_rev_parse_head(workspace)
        if head != str(binding.get("base_sha") or "").lower():
            conditions.append("stale_base_sha")
    except WriteScopeBindingError as exc:
        conditions.append("base_sha_unreadable")
        notes.append(str(exc))

    # Duplicate receipt / corrupt receipt probe.
    for receipt in receipts:
        try:
            # already verified by list_receipts; keep signal for audit trail
            _ = receipt["receipt_id"]
        except Exception:  # pragma: no cover - defensive
            conditions.append("corrupt_receipt")

    recommended: RecoveryDecision = "auto"
    if "orphan_binding_missing_approval" in conditions:
        recommended = "mark-failed"
        notes.append("Orphan binding → mark failed; require new approval for any future mutation.")
    elif "expired_approval" in conditions or "revoked_approval" in conditions:
        recommended = "mark-failed"
        notes.append("Expired/revoked Approval → mark binding failed; require new approval.")
    elif "stale_base_sha" in conditions:
        recommended = "mark-failed"
        notes.append("Stale base_sha → fail closed; require new approval at current HEAD.")
    elif "dirty_workspace" in conditions:
        recommended = "restore"
        notes.append(
            "Dirty workspace after crash → operator must choose restore or accept-partial; "
            "default recommendation is restore then mark failed."
        )
    elif "missing_receipt" in conditions:
        recommended = "mark-failed"
        notes.append("Missing receipt → cannot fabricate success; mark binding failed.")
    else:
        recommended = "mark-failed"
        notes.append("Default fail-closed recovery: mark binding failed.")

    notes.append("No automatic continuation of mutation authority.")
    return RecoveryDiagnosis(
        binding_id=binding["binding_id"],
        binding_status=status,
        conditions=conditions,
        dirty_paths=dirty,
        recommended_decision=recommended,
        notes=notes,
    )


def recover_binding(
    binding_id: str,
    *,
    decision: RecoveryDecision = "auto",
    bindings_dir: Path | None = None,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
    receipts_dir: Path | None = None,
) -> dict[str, Any]:
    """Apply an explicit fail-closed recovery outcome.

    Never transitions Binding to completed. Never reactivates mutation authority.
    """
    diagnosis = diagnose_binding_recovery(
        binding_id,
        bindings_dir=bindings_dir,
        approvals_dir=approvals_dir,
        events_dir=events_dir,
        receipts_dir=receipts_dir,
    )
    chosen: RecoveryDecision = decision
    if chosen == "auto":
        if (
            "dirty_workspace" in diagnosis.conditions
            and "orphan_binding_missing_approval" not in diagnosis.conditions
            and "expired_approval" not in diagnosis.conditions
            and "revoked_approval" not in diagnosis.conditions
            and "stale_base_sha" not in diagnosis.conditions
        ):
            # Dirty workspace requires explicit operator choice.
            return {
                "kind": "write_scope_recovery",
                "schema_version": 1,
                "binding_id": diagnosis.binding_id,
                "outcome": OUTCOME_MANUAL,
                "outcomes": [OUTCOME_MANUAL],
                "diagnosis": diagnosis.to_dict(),
                "binding": load_binding(binding_id, bindings_dir=bindings_dir),
                "approval": None,
                "restored_paths": [],
                "residual_mutation_authority": "none_until_explicit_recover_decision",
                "note": (
                    "Dirty workspace after crash requires explicit "
                    "--decision restore|accept-partial. No automatic continuation."
                ),
            }
        chosen = diagnosis.recommended_decision
        if chosen in {"restore", "accept-partial"} and "dirty_workspace" in diagnosis.conditions:
            # Still require explicit for dirty unless caller passed it.
            if decision == "auto":
                chosen = "mark-failed"

    try:
        binding = load_binding(binding_id, bindings_dir=bindings_dir)
    except WriteScopeBindingError as exc:
        raise WriteScopeRecoveryError(str(exc)) from exc

    if binding["status"] == STATUS_COMPLETED:
        raise WriteScopeRecoveryError(
            "refuse to recover a completed binding into a new success state"
        )

    # Suspend active bindings before terminal classification.
    if binding["status"] == STATUS_ACTIVE:
        binding = transition_binding_status(
            binding_id,
            STATUS_SUSPENDED,
            reason="recover:suspend_active_after_restart",
            bindings_dir=bindings_dir,
            events_dir=events_dir,
        )

    approval = None
    try:
        approval = load_approval(
            binding["approval_id"],
            approvals_dir=approvals_dir,
            events_dir=events_dir,
            evaluate_ttl=True,
            persist_expiry=True,
        )
    except WriteScopeApprovalError:
        approval = None

    restored: list[str] = []
    outcomes: list[str] = []
    workspace = Path(str(binding.get("workspace_root") or "")).expanduser()

    if chosen == "restore":
        if diagnosis.dirty_paths:
            try:
                # Prefer restoring only dirty paths under bound writable roots when possible.
                scope = None
                try:
                    scope = load_scope_roots_for_binding(
                        binding,
                        approvals_dir=approvals_dir,
                        events_dir=events_dir,
                    )
                except WriteScopeEnforcementError:
                    scope = None
                targets = list(diagnosis.dirty_paths)
                if scope is not None:
                    # Restore all dirty paths for crash recovery honesty.
                    targets = list(diagnosis.dirty_paths)
                restored = restore_paths(workspace, targets)
            except Exception as exc:  # noqa: BLE001 - surface as manual
                append_scope_event(
                    "write_scope.recover",
                    {
                        "binding_id": binding_id,
                        "outcome": OUTCOME_MANUAL,
                        "error": str(exc),
                    },
                    events_dir=events_dir,
                    at=utc_now_iso(),
                )
                return {
                    "kind": "write_scope_recovery",
                    "schema_version": 1,
                    "binding_id": binding_id,
                    "outcome": OUTCOME_MANUAL,
                    "outcomes": [OUTCOME_MANUAL],
                    "diagnosis": diagnosis.to_dict(),
                    "binding": load_binding(binding_id, bindings_dir=bindings_dir),
                    "approval": approval,
                    "restored_paths": [],
                    "residual_mutation_authority": "none",
                    "note": f"restore failed; manual intervention required: {exc}",
                }
        outcomes.append(OUTCOME_RESTORE_CONFIRMED)
        reason = "recover:restore_confirmed"
    elif chosen == "accept-partial":
        outcomes.append(OUTCOME_ACCEPT_PARTIAL)
        reason = "recover:accept_as_partial_failure"
    elif chosen == "mark-failed":
        outcomes.append(OUTCOME_MARK_FAILED)
        reason = "recover:mark_binding_failed"
    else:
        raise WriteScopeRecoveryError(f"unsupported recovery decision: {chosen!r}")

    # Terminalize binding as failed (never completed).
    if binding["status"] in {STATUS_ACTIVE, STATUS_SUSPENDED}:
        binding = transition_binding_status(
            binding_id,
            STATUS_FAILED,
            reason=reason,
            bindings_dir=bindings_dir,
            events_dir=events_dir,
        )
        if OUTCOME_MARK_FAILED not in outcomes:
            outcomes.append(OUTCOME_MARK_FAILED)
    elif binding["status"] == STATUS_FAILED:
        if OUTCOME_MARK_FAILED not in outcomes:
            outcomes.append(OUTCOME_MARK_FAILED)

    # Approval is never auto-reactivated; caller must mint a new Approval when needed.
    require_new = False
    if approval is None or approval.get("status") in {
        STATUS_EXPIRED,
        STATUS_REVOKED,
        STATUS_CONSUMED,
    }:
        require_new = True
    elif chosen in {"restore", "accept-partial", "mark-failed"}:
        # Partial / crash paths require a fresh Approval for any future mutation
        # (v3.3: no automatic continuation / checkpoint resume).
        if any(
            c in diagnosis.conditions
            for c in (
                "dirty_workspace",
                "partial_mutation_suspected",
                "missing_receipt",
                "stale_base_sha",
                "orphan_binding_missing_approval",
                "expired_approval",
                "revoked_approval",
            )
        ):
            require_new = True
        # If Approval still approved but binding failed without mutation evidence,
        # Approval may remain reusable for a *new* Binding — report both outcomes.
        if (
            approval.get("status") == STATUS_APPROVED
            and is_approval_active(approval)
            and "dirty_workspace" not in diagnosis.conditions
            and "missing_receipt" in diagnosis.conditions
            and "stale_base_sha" not in diagnosis.conditions
        ):
            # Clean crash before mutation: Approval reusable; still no continuation.
            require_new = False

    if require_new:
        outcomes.append(OUTCOME_REQUIRE_NEW_APPROVAL)

    primary = outcomes[0] if outcomes else OUTCOME_MARK_FAILED
    residual = "none"
    if (
        approval is not None
        and approval.get("status") == STATUS_APPROVED
        and is_approval_active(approval)
        and not require_new
    ):
        residual = "approval_only_requires_new_binding"

    append_scope_event(
        "write_scope.recover",
        {
            "binding_id": binding_id,
            "outcome": primary,
            "outcomes": outcomes,
            "decision": chosen,
            "restored_paths": restored,
            "residual_mutation_authority": residual,
        },
        events_dir=events_dir,
        at=utc_now_iso(),
    )

    return {
        "kind": "write_scope_recovery",
        "schema_version": 1,
        "binding_id": binding_id,
        "outcome": primary,
        "outcomes": outcomes,
        "diagnosis": diagnosis.to_dict(),
        "binding": binding,
        "approval": approval,
        "restored_paths": restored,
        "residual_mutation_authority": residual,
        "note": (
            "Recovery does not fabricate successful completion and does not "
            "continue mutation authority automatically."
        ),
    }


__all__ = [
    "ALLOWED_OUTCOMES",
    "OUTCOME_ACCEPT_PARTIAL",
    "OUTCOME_MANUAL",
    "OUTCOME_MARK_FAILED",
    "OUTCOME_REQUIRE_NEW_APPROVAL",
    "OUTCOME_RESTORE_CONFIRMED",
    "RecoveryDecision",
    "RecoveryDiagnosis",
    "WriteScopeRecoveryError",
    "diagnose_binding_recovery",
    "recover_binding",
    "workspace_dirty_paths",
]
