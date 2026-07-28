"""Durable WriteScopeApproval + Revocation store (Wave 2).

Workers propose; operators approve; runtime enforces.
An Approval is the sole durable operator mutation grant. Approval alone does
NOT enable mutation execution — binding/enforce land in later waves.

Lifecycle (Wave 2)
------------------
Statuses: approved | revoked | expired | consumed

Allowed transitions:
  (new)     -> approved   via operator approve
  approved  -> revoked    via operator revoke
  approved  -> expired    via lazy TTL evaluation on load/list/show/is_active
  approved  -> consumed   reserved for Wave 3/4 binding completion (rejected here)

Rejected (fail closed, never repaired):
  revoked   -> *          (terminal)
  expired   -> *          (terminal)
  consumed  -> *          (terminal)
  *         -> approved   except the initial mint
  any       -> consumed   in Wave 2 public API

TTL forms accepted by parse_ttl_duration:
  Ns / Nm / Nh / Nd  (integer N >= 1), e.g. 30s, 30m, 2h, 1d
  Combinations of those units in descending order, e.g. 1h30m, 1d2h
  Pure integer seconds are rejected — unit suffix is mandatory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .app_paths import (
    ensure_parent_dir,
    write_scope_approvals_dir,
    write_scope_events_dir,
    write_scope_revocations_dir,
)
from .write_scope_proposals import (
    WriteScopeProposalError,
    compute_body_hash,
    load_proposal,
    normalize_write_scope_body,
    utc_now_iso,
    verify_proposal_record,
)

APPROVAL_KIND = "write_scope_approval"
APPROVAL_SCHEMA_VERSION = 1
APPROVAL_ID_PREFIX = "wsa-"
REVOCATION_KIND = "write_scope_revocation"
REVOCATION_SCHEMA_VERSION = 1
REVOCATION_ID_PREFIX = "wsr-"

STATUS_APPROVED = "approved"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"
STATUS_CONSUMED = "consumed"

APPROVAL_STATUSES = frozenset(
    {STATUS_APPROVED, STATUS_REVOKED, STATUS_EXPIRED, STATUS_CONSUMED}
)
TERMINAL_STATUSES = frozenset({STATUS_REVOKED, STATUS_EXPIRED, STATUS_CONSUMED})

PROVENANCE_OPERATOR_ASSERTED = "operator_asserted"
APPROVAL_SOURCE_CLI = "cli"
APPROVAL_SOURCE_API = "api"

# Explicit transition table: from_status -> frozenset(to_status).
# Initial mint is modeled as from_status=None -> approved.
LIFECYCLE_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({STATUS_APPROVED}),
    STATUS_APPROVED: frozenset({STATUS_REVOKED, STATUS_EXPIRED, STATUS_CONSUMED}),
    STATUS_REVOKED: frozenset(),
    STATUS_EXPIRED: frozenset(),
    STATUS_CONSUMED: frozenset(),
}

_TTL_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_TTL_UNIT_ORDER = ("d", "h", "m", "s")

_WORKER_IDENTITY_MARKERS = frozenset(
    {
        "worker",
        "agent",
        "hermes",
        "claude",
        "backend",
        "runner",
        "self",
        "model",
    }
)

_STORE_LOCK = threading.RLock()


class WriteScopeApprovalError(ValueError):
    """Raised when an approval/revocation cannot be created or loaded truthfully."""


def parse_ttl_duration(value: str) -> timedelta:
    """Parse a human TTL into a timedelta.

    Accepted forms (case-insensitive unit letters):
      - single unit: ``30s``, ``30m``, ``2h``, ``1d``
      - combined descending units: ``1h30m``, ``1d2h``, ``2h15m30s``

    Rejected: empty, zero/negative, bare integers, unknown units, ascending
    or repeated unit order (e.g. ``30m1h``), fractional values.
    """
    text = str(value or "").strip().lower()
    if not text:
        raise WriteScopeApprovalError("TTL is required")
    if text.isdigit():
        raise WriteScopeApprovalError(
            "TTL must include a unit suffix (s/m/h/d); bare seconds are rejected"
        )
    remaining = text
    total_seconds = 0
    seen_units: list[str] = []
    token_re = re.compile(r"(\d+)([smhd])")
    while remaining:
        match = token_re.match(remaining)
        if match is None:
            raise WriteScopeApprovalError(f"invalid TTL duration: {value!r}")
        amount = int(match.group(1))
        unit = match.group(2)
        if amount <= 0:
            raise WriteScopeApprovalError(f"TTL unit amount must be >= 1: {value!r}")
        if unit in seen_units:
            raise WriteScopeApprovalError(f"TTL repeats unit {unit!r}: {value!r}")
        if seen_units:
            prev = seen_units[-1]
            if _TTL_UNIT_ORDER.index(unit) <= _TTL_UNIT_ORDER.index(prev):
                raise WriteScopeApprovalError(
                    f"TTL units must be in descending order (d>h>m>s): {value!r}"
                )
        seen_units.append(unit)
        total_seconds += amount * _TTL_UNIT_SECONDS[unit]
        remaining = remaining[match.end() :]
    if total_seconds <= 0:
        raise WriteScopeApprovalError(f"TTL must be positive: {value!r}")
    return timedelta(seconds=total_seconds)


def parse_iso_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise WriteScopeApprovalError("timestamp is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WriteScopeApprovalError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assert_transition(from_status: str | None, to_status: str) -> None:
    allowed = LIFECYCLE_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise WriteScopeApprovalError(
            f"illegal lifecycle transition: {from_status!r} -> {to_status!r}"
        )


def _assert_operator_identity(identity: str, *, field: str) -> str:
    value = str(identity or "").strip()
    if not value:
        raise WriteScopeApprovalError(f"{field} is required (operator identity)")
    lowered = value.lower()
    if lowered in _WORKER_IDENTITY_MARKERS or lowered.startswith("worker:"):
        raise WriteScopeApprovalError(
            f"{field} rejects worker/self-approval identity: {value!r}"
        )
    if lowered in {"prose", "transcript", "memory"}:
        raise WriteScopeApprovalError(
            f"{field} rejects non-operator approval source: {value!r}"
        )
    return value


def _assert_operator_provenance(provenance: str) -> str:
    value = str(provenance or "").strip()
    if value != PROVENANCE_OPERATOR_ASSERTED:
        raise WriteScopeApprovalError(
            f"provenance must be {PROVENANCE_OPERATOR_ASSERTED!r}; got {value!r}"
        )
    return value


def path_is_under_docs(path: str) -> bool:
    """True when a normalized repo-relative path is docs-only eligible."""
    normalized = str(path or "").strip()
    if not normalized:
        return False
    if normalized == "docs" or normalized == "docs/":
        return True
    return normalized.startswith("docs/")


def validate_approval_body(body: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed approve-time body checks beyond proposal normalize."""
    frozen = normalize_write_scope_body(body)
    if frozen is None:
        raise WriteScopeApprovalError(
            "approval body failed WriteScopeBody normalization "
            "(empty roots, wildcards, path traversal, or hostile claims rejected)"
        )
    allowed = frozen["allowed_roots"]
    if not allowed:
        raise WriteScopeApprovalError("allowed_roots must be non-empty")
    if frozen["docs_only"]:
        bad = [item for item in allowed if not path_is_under_docs(item)]
        if bad:
            raise WriteScopeApprovalError(
                "docs_only=true requires every allowed_roots entry under docs/; "
                f"conflicts: {bad}"
            )
    # denied_roots already normalized by normalize_write_scope_body.
    # Deny-wins is an enforcement semantic: documented here and retained on the
    # frozen body for later waves. Overlap is allowed as data.
    return frozen


def compute_approval_id(
    *,
    proposal_id: str,
    approved_at: str,
    approved_by: str,
    expires_at: str,
    body_hash: str,
) -> str:
    material = (
        f"{proposal_id}:{approved_at}:{approved_by}:{expires_at}:{body_hash}"
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"{APPROVAL_ID_PREFIX}{digest}"


def compute_revocation_id(*, approval_id: str) -> str:
    digest = hashlib.sha256(str(approval_id).encode("utf-8")).hexdigest()[:24]
    return f"{REVOCATION_ID_PREFIX}{digest}"


def approval_path(approval_id: str, *, base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else write_scope_approvals_dir()
    return root / f"{approval_id}.json"


def revocation_path(revocation_id: str, *, base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else write_scope_revocations_dir()
    return root / f"{revocation_id}.json"


def events_path(*, base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else write_scope_events_dir()
    return root / "write-scope-events.jsonl"


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


def append_scope_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    events_dir: Path | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    event = {
        "event_type": str(event_type),
        "at": at or utc_now_iso(),
        **payload,
    }
    path = events_path(base_dir=events_dir)
    ensure_parent_dir(path)
    with _STORE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return event


def build_approval_record(
    *,
    proposal: dict[str, Any],
    approved_by: str,
    expires_at: str,
    approved_at: str | None = None,
    approval_source: str = APPROVAL_SOURCE_CLI,
    provenance: str = PROVENANCE_OPERATOR_ASSERTED,
) -> dict[str, Any]:
    verified_proposal = verify_proposal_record(proposal)
    operator = _assert_operator_identity(approved_by, field="approved_by")
    _assert_operator_provenance(provenance)
    source = str(approval_source or "").strip()
    if source not in {APPROVAL_SOURCE_CLI, APPROVAL_SOURCE_API}:
        raise WriteScopeApprovalError(f"unsupported approval_source: {source!r}")

    frozen = validate_approval_body(verified_proposal["body"])
    body_hash = compute_body_hash(frozen)
    if body_hash != verified_proposal["body_hash"]:
        raise WriteScopeApprovalError(
            f"body_hash mismatch vs stored proposal: "
            f"proposal={verified_proposal['body_hash']} computed={body_hash}"
        )

    approved_ts = approved_at or utc_now_iso()
    # Validate expires_at parses and is strictly after approved_at.
    approved_dt = parse_iso_utc(approved_ts)
    expires_dt = parse_iso_utc(expires_at)
    if expires_dt <= approved_dt:
        raise WriteScopeApprovalError(
            "expires_at must be strictly after approved_at (TTL must be positive)"
        )

    approval_id = compute_approval_id(
        proposal_id=verified_proposal["proposal_id"],
        approved_at=approved_ts,
        approved_by=operator,
        expires_at=expires_at,
        body_hash=body_hash,
    )
    _assert_transition(None, STATUS_APPROVED)
    return {
        "kind": APPROVAL_KIND,
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "approval_id": approval_id,
        "proposal_id": verified_proposal["proposal_id"],
        "run_id": verified_proposal["run_id"],
        "body": frozen,
        "body_hash": body_hash,
        "approved_by": operator,
        "approved_at": approved_ts,
        "expires_at": expires_at,
        "status": STATUS_APPROVED,
        "approval_source": source,
        "provenance": PROVENANCE_OPERATOR_ASSERTED,
    }


def evaluate_expiry(
    record: dict[str, Any],
    *,
    now: datetime | None = None,
    persist: bool = False,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
) -> dict[str, Any]:
    """Lazy-evaluate approved -> expired when now >= expires_at."""
    if not isinstance(record, dict):
        raise WriteScopeApprovalError("approval record is not an object")
    status = str(record.get("status") or "")
    if status != STATUS_APPROVED:
        return dict(record)
    expires_at = str(record.get("expires_at") or "")
    expires_dt = parse_iso_utc(expires_at)
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if current < expires_dt:
        return dict(record)

    _assert_transition(STATUS_APPROVED, STATUS_EXPIRED)
    updated = dict(record)
    updated["status"] = STATUS_EXPIRED
    if persist:
        path = approval_path(updated["approval_id"], base_dir=approvals_dir)
        with _STORE_LOCK:
            _atomic_write_json(path, updated)
        append_scope_event(
            "write_scope.expired",
            {
                "approval_id": updated["approval_id"],
                "proposal_id": updated["proposal_id"],
                "expires_at": updated["expires_at"],
            },
            events_dir=events_dir,
            at=utc_now_iso()
            if now is None
            else current.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )
    return updated


def verify_approval_record(
    record: dict[str, Any],
    *,
    now: datetime | None = None,
    evaluate_ttl: bool = True,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise WriteScopeApprovalError("approval record is not an object")
    if record.get("kind") != APPROVAL_KIND:
        raise WriteScopeApprovalError("approval kind mismatch")
    if record.get("schema_version") != APPROVAL_SCHEMA_VERSION:
        raise WriteScopeApprovalError("unsupported approval schema_version")
    status = str(record.get("status") or "")
    if status not in APPROVAL_STATUSES:
        raise WriteScopeApprovalError(f"unsupported approval status: {status!r}")
    _assert_operator_provenance(str(record.get("provenance") or ""))
    approval_id = str(record.get("approval_id") or "").strip()
    proposal_id = str(record.get("proposal_id") or "").strip()
    run_id = str(record.get("run_id") or "").strip()
    stored_hash = str(record.get("body_hash") or "").strip()
    approved_by = _assert_operator_identity(
        str(record.get("approved_by") or ""), field="approved_by"
    )
    approved_at = str(record.get("approved_at") or "").strip()
    expires_at = str(record.get("expires_at") or "").strip()
    if not approval_id or not proposal_id or not run_id:
        raise WriteScopeApprovalError("approval identity fields are required")
    if not approved_at or not expires_at:
        raise WriteScopeApprovalError("approved_at and expires_at are required")

    frozen = validate_approval_body(record.get("body"))
    expected_hash = compute_body_hash(frozen)
    if stored_hash != expected_hash:
        raise WriteScopeApprovalError(
            f"body_hash mismatch for {approval_id}: stored body was mutated"
        )
    expected_id = compute_approval_id(
        proposal_id=proposal_id,
        approved_at=approved_at,
        approved_by=approved_by,
        expires_at=expires_at,
        body_hash=expected_hash,
    )
    if approval_id != expected_id:
        raise WriteScopeApprovalError(
            f"approval_id mismatch: expected {expected_id}, got {approval_id}"
        )

    source = str(record.get("approval_source") or "").strip()
    if source not in {APPROVAL_SOURCE_CLI, APPROVAL_SOURCE_API}:
        raise WriteScopeApprovalError(f"unsupported approval_source: {source!r}")

    verified: dict[str, Any] = {
        "kind": APPROVAL_KIND,
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "approval_id": approval_id,
        "proposal_id": proposal_id,
        "run_id": run_id,
        "body": frozen,
        "body_hash": expected_hash,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "expires_at": expires_at,
        "status": status,
        "approval_source": source,
        "provenance": PROVENANCE_OPERATOR_ASSERTED,
    }
    revocation_id = record.get("revocation_id")
    if revocation_id is not None:
        verified["revocation_id"] = str(revocation_id)

    if status == STATUS_REVOKED and not verified.get("revocation_id"):
        raise WriteScopeApprovalError("revoked approval missing revocation_id")

    if evaluate_ttl:
        verified = evaluate_expiry(verified, now=now, persist=False)
    return verified


def is_approval_active(
    record: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """True only when status evaluates to approved and TTL has not elapsed.

    Wave 2: active means the grant record is non-terminal. It does NOT mean
    mutation execution is enabled (binding required in Wave 3).
    """
    verified = verify_approval_record(record, now=now, evaluate_ttl=True)
    return verified["status"] == STATUS_APPROVED


def save_approval(
    record: dict[str, Any],
    *,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
    emit_event: bool = True,
) -> dict[str, Any]:
    verified = verify_approval_record(record, evaluate_ttl=False)
    if verified["status"] != STATUS_APPROVED:
        raise WriteScopeApprovalError(
            f"cannot persist non-approved initial status: {verified['status']}"
        )
    path = approval_path(verified["approval_id"], base_dir=approvals_dir)
    with _STORE_LOCK:
        if path.exists():
            existing = load_approval(
                verified["approval_id"],
                approvals_dir=approvals_dir,
                events_dir=events_dir,
                evaluate_ttl=False,
            )
            if (
                existing["proposal_id"] == verified["proposal_id"]
                and existing["body_hash"] == verified["body_hash"]
                and existing["approved_at"] == verified["approved_at"]
                and existing["expires_at"] == verified["expires_at"]
                and existing["approved_by"] == verified["approved_by"]
            ):
                return existing
            raise WriteScopeApprovalError(
                f"approval_id collision with different content: {verified['approval_id']}"
            )
        _atomic_write_json(path, verified)
    if emit_event:
        append_scope_event(
            "write_scope.approved",
            {
                "approval_id": verified["approval_id"],
                "proposal_id": verified["proposal_id"],
                "approved_by": verified["approved_by"],
                "expires_at": verified["expires_at"],
                "body_hash": verified["body_hash"],
            },
            events_dir=events_dir,
            at=verified["approved_at"],
        )
    return verified


def load_approval(
    approval_id: str,
    *,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
    now: datetime | None = None,
    evaluate_ttl: bool = True,
    persist_expiry: bool = True,
) -> dict[str, Any]:
    ref = str(approval_id or "").strip()
    if not ref:
        raise WriteScopeApprovalError("approval_id is required")
    path = approval_path(ref, base_dir=approvals_dir)
    if not path.is_file():
        raise WriteScopeApprovalError(f"approval not found: {ref}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WriteScopeApprovalError(f"corrupt approval record: {ref}") from exc
    verified = verify_approval_record(raw, now=now, evaluate_ttl=False)
    if evaluate_ttl:
        verified = evaluate_expiry(
            verified,
            now=now,
            persist=persist_expiry,
            approvals_dir=approvals_dir,
            events_dir=events_dir,
        )
    return verified


def list_approvals(
    *,
    run_id: str | None = None,
    status: str | None = None,
    proposal_id: str | None = None,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    root = approvals_dir if approvals_dir is not None else write_scope_approvals_dir()
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.glob("wsa-*.json")):
        try:
            record = load_approval(
                path.stem,
                approvals_dir=root,
                events_dir=events_dir,
                now=now,
                evaluate_ttl=True,
                persist_expiry=True,
            )
        except (OSError, WriteScopeApprovalError):
            continue
        if run_id is not None and record["run_id"] != run_id:
            continue
        if proposal_id is not None and record["proposal_id"] != proposal_id:
            continue
        if status is not None and record["status"] != status:
            continue
        items.append(record)
    items.sort(key=lambda item: (item.get("approved_at") or "", item["approval_id"]))
    return items


def approve_proposal(
    proposal_id: str,
    *,
    ttl: str,
    approved_by: str,
    approval_source: str = APPROVAL_SOURCE_CLI,
    provenance: str = PROVENANCE_OPERATOR_ASSERTED,
    approved_at: str | None = None,
    now: datetime | None = None,
    proposals_dir: Path | None = None,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
) -> dict[str, Any]:
    """Create the sole durable operator grant for a stored proposal.

    Fail closed: unknown proposal, hash mismatch, docs_only conflict, empty
    roots, wildcards/traversal (via normalize), non-operator provenance,
    worker identity, missing/invalid TTL.
    """
    _assert_operator_provenance(provenance)
    operator = _assert_operator_identity(approved_by, field="approved_by")
    duration = parse_ttl_duration(ttl)

    try:
        proposal = load_proposal(proposal_id, base_dir=proposals_dir)
    except WriteScopeProposalError as exc:
        raise WriteScopeApprovalError(str(exc)) from exc

    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    approved_ts = (
        approved_at
        or current.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    expires_dt = parse_iso_utc(approved_ts) + duration
    expires_at = expires_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    record = build_approval_record(
        proposal=proposal,
        approved_by=operator,
        expires_at=expires_at,
        approved_at=approved_ts,
        approval_source=approval_source,
        provenance=provenance,
    )
    # Immediate expiry: if TTL already elapsed relative to `now` (e.g. ttl that
    # rounds to past under injected clocks), refuse rather than persist active.
    if current >= parse_iso_utc(expires_at):
        raise WriteScopeApprovalError(
            "approval would be expired at grant time; refuse immediate expiry grant"
        )
    return save_approval(
        record,
        approvals_dir=approvals_dir,
        events_dir=events_dir,
        emit_event=True,
    )


def verify_revocation_record(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise WriteScopeApprovalError("revocation record is not an object")
    if record.get("kind") != REVOCATION_KIND:
        raise WriteScopeApprovalError("revocation kind mismatch")
    if record.get("schema_version") != REVOCATION_SCHEMA_VERSION:
        raise WriteScopeApprovalError("unsupported revocation schema_version")
    _assert_operator_provenance(str(record.get("provenance") or ""))
    revocation_id = str(record.get("revocation_id") or "").strip()
    approval_id = str(record.get("approval_id") or "").strip()
    revoked_by = _assert_operator_identity(
        str(record.get("revoked_by") or ""), field="revoked_by"
    )
    revoked_at = str(record.get("revoked_at") or "").strip()
    reason = str(record.get("reason") or "").strip()
    if not revocation_id or not approval_id or not revoked_at or not reason:
        raise WriteScopeApprovalError("revocation fields are incomplete")
    expected_id = compute_revocation_id(approval_id=approval_id)
    if revocation_id != expected_id:
        raise WriteScopeApprovalError(
            f"revocation_id mismatch: expected {expected_id}, got {revocation_id}"
        )
    return {
        "kind": REVOCATION_KIND,
        "schema_version": REVOCATION_SCHEMA_VERSION,
        "revocation_id": revocation_id,
        "approval_id": approval_id,
        "revoked_by": revoked_by,
        "revoked_at": revoked_at,
        "reason": reason,
        "provenance": PROVENANCE_OPERATOR_ASSERTED,
    }


def load_revocation(
    revocation_id: str,
    *,
    revocations_dir: Path | None = None,
) -> dict[str, Any]:
    ref = str(revocation_id or "").strip()
    if not ref:
        raise WriteScopeApprovalError("revocation_id is required")
    path = revocation_path(ref, base_dir=revocations_dir)
    if not path.is_file():
        raise WriteScopeApprovalError(f"revocation not found: {ref}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WriteScopeApprovalError(f"corrupt revocation record: {ref}") from exc
    return verify_revocation_record(raw)


def revoke_approval(
    approval_id: str,
    *,
    reason: str,
    revoked_by: str,
    provenance: str = PROVENANCE_OPERATOR_ASSERTED,
    revoked_at: str | None = None,
    now: datetime | None = None,
    approvals_dir: Path | None = None,
    revocations_dir: Path | None = None,
    events_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Revoke an approval.

    Returns (approval, revocation, already_revoked).
    Idempotent: re-revoke of a revoked approval returns the existing
    revocation, preserves original revoked_at/reason, and emits an audit event.
    """
    _assert_operator_provenance(provenance)
    operator = _assert_operator_identity(revoked_by, field="revoked_by")
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise WriteScopeApprovalError("revoke reason is required (non-authoritative)")

    approval = load_approval(
        approval_id,
        approvals_dir=approvals_dir,
        events_dir=events_dir,
        now=now,
        evaluate_ttl=True,
        persist_expiry=True,
    )
    status = approval["status"]
    if status == STATUS_REVOKED:
        rev_id = str(approval.get("revocation_id") or "")
        existing = load_revocation(rev_id, revocations_dir=revocations_dir)
        append_scope_event(
            "write_scope.revoke_idempotent",
            {
                "approval_id": approval["approval_id"],
                "revocation_id": existing["revocation_id"],
                "revoked_by": operator,
                "reason": reason_text,
                "note": "already revoked; original revocation retained",
            },
            events_dir=events_dir,
        )
        return approval, existing, True

    if status in TERMINAL_STATUSES:
        raise WriteScopeApprovalError(
            f"cannot revoke approval in terminal status {status!r}"
        )

    _assert_transition(STATUS_APPROVED, STATUS_REVOKED)
    revoked_ts = revoked_at or utc_now_iso()
    revocation = verify_revocation_record(
        {
            "kind": REVOCATION_KIND,
            "schema_version": REVOCATION_SCHEMA_VERSION,
            "revocation_id": compute_revocation_id(approval_id=approval["approval_id"]),
            "approval_id": approval["approval_id"],
            "revoked_by": operator,
            "revoked_at": revoked_ts,
            "reason": reason_text,
            "provenance": PROVENANCE_OPERATOR_ASSERTED,
        }
    )
    updated = dict(approval)
    updated["status"] = STATUS_REVOKED
    updated["revocation_id"] = revocation["revocation_id"]

    with _STORE_LOCK:
        _atomic_write_json(
            revocation_path(revocation["revocation_id"], base_dir=revocations_dir),
            revocation,
        )
        _atomic_write_json(
            approval_path(updated["approval_id"], base_dir=approvals_dir),
            updated,
        )
    append_scope_event(
        "write_scope.revoked",
        {
            "approval_id": updated["approval_id"],
            "revocation_id": revocation["revocation_id"],
            "revoked_by": operator,
            "reason": reason_text,
        },
        events_dir=events_dir,
        at=revoked_ts,
    )
    return updated, revocation, False


def transition_approval_status(
    approval_id: str,
    to_status: str,
    *,
    approvals_dir: Path | None = None,
    events_dir: Path | None = None,
    now: datetime | None = None,
    allow_consumed: bool = False,
) -> dict[str, Any]:
    """Explicit status transition helper.

    Wave 2 rejects consumed transitions unless allow_consumed=True (reserved
    for Wave 3/4 callers). Impossible transitions raise rather than repair.
    Prefer revoke_approval / evaluate_expiry for normal paths.
    """
    target = str(to_status or "").strip()
    if target not in APPROVAL_STATUSES:
        raise WriteScopeApprovalError(f"unknown target status: {target!r}")
    if target == STATUS_CONSUMED and not allow_consumed:
        raise WriteScopeApprovalError(
            "transition to consumed is reserved for Wave 3/4 binding completion"
        )
    if target == STATUS_REVOKED:
        raise WriteScopeApprovalError("use revoke_approval for revoked transitions")
    if target == STATUS_EXPIRED:
        record = load_approval(
            approval_id,
            approvals_dir=approvals_dir,
            events_dir=events_dir,
            now=now,
            evaluate_ttl=True,
            persist_expiry=True,
        )
        if record["status"] != STATUS_EXPIRED:
            raise WriteScopeApprovalError(
                "cannot force expire before expires_at; lazy TTL evaluation only"
            )
        return record
    if target == STATUS_APPROVED:
        raise WriteScopeApprovalError(
            "illegal lifecycle transition: cannot re-activate to approved"
        )
    # allow_consumed path for future waves — still enforce transition table.
    record = load_approval(
        approval_id,
        approvals_dir=approvals_dir,
        events_dir=events_dir,
        now=now,
        evaluate_ttl=True,
        persist_expiry=True,
    )
    _assert_transition(record["status"], target)
    updated = dict(record)
    updated["status"] = target
    _atomic_write_json(
        approval_path(updated["approval_id"], base_dir=approvals_dir),
        updated,
    )
    append_scope_event(
        "write_scope.status_transition",
        {
            "approval_id": updated["approval_id"],
            "from_status": record["status"],
            "to_status": target,
        },
        events_dir=events_dir,
    )
    return updated


def reject_worker_minted_approval(payload: Any) -> None:
    """Explicit guard: workers have no approval mint path.

    Any call attempting to persist a worker-shaped approval payload fails closed.
    """
    raise WriteScopeApprovalError(
        "workers cannot mint WriteScopeApproval records; "
        "operator approve_proposal is the sole grant path"
    )


__all__ = [
    "APPROVAL_ID_PREFIX",
    "APPROVAL_KIND",
    "APPROVAL_SCHEMA_VERSION",
    "APPROVAL_SOURCE_API",
    "APPROVAL_SOURCE_CLI",
    "APPROVAL_STATUSES",
    "LIFECYCLE_TRANSITIONS",
    "PROVENANCE_OPERATOR_ASSERTED",
    "REVOCATION_ID_PREFIX",
    "REVOCATION_KIND",
    "REVOCATION_SCHEMA_VERSION",
    "STATUS_APPROVED",
    "STATUS_CONSUMED",
    "STATUS_EXPIRED",
    "STATUS_REVOKED",
    "TERMINAL_STATUSES",
    "WriteScopeApprovalError",
    "append_scope_event",
    "approve_proposal",
    "approval_path",
    "build_approval_record",
    "compute_approval_id",
    "compute_revocation_id",
    "evaluate_expiry",
    "events_path",
    "is_approval_active",
    "list_approvals",
    "load_approval",
    "load_revocation",
    "parse_iso_utc",
    "parse_ttl_duration",
    "path_is_under_docs",
    "reject_worker_minted_approval",
    "revoke_approval",
    "revocation_path",
    "save_approval",
    "transition_approval_status",
    "validate_approval_body",
    "verify_approval_record",
    "verify_revocation_record",
]
