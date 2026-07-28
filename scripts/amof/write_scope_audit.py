"""Reconstruct Write-Scope Authority lineage for audit (Wave 5).

``amof scope audit`` rebuilds the propose → approve → bind → enforce →
receipt chain from durable app-data records. Audit never grants authority.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .app_paths import runs_dir, write_scope_receipts_dir
from .write_scope_approvals import (
    WriteScopeApprovalError,
    list_approvals,
    load_approval,
    load_revocation,
)
from .write_scope_bindings import (
    WriteScopeBindingError,
    list_bindings,
    load_binding,
)
from .write_scope_enforcement import (
    WriteScopeEnforcementError,
    list_receipts,
    load_receipt,
)
from .write_scope_proposals import (
    WriteScopeProposalError,
    list_proposals,
    load_proposal,
)


class WriteScopeAuditError(ValueError):
    """Raised when audit cannot reconstruct lineage truthfully."""


def _load_execution_result(run_id: str) -> dict[str, Any] | None:
    """Best-effort AgentRunResult / plan-result lookup under runs_dir."""
    ref = str(run_id or "").strip()
    if not ref:
        return None
    root = runs_dir()
    if not root.exists():
        return None
    candidates = (
        "result.json",
        "agent-run-result.json",
        "plan-result.json",
        "handoff-result.json",
    )
    for events_path in root.rglob("events.jsonl"):
        session_dir = events_path.parent
        if session_dir.name != ref and not any(
            part == ref for part in session_dir.parts
        ):
            # Also match when run_id appears in events.
            try:
                text = events_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if ref not in text:
                continue
        for name in candidates:
            path = session_dir / name
            if not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                return raw
    # Direct session directory name match.
    for path in root.rglob("*"):
        if not path.is_dir() or path.name != ref:
            continue
        for name in candidates:
            candidate = path / name
            if not candidate.is_file():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                return raw
    return None


def _terminal_authority_state(
    *,
    proposal: dict[str, Any] | None,
    approval: dict[str, Any] | None,
    binding: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    revocation: dict[str, Any] | None,
) -> dict[str, Any]:
    approval_status = approval.get("status") if approval else None
    binding_status = binding.get("status") if binding else None
    compliance = receipt.get("compliance") if receipt else None
    residual = "none"
    if (
        approval_status == "approved"
        and binding_status not in {"active", "completed", "suspended"}
        and (binding_status is None or binding_status in {"failed", "revoked"})
    ):
        # Unused / failed-bind Approval may still be grantable for a new Binding.
        residual = "approval_only_requires_new_binding"
    if approval_status == "approved" and binding_status in {"active", "suspended"}:
        residual = "binding_requires_recover_or_finalize"
    if approval_status in {"revoked", "expired", "consumed"}:
        residual = "none"
    if binding_status == "completed" and approval_status == "consumed":
        residual = "none"
    return {
        "proposal_status": proposal.get("status") if proposal else None,
        "approval_status": approval_status,
        "binding_status": binding_status,
        "receipt_compliance": compliance,
        "revocation_present": revocation is not None,
        "residual_mutation_authority": residual,
        "note": (
            "Audit is read-only. Residual authority never expands beyond "
            "durable Approval/Binding state."
        ),
    }


def _resolve_seed(ref: str) -> dict[str, Any]:
    text = str(ref or "").strip()
    if not text:
        raise WriteScopeAuditError(
            "id is required (proposal_id, approval_id, binding_id, or run_id)"
        )

    errors: list[str] = []

    if text.startswith("wsb-") or (not text.startswith(("wsp-", "wsa-", "wmr-"))):
        try:
            return {"kind": "binding", "binding": load_binding(text)}
        except WriteScopeBindingError as exc:
            if text.startswith("wsb-"):
                raise WriteScopeAuditError(str(exc)) from exc
            errors.append(str(exc))

    if text.startswith("wsa-") or not text.startswith(("wsp-", "wmr-")):
        try:
            return {"kind": "approval", "approval": load_approval(text)}
        except WriteScopeApprovalError as exc:
            if text.startswith("wsa-"):
                raise WriteScopeAuditError(str(exc)) from exc
            errors.append(str(exc))

    if text.startswith("wsp-") or not text.startswith("wmr-"):
        try:
            return {"kind": "proposal", "proposal": load_proposal(text)}
        except WriteScopeProposalError as exc:
            if text.startswith("wsp-"):
                raise WriteScopeAuditError(str(exc)) from exc
            errors.append(str(exc))

    if text.startswith("wmr-"):
        try:
            return {"kind": "receipt", "receipt": load_receipt(text)}
        except WriteScopeEnforcementError as exc:
            raise WriteScopeAuditError(str(exc)) from exc

    # run_id path: find bindings / approvals / proposals for that run.
    bindings = list_bindings(run_id=text)
    approvals = list_approvals(run_id=text)
    proposals = list_proposals(run_id=text)
    receipts = list_receipts(run_id=text)
    if bindings or approvals or proposals or receipts:
        return {
            "kind": "run",
            "run_id": text,
            "bindings": bindings,
            "approvals": approvals,
            "proposals": proposals,
            "receipts": receipts,
        }

    raise WriteScopeAuditError(
        f"scope audit target not found: {text}"
        + (f" ({'; '.join(errors)})" if errors else "")
    )


def audit_write_scope(ref: str) -> dict[str, Any]:
    """Reconstruct lineage for a proposal, approval, binding, receipt, or run id."""
    seed = _resolve_seed(ref)
    proposal: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    binding: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    revocation: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None
    related: dict[str, Any] = {}

    if seed["kind"] == "run":
        related = {
            "proposals": seed["proposals"],
            "approvals": seed["approvals"],
            "bindings": seed["bindings"],
            "receipts": seed["receipts"],
        }
        if seed["bindings"]:
            binding = seed["bindings"][-1]
        elif seed["approvals"]:
            approval = seed["approvals"][-1]
        elif seed["proposals"]:
            proposal = seed["proposals"][-1]
        if seed["receipts"]:
            receipt = seed["receipts"][-1]
        execution_result = _load_execution_result(seed["run_id"])

    if seed["kind"] == "receipt":
        receipt = seed["receipt"]
        try:
            binding = load_binding(receipt["binding_id"])
        except WriteScopeBindingError:
            binding = None
        try:
            approval = load_approval(receipt["approval_id"])
        except WriteScopeApprovalError:
            approval = None

    if seed["kind"] == "binding":
        binding = seed["binding"]

    if seed["kind"] == "approval":
        approval = seed["approval"]

    if seed["kind"] == "proposal":
        proposal = seed["proposal"]

    if binding is not None and approval is None:
        try:
            approval = load_approval(binding["approval_id"])
        except WriteScopeApprovalError:
            approval = None

    if approval is not None and proposal is None:
        try:
            proposal = load_proposal(approval["proposal_id"])
        except WriteScopeProposalError:
            proposal = None

    if binding is not None and receipt is None:
        receipts = list_receipts(binding_id=binding["binding_id"])
        receipt = receipts[-1] if receipts else None

    if approval is not None and binding is None:
        bindings = list_bindings(approval_id=approval["approval_id"])
        binding = bindings[-1] if bindings else None
        if binding is not None and receipt is None:
            receipts = list_receipts(binding_id=binding["binding_id"])
            receipt = receipts[-1] if receipts else None

    if proposal is not None and approval is None:
        approvals = list_approvals(proposal_id=proposal["proposal_id"])
        approval = approvals[-1] if approvals else None
        if approval is not None and binding is None:
            bindings = list_bindings(approval_id=approval["approval_id"])
            binding = bindings[-1] if bindings else None
            if binding is not None and receipt is None:
                receipts = list_receipts(binding_id=binding["binding_id"])
                receipt = receipts[-1] if receipts else None

    if approval is not None and approval.get("revocation_id"):
        try:
            revocation = load_revocation(str(approval["revocation_id"]))
        except WriteScopeApprovalError:
            revocation = {
                "error": "revocation_missing_or_corrupt",
                "revocation_id": approval.get("revocation_id"),
            }

    run_id = None
    if binding is not None:
        run_id = binding.get("run_id")
    elif approval is not None:
        run_id = approval.get("run_id")
    elif proposal is not None:
        run_id = proposal.get("run_id")
    elif receipt is not None:
        run_id = receipt.get("run_id")
    if execution_result is None and run_id:
        execution_result = _load_execution_result(str(run_id))

    terminal = _terminal_authority_state(
        proposal=proposal,
        approval=approval,
        binding=binding,
        receipt=receipt,
        revocation=revocation if isinstance(revocation, dict) and "error" not in revocation else None,
    )

    return {
        "kind": "write_scope_audit",
        "schema_version": 1,
        "query": str(ref).strip(),
        "proposal": proposal,
        "approval": approval,
        "revocation": revocation,
        "binding": binding,
        "execution_result": execution_result,
        "mutation_receipt": receipt,
        "terminal_authority_state": terminal,
        "related": related,
        "receipts_dir": str(write_scope_receipts_dir()),
    }


__all__ = [
    "WriteScopeAuditError",
    "audit_write_scope",
]
