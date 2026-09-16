"""Local reference-system capability (v0).

Sibling of Write-Scope and Kubernetes capability. Same lifecycle:

    proposal → approval → binding → execution → verification → receipt

One primitive: object + action + state + bounded parameters.
Vertical demos are fixtures over this primitive. This is not an enterprise
integration, policy language, or second permission platform.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .app_paths import ensure_parent_dir, get_app_paths
from .capability import (
    CAPABILITY_REFERENCE_ACTION,
    CapabilityBodyError,
    REFERENCE_CAPABILITIES,
)
from .write_scope_approvals import (
    APPROVAL_SOURCE_CLI,
    PROVENANCE_OPERATOR_ASSERTED,
    STATUS_APPROVED,
    STATUS_CONSUMED,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    WriteScopeApprovalError,
    parse_iso_utc,
    parse_ttl_duration,
)
from .write_scope_bindings import STATUS_ACTIVE, STATUS_COMPLETED, STATUS_FAILED
from .write_scope_proposals import utc_now_iso

PROPOSAL_KIND = "reference_capability_proposal"
APPROVAL_KIND = "reference_capability_approval"
BINDING_KIND = "reference_capability_binding"
RECEIPT_KIND = "reference_capability_receipt"
SCHEMA_VERSION = 1

PROPOSAL_ID_PREFIX = "rcp-"
APPROVAL_ID_PREFIX = "rca-"
BINDING_ID_PREFIX = "rcb-"
RECEIPT_ID_PREFIX = "rcr-"

COMPLIANCE_WITHIN_SCOPE = "within_scope"
COMPLIANCE_DENIED = "denied"
COMPLIANCE_UNVERIFIED = "unverified"

ACCEPTANCE_PASS = "PASS"
ACCEPTANCE_FAIL = "FAIL"
ACCEPTANCE_UNVERIFIED = "UNVERIFIED"

_SYSTEM_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._:-]{0,61}[A-Za-z0-9])?$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._:-]{0,61}[A-Za-z0-9])?$")
_WORKER_IDENTITY_MARKERS = frozenset(
    {"worker", "agent", "hermes", "claude", "backend", "runner", "self", "model"}
)
BODY_HASH_FIELDS = (
    "capability",
    "system",
    "objects",
    "actions",
    "constraints",
    "denied_objects",
    "denied_actions",
)

_STORE_LOCK = threading.RLock()


class ReferenceCapabilityError(ValueError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def _assert_operator_identity(identity: str, *, field: str) -> str:
    value = str(identity or "").strip()
    if not value:
        raise ReferenceCapabilityError(f"{field} is required (operator identity)", code="invalid_approval")
    lowered = value.lower()
    if lowered in _WORKER_IDENTITY_MARKERS or lowered.startswith("worker:"):
        raise ReferenceCapabilityError(
            f"{field} rejects worker/self-approval identity: {value!r}",
            code="invalid_approval",
        )
    return value


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


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _id_from(prefix: str, material: str) -> str:
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}{digest}"


def _sha256_canonical(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _string_list(value: Any, *, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        if any(not isinstance(item, str) for item in value):
            raise CapabilityBodyError(f"{name} entries must be strings")
        items = list(value)
    else:
        raise CapabilityBodyError(f"{name} must be a string or list of strings")
    cleaned = [item.strip() for item in items]
    if any(not item for item in cleaned):
        raise CapabilityBodyError(f"{name} rejects empty entries")
    return _unique_preserve(cleaned)


def expand_object_range(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise CapabilityBodyError("object_range must be an object")
    prefix = str(value.get("prefix") or "")
    try:
        start = int(value.get("start"))
        end = int(value.get("end"))
        width = int(value.get("width") or 0)
    except (TypeError, ValueError) as exc:
        raise CapabilityBodyError("object_range start/end/width must be integers") from exc
    if start > end or (end - start) > 500:
        raise CapabilityBodyError("object_range is empty or exceeds 500 objects")
    out = []
    for index in range(start, end + 1):
        token = f"{prefix}{index:0{width}d}" if width else f"{prefix}{index}"
        out.append(token)
    return out


def _normalize_constraints(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CapabilityBodyError("constraints must be an object")
    out: dict[str, Any] = {}
    for key, raw in value.items():
        name = str(key or "").strip()
        if not name or _TOKEN_RE.fullmatch(name) is None:
            raise CapabilityBodyError(f"invalid constraint key: {key!r}")
        if isinstance(raw, bool) or raw is None:
            out[name] = raw
        elif isinstance(raw, int) and not isinstance(raw, bool):
            out[name] = raw
        elif isinstance(raw, str):
            cleaned = raw.strip()
            if not cleaned:
                raise CapabilityBodyError(f"constraint {name} rejects empty values")
            out[name] = cleaned
        else:
            raise CapabilityBodyError(f"constraint {name} must be a string, int, or bool")
    return out


def normalize_reference_capability_body(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapabilityBodyError("capability body must be an object")
    capability = str(value.get("capability") or "").strip()
    if capability not in REFERENCE_CAPABILITIES:
        raise CapabilityBodyError(
            f"unsupported capability {capability!r}; v0 supports {sorted(REFERENCE_CAPABILITIES)}"
        )
    system = str(value.get("system") or "").strip()
    if not system or _SYSTEM_RE.fullmatch(system) is None:
        raise CapabilityBodyError("system must be a logical reference-system id")
    objects = _string_list(value.get("objects"), name="objects")
    objects.extend(expand_object_range(value.get("object_range")))
    objects = _unique_preserve(objects)
    if not objects:
        raise CapabilityBodyError("at least one object is required")
    bad_objects = [item for item in objects if _TOKEN_RE.fullmatch(item) is None]
    if bad_objects:
        raise CapabilityBodyError(f"invalid object id(s): {bad_objects}")
    actions = [item.lower() for item in _string_list(value.get("actions"), name="actions")]
    if not actions:
        raise CapabilityBodyError("at least one action is required")
    bad_actions = [item for item in actions if _TOKEN_RE.fullmatch(item) is None]
    if bad_actions:
        raise CapabilityBodyError(f"invalid action(s): {bad_actions}")
    denied_objects = _string_list(value.get("denied_objects"), name="denied_objects")
    denied_actions = [
        item.lower() for item in _string_list(value.get("denied_actions"), name="denied_actions")
    ]
    reason = str(value.get("reason") or "").strip()
    if not reason:
        raise CapabilityBodyError("reason is required")
    return {
        "capability": capability,
        "system": system,
        "objects": objects,
        "actions": actions,
        "constraints": _normalize_constraints(value.get("constraints")),
        "denied_objects": denied_objects,
        "denied_actions": denied_actions,
        "reason": reason,
    }


def compute_reference_body_hash(body: dict[str, Any]) -> str:
    payload = {field: body[field] for field in BODY_HASH_FIELDS}
    return _sha256_canonical(payload)


@dataclass(frozen=True)
class ReferenceCapabilityStore:
    proposals_dir: Path
    approvals_dir: Path
    bindings_dir: Path
    receipts_dir: Path
    events_dir: Path

    @classmethod
    def from_env(cls) -> ReferenceCapabilityStore:
        root = get_app_paths().data_root / "capabilities" / "reference"
        return cls(
            proposals_dir=root / "proposals",
            approvals_dir=root / "approvals",
            bindings_dir=root / "bindings",
            receipts_dir=root / "receipts",
            events_dir=root / "events",
        )


@dataclass(frozen=True)
class ReferenceOperation:
    system: str
    object: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    mission_id: str = ""
    requested_by: str = ""
    run_id: str = ""


@dataclass
class ExecutionOutcome:
    ok: bool
    code: str
    receipt: dict[str, Any] | None = None
    denial: dict[str, Any] | None = None
    binding: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None


class ReferenceTransport(Protocol):
    def apply(self, operation: ReferenceOperation) -> dict[str, Any]:
        """Mutate state. Must not be called when authority classified a denial."""


class RecordingReferenceTransport:
    """In-process reference-system transport. Records every invocation."""

    def __init__(self, state: dict[str, dict[str, Any]]):
        self.state = copy.deepcopy(state)
        self.calls: list[dict[str, Any]] = []

    def apply(self, operation: ReferenceOperation) -> dict[str, Any]:
        self.calls.append(
            {
                "system": operation.system,
                "object": operation.object,
                "action": operation.action,
                "params": copy.deepcopy(operation.params),
            }
        )
        current = copy.deepcopy(self.state.get(operation.object) or {})
        updated = _apply_action(current, operation.action, operation.params)
        self.state[operation.object] = updated
        return copy.deepcopy(updated)

    def get(self, object_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.state.get(object_id) or {})


def _apply_action(current: dict[str, Any], action: str, params: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(current)
    updated["generation"] = int(updated.get("generation") or 0) + 1
    if action == "migrate":
        updated["location"] = str(params.get("target") or "")
        updated["migrated"] = True
        return updated
    if action == "disable":
        updated["enabled"] = False
        return updated
    if action == "set_status":
        updated["status"] = str(params.get("status") or "")
        return updated
    if action == "adjust":
        updated["value"] = int(params.get("amount") or 0)
        return updated
    if action == "append_note":
        notes = list(updated.get("notes") or [])
        notes.append(str(params.get("note") or ""))
        updated["notes"] = notes
        return updated
    raise ReferenceCapabilityError(f"unsupported reference action: {action}", code="wrong_action")


def classify_operation(body: dict[str, Any], operation: ReferenceOperation) -> str | None:
    frozen = normalize_reference_capability_body(body)
    action = str(operation.action or "").strip().lower()
    obj = str(operation.object or "").strip()
    system = str(operation.system or "").strip()
    if system != frozen["system"]:
        return "wrong_system"
    if obj in frozen["denied_objects"] or obj not in frozen["objects"]:
        return "wrong_object"
    if action in frozen["denied_actions"] or action not in frozen["actions"]:
        return "wrong_action"
    constraints = frozen["constraints"]
    params = operation.params or {}
    if "max_amount" in constraints:
        try:
            amount = int(params.get("amount"))
        except (TypeError, ValueError):
            return "constraint_violation"
        if amount > int(constraints["max_amount"]):
            return "constraint_violation"
    for key, expected in constraints.items():
        if key == "max_amount":
            continue
        if str(params.get(key, "")) != str(expected):
            return "constraint_violation"
    return None


class ReferenceExecutor:
    def __init__(self, transport: RecordingReferenceTransport):
        self.transport = transport

    def execute(self, operation: ReferenceOperation) -> dict[str, Any]:
        before = self.transport.get(operation.object)
        after = self.transport.apply(operation)
        verified = after != before or operation.action == "append_note"
        if operation.action == "migrate":
            verified = after.get("location") == operation.params.get("target") and after.get("migrated") is True
        elif operation.action == "disable":
            verified = after.get("enabled") is False
        elif operation.action == "set_status":
            verified = after.get("status") == operation.params.get("status")
        elif operation.action == "adjust":
            verified = after.get("value") == int(operation.params.get("amount") or 0)
        elif operation.action == "append_note":
            notes = after.get("notes") or []
            verified = bool(notes) and notes[-1] == operation.params.get("note")
        return {
            "executed": True,
            "verified": bool(verified),
            "verification_method": "reference_state_readback",
            "before": before,
            "after": after,
            "change": {
                "before_digest": _sha256_canonical(before),
                "after_digest": _sha256_canonical(after),
                "before_generation": before.get("generation"),
                "after_generation": after.get("generation"),
                "changed": before != after,
            },
        }


def append_capability_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    store: ReferenceCapabilityStore,
) -> None:
    event = {"event_type": str(event_type), "at": utc_now_iso(), **payload}
    path = store.events_dir / "reference-capability-events.jsonl"
    ensure_parent_dir(path)
    with _STORE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def build_proposal_record(
    *,
    run_id: str,
    body: dict[str, Any],
    requested_by: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    parent = str(run_id or "").strip()
    worker = str(requested_by or "").strip()
    if not parent or not worker:
        raise ReferenceCapabilityError("run_id and requested_by are required", code="invalid_proposal")
    try:
        frozen = normalize_reference_capability_body(body)
    except CapabilityBodyError as exc:
        raise ReferenceCapabilityError(str(exc), code="invalid_proposal") from exc
    body_hash = compute_reference_body_hash(frozen)
    return {
        "kind": PROPOSAL_KIND,
        "schema_version": SCHEMA_VERSION,
        "proposal_id": _id_from(PROPOSAL_ID_PREFIX, f"{parent}:{body_hash}"),
        "run_id": parent,
        "requested_by": worker,
        "body": frozen,
        "body_hash": body_hash,
        "created_at": created_at or utc_now_iso(),
        "status": "proposed",
    }


def propose_reference_capability(
    *,
    run_id: str,
    body: dict[str, Any],
    requested_by: str,
    store: ReferenceCapabilityStore | None = None,
) -> dict[str, Any]:
    store = store or ReferenceCapabilityStore.from_env()
    record = build_proposal_record(run_id=run_id, body=body, requested_by=requested_by)
    path = store.proposals_dir / f"{record['proposal_id']}.json"
    with _STORE_LOCK:
        if path.exists():
            return _read_json(path)
        _atomic_write_json(path, record)
    append_capability_event(
        "reference_capability.proposed",
        {"proposal_id": record["proposal_id"], "run_id": record["run_id"]},
        store=store,
    )
    return record


def load_proposal(proposal_id: str, *, store: ReferenceCapabilityStore) -> dict[str, Any]:
    path = store.proposals_dir / f"{proposal_id}.json"
    if not path.is_file():
        raise ReferenceCapabilityError(f"proposal not found: {proposal_id}", code="invalid_proposal")
    return _read_json(path)


def compute_approval_id(
    *,
    proposal_id: str,
    approved_at: str,
    approved_by: str,
    expires_at: str,
    body_hash: str,
) -> str:
    return _id_from(
        APPROVAL_ID_PREFIX,
        f"{proposal_id}:{approved_at}:{approved_by}:{expires_at}:{body_hash}",
    )


def _evaluate_approval_ttl(record: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    if record.get("status") != STATUS_APPROVED:
        return record
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if current >= parse_iso_utc(str(record.get("expires_at") or "")):
        updated = dict(record)
        updated["status"] = STATUS_EXPIRED
        return updated
    return record


def load_approval(
    approval_id: str,
    *,
    store: ReferenceCapabilityStore,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = store.approvals_dir / f"{approval_id}.json"
    if not path.is_file():
        raise ReferenceCapabilityError(f"approval not found: {approval_id}", code="no_approval")
    return _evaluate_approval_ttl(_read_json(path), now=now)


def approve_proposal(
    proposal_id: str,
    *,
    ttl: str,
    approved_by: str,
    store: ReferenceCapabilityStore | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    store = store or ReferenceCapabilityStore.from_env()
    operator = _assert_operator_identity(approved_by, field="approved_by")
    try:
        duration = parse_ttl_duration(ttl)
    except WriteScopeApprovalError as exc:
        raise ReferenceCapabilityError(str(exc), code="invalid_ttl") from exc
    proposal = load_proposal(proposal_id, store=store)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    approved_at = current.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    expires_at = (parse_iso_utc(approved_at) + duration).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    record = {
        "kind": APPROVAL_KIND,
        "schema_version": SCHEMA_VERSION,
        "approval_id": compute_approval_id(
            proposal_id=proposal["proposal_id"],
            approved_at=approved_at,
            approved_by=operator,
            expires_at=expires_at,
            body_hash=proposal["body_hash"],
        ),
        "proposal_id": proposal["proposal_id"],
        "run_id": proposal["run_id"],
        "requested_by": proposal["requested_by"],
        "body": proposal["body"],
        "body_hash": proposal["body_hash"],
        "approved_by": operator,
        "approved_at": approved_at,
        "expires_at": expires_at,
        "status": STATUS_APPROVED,
        "approval_source": APPROVAL_SOURCE_CLI,
        "provenance": PROVENANCE_OPERATOR_ASSERTED,
    }
    path = store.approvals_dir / f"{record['approval_id']}.json"
    with _STORE_LOCK:
        if path.exists():
            return load_approval(record["approval_id"], store=store)
        _atomic_write_json(path, record)
    append_capability_event(
        "reference_capability.approved",
        {"approval_id": record["approval_id"], "proposal_id": record["proposal_id"]},
        store=store,
    )
    return record


def _list_bindings(store: ReferenceCapabilityStore) -> list[dict[str, Any]]:
    if not store.bindings_dir.is_dir():
        return []
    return [_read_json(path) for path in sorted(store.bindings_dir.glob("rcb-*.json"))]


def create_binding(
    approval_id: str,
    *,
    run_id: str,
    store: ReferenceCapabilityStore | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    store = store or ReferenceCapabilityStore.from_env()
    approval = load_approval(approval_id, store=store, now=now)
    status = approval.get("status")
    if status == STATUS_EXPIRED:
        raise ReferenceCapabilityError("approval expired; cannot bind", code="expired")
    if status == STATUS_REVOKED:
        raise ReferenceCapabilityError("approval revoked; cannot bind", code="revoked")
    if status == STATUS_CONSUMED:
        raise ReferenceCapabilityError("approval consumed; cannot bind", code="reused_single_use")
    if status != STATUS_APPROVED:
        raise ReferenceCapabilityError(f"approval is not active: {status}", code="no_approval")
    for existing in _list_bindings(store):
        if existing.get("approval_id") != approval["approval_id"]:
            continue
        if existing.get("status") in {STATUS_ACTIVE, STATUS_COMPLETED}:
            raise ReferenceCapabilityError(
                f"approval already bound ({existing['status']}): {existing['binding_id']}",
                code="reused_single_use",
            )
    bound_at = utc_now_iso()
    binding = {
        "kind": BINDING_KIND,
        "schema_version": SCHEMA_VERSION,
        "binding_id": _id_from(
            BINDING_ID_PREFIX,
            f"{approval['approval_id']}:{run_id}:{bound_at}",
        ),
        "approval_id": approval["approval_id"],
        "proposal_id": approval["proposal_id"],
        "run_id": str(run_id or "").strip() or approval["run_id"],
        "bound_at": bound_at,
        "status": STATUS_ACTIVE,
        "body": approval["body"],
        "body_hash": approval["body_hash"],
        "capability": approval["body"]["capability"],
        "consumes_approval": False,
    }
    _atomic_write_json(store.bindings_dir / f"{binding['binding_id']}.json", binding)
    append_capability_event(
        "reference_capability.bound",
        {"binding_id": binding["binding_id"], "approval_id": binding["approval_id"]},
        store=store,
    )
    return binding


def _finalize_binding(
    binding_id: str,
    *,
    status: str,
    consumes_approval: bool,
    reason: str | None,
    store: ReferenceCapabilityStore,
) -> dict[str, Any]:
    path = store.bindings_dir / f"{binding_id}.json"
    record = _read_json(path)
    if record["status"] != STATUS_ACTIVE:
        raise ReferenceCapabilityError(
            f"illegal binding transition {record['status']!r} -> {status!r}",
            code="invalid_binding",
        )
    record["status"] = status
    record["consumes_approval"] = bool(consumes_approval)
    record["terminal_at"] = utc_now_iso()
    if reason:
        record["terminal_reason"] = reason
    _atomic_write_json(path, record)
    if consumes_approval and status == STATUS_COMPLETED:
        approval_path = store.approvals_dir / f"{record['approval_id']}.json"
        approval = _read_json(approval_path)
        approval["status"] = STATUS_CONSUMED
        _atomic_write_json(approval_path, approval)
    return record


def _acceptance_for(compliance: str, *, verified: bool, executed: bool) -> str:
    if compliance == COMPLIANCE_DENIED:
        return ACCEPTANCE_FAIL
    if not verified or compliance == COMPLIANCE_UNVERIFIED:
        return ACCEPTANCE_UNVERIFIED
    if executed and verified:
        return ACCEPTANCE_PASS
    return ACCEPTANCE_UNVERIFIED


def _build_receipt(
    *,
    binding: dict[str, Any],
    approval: dict[str, Any],
    operation: ReferenceOperation,
    compliance: str,
    within_scope: bool,
    executed: bool,
    verified: bool,
    verification_method: str | None,
    change: dict[str, Any] | None,
    stop_reason: str | None,
    code: str | None,
) -> dict[str, Any]:
    acceptance = _acceptance_for(compliance, verified=verified, executed=executed)
    if acceptance == ACCEPTANCE_PASS and (not verified or not executed or not within_scope):
        acceptance = ACCEPTANCE_UNVERIFIED
    created_at = utc_now_iso()
    op_payload = {
        "action": operation.action,
        "object": operation.object,
        "params": operation.params,
    }
    receipt = {
        "kind": RECEIPT_KIND,
        "schema_version": SCHEMA_VERSION,
        "receipt_id": _id_from(
            RECEIPT_ID_PREFIX,
            f"{binding['binding_id']}:{operation.run_id}:{created_at}:{compliance}",
        ),
        "run_id": operation.run_id,
        "mission_id": operation.mission_id or binding.get("run_id") or "",
        "requested_by": operation.requested_by,
        "proposal_id": binding.get("proposal_id"),
        "approval_id": binding["approval_id"],
        "binding_id": binding["binding_id"],
        "capability": CAPABILITY_REFERENCE_ACTION,
        "granted": approval.get("body") or {},
        "target": {"system": operation.system, "object": operation.object},
        "operation": {"action": operation.action, "digest": _sha256_canonical(op_payload)},
        "change": change,
        "within_scope": within_scope,
        "verification": {"verified": verified, "method": verification_method},
        "executed": executed,
        "compliance": compliance,
        "acceptance_state": acceptance,
        "binding_status": binding.get("status"),
        "approval_status": approval.get("status"),
        "created_at": created_at,
        "evaluated_at": created_at,
        "stop_reason": stop_reason,
        "denial_code": code,
        "secrets_omitted": True,
    }
    return receipt


def execute_reference_capability(
    *,
    operation: ReferenceOperation,
    approval_id: str | None = None,
    executor: ReferenceExecutor,
    store: ReferenceCapabilityStore | None = None,
    now: datetime | None = None,
) -> ExecutionOutcome:
    store = store or ReferenceCapabilityStore.from_env()
    op = ReferenceOperation(
        system=str(operation.system or "").strip(),
        object=str(operation.object or "").strip(),
        action=str(operation.action or "").strip().lower(),
        params=dict(operation.params or {}),
        mission_id=str(operation.mission_id or "").strip(),
        requested_by=str(operation.requested_by or "").strip(),
        run_id=str(operation.run_id or "").strip(),
    )
    if not op.run_id:
        raise ReferenceCapabilityError("run_id is required", code="invalid_request")
    if not approval_id:
        return ExecutionOutcome(
            ok=False,
            code="no_approval",
            denial={"code": "no_approval", "message": "no approval provided"},
        )
    try:
        approval = load_approval(approval_id, store=store, now=now)
        binding = create_binding(approval["approval_id"], run_id=op.run_id, store=store, now=now)
    except ReferenceCapabilityError as exc:
        return ExecutionOutcome(ok=False, code=exc.code or "no_approval", denial={"code": exc.code})

    scope_code = classify_operation(binding["body"], op)
    if scope_code:
        binding = _finalize_binding(
            binding["binding_id"],
            status=STATUS_FAILED,
            consumes_approval=False,
            reason=scope_code,
            store=store,
        )
        receipt = _build_receipt(
            binding=binding,
            approval=approval,
            operation=op,
            compliance=COMPLIANCE_DENIED,
            within_scope=False,
            executed=False,
            verified=True,
            verification_method="pre_execution_scope_check",
            change=None,
            stop_reason=scope_code,
            code=scope_code,
        )
        _atomic_write_json(store.receipts_dir / f"{receipt['receipt_id']}.json", receipt)
        return ExecutionOutcome(ok=False, code=scope_code, receipt=receipt, binding=binding, approval=approval)

    result = executor.execute(op)
    executed = bool(result.get("executed"))
    verified = bool(result.get("verified"))
    if not executed or not verified:
        binding = _finalize_binding(
            binding["binding_id"],
            status=STATUS_FAILED,
            consumes_approval=False,
            reason="unverified",
            store=store,
        )
        receipt = _build_receipt(
            binding=binding,
            approval=approval,
            operation=op,
            compliance=COMPLIANCE_UNVERIFIED,
            within_scope=True,
            executed=executed,
            verified=False,
            verification_method=result.get("verification_method"),
            change=None,
            stop_reason="executor returned unverified result",
            code="unverified",
        )
        _atomic_write_json(store.receipts_dir / f"{receipt['receipt_id']}.json", receipt)
        return ExecutionOutcome(ok=False, code="unverified", receipt=receipt, binding=binding, approval=approval)

    binding = _finalize_binding(
        binding["binding_id"],
        status=STATUS_COMPLETED,
        consumes_approval=True,
        reason="verified",
        store=store,
    )
    approval = load_approval(approval["approval_id"], store=store, now=now)
    receipt = _build_receipt(
        binding=binding,
        approval=approval,
        operation=op,
        compliance=COMPLIANCE_WITHIN_SCOPE,
        within_scope=True,
        executed=True,
        verified=True,
        verification_method=result.get("verification_method"),
        change=result.get("change"),
        stop_reason=None,
        code=None,
    )
    _atomic_write_json(store.receipts_dir / f"{receipt['receipt_id']}.json", receipt)
    return ExecutionOutcome(ok=True, code="ok", receipt=receipt, binding=binding, approval=approval)


__all__ = [
    "ACCEPTANCE_FAIL",
    "ACCEPTANCE_PASS",
    "ACCEPTANCE_UNVERIFIED",
    "ExecutionOutcome",
    "RecordingReferenceTransport",
    "ReferenceCapabilityError",
    "ReferenceCapabilityStore",
    "ReferenceExecutor",
    "ReferenceOperation",
    "approve_proposal",
    "classify_operation",
    "execute_reference_capability",
    "normalize_reference_capability_body",
    "propose_reference_capability",
]
