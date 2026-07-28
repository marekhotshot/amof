"""Write-scope migration helpers (Wave 5).

Rules
-----
- Nested AgentRunResult proposal data may be read/migrated into durable Proposal
  records via the Wave 1 persist path.
- Legacy ``--approve-writable-root`` / naked path elevation MUST NEVER be
  converted into historical WriteScopeApproval records.
- Unknown or corrupt records fail closed.
- AgentRunResult remains backwards-readable (additive fields only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .write_scope_approvals import WriteScopeApprovalError, verify_approval_record
from .write_scope_bindings import WriteScopeBindingError, verify_binding_record
from .write_scope_enforcement import WriteScopeEnforcementError, verify_receipt_record
from .write_scope_proposals import (
    PersistOutcome,
    WriteScopeProposalError,
    persist_write_scope_proposals_from_result,
    verify_proposal_record,
)

LEGACY_PATH_ELEVATION_MIGRATION_REFUSAL = (
    "legacy --approve-writable-root / writable_root_approval cli_flag events "
    "are compatibility path elevation only and MUST NOT be converted into "
    "WriteScopeApproval history"
)


class WriteScopeMigrationError(ValueError):
    """Raised when migration cannot proceed truthfully."""


@dataclass(frozen=True)
class MigrationScanResult:
    proposals_ok: int
    approvals_ok: int
    bindings_ok: int
    receipts_ok: int
    corrupt: list[dict[str, Any]]
    legacy_flag_events_seen: int
    legacy_approvals_fabricated: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposals_ok": self.proposals_ok,
            "approvals_ok": self.approvals_ok,
            "bindings_ok": self.bindings_ok,
            "receipts_ok": self.receipts_ok,
            "corrupt": list(self.corrupt),
            "legacy_flag_events_seen": self.legacy_flag_events_seen,
            "legacy_approvals_fabricated": self.legacy_approvals_fabricated,
            "legacy_policy": LEGACY_PATH_ELEVATION_MIGRATION_REFUSAL,
        }


def migrate_nested_proposals_from_result(
    result: dict[str, Any] | None,
    *,
    run_id: str | None = None,
    base_dir: Path | None = None,
) -> PersistOutcome:
    """Read nested proposal data from AgentRunResult and persist durable Proposals.

    Preserves backwards-readable AgentRunResult: does not rewrite the result.
    """
    return persist_write_scope_proposals_from_result(
        result,
        run_id=run_id,
        base_dir=base_dir,
    )


def refuse_legacy_path_elevation_as_approval(
    *,
    roots: list[str] | None = None,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explicitly refuse converting naked writable-root flags into Approvals."""
    return {
        "converted": False,
        "approval": None,
        "roots": list(roots or []),
        "event_type": (event or {}).get("event_type") or (event or {}).get("type"),
        "reason": LEGACY_PATH_ELEVATION_MIGRATION_REFUSAL,
    }


def _scan_dir(
    root: Path,
    *,
    glob_pat: str,
    verifier,
    error_types: tuple[type[BaseException], ...],
    kind: str,
) -> tuple[int, list[dict[str, Any]]]:
    ok = 0
    corrupt: list[dict[str, Any]] = []
    if not root.exists():
        return ok, corrupt
    for path in sorted(root.glob(glob_pat)):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            verifier(raw)
            ok += 1
        except error_types as exc:
            corrupt.append(
                {
                    "kind": kind,
                    "path": str(path),
                    "error": str(exc),
                    "action": "fail_closed_left_in_place",
                }
            )
        except (OSError, json.JSONDecodeError) as exc:
            corrupt.append(
                {
                    "kind": kind,
                    "path": str(path),
                    "error": str(exc),
                    "action": "fail_closed_left_in_place",
                }
            )
    return ok, corrupt


def scan_write_scope_store(
    *,
    proposals_dir: Path,
    approvals_dir: Path,
    bindings_dir: Path,
    receipts_dir: Path,
    events_path: Path | None = None,
) -> MigrationScanResult:
    """Scan durable stores; count OK records; list corrupt; never fabricate Approvals."""
    p_ok, p_bad = _scan_dir(
        proposals_dir,
        glob_pat="wsp-*.json",
        verifier=verify_proposal_record,
        error_types=(WriteScopeProposalError,),
        kind="proposal",
    )
    a_ok, a_bad = _scan_dir(
        approvals_dir,
        glob_pat="wsa-*.json",
        verifier=lambda raw: verify_approval_record(raw, evaluate_ttl=False),
        error_types=(WriteScopeApprovalError,),
        kind="approval",
    )
    b_ok, b_bad = _scan_dir(
        bindings_dir,
        glob_pat="wsb-*.json",
        verifier=verify_binding_record,
        error_types=(WriteScopeBindingError,),
        kind="binding",
    )
    r_ok, r_bad = _scan_dir(
        receipts_dir,
        glob_pat="wmr-*.json",
        verifier=verify_receipt_record,
        error_types=(WriteScopeEnforcementError,),
        kind="receipt",
    )

    legacy_seen = 0
    if events_path is not None and events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            et = str(payload.get("event_type") or payload.get("type") or "")
            source = str(
                payload.get("approval_source")
                or (payload.get("data") or {}).get("approval_source")
                or ""
            )
            if (
                "writable_root" in et
                or source == "cli_flag"
                or "approve-writable-root" in json.dumps(payload)
            ):
                legacy_seen += 1
                # Explicit refusal — never mint Approval from these events.
                refuse_legacy_path_elevation_as_approval(event=payload)

    return MigrationScanResult(
        proposals_ok=p_ok,
        approvals_ok=a_ok,
        bindings_ok=b_ok,
        receipts_ok=r_ok,
        corrupt=p_bad + a_bad + b_bad + r_bad,
        legacy_flag_events_seen=legacy_seen,
        legacy_approvals_fabricated=0,
    )


__all__ = [
    "LEGACY_PATH_ELEVATION_MIGRATION_REFUSAL",
    "MigrationScanResult",
    "WriteScopeMigrationError",
    "migrate_nested_proposals_from_result",
    "refuse_legacy_path_elevation_as_approval",
    "scan_write_scope_store",
]
