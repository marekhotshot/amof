"""CLI surfaces for WriteScopeProposal, Approval, Binding, audit, and recover.

Wave 1: list|show proposals (evidence only).
Wave 2: approve|revoke + show/list approvals with TTL and revocation.
Wave 3: show/list bindings; Approval alone still does not mutate — Binding does.
Wave 4: enforcement + MutationReceipt (via execute path).
Wave 5: audit|recover + migration honesty (scan|migrate).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..write_scope_approvals import (
    STATUS_APPROVED,
    STATUS_CONSUMED,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    WriteScopeApprovalError,
    approve_proposal,
    is_approval_active,
    list_approvals,
    load_approval,
    revoke_approval,
)
from ..write_scope_audit import WriteScopeAuditError, audit_write_scope
from ..write_scope_bindings import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REVOKED as BINDING_STATUS_REVOKED,
    STATUS_SUSPENDED,
    WriteScopeBindingError,
    list_bindings,
    load_binding,
)
from ..write_scope_migration import (
    migrate_nested_proposals_from_result,
    scan_write_scope_store,
)
from ..write_scope_proposals import (
    WriteScopeProposalError,
    collect_candidate_bodies,
    list_proposals,
    load_proposal,
    normalize_write_scope_body,
)
from ..write_scope_recovery import WriteScopeRecoveryError, recover_binding

PROPOSAL_STATUSES = frozenset({"proposed"})
APPROVAL_STATUSES = frozenset(
    {STATUS_APPROVED, STATUS_REVOKED, STATUS_EXPIRED, STATUS_CONSUMED}
)
BINDING_STATUSES = frozenset(
    {
        STATUS_ACTIVE,
        STATUS_COMPLETED,
        STATUS_FAILED,
        BINDING_STATUS_REVOKED,
        STATUS_SUSPENDED,
    }
)


class ScopeCliError(RuntimeError):
    """Raised when a scope command cannot be completed truthfully."""


def _print_list_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No write-scope records found.")
        return
    headers = (
        "kind",
        "id",
        "status",
        "run_id",
        "target_id",
        "created_or_approved_or_bound_at",
        "expires_at",
        "body_hash",
    )
    print("\t".join(headers))
    for record in records:
        kind = str(record.get("kind") or "-")
        body = record.get("body") or {}
        if kind == "write_scope_binding":
            record_id = str(record.get("binding_id") or "-")
            when = str(record.get("bound_at") or "-")
            expires = "-"
            target = str(record.get("target_id") or "-")
            body_hash = str(record.get("body_hash") or "-")
        elif kind == "write_scope_approval":
            record_id = str(record.get("approval_id") or "-")
            when = str(record.get("approved_at") or "-")
            expires = str(record.get("expires_at") or "-")
            target = str(body.get("target_id") or "-")
            body_hash = str(record.get("body_hash") or "-")
        else:
            record_id = str(record.get("proposal_id") or "-")
            when = str(record.get("created_at") or "-")
            expires = "-"
            target = str(body.get("target_id") or "-")
            body_hash = str(record.get("body_hash") or "-")
        print(
            "\t".join(
                (
                    kind,
                    record_id,
                    str(record.get("status") or "-"),
                    str(record.get("run_id") or "-"),
                    target,
                    when,
                    expires,
                    body_hash,
                )
            )
        )


def _print_proposal(record: dict[str, Any]) -> None:
    body = record.get("body") or {}
    pairs = [
        ("kind", record.get("kind") or "-"),
        ("proposal_id", record.get("proposal_id") or "-"),
        ("run_id", record.get("run_id") or "-"),
        ("status", record.get("status") or "-"),
        ("created_at", record.get("created_at") or "-"),
        ("body_hash", record.get("body_hash") or "-"),
        ("target_id", body.get("target_id") or "-"),
        ("base_sha", body.get("base_sha") or "-"),
        ("allowed_roots", ", ".join(body.get("allowed_roots") or []) or "-"),
        ("denied_roots", ", ".join(body.get("denied_roots") or []) or "-"),
        ("docs_only", str(body.get("docs_only"))),
        ("source_mutation", str(body.get("source_mutation"))),
        ("expected_checks", ", ".join(body.get("expected_checks") or []) or "-"),
        ("reason", body.get("reason") or "-"),
    ]
    for key, value in pairs:
        print(f"{key}: {value}")


def _print_approval(record: dict[str, Any]) -> None:
    body = record.get("body") or {}
    pairs = [
        ("kind", record.get("kind") or "-"),
        ("approval_id", record.get("approval_id") or "-"),
        ("proposal_id", record.get("proposal_id") or "-"),
        ("run_id", record.get("run_id") or "-"),
        ("status", record.get("status") or "-"),
        ("active_grant", str(is_approval_active(record))),
        ("approved_by", record.get("approved_by") or "-"),
        ("approved_at", record.get("approved_at") or "-"),
        ("expires_at", record.get("expires_at") or "-"),
        ("approval_source", record.get("approval_source") or "-"),
        ("provenance", record.get("provenance") or "-"),
        ("body_hash", record.get("body_hash") or "-"),
        ("revocation_id", record.get("revocation_id") or "-"),
        ("target_id", body.get("target_id") or "-"),
        ("base_sha", body.get("base_sha") or "-"),
        ("allowed_roots", ", ".join(body.get("allowed_roots") or []) or "-"),
        ("denied_roots", ", ".join(body.get("denied_roots") or []) or "-"),
        ("docs_only", str(body.get("docs_only"))),
        ("source_mutation", str(body.get("source_mutation"))),
        ("expected_checks", ", ".join(body.get("expected_checks") or []) or "-"),
        ("reason", body.get("reason") or "-"),
        (
            "note",
            "Approval alone does not enable mutation; Runtime Binding required.",
        ),
    ]
    for key, value in pairs:
        print(f"{key}: {value}")


def _print_binding(record: dict[str, Any]) -> None:
    pairs = [
        ("kind", record.get("kind") or "-"),
        ("binding_id", record.get("binding_id") or "-"),
        ("approval_id", record.get("approval_id") or "-"),
        ("run_id", record.get("run_id") or "-"),
        ("target_id", record.get("target_id") or "-"),
        ("runner_id", record.get("runner_id") or "-"),
        ("status", record.get("status") or "-"),
        ("bound_at", record.get("bound_at") or "-"),
        ("base_sha", record.get("base_sha") or "-"),
        ("body_hash", record.get("body_hash") or "-"),
        ("workspace_root", record.get("workspace_root") or "-"),
        ("writable_roots", ", ".join(record.get("writable_roots") or []) or "-"),
        ("terminal_at", record.get("terminal_at") or "-"),
        ("terminal_reason", record.get("terminal_reason") or "-"),
        (
            "note",
            "Binding reserves Approval for one mutating attempt; "
            "successful in-scope mutation consumes Approval; "
            "crash recovery uses amof scope recover.",
        ),
    ]
    for key, value in pairs:
        print(f"{key}: {value}")


def _print_audit(record: dict[str, Any]) -> None:
    terminal = record.get("terminal_authority_state") or {}
    proposal = record.get("proposal") or {}
    approval = record.get("approval") or {}
    binding = record.get("binding") or {}
    receipt = record.get("mutation_receipt") or {}
    revocation = record.get("revocation") or {}
    pairs = [
        ("kind", record.get("kind") or "-"),
        ("query", record.get("query") or "-"),
        ("proposal_id", proposal.get("proposal_id") or "-"),
        ("approval_id", approval.get("approval_id") or "-"),
        ("approval_status", approval.get("status") or "-"),
        ("revocation_id", (revocation.get("revocation_id") if isinstance(revocation, dict) else None) or "-"),
        ("binding_id", binding.get("binding_id") or "-"),
        ("binding_status", binding.get("status") or "-"),
        ("receipt_id", receipt.get("receipt_id") or "-"),
        ("compliance", receipt.get("compliance") or "-"),
        ("execution_result", "present" if record.get("execution_result") else "absent"),
        ("residual_mutation_authority", terminal.get("residual_mutation_authority") or "-"),
        ("note", terminal.get("note") or "-"),
    ]
    for key, value in pairs:
        print(f"{key}: {value}")


def _print_recovery(record: dict[str, Any]) -> None:
    diagnosis = record.get("diagnosis") or {}
    pairs = [
        ("kind", record.get("kind") or "-"),
        ("binding_id", record.get("binding_id") or "-"),
        ("outcome", record.get("outcome") or "-"),
        ("outcomes", ", ".join(record.get("outcomes") or []) or "-"),
        ("residual_mutation_authority", record.get("residual_mutation_authority") or "-"),
        ("restored_paths", ", ".join(record.get("restored_paths") or []) or "-"),
        ("conditions", ", ".join(diagnosis.get("conditions") or []) or "-"),
        ("dirty_paths", ", ".join(diagnosis.get("dirty_paths") or []) or "-"),
        ("note", record.get("note") or "-"),
    ]
    for key, value in pairs:
        print(f"{key}: {value}")


def _print_scan(record: dict[str, Any]) -> None:
    pairs = [
        ("proposals_ok", record.get("proposals_ok")),
        ("approvals_ok", record.get("approvals_ok")),
        ("bindings_ok", record.get("bindings_ok")),
        ("receipts_ok", record.get("receipts_ok")),
        ("corrupt_count", len(record.get("corrupt") or [])),
        ("legacy_flag_events_seen", record.get("legacy_flag_events_seen")),
        ("legacy_approvals_fabricated", record.get("legacy_approvals_fabricated")),
        ("legacy_policy", record.get("legacy_policy") or "-"),
    ]
    for key, value in pairs:
        print(f"{key}: {value}")
    for item in record.get("corrupt") or []:
        print(
            "corrupt:\t"
            f"{item.get('kind') or '-'}\t"
            f"{item.get('path') or '-'}\t"
            f"{item.get('error') or '-'}\t"
            f"{item.get('action') or '-'}"
        )


def _print_migrate(record: dict[str, Any]) -> None:
    pairs = [
        ("mode", record.get("mode") or "-"),
        ("applied", record.get("applied")),
        ("candidate_count", record.get("candidate_count")),
        ("persisted_count", record.get("persisted_count")),
        ("rejected_count", record.get("rejected_count")),
        ("skipped_prose_only", record.get("skipped_prose_only")),
        ("note", record.get("note") or "-"),
    ]
    for key, value in pairs:
        print(f"{key}: {value}")


def _resolve_scan_store(store: str) -> tuple[Path, Path, Path, Path, Path | None]:
    root = Path(str(store or "").strip()).expanduser()
    if not str(store or "").strip():
        raise ScopeCliError(
            "scan requires explicit --store <write-scopes-root> "
            "(fail-closed: no implicit AMOF_HOME default)."
        )
    if not root.exists():
        raise ScopeCliError(f"scan --store path does not exist: {root}")
    if not root.is_dir():
        raise ScopeCliError(f"scan --store must be a directory: {root}")
    proposals = root / "proposals"
    approvals = root / "approvals"
    bindings = root / "bindings"
    receipts = root / "receipts"
    events_dir = root / "events"
    events_path: Path | None = None
    if events_dir.is_dir():
        # Prefer a single events.jsonl if present; else leave unset.
        candidate = events_dir / "events.jsonl"
        if candidate.is_file():
            events_path = candidate
    return proposals, approvals, bindings, receipts, events_path


def _load_result_json(path_text: str) -> dict[str, Any]:
    text = str(path_text or "").strip()
    if not text:
        raise ScopeCliError("migrate requires --result <path-to-AgentRunResult.json>.")
    path = Path(text).expanduser()
    if not path.is_file():
        raise ScopeCliError(f"migrate --result file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScopeCliError(f"migrate --result is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScopeCliError("migrate --result must be a JSON object (AgentRunResult).")
    return payload


def _migrate_dry_run_preview(
    result: dict[str, Any],
    *,
    run_id: str | None,
) -> dict[str, Any]:
    """Preview migrate without calling the persist helper (no writes)."""
    parent = str(run_id or result.get("session_id") or result.get("run_id") or "").strip()
    candidates, rejected = collect_candidate_bodies(result)
    normalizable = 0
    for index, candidate in enumerate(candidates):
        frozen = normalize_write_scope_body(candidate)
        if frozen is None:
            rejected.append(
                {
                    "reason": "malformed_proposal",
                    "index": index,
                    "detail": "proposal failed WriteScopeBody normalization",
                }
            )
            continue
        normalizable += 1
    if candidates and not parent:
        rejected.append(
            {
                "reason": "missing_parent_run_id",
                "detail": "proposals require a parent run_id / session_id",
            }
        )
        normalizable = 0
    return {
        "kind": "write_scope_migrate",
        "mode": "dry-run",
        "applied": False,
        "run_id": parent or None,
        "candidate_count": normalizable,
        "persisted_count": 0,
        "rejected_count": len(rejected),
        "rejected": rejected,
        "skipped_prose_only": not candidates,
        "persisted": [],
        "note": "dry-run only; pass --apply to persist via migrate_nested_proposals_from_result",
    }


def _load_show_record(ref: str) -> dict[str, Any]:
    """Show proposal, approval, or binding by id prefix / fallback lookup."""
    text = str(ref or "").strip()
    if not text:
        raise ScopeCliError("id is required (proposal_id, approval_id, or binding_id).")
    if text.startswith("wsb-"):
        try:
            return load_binding(text)
        except WriteScopeBindingError as exc:
            raise ScopeCliError(str(exc)) from exc
    if text.startswith("wsa-"):
        try:
            return load_approval(text)
        except WriteScopeApprovalError as exc:
            raise ScopeCliError(str(exc)) from exc
    if text.startswith("wsp-"):
        try:
            return load_proposal(text)
        except WriteScopeProposalError as exc:
            raise ScopeCliError(str(exc)) from exc
    # Ambiguous prefix: try binding, approval, then proposal.
    try:
        return load_binding(text)
    except WriteScopeBindingError:
        pass
    try:
        return load_approval(text)
    except WriteScopeApprovalError:
        pass
    try:
        return load_proposal(text)
    except WriteScopeProposalError as exc:
        raise ScopeCliError(f"scope record not found: {text}") from exc


def cmd_scope(args: argparse.Namespace) -> int:
    action = str(getattr(args, "scope_cmd", "") or "").strip()
    try:
        if action == "list":
            run_id = str(getattr(args, "from_run", "") or "").strip() or None
            status = str(getattr(args, "status", "") or "").strip() or None
            records: list[dict[str, Any]] = []
            known = PROPOSAL_STATUSES | APPROVAL_STATUSES | BINDING_STATUSES
            if status is not None and status not in known:
                raise ScopeCliError(f"unsupported --status: {status}")

            include_proposals = status is None or status in PROPOSAL_STATUSES
            include_approvals = status is None or status in APPROVAL_STATUSES
            include_bindings = status is None or status in BINDING_STATUSES
            # Ambiguous statuses shared by approvals and bindings (revoked).
            if status == STATUS_REVOKED:
                include_approvals = True
                include_bindings = True

            if include_proposals and (status is None or status in PROPOSAL_STATUSES):
                proposal_status = status if status in PROPOSAL_STATUSES else None
                if status is None or status in PROPOSAL_STATUSES:
                    records.extend(
                        list_proposals(run_id=run_id, status=proposal_status or "proposed")
                        if proposal_status
                        else list_proposals(run_id=run_id, status=None)
                    )
            if include_approvals and (
                status is None or status in APPROVAL_STATUSES
            ):
                approval_status = status if status in APPROVAL_STATUSES else None
                records.extend(
                    list_approvals(run_id=run_id, status=approval_status)
                )
            if include_bindings and (status is None or status in BINDING_STATUSES):
                binding_status = status if status in BINDING_STATUSES else None
                # Avoid listing approvals' "revoked" filter as only bindings when
                # status is a binding-only value; for shared revoked, list both.
                if status is None or status in BINDING_STATUSES:
                    records.extend(
                        list_bindings(run_id=run_id, status=binding_status)
                    )

            kind_order = {
                "write_scope_proposal": 0,
                "write_scope_approval": 1,
                "write_scope_binding": 2,
            }
            records.sort(
                key=lambda item: (
                    kind_order.get(str(item.get("kind") or ""), 9),
                    item.get("created_at")
                    or item.get("approved_at")
                    or item.get("bound_at")
                    or "",
                    item.get("proposal_id")
                    or item.get("approval_id")
                    or item.get("binding_id")
                    or "",
                )
            )
            if bool(getattr(args, "json", False)):
                print(json.dumps(records, indent=2))
            else:
                _print_list_table(records)
            return 0

        if action == "show":
            ref = str(
                getattr(args, "scope_id", None)
                or getattr(args, "proposal_id", None)
                or ""
            ).strip()
            record = _load_show_record(ref)
            if bool(getattr(args, "json", False)):
                print(json.dumps(record, indent=2))
            elif record.get("kind") == "write_scope_binding":
                _print_binding(record)
            elif record.get("kind") == "write_scope_approval":
                _print_approval(record)
            else:
                _print_proposal(record)
            return 0

        if action == "approve":
            proposal_id = str(getattr(args, "proposal_id", "") or "").strip()
            ttl = str(getattr(args, "ttl", "") or "").strip()
            approved_by = str(getattr(args, "approved_by", "") or "").strip()
            if not proposal_id:
                raise ScopeCliError("proposal_id is required.")
            if not ttl:
                raise ScopeCliError("TTL is mandatory: pass --ttl <duration> (e.g. 2h, 30m, 1d).")
            if not approved_by:
                raise ScopeCliError(
                    "operator identity is required: pass --approved-by <operator-id>."
                )
            try:
                record = approve_proposal(
                    proposal_id,
                    ttl=ttl,
                    approved_by=approved_by,
                    approval_source="cli",
                    provenance="operator_asserted",
                )
            except WriteScopeApprovalError as exc:
                raise ScopeCliError(str(exc)) from exc
            if bool(getattr(args, "json", False)):
                print(json.dumps(record, indent=2))
            else:
                print(f"approval_id: {record['approval_id']}")
                print(f"status: {record['status']}")
                print(f"expires_at: {record['expires_at']}")
                print(
                    "note: Approval alone does not enable mutation execution "
                    "(use --write-scope-approval to bind)."
                )
            return 0

        if action == "revoke":
            approval_id = str(getattr(args, "approval_id", "") or "").strip()
            reason = str(getattr(args, "reason", "") or "").strip()
            revoked_by = str(getattr(args, "revoked_by", "") or "").strip()
            if not approval_id:
                raise ScopeCliError("approval_id is required.")
            if not reason:
                raise ScopeCliError("revoke --reason is required (non-authoritative).")
            if not revoked_by:
                raise ScopeCliError(
                    "operator identity is required: pass --revoked-by <operator-id>."
                )
            try:
                approval, revocation, already = revoke_approval(
                    approval_id,
                    reason=reason,
                    revoked_by=revoked_by,
                    provenance="operator_asserted",
                )
            except WriteScopeApprovalError as exc:
                raise ScopeCliError(str(exc)) from exc
            payload = {
                "approval": approval,
                "revocation": revocation,
                "already_revoked": already,
            }
            if bool(getattr(args, "json", False)):
                print(json.dumps(payload, indent=2))
            else:
                print(f"approval_id: {approval['approval_id']}")
                print(f"status: {approval['status']}")
                print(f"revocation_id: {revocation['revocation_id']}")
                print(f"already_revoked: {already}")
            return 0

        if action == "audit":
            ref = str(getattr(args, "scope_id", "") or "").strip()
            if not ref:
                raise ScopeCliError(
                    "id is required (proposal_id, approval_id, binding_id, or run_id)."
                )
            try:
                record = audit_write_scope(ref)
            except WriteScopeAuditError as exc:
                raise ScopeCliError(str(exc)) from exc
            if bool(getattr(args, "json", False)):
                print(json.dumps(record, indent=2))
            else:
                _print_audit(record)
            return 0

        if action == "recover":
            binding_id = str(getattr(args, "binding_id", "") or "").strip()
            if not binding_id:
                raise ScopeCliError("binding_id is required.")
            decision = str(getattr(args, "decision", "") or "auto").strip() or "auto"
            if decision not in {"auto", "restore", "accept-partial", "mark-failed"}:
                raise ScopeCliError(
                    "unsupported --decision; use auto|restore|accept-partial|mark-failed"
                )
            try:
                record = recover_binding(binding_id, decision=decision)  # type: ignore[arg-type]
            except WriteScopeRecoveryError as exc:
                raise ScopeCliError(str(exc)) from exc
            if bool(getattr(args, "json", False)):
                print(json.dumps(record, indent=2))
            else:
                _print_recovery(record)
            return 0

        if action == "scan":
            store = str(getattr(args, "store", "") or "").strip()
            if not store:
                raise ScopeCliError(
                    "scan requires explicit --store <write-scopes-root> "
                    "(fail-closed: no implicit AMOF_HOME default)."
                )
            proposals_dir, approvals_dir, bindings_dir, receipts_dir, auto_events = (
                _resolve_scan_store(store)
            )
            events_override = str(getattr(args, "events_path", "") or "").strip()
            events_path = Path(events_override).expanduser() if events_override else auto_events
            if events_override and (events_path is None or not events_path.is_file()):
                raise ScopeCliError(f"scan --events file not found: {events_override}")
            scan = scan_write_scope_store(
                proposals_dir=proposals_dir,
                approvals_dir=approvals_dir,
                bindings_dir=bindings_dir,
                receipts_dir=receipts_dir,
                events_path=events_path,
            )
            record = scan.to_dict()
            if bool(getattr(args, "json", False)):
                print(json.dumps(record, indent=2))
            else:
                _print_scan(record)
            return 0

        if action == "migrate":
            result = _load_result_json(str(getattr(args, "result_path", "") or ""))
            run_id = str(getattr(args, "run_id", "") or "").strip() or None
            apply = bool(getattr(args, "apply", False))
            base_dir_text = str(getattr(args, "base_dir", "") or "").strip()
            base_dir = Path(base_dir_text).expanduser() if base_dir_text else None
            if not apply:
                record = _migrate_dry_run_preview(result, run_id=run_id)
                if bool(getattr(args, "json", False)):
                    print(json.dumps(record, indent=2))
                else:
                    _print_migrate(record)
                return 0
            outcome = migrate_nested_proposals_from_result(
                result,
                run_id=run_id,
                base_dir=base_dir,
            )
            record = {
                "kind": "write_scope_migrate",
                "mode": "apply",
                "applied": True,
                "run_id": run_id or result.get("session_id") or result.get("run_id"),
                "candidate_count": len(outcome.persisted),
                "persisted_count": len(outcome.persisted),
                "rejected_count": len(outcome.rejected),
                "rejected": list(outcome.rejected),
                "skipped_prose_only": outcome.skipped_prose_only,
                "persisted": list(outcome.persisted),
                "note": (
                    "persisted durable Proposal records; "
                    "AgentRunResult was not rewritten"
                ),
            }
            if bool(getattr(args, "json", False)):
                print(json.dumps(record, indent=2))
            else:
                _print_migrate(record)
            return 0

        sys.stderr.write(
            "Usage: amof scope {list,show,approve,revoke,audit,recover,scan,migrate} ...\n"
        )
        return 1
    except ScopeCliError as exc:
        sys.stderr.write(f"[scope] {exc}\n")
        return 1


__all__ = ["ScopeCliError", "cmd_scope"]
