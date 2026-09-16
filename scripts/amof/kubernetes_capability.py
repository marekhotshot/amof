"""Kubernetes capability authority (v0).

Sibling of Write-Scope Authority. Same lifecycle, different body:

    proposal → approval → binding → execution → verification → receipt

A worker proposal is never authority. Execution without a valid binding fails
closed. The default executor is an in-process fixture — not a cluster client.

Does not replace Kubernetes RBAC, OS isolation, or the Git write-scope store.
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
    CAPABILITY_KUBERNETES_MUTATE,
    CapabilityBodyError,
    compute_kubernetes_body_hash,
    normalize_kubernetes_capability_body,
    verb_requires_mutate,
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
from .write_scope_bindings import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from .write_scope_proposals import utc_now_iso

PROPOSAL_KIND = "kubernetes_capability_proposal"
APPROVAL_KIND = "kubernetes_capability_approval"
BINDING_KIND = "kubernetes_capability_binding"
RECEIPT_KIND = "kubernetes_capability_receipt"
REVOCATION_KIND = "kubernetes_capability_revocation"
DENIAL_KIND = "kubernetes_capability_denial"
SCHEMA_VERSION = 1

PROPOSAL_ID_PREFIX = "kcp-"
APPROVAL_ID_PREFIX = "kca-"
BINDING_ID_PREFIX = "kcb-"
RECEIPT_ID_PREFIX = "kcr-"
REVOCATION_ID_PREFIX = "kcv-"

PROPOSAL_STATUS_PROPOSED = "proposed"

COMPLIANCE_WITHIN_SCOPE = "within_scope"
COMPLIANCE_DENIED = "denied"
COMPLIANCE_UNVERIFIED = "unverified"

ACCEPTANCE_PASS = "PASS"
ACCEPTANCE_FAIL = "FAIL"
ACCEPTANCE_UNVERIFIED = "UNVERIFIED"

ALLOWED_PATCH_KEYS = frozenset({"replicas", "annotation"})
ALLOWED_ANNOTATION_KEY = "amof.dev/capability-probe"
_ANNOTATION_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/=@-]{1,128}$")
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


def _assert_operator_identity(identity: str, *, field: str) -> str:
    value = str(identity or "").strip()
    if not value:
        raise KubernetesCapabilityError(f"{field} is required (operator identity)", code="invalid_approval")
    lowered = value.lower()
    if lowered in _WORKER_IDENTITY_MARKERS or lowered.startswith("worker:"):
        raise KubernetesCapabilityError(
            f"{field} rejects worker/self-approval identity: {value!r}",
            code="invalid_approval",
        )
    return value


def _assert_operator_provenance(provenance: str) -> str:
    value = str(provenance or "").strip()
    if value != PROVENANCE_OPERATOR_ASSERTED:
        raise KubernetesCapabilityError(
            f"provenance must be {PROVENANCE_OPERATOR_ASSERTED!r}; got {value!r}",
            code="invalid_approval",
        )
    return value


class KubernetesCapabilityError(ValueError):
    """Fail-closed capability authority error."""

    def __init__(self, message: str, *, code: str | None = None, denial: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.denial = denial


class KubernetesCapabilityInterrupted(KubernetesCapabilityError):
    """Execution was interrupted before verification."""


@dataclass(frozen=True)
class KubernetesCapabilityStore:
    """App-data directories for the Kubernetes capability sibling store."""

    proposals_dir: Path
    approvals_dir: Path
    bindings_dir: Path
    receipts_dir: Path
    revocations_dir: Path
    events_dir: Path

    @classmethod
    def from_env(cls) -> KubernetesCapabilityStore:
        root = get_app_paths().data_root / "capabilities" / "kubernetes"
        return cls(
            proposals_dir=root / "proposals",
            approvals_dir=root / "approvals",
            bindings_dir=root / "bindings",
            receipts_dir=root / "receipts",
            revocations_dir=root / "revocations",
            events_dir=root / "events",
        )


@dataclass(frozen=True)
class KubernetesOperation:
    """Requested Kubernetes action. Patch bodies are structured and hashed."""

    cluster: str
    namespace: str
    verb: str
    resource: str
    name: str | None = None
    patch: dict[str, Any] | None = None
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


class KubernetesExecutor(Protocol):
    def execute(self, operation: KubernetesOperation) -> dict[str, Any]:
        """Return a verification payload. Must not claim success if unverified."""


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


def append_capability_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    store: KubernetesCapabilityStore,
    at: str | None = None,
) -> dict[str, Any]:
    event = {"event_type": str(event_type), "at": at or utc_now_iso(), **payload}
    path = store.events_dir / "kubernetes-capability-events.jsonl"
    ensure_parent_dir(path)
    with _STORE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return event


def _normalize_annotation(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise KubernetesCapabilityError("patch.annotation must be an object", code="invalid_patch")
    extra = set(value) - {"key", "value"}
    if extra:
        raise KubernetesCapabilityError(
            f"unsupported annotation fields {sorted(extra)}",
            code="invalid_patch",
        )
    key = str(value.get("key") or "").strip()
    if key != ALLOWED_ANNOTATION_KEY:
        raise KubernetesCapabilityError(
            f"annotation key must be {ALLOWED_ANNOTATION_KEY}",
            code="invalid_patch",
        )
    text = value.get("value")
    if not isinstance(text, str) or _ANNOTATION_VALUE_RE.fullmatch(text) is None:
        raise KubernetesCapabilityError(
            "annotation value must be a bounded AMOF-owned token",
            code="invalid_patch",
        )
    return {"key": key, "value": text}


def normalize_patch(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise KubernetesCapabilityError("patch must be an object", code="invalid_patch")
    extra = set(value) - ALLOWED_PATCH_KEYS
    if extra:
        raise KubernetesCapabilityError(
            f"unsupported patch fields {sorted(extra)}; v0 allows {sorted(ALLOWED_PATCH_KEYS)}",
            code="invalid_patch",
        )
    if not value:
        raise KubernetesCapabilityError("patch must not be empty", code="invalid_patch")
    if "replicas" in value and "annotation" in value:
        raise KubernetesCapabilityError(
            "patch must use one bounded operation shape",
            code="invalid_patch",
        )
    if "replicas" in value:
        replicas = value["replicas"]
        if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 0:
            raise KubernetesCapabilityError(
                "patch.replicas must be a non-negative integer",
                code="invalid_patch",
            )
        return {"replicas": int(replicas)}
    return {"annotation": _normalize_annotation(value.get("annotation"))}


def operation_digest(operation: KubernetesOperation) -> str:
    payload = {
        "cluster": operation.cluster,
        "namespace": operation.namespace,
        "verb": operation.verb,
        "resource": operation.resource,
        "name": operation.name,
        "patch": normalize_patch(operation.patch),
    }
    return _sha256_canonical(payload)


def classify_operation(body: dict[str, Any], operation: KubernetesOperation) -> str | None:
    """Return a denial code if the operation is outside the frozen grant."""
    frozen = normalize_kubernetes_capability_body(body)
    verb = str(operation.verb or "").strip().lower()
    resource = str(operation.resource or "").strip().lower()
    namespace = str(operation.namespace or "").strip()
    cluster = str(operation.cluster or "").strip()

    if cluster != frozen["cluster"]:
        return "wrong_cluster"
    if namespace in frozen["denied_namespaces"]:
        return "wrong_namespace"
    if namespace not in frozen["namespaces"]:
        return "wrong_namespace"
    if resource in frozen["denied_resources"]:
        return "wrong_resource"
    if resource not in frozen["resources"]:
        return "wrong_resource"
    if verb_requires_mutate(verb) and frozen["capability"] != CAPABILITY_KUBERNETES_MUTATE:
        return "scope_escalation"
    if verb in frozen["denied_verbs"]:
        return "wrong_verb"
    if verb not in frozen["verbs"]:
        return "wrong_verb"
    if verb_requires_mutate(verb) and operation.name in (None, ""):
        return "wrong_resource"
    if verb == "get" and operation.name in (None, ""):
        return "wrong_resource"
    return None


# --- fixture executor -------------------------------------------------------

DEFAULT_FIXTURE_CLUSTER = "local-fixture"


def default_fixture_state() -> dict[str, Any]:
    return {
        "cluster": DEFAULT_FIXTURE_CLUSTER,
        "namespaces": {
            "demo": {
                "deployments": {
                    "web": {
                        "replicas": 1,
                        "generation": 1,
                        "resource_version": "1",
                        "annotations": {},
                    }
                }
            }
        },
    }


@dataclass
class FixtureKubernetesExecutor:
    """In-process fake cluster. No kubeconfig, no network, no secrets."""

    state: dict[str, Any] = field(default_factory=default_fixture_state)

    def execute(self, operation: KubernetesOperation) -> dict[str, Any]:
        if str(self.state.get("cluster") or "") != operation.cluster:
            raise KubernetesCapabilityError(
                f"fixture cluster mismatch: {self.state.get('cluster')!r}",
                code="wrong_cluster",
            )
        ns = (self.state.get("namespaces") or {}).get(operation.namespace)
        if not isinstance(ns, dict):
            raise KubernetesCapabilityError(
                f"fixture namespace missing: {operation.namespace}",
                code="wrong_namespace",
            )
        bucket = ns.get(operation.resource)
        if not isinstance(bucket, dict):
            raise KubernetesCapabilityError(
                f"fixture resource missing: {operation.resource}",
                code="wrong_resource",
            )
        verb = operation.verb
        if verb == "list":
            names = sorted(bucket)
            return {
                "executed": True,
                "verified": True,
                "verification_method": "fixture_state_compare",
                "result_digest": _sha256_canonical({"list": names}),
                "before": None,
                "after": {"names": names},
                "changed": False,
            }
        if verb == "get":
            current = bucket.get(operation.name)
            if current is None:
                raise KubernetesCapabilityError(
                    f"fixture object missing: {operation.name}",
                    code="wrong_resource",
                )
            snapshot = copy.deepcopy(current)
            return {
                "executed": True,
                "verified": True,
                "verification_method": "fixture_state_compare",
                "result_digest": _sha256_canonical(snapshot),
                "before": snapshot,
                "after": snapshot,
                "changed": False,
            }
        if verb == "patch":
            current = bucket.get(operation.name)
            if current is None:
                raise KubernetesCapabilityError(
                    f"fixture object missing: {operation.name}",
                    code="wrong_resource",
                )
            patch = normalize_patch(operation.patch)
            if patch is None:
                raise KubernetesCapabilityError("patch is required for mutate", code="invalid_patch")
            before = copy.deepcopy(current)
            updated = copy.deepcopy(current)
            if "replicas" in patch:
                updated["replicas"] = patch["replicas"]
            if "annotation" in patch:
                annotations = dict(updated.get("annotations") or {})
                annotations[patch["annotation"]["key"]] = patch["annotation"]["value"]
                updated["annotations"] = annotations
            updated["generation"] = int(updated.get("generation") or 0) + 1
            updated["resource_version"] = str(int(updated.get("resource_version") or 0) + 1)
            bucket[operation.name] = updated
            return {
                "executed": True,
                "verified": True,
                "verification_method": "fixture_state_compare",
                "result_digest": _sha256_canonical({"before": before, "after": updated}),
                "before_digest": _sha256_canonical(before),
                "after_digest": _sha256_canonical(updated),
                "before": before,
                "after": copy.deepcopy(updated),
                "changed": before != updated,
            }
        raise KubernetesCapabilityError(f"unsupported fixture verb: {verb}", code="wrong_verb")


# --- proposal ---------------------------------------------------------------

def build_proposal_record(
    *,
    run_id: str,
    body: dict[str, Any],
    requested_by: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    parent = str(run_id or "").strip()
    if not parent:
        raise KubernetesCapabilityError("missing parent run_id", code="invalid_proposal")
    worker = str(requested_by or "").strip()
    if not worker:
        raise KubernetesCapabilityError("requested_by is required", code="invalid_proposal")
    try:
        frozen = normalize_kubernetes_capability_body(body)
    except CapabilityBodyError as exc:
        raise KubernetesCapabilityError(str(exc), code="invalid_proposal") from exc
    body_hash = compute_kubernetes_body_hash(frozen)
    proposal_id = _id_from(PROPOSAL_ID_PREFIX, f"{parent}:{body_hash}")
    return {
        "kind": PROPOSAL_KIND,
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "run_id": parent,
        "requested_by": worker,
        "body": frozen,
        "body_hash": body_hash,
        "created_at": created_at or utc_now_iso(),
        "status": PROPOSAL_STATUS_PROPOSED,
    }


def verify_proposal_record(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("kind") != PROPOSAL_KIND:
        raise KubernetesCapabilityError("proposal kind mismatch", code="invalid_proposal")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise KubernetesCapabilityError("unsupported proposal schema_version", code="invalid_proposal")
    if record.get("status") != PROPOSAL_STATUS_PROPOSED:
        raise KubernetesCapabilityError(
            f"unsupported proposal status: {record.get('status')}",
            code="invalid_proposal",
        )
    try:
        frozen = normalize_kubernetes_capability_body(record.get("body"))
    except CapabilityBodyError as exc:
        raise KubernetesCapabilityError(str(exc), code="invalid_proposal") from exc
    expected_hash = compute_kubernetes_body_hash(frozen)
    if record.get("body_hash") != expected_hash:
        raise KubernetesCapabilityError("proposal body_hash mismatch", code="invalid_proposal")
    expected_id = _id_from(PROPOSAL_ID_PREFIX, f"{record.get('run_id')}:{expected_hash}")
    if record.get("proposal_id") != expected_id:
        raise KubernetesCapabilityError("proposal_id mismatch", code="invalid_proposal")
    return {**record, "body": frozen, "body_hash": expected_hash}


def save_proposal(
    record: dict[str, Any],
    *,
    store: KubernetesCapabilityStore,
) -> dict[str, Any]:
    verified = verify_proposal_record(record)
    path = store.proposals_dir / f"{verified['proposal_id']}.json"
    with _STORE_LOCK:
        if path.exists():
            existing = verify_proposal_record(_read_json(path))
            if existing["body_hash"] == verified["body_hash"] and existing["run_id"] == verified["run_id"]:
                return existing
            raise KubernetesCapabilityError(
                f"proposal_id collision: {verified['proposal_id']}",
                code="invalid_proposal",
            )
        _atomic_write_json(path, verified)
    append_capability_event(
        "kubernetes_capability.proposed",
        {"proposal_id": verified["proposal_id"], "run_id": verified["run_id"]},
        store=store,
    )
    return verified


def propose_kubernetes_capability(
    *,
    run_id: str,
    body: dict[str, Any],
    requested_by: str,
    store: KubernetesCapabilityStore | None = None,
) -> dict[str, Any]:
    store = store or KubernetesCapabilityStore.from_env()
    return save_proposal(
        build_proposal_record(run_id=run_id, body=body, requested_by=requested_by),
        store=store,
    )


def load_proposal(proposal_id: str, *, store: KubernetesCapabilityStore | None = None) -> dict[str, Any]:
    store = store or KubernetesCapabilityStore.from_env()
    ref = str(proposal_id or "").strip()
    path = store.proposals_dir / f"{ref}.json"
    if not path.is_file():
        raise KubernetesCapabilityError(f"proposal not found: {ref}", code="no_proposal")
    return verify_proposal_record(_read_json(path))


def list_proposals(
    *,
    run_id: str | None = None,
    store: KubernetesCapabilityStore | None = None,
) -> list[dict[str, Any]]:
    store = store or KubernetesCapabilityStore.from_env()
    if not store.proposals_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(store.proposals_dir.glob("kcp-*.json")):
        try:
            record = verify_proposal_record(_read_json(path))
        except (OSError, json.JSONDecodeError, KubernetesCapabilityError):
            continue
        if run_id and record.get("run_id") != run_id:
            continue
        items.append(record)
    return items


# --- approval / revocation --------------------------------------------------

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


def verify_approval_record(
    record: dict[str, Any],
    *,
    evaluate_ttl: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("kind") != APPROVAL_KIND:
        raise KubernetesCapabilityError("approval kind mismatch", code="invalid_approval")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise KubernetesCapabilityError("unsupported approval schema_version", code="invalid_approval")
    try:
        frozen = normalize_kubernetes_capability_body(record.get("body"))
    except CapabilityBodyError as exc:
        raise KubernetesCapabilityError(str(exc), code="invalid_approval") from exc
    expected_hash = compute_kubernetes_body_hash(frozen)
    if record.get("body_hash") != expected_hash:
        raise KubernetesCapabilityError("approval body_hash mismatch", code="invalid_approval")
    expected_id = compute_approval_id(
        proposal_id=str(record.get("proposal_id") or ""),
        approved_at=str(record.get("approved_at") or ""),
        approved_by=str(record.get("approved_by") or ""),
        expires_at=str(record.get("expires_at") or ""),
        body_hash=expected_hash,
    )
    if record.get("approval_id") != expected_id:
        raise KubernetesCapabilityError("approval_id mismatch", code="invalid_approval")
    projected = {**record, "body": frozen, "body_hash": expected_hash}
    if evaluate_ttl:
        projected = _evaluate_approval_ttl(projected, now=now)
    return projected


def load_approval(
    approval_id: str,
    *,
    store: KubernetesCapabilityStore | None = None,
    now: datetime | None = None,
    persist_expiry: bool = True,
) -> dict[str, Any]:
    store = store or KubernetesCapabilityStore.from_env()
    ref = str(approval_id or "").strip()
    path = store.approvals_dir / f"{ref}.json"
    if not path.is_file():
        raise KubernetesCapabilityError(f"approval not found: {ref}", code="no_approval")
    record = verify_approval_record(_read_json(path), now=now)
    if persist_expiry and record.get("status") == STATUS_EXPIRED:
        stored = _read_json(path)
        if stored.get("status") != STATUS_EXPIRED:
            _atomic_write_json(path, record)
            append_capability_event(
                "kubernetes_capability.expired",
                {"approval_id": ref},
                store=store,
            )
    return record


def is_approval_active(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    projected = _evaluate_approval_ttl(record, now=now)
    return projected.get("status") == STATUS_APPROVED


def approve_proposal(
    proposal_id: str,
    *,
    ttl: str,
    approved_by: str,
    store: KubernetesCapabilityStore | None = None,
    now: datetime | None = None,
    approval_source: str = APPROVAL_SOURCE_CLI,
    provenance: str = PROVENANCE_OPERATOR_ASSERTED,
) -> dict[str, Any]:
    store = store or KubernetesCapabilityStore.from_env()
    _assert_operator_provenance(provenance)
    operator = _assert_operator_identity(approved_by, field="approved_by")
    try:
        duration = parse_ttl_duration(ttl)
    except WriteScopeApprovalError as exc:
        raise KubernetesCapabilityError(str(exc), code="invalid_ttl") from exc
    proposal = load_proposal(proposal_id, store=store)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    approved_at = current.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    expires_at = (parse_iso_utc(approved_at) + duration).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    if current >= parse_iso_utc(expires_at):
        raise KubernetesCapabilityError(
            "approval would be expired at grant time",
            code="invalid_ttl",
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
        "approval_source": approval_source,
        "provenance": provenance,
    }
    verified = verify_approval_record(record, evaluate_ttl=False)
    path = store.approvals_dir / f"{verified['approval_id']}.json"
    with _STORE_LOCK:
        if path.exists():
            return load_approval(verified["approval_id"], store=store, persist_expiry=False)
        _atomic_write_json(path, verified)
    append_capability_event(
        "kubernetes_capability.approved",
        {"approval_id": verified["approval_id"], "proposal_id": verified["proposal_id"]},
        store=store,
    )
    return verified


def revoke_approval(
    approval_id: str,
    *,
    reason: str,
    revoked_by: str,
    store: KubernetesCapabilityStore | None = None,
    provenance: str = PROVENANCE_OPERATOR_ASSERTED,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    store = store or KubernetesCapabilityStore.from_env()
    _assert_operator_provenance(provenance)
    operator = _assert_operator_identity(revoked_by, field="revoked_by")
    rationale = str(reason or "").strip()
    if not rationale:
        raise KubernetesCapabilityError("revoke reason is required", code="invalid_approval")
    approval = load_approval(approval_id, store=store)
    revocation_id = _id_from(REVOCATION_ID_PREFIX, approval["approval_id"])
    rev_path = store.revocations_dir / f"{revocation_id}.json"
    if approval.get("status") == STATUS_REVOKED and rev_path.is_file():
        return approval, _read_json(rev_path), True
    if approval.get("status") in {STATUS_EXPIRED, STATUS_CONSUMED}:
        raise KubernetesCapabilityError(
            f"cannot revoke terminal approval status={approval['status']}",
            code="invalid_approval",
        )
    revocation = {
        "kind": REVOCATION_KIND,
        "schema_version": SCHEMA_VERSION,
        "revocation_id": revocation_id,
        "approval_id": approval["approval_id"],
        "revoked_by": operator,
        "revoked_at": utc_now_iso(),
        "reason": rationale,
        "provenance": provenance,
    }
    updated = dict(approval)
    updated["status"] = STATUS_REVOKED
    updated["revocation_id"] = revocation_id
    _atomic_write_json(store.approvals_dir / f"{approval['approval_id']}.json", updated)
    _atomic_write_json(rev_path, revocation)
    append_capability_event(
        "kubernetes_capability.revoked",
        {"approval_id": approval["approval_id"], "revocation_id": revocation_id},
        store=store,
    )
    return updated, revocation, False


def list_approvals(
    *,
    run_id: str | None = None,
    status: str | None = None,
    store: KubernetesCapabilityStore | None = None,
) -> list[dict[str, Any]]:
    store = store or KubernetesCapabilityStore.from_env()
    if not store.approvals_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(store.approvals_dir.glob("kca-*.json")):
        try:
            record = load_approval(path.stem, store=store)
        except KubernetesCapabilityError:
            continue
        if run_id and record.get("run_id") != run_id:
            continue
        if status and record.get("status") != status:
            continue
        items.append(record)
    return items


def _transition_approval(
    approval_id: str,
    target: str,
    *,
    store: KubernetesCapabilityStore,
) -> dict[str, Any]:
    record = load_approval(approval_id, store=store)
    if record["status"] == target:
        return record
    if record["status"] != STATUS_APPROVED:
        raise KubernetesCapabilityError(
            f"illegal approval transition {record['status']!r} -> {target!r}",
            code="invalid_approval",
        )
    updated = dict(record)
    updated["status"] = target
    _atomic_write_json(store.approvals_dir / f"{approval_id}.json", updated)
    append_capability_event(
        "kubernetes_capability.approval_status",
        {"approval_id": approval_id, "from_status": record["status"], "to_status": target},
        store=store,
    )
    return updated


# --- binding ----------------------------------------------------------------

def compute_binding_id(*, approval_id: str, run_id: str, bound_at: str) -> str:
    return _id_from(BINDING_ID_PREFIX, f"{approval_id}:{run_id}:{bound_at}")


def verify_binding_record(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("kind") != BINDING_KIND:
        raise KubernetesCapabilityError("binding kind mismatch", code="invalid_binding")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise KubernetesCapabilityError("unsupported binding schema_version", code="invalid_binding")
    try:
        frozen = normalize_kubernetes_capability_body(record.get("body"))
    except CapabilityBodyError as exc:
        raise KubernetesCapabilityError(str(exc), code="invalid_binding") from exc
    expected_hash = compute_kubernetes_body_hash(frozen)
    if record.get("body_hash") != expected_hash:
        raise KubernetesCapabilityError("binding body_hash mismatch", code="invalid_binding")
    expected_id = compute_binding_id(
        approval_id=str(record.get("approval_id") or ""),
        run_id=str(record.get("run_id") or ""),
        bound_at=str(record.get("bound_at") or ""),
    )
    if record.get("binding_id") != expected_id:
        raise KubernetesCapabilityError("binding_id mismatch", code="invalid_binding")
    return {**record, "body": frozen, "body_hash": expected_hash}


def load_binding(binding_id: str, *, store: KubernetesCapabilityStore | None = None) -> dict[str, Any]:
    store = store or KubernetesCapabilityStore.from_env()
    ref = str(binding_id or "").strip()
    path = store.bindings_dir / f"{ref}.json"
    if not path.is_file():
        raise KubernetesCapabilityError(f"binding not found: {ref}", code="no_binding")
    return verify_binding_record(_read_json(path))


def list_bindings(
    *,
    run_id: str | None = None,
    status: str | None = None,
    store: KubernetesCapabilityStore | None = None,
) -> list[dict[str, Any]]:
    store = store or KubernetesCapabilityStore.from_env()
    if not store.bindings_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(store.bindings_dir.glob("kcb-*.json")):
        try:
            record = verify_binding_record(_read_json(path))
        except (OSError, json.JSONDecodeError, KubernetesCapabilityError):
            continue
        if run_id and record.get("run_id") != run_id:
            continue
        if status and record.get("status") != status:
            continue
        items.append(record)
    return items


def _blocking_binding(approval_id: str, *, store: KubernetesCapabilityStore) -> dict[str, Any] | None:
    """Active bindings always block. Completed mutate bindings block re-use."""
    for record in list_bindings(store=store):
        if record.get("approval_id") != approval_id:
            continue
        if record.get("status") == STATUS_ACTIVE:
            return record
        if record.get("status") == STATUS_COMPLETED and record.get("consumes_approval") is True:
            return record
    return None


def create_binding(
    approval_id: str,
    *,
    run_id: str,
    store: KubernetesCapabilityStore | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    store = store or KubernetesCapabilityStore.from_env()
    approval = load_approval(approval_id, store=store, now=now)
    status = approval.get("status")
    if status == STATUS_EXPIRED:
        raise KubernetesCapabilityError("approval expired; cannot bind", code="expired")
    if status == STATUS_REVOKED:
        raise KubernetesCapabilityError("approval revoked; cannot bind", code="revoked")
    if status == STATUS_CONSUMED:
        raise KubernetesCapabilityError("approval consumed; cannot bind", code="reused_single_use")
    if status != STATUS_APPROVED:
        raise KubernetesCapabilityError(
            f"approval is not active: {status}",
            code="no_approval",
        )
    blocking = _blocking_binding(approval["approval_id"], store=store)
    if blocking is not None:
        code = "reused_single_use" if blocking.get("consumes_approval") else "no_binding"
        if blocking.get("status") == STATUS_ACTIVE:
            code = "reused_single_use"
        raise KubernetesCapabilityError(
            f"approval already bound ({blocking['status']}): {blocking['binding_id']}",
            code=code,
        )
    bound_at = utc_now_iso()
    binding = {
        "kind": BINDING_KIND,
        "schema_version": SCHEMA_VERSION,
        "binding_id": compute_binding_id(
            approval_id=approval["approval_id"],
            run_id=str(run_id or "").strip() or approval["run_id"],
            bound_at=bound_at,
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
    verified = verify_binding_record(binding)
    _atomic_write_json(store.bindings_dir / f"{verified['binding_id']}.json", verified)
    append_capability_event(
        "kubernetes_capability.bound",
        {"binding_id": verified["binding_id"], "approval_id": verified["approval_id"]},
        store=store,
    )
    return verified


def _finalize_binding(
    binding_id: str,
    *,
    status: str,
    consumes_approval: bool,
    reason: str | None,
    store: KubernetesCapabilityStore,
) -> dict[str, Any]:
    record = load_binding(binding_id, store=store)
    if record["status"] != STATUS_ACTIVE:
        raise KubernetesCapabilityError(
            f"illegal binding transition {record['status']!r} -> {status!r}",
            code="invalid_binding",
        )
    updated = dict(record)
    updated["status"] = status
    updated["consumes_approval"] = bool(consumes_approval)
    updated["terminal_at"] = utc_now_iso()
    if reason:
        updated["terminal_reason"] = reason
    verified = verify_binding_record(updated)
    _atomic_write_json(store.bindings_dir / f"{binding_id}.json", verified)
    return verified


# --- receipt / execute ------------------------------------------------------

def compute_receipt_id(
    *,
    binding_id: str,
    run_id: str,
    evaluated_at: str,
    compliance: str,
    acceptance_state: str,
) -> str:
    return _id_from(
        RECEIPT_ID_PREFIX,
        f"{binding_id}:{run_id}:{evaluated_at}:{compliance}:{acceptance_state}",
    )


def _operation_name(operation: KubernetesOperation) -> str:
    patch = normalize_patch(operation.patch)
    if patch and "annotation" in patch:
        return "annotation_patch"
    if patch and "replicas" in patch:
        return "replica_patch"
    return str(operation.verb or "").strip().lower() or "unknown"


def _generation_of(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    generation = value.get("generation")
    if isinstance(generation, int) and not isinstance(generation, bool):
        return generation
    return None


def _resource_version_of(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("resourceVersion", value.get("resource_version"))
    if raw is None:
        return None
    return str(raw)


def _acceptance_for(compliance: str, *, verified: bool, executed: bool) -> str:
    if compliance == COMPLIANCE_UNVERIFIED or not verified:
        return ACCEPTANCE_UNVERIFIED
    if compliance == COMPLIANCE_WITHIN_SCOPE and executed and verified:
        return ACCEPTANCE_PASS
    return ACCEPTANCE_FAIL


def _build_receipt(
    *,
    binding: dict[str, Any],
    approval: dict[str, Any],
    operation: KubernetesOperation,
    compliance: str,
    acceptance_state: str,
    within_scope: bool,
    executed: bool,
    verified: bool,
    verification_method: str | None,
    change: dict[str, Any] | None,
    stop_reason: str | None,
    code: str,
) -> dict[str, Any]:
    evaluated_at = utc_now_iso()
    if acceptance_state == ACCEPTANCE_PASS and (
        compliance != COMPLIANCE_WITHIN_SCOPE or not verified or not executed
    ):
        raise KubernetesCapabilityError(
            "refusing to mint PASS receipt without verified in-scope execution",
            code="unverified",
        )
    if acceptance_state == ACCEPTANCE_PASS and compliance == COMPLIANCE_UNVERIFIED:
        raise KubernetesCapabilityError(
            "UNVERIFIED must not become PASS",
            code="unverified",
        )
    receipt = {
        "kind": RECEIPT_KIND,
        "schema_version": SCHEMA_VERSION,
        "receipt_id": compute_receipt_id(
            binding_id=binding["binding_id"],
            run_id=operation.run_id or binding["run_id"],
            evaluated_at=evaluated_at,
            compliance=compliance,
            acceptance_state=acceptance_state,
        ),
        "run_id": operation.run_id or binding["run_id"],
        "mission_id": operation.mission_id or binding["run_id"],
        "requested_by": operation.requested_by or approval.get("requested_by") or "",
        "proposal_id": approval.get("proposal_id"),
        "approval_id": approval["approval_id"],
        "binding_id": binding["binding_id"],
        "capability": binding["body"]["capability"],
        "granted": {
            "cluster": binding["body"]["cluster"],
            "namespaces": list(binding["body"]["namespaces"]),
            "verbs": list(binding["body"]["verbs"]),
            "resources": list(binding["body"]["resources"]),
            "denied_namespaces": list(binding["body"]["denied_namespaces"]),
            "denied_verbs": list(binding["body"]["denied_verbs"]),
            "denied_resources": list(binding["body"]["denied_resources"]),
        },
        "target": {
            "cluster": operation.cluster,
            "namespace": operation.namespace,
            "resource": operation.resource,
            "name": operation.name,
        },
        "operation": {
            "verb": operation.verb,
            "name": _operation_name(operation),
            "digest": operation_digest(operation),
        },
        "change": change,
        "within_scope": within_scope,
        "verification": {
            "method": verification_method,
            "verified": verified,
        },
        "executed": executed,
        "compliance": compliance,
        "acceptance_state": acceptance_state,
        "binding_status": binding.get("status"),
        "approval_status": approval.get("status"),
        "created_at": evaluated_at,
        "evaluated_at": evaluated_at,
        "stop_reason": stop_reason,
        "denial_code": None if acceptance_state == ACCEPTANCE_PASS else code,
        "secrets_omitted": True,
    }
    return receipt


def _write_receipt(receipt: dict[str, Any], *, store: KubernetesCapabilityStore) -> dict[str, Any]:
    _atomic_write_json(store.receipts_dir / f"{receipt['receipt_id']}.json", receipt)
    append_capability_event(
        "kubernetes_capability.receipt",
        {
            "receipt_id": receipt["receipt_id"],
            "acceptance_state": receipt["acceptance_state"],
            "compliance": receipt["compliance"],
        },
        store=store,
    )
    return receipt


def _denial(
    *,
    code: str,
    message: str,
    operation: KubernetesOperation,
    approval: dict[str, Any] | None = None,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": DENIAL_KIND,
        "schema_version": SCHEMA_VERSION,
        "code": code,
        "message": message,
        "acceptance_state": ACCEPTANCE_FAIL if code != "unverified" else ACCEPTANCE_UNVERIFIED,
        "mission_id": operation.mission_id,
        "requested_by": operation.requested_by,
        "run_id": operation.run_id,
        "approval_id": None if approval is None else approval.get("approval_id"),
        "binding_id": None if binding is None else binding.get("binding_id"),
        "target": {
            "cluster": operation.cluster,
            "namespace": operation.namespace,
            "resource": operation.resource,
            "name": operation.name,
            "verb": operation.verb,
        },
        "created_at": utc_now_iso(),
    }


def _coerce_executor(executor: KubernetesExecutor | str | None) -> KubernetesExecutor:
    if executor is None or executor == "fixture":
        return FixtureKubernetesExecutor()
    if executor == "live":
        from .kubernetes_live import LiveKubernetesExecutor

        return LiveKubernetesExecutor()
    if hasattr(executor, "execute"):
        return executor  # type: ignore[return-value]
    raise KubernetesCapabilityError(
        f"unsupported executor: {executor!r}",
        code="invalid_request",
    )


def execute_kubernetes_capability(
    *,
    operation: KubernetesOperation,
    approval_id: str | None = None,
    binding_id: str | None = None,
    executor: KubernetesExecutor | str | None = None,
    store: KubernetesCapabilityStore | None = None,
    now: datetime | None = None,
) -> ExecutionOutcome:
    """Bind, enforce, execute, verify, and receipt one Kubernetes attempt.

    Fail closed before the executor runs when authority or scope is invalid.
    Interrupted or unverified execution never mints PASS.
    """
    store = store or KubernetesCapabilityStore.from_env()
    executor = _coerce_executor(executor)
    op = KubernetesOperation(
        cluster=str(operation.cluster or "").strip(),
        namespace=str(operation.namespace or "").strip(),
        verb=str(operation.verb or "").strip().lower(),
        resource=str(operation.resource or "").strip().lower(),
        name=(None if operation.name is None else str(operation.name).strip() or None),
        patch=normalize_patch(operation.patch),
        mission_id=str(operation.mission_id or "").strip(),
        requested_by=str(operation.requested_by or "").strip(),
        run_id=str(operation.run_id or "").strip(),
    )
    if not op.run_id:
        raise KubernetesCapabilityError("run_id is required", code="invalid_request")

    if not approval_id and not binding_id:
        denial = _denial(code="no_approval", message="no approval provided", operation=op)
        return ExecutionOutcome(ok=False, code="no_approval", denial=denial)

    binding: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    try:
        if binding_id:
            binding = load_binding(binding_id, store=store)
            if binding.get("status") != STATUS_ACTIVE:
                raise KubernetesCapabilityError(
                    f"binding is not active: {binding.get('status')}",
                    code="no_binding",
                )
            approval = load_approval(binding["approval_id"], store=store, now=now)
        else:
            approval = load_approval(str(approval_id), store=store, now=now)
            binding = create_binding(approval["approval_id"], run_id=op.run_id, store=store, now=now)
    except KubernetesCapabilityError as exc:
        denial = _denial(
            code=exc.code or "no_approval",
            message=str(exc),
            operation=op,
            approval=approval,
            binding=binding,
        )
        return ExecutionOutcome(ok=False, code=exc.code or "no_approval", denial=denial, approval=approval, binding=binding)

    # Re-check frozen binding body — mutation authority cannot expand at execute.
    if binding["body_hash"] != approval["body_hash"]:
        denial = _denial(
            code="scope_escalation",
            message="binding body_hash diverged from approval",
            operation=op,
            approval=approval,
            binding=binding,
        )
        binding = _finalize_binding(
            binding["binding_id"],
            status=STATUS_FAILED,
            consumes_approval=False,
            reason="scope_escalation",
            store=store,
        )
        return ExecutionOutcome(ok=False, code="scope_escalation", denial=denial, approval=approval, binding=binding)

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
            acceptance_state=ACCEPTANCE_FAIL,
            within_scope=False,
            executed=False,
            verified=True,
            verification_method="pre_execution_scope_check",
            change=None,
            stop_reason=scope_code,
            code=scope_code,
        )
        _write_receipt(receipt, store=store)
        return ExecutionOutcome(
            ok=False,
            code=scope_code,
            receipt=receipt,
            binding=binding,
            approval=approval,
        )

    try:
        result = executor.execute(op)
    except KubernetesCapabilityInterrupted as exc:
        binding = _finalize_binding(
            binding["binding_id"],
            status=STATUS_FAILED,
            consumes_approval=False,
            reason="interrupted",
            store=store,
        )
        receipt = _build_receipt(
            binding=binding,
            approval=approval,
            operation=op,
            compliance=COMPLIANCE_UNVERIFIED,
            acceptance_state=ACCEPTANCE_UNVERIFIED,
            within_scope=True,
            executed=False,
            verified=False,
            verification_method=None,
            change=None,
            stop_reason=str(exc),
            code="unverified",
        )
        _write_receipt(receipt, store=store)
        return ExecutionOutcome(
            ok=False,
            code="unverified",
            receipt=receipt,
            binding=binding,
            approval=approval,
        )
    except KubernetesCapabilityError as exc:
        binding = _finalize_binding(
            binding["binding_id"],
            status=STATUS_FAILED,
            consumes_approval=False,
            reason=exc.code or "execution_failed",
            store=store,
        )
        receipt = _build_receipt(
            binding=binding,
            approval=approval,
            operation=op,
            compliance=COMPLIANCE_DENIED,
            acceptance_state=ACCEPTANCE_FAIL,
            within_scope=True,
            executed=False,
            verified=True,
            verification_method="executor_error",
            change=None,
            stop_reason=str(exc),
            code=exc.code or "execution_failed",
        )
        _write_receipt(receipt, store=store)
        return ExecutionOutcome(
            ok=False,
            code=exc.code or "execution_failed",
            receipt=receipt,
            binding=binding,
            approval=approval,
        )

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
            acceptance_state=ACCEPTANCE_UNVERIFIED,
            within_scope=True,
            executed=executed,
            verified=False,
            verification_method=result.get("verification_method"),
            change=None,
            stop_reason="executor returned unverified result",
            code="unverified",
        )
        _write_receipt(receipt, store=store)
        return ExecutionOutcome(
            ok=False,
            code="unverified",
            receipt=receipt,
            binding=binding,
            approval=approval,
        )

    # Post-execution re-classify: reject silent expansion in the result target.
    if classify_operation(binding["body"], op) is not None:
        raise KubernetesCapabilityError("post-execution scope expanded", code="scope_escalation")

    change = None
    if result.get("changed") or verb_requires_mutate(op.verb):
        change = {
            "digest": result.get("result_digest"),
            "before_digest": result.get("before_digest")
            or (
                None
                if result.get("before") is None
                else _sha256_canonical(result.get("before"))
            ),
            "after_digest": result.get("after_digest")
            or (
                None
                if result.get("after") is None
                else _sha256_canonical(result.get("after"))
            ),
            "before_generation": _generation_of(result.get("before")),
            "after_generation": _generation_of(result.get("after")),
            "before_resource_version": _resource_version_of(result.get("before")),
            "after_resource_version": _resource_version_of(result.get("after")),
            "changed": bool(result.get("changed")),
        }

    consumes = bool(verb_requires_mutate(op.verb) and result.get("changed"))
    binding = _finalize_binding(
        binding["binding_id"],
        status=STATUS_COMPLETED,
        consumes_approval=consumes,
        reason=None,
        store=store,
    )
    if consumes:
        approval = _transition_approval(approval["approval_id"], STATUS_CONSUMED, store=store)

    receipt = _build_receipt(
        binding=binding,
        approval=approval,
        operation=op,
        compliance=COMPLIANCE_WITHIN_SCOPE,
        acceptance_state=ACCEPTANCE_PASS,
        within_scope=True,
        executed=True,
        verified=True,
        verification_method=str(result.get("verification_method") or "fixture_state_compare"),
        change=change,
        stop_reason=None,
        code="ok",
    )
    _write_receipt(receipt, store=store)
    return ExecutionOutcome(
        ok=True,
        code="ok",
        receipt=receipt,
        binding=binding,
        approval=approval,
    )


def load_receipt(receipt_id: str, *, store: KubernetesCapabilityStore | None = None) -> dict[str, Any]:
    store = store or KubernetesCapabilityStore.from_env()
    path = store.receipts_dir / f"{str(receipt_id).strip()}.json"
    if not path.is_file():
        raise KubernetesCapabilityError(f"receipt not found: {receipt_id}", code="no_receipt")
    return _read_json(path)


def audit_kubernetes_capability(
    ref: str,
    *,
    store: KubernetesCapabilityStore | None = None,
) -> dict[str, Any]:
    store = store or KubernetesCapabilityStore.from_env()
    text = str(ref or "").strip()
    proposal = None
    approval = None
    binding = None
    receipt = None
    if text.startswith("kcp-"):
        proposal = load_proposal(text, store=store)
    elif text.startswith("kca-"):
        approval = load_approval(text, store=store)
    elif text.startswith("kcb-"):
        binding = load_binding(text, store=store)
    elif text.startswith("kcr-"):
        receipt = load_receipt(text, store=store)
    else:
        for candidate in list_proposals(run_id=text, store=store):
            proposal = candidate
            break
        if proposal is None:
            for candidate in list_approvals(run_id=text, store=store):
                approval = candidate
                break
        if proposal is None and approval is None:
            raise KubernetesCapabilityError(f"capability record not found: {text}", code="not_found")

    if binding is not None and approval is None:
        approval = load_approval(binding["approval_id"], store=store)
    if approval is not None and proposal is None:
        proposal = load_proposal(approval["proposal_id"], store=store)
    if receipt is not None and binding is None:
        binding = load_binding(receipt["binding_id"], store=store)
        approval = load_approval(receipt["approval_id"], store=store)
        proposal = load_proposal(approval["proposal_id"], store=store)
    if approval is not None and binding is None:
        matches = [
            item
            for item in list_bindings(store=store)
            if item.get("approval_id") == approval["approval_id"]
        ]
        if matches:
            binding = sorted(matches, key=lambda item: item.get("bound_at") or "")[-1]
    if binding is not None and receipt is None and store.receipts_dir.is_dir():
        for path in sorted(store.receipts_dir.glob("kcr-*.json")):
            candidate = _read_json(path)
            if candidate.get("binding_id") == binding["binding_id"]:
                receipt = candidate

    residual = "none"
    if approval is not None and is_approval_active(approval):
        residual = "read" if approval["body"]["capability"] != CAPABILITY_KUBERNETES_MUTATE else "granted"
        if any(
            item.get("approval_id") == approval["approval_id"] and item.get("consumes_approval")
            for item in list_bindings(store=store)
        ):
            residual = "none"

    return {
        "kind": "kubernetes_capability_audit",
        "query": text,
        "proposal": proposal,
        "approval": approval,
        "binding": binding,
        "receipt": receipt,
        "residual_authority": residual,
    }


def is_kubernetes_scope_id(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith(("kcp-", "kca-", "kcb-", "kcr-", "kcv-"))


__all__ = [
    "ACCEPTANCE_FAIL",
    "ACCEPTANCE_PASS",
    "ACCEPTANCE_UNVERIFIED",
    "ALLOWED_ANNOTATION_KEY",
    "APPROVAL_KIND",
    "BINDING_KIND",
    "DEFAULT_FIXTURE_CLUSTER",
    "DENIAL_KIND",
    "ExecutionOutcome",
    "FixtureKubernetesExecutor",
    "KubernetesCapabilityError",
    "KubernetesCapabilityInterrupted",
    "KubernetesCapabilityStore",
    "KubernetesOperation",
    "PROPOSAL_KIND",
    "RECEIPT_KIND",
    "approve_proposal",
    "audit_kubernetes_capability",
    "classify_operation",
    "create_binding",
    "default_fixture_state",
    "execute_kubernetes_capability",
    "is_approval_active",
    "is_kubernetes_scope_id",
    "list_approvals",
    "list_bindings",
    "list_proposals",
    "load_approval",
    "load_binding",
    "load_proposal",
    "load_receipt",
    "propose_kubernetes_capability",
    "revoke_approval",
]
