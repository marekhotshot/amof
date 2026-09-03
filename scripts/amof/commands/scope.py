"""CLI surfaces for WriteScopeProposal, Approval, Binding, audit, and recover.

Wave 1: list|show proposals (evidence only).
Wave 2: approve|revoke + show/list approvals with TTL and revocation.
Wave 3: show/list bindings; Approval alone still does not mutate — Binding does.
Wave 4: enforcement + MutationReceipt (via execute path).
Wave 5: audit|recover + migration honesty.
"""

from __future__ import annotations

import argparse
import importlib.util
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
from ..write_scope_proposals import (
    WriteScopeProposalError,
    list_proposals,
    load_proposal,
    persist_write_scope_proposals_from_result,
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


_EMPTY_LIST_HINT = (
    "0 proposals. Proposals are emitted by governed worker runs; import one with: "
    "amof scope import-result <result.json> --run-id <id>"
)


def _print_list_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print(_EMPTY_LIST_HINT)
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


def _agent_run_result_schema_path() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts" / "agent-run-result.schema.json"


_EXAMPLE_RESULT_FILES = {
    "src-only": "agent-run-result.src-only.json",
}


def write_scope_example_path(name: str) -> Path:
    """Resolve a packaged or repo-root write-scope example result file."""
    filename = _EXAMPLE_RESULT_FILES.get(name)
    if not filename:
        raise ScopeCliError(f"unknown import-result example: {name}")
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "examples" / "write-scope" / filename,
        here.parents[3] / "examples" / "write-scope" / filename,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ScopeCliError(
        f"example {name!r} is not installed; expected {filename} under examples/write-scope/"
    )


def _validate_agent_run_result(payload: Any) -> None:
    """Fail closed on an invalid agent-run-result envelope."""
    if not isinstance(payload, dict):
        raise ScopeCliError("result file is not a JSON object")
    if payload.get("result_kind") != "agent_run_result":
        raise ScopeCliError("result_kind must be 'agent_run_result'")
    schema_path = _agent_run_result_schema_path()
    if importlib.util.find_spec("jsonschema") is None or not schema_path.is_file():
        return
    import jsonschema

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(instance=payload, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ScopeCliError(f"agent-run-result schema validation failed: {exc.message}") from exc


def _cmd_scope_import_result(args: argparse.Namespace) -> int:
    example = str(getattr(args, "example", "") or "").strip()
    raw_result = str(getattr(args, "result_file", "") or "").strip()
    run_id = str(getattr(args, "run_id", "") or "").strip()
    if example and raw_result:
        raise ScopeCliError("pass a result file or --example, not both")
    if example:
        result_path = write_scope_example_path(example)
        sys.stderr.write(
            "[scope] importing learning fixture "
            f"{example} ({result_path.name}); not evidence\n"
        )
    else:
        result_path = Path(raw_result)
        if not raw_result or not result_path.is_file():
            raise ScopeCliError(
                "result file not found; pass a worker agent-run-result.json "
                "or --example src-only"
            )
    if not run_id:
        raise ScopeCliError("--run-id is required")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScopeCliError(f"result file is not valid JSON: {exc}") from exc
    _validate_agent_run_result(payload)
    outcome = persist_write_scope_proposals_from_result(payload, run_id=run_id)
    if outcome.rejected and not outcome.persisted:
        reasons = ", ".join(
            str(item.get("reason") or item.get("detail") or item) for item in outcome.rejected
        )
        raise ScopeCliError(f"no proposals persisted ({reasons or 'rejected'})")
    if not outcome.persisted:
        raise ScopeCliError(
            "no write_scope_proposals in result; worker must emit proposal evidence"
        )
    ids = [str(item["proposal_id"]) for item in outcome.persisted]
    if bool(getattr(args, "json", False)):
        print(
            json.dumps(
                {
                    "proposal_ids": ids,
                    "persisted": outcome.persisted,
                    "rejected": outcome.rejected,
                },
                indent=2,
            )
        )
    else:
        for proposal_id in ids:
            print(proposal_id)
        if outcome.rejected:
            sys.stderr.write(
                f"[scope] {len(outcome.rejected)} candidate(s) rejected; "
                f"{len(ids)} persisted\n"
            )
    return 0


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
            corrupt = [item for item in records if item.get("status") == "corrupt"]
            for item in corrupt:
                sys.stderr.write(
                    f"[scope] corrupt proposal {item.get('proposal_id')}: "
                    f"{item.get('integrity_error')}\n"
                )
            if bool(getattr(args, "json", False)):
                print(json.dumps(records, indent=2))
            else:
                _print_list_table(records)
            return 1 if corrupt else 0

        if action == "import-result":
            return _cmd_scope_import_result(args)

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

        sys.stderr.write(
            "Usage: amof scope {list,show,approve,revoke,audit,recover,import-result} ...\n"
        )
        return 1
    except ScopeCliError as exc:
        sys.stderr.write(f"[scope] {exc}\n")
        return 1


__all__ = ["ScopeCliError", "cmd_scope"]
