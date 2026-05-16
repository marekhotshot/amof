"""Local generated-build candidate write path.

This module implements the first generated-build mutation boundary:
`generated_build.promote_candidate.v1`.

It is local-only and does not touch deploy/release state. It reads a
stored generated-build artifact, evaluates admission policy, and on
success writes:

* a candidate record under `.amof/generated-builds/candidates/...`
* an audit receipt under `.amof/generated-builds/candidates/audit/...`

The original artifact is never modified.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .admission import evaluate_admission
from .store import load_artifact, store_root


ACTION_PROMOTE_CANDIDATE = "generated_build.promote_candidate.v1"
RESULT_CANDIDATE_CREATED = "candidate_created"
RESULT_REFUSED = "refused"
ADMISSION_CANDIDATE_ONLY = "candidate_only"

REQUIRED_ACKNOWLEDGEMENTS = {
    "runtime_proven_is_not_deploy_admission",
    "candidate_only_is_not_deploy_admitted",
    "existing_build_precedence_checked",
    "no_release_or_deploy_wiring_created",
}


def promote_candidate(request: Dict[str, Any], *, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Promote a stored runtime_proven artifact to local candidate_only.

    Returns a dict conforming to
    `contracts/generated-build-candidate-write-result.schema.json`.
    """
    created_at = _now()
    try:
        _validate_request_shape(request)
    except ValueError as exc:
        return _refused_result(
            request,
            created_at=created_at,
            refusal_conditions=[str(exc)],
            missing_prerequisites=[],
        )

    context = dict(context or {})
    idempotency_key = _idempotency_key(request, context=context)
    normalized_request = _normalized_idempotency_payload(request, context=context)
    existing_replay = _read_idempotency_record(idempotency_key)
    if existing_replay:
        if existing_replay.get("normalized_request") == normalized_request:
            replayed = dict(existing_replay.get("result") or {})
            replayed["idempotency_key"] = idempotency_key
            replayed["replayed"] = True
            return replayed
        return _refused_result(
            request,
            created_at=created_at,
            refusal_conditions=["idempotency_key_conflict"],
            missing_prerequisites=[],
            idempotency_key=idempotency_key,
            replayed=False,
        )

    artifact_ref = request["artifact_ref"]
    service = artifact_ref.get("service")
    try:
        artifact = load_artifact(artifact_ref["repo_path"], service=service)
    except FileNotFoundError:
        result = _refused_result(
            request,
            created_at=created_at,
            refusal_conditions=["artifact_not_found"],
            missing_prerequisites=["stored_generated_build_artifact"],
            idempotency_key=idempotency_key,
            replayed=False,
        )
        return _persist_idempotency_result(idempotency_key, normalized_request, result)

    confirmation = request["operator_confirmation"]
    admission_context = dict(context)
    admission_context.setdefault("deploy_pull_reference_present", False)
    admission_context.setdefault("source_ref_confirmed", False)

    admission = evaluate_admission(
        artifact,
        artifact_path=artifact_ref["artifact_path"],
        context=admission_context,
    )

    refusal_conditions: list[str] = []
    missing_prerequisites: list[str] = []

    if artifact.get("status") != "runtime_proven":
        refusal_conditions.append("artifact_status_below_runtime_proven")
    if artifact.get("build_contract_kind") != "generated":
        refusal_conditions.append("build_contract_kind_not_generated")

    digest = _artifact_digest(artifact)
    if not digest:
        refusal_conditions.append("build_digest_missing_or_invalid")
    elif confirmation.get("confirmed_image_digest") != digest:
        refusal_conditions.append("operator_confirmation_digest_mismatch")

    if confirmation.get("confirmed_runtime_family") != artifact.get("runtime_family"):
        refusal_conditions.append("operator_confirmation_runtime_family_mismatch")
    if confirmation.get("confirmed_template_id") != (artifact.get("dockerfile_template") or {}).get("id"):
        refusal_conditions.append("operator_confirmation_template_mismatch")
    if confirmation.get("confirmed_source_repo") != (artifact.get("source_repo") or {}).get("host_path"):
        refusal_conditions.append("operator_confirmation_source_repo_mismatch")
    if confirmation.get("confirmed_entrypoint") != artifact.get("entrypoint"):
        refusal_conditions.append("operator_confirmation_entrypoint_mismatch")

    missing_ack = sorted(REQUIRED_ACKNOWLEDGEMENTS - set(confirmation.get("acknowledgements") or []))
    if missing_ack:
        refusal_conditions.append("required_acknowledgement_missing")
        missing_prerequisites.extend(f"acknowledgement:{ack}" for ack in missing_ack)

    if context.get("existing_build_contract_present") and not context.get("existing_build_precedence_resolved"):
        refusal_conditions.append("existing_build_contract_present")
        missing_prerequisites.append("existing_build_contract_retirement_decision")

    principal = context.get("authenticated_principal")
    if isinstance(principal, dict):
        confirmed_by = str(confirmation.get("confirmed_by") or "")
        principal_candidates = {
            str(principal.get("id") or ""),
            str(principal.get("email") or ""),
            str(principal.get("principal_id") or ""),
        }
        if confirmed_by and confirmed_by not in principal_candidates:
            refusal_conditions.append("operator_confirmation_principal_mismatch")

    if admission.get("admission_status") != ADMISSION_CANDIDATE_ONLY:
        refusal_conditions.append("precedence_decision_not_candidate_safe")
        missing_prerequisites.extend(admission.get("missing_prerequisites") or [])

    candidate_id = _candidate_id(
        target_ecosystem=request["target_ecosystem"],
        target_service=request["target_service"],
        image_digest=digest or confirmation.get("confirmed_image_digest", ""),
    )
    active_path = _active_candidate_path(request["target_ecosystem"], request["target_service"])
    supersede_id = request.get("supersede_candidate_id")
    if active_path.exists() and not supersede_id:
        refusal_conditions.append("active_candidate_exists_without_supersede")
        missing_prerequisites.append("supersede_candidate_id")
    if active_path.exists() and supersede_id:
        try:
            active_candidate = json.loads(active_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            active_candidate = {}
        if active_candidate.get("candidate_id") != supersede_id:
            refusal_conditions.append("supersede_candidate_mismatch")
            missing_prerequisites.append("active_candidate_matching_supersede_candidate_id")

    if refusal_conditions:
        result = _refused_result(
            request,
            artifact=artifact,
            admission=admission,
            created_at=created_at,
            refusal_conditions=sorted(set(refusal_conditions)),
            missing_prerequisites=sorted(set(missing_prerequisites)),
            idempotency_key=idempotency_key,
            replayed=False,
        )
        return _persist_idempotency_result(idempotency_key, normalized_request, result)

    if active_path.exists() and supersede_id:
        _mark_candidate_superseded(active_path, supersede_id)

    candidate_record = {
        "candidate_id": candidate_id,
        "status": ADMISSION_CANDIDATE_ONLY,
        "artifact_ref": _result_artifact_ref(request, artifact),
        "target_ecosystem": request["target_ecosystem"],
        "target_service": request["target_service"],
        "image_digest": digest,
        "runtime_family": artifact.get("runtime_family"),
        "template_id": (artifact.get("dockerfile_template") or {}).get("id"),
        "created_by": confirmation.get("confirmed_by"),
        "created_at": created_at,
        "supersedes_candidate_id": supersede_id,
        "admission_policy_result": admission,
        "audit_receipt_path": str(_audit_path(candidate_id)),
        "idempotency_key": idempotency_key,
    }
    candidate_path = _candidate_record_path(candidate_id)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(json.dumps(candidate_record, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_text(json.dumps(candidate_record, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    result = _success_result(
        request,
        artifact=artifact,
        candidate_id=candidate_id,
        created_at=created_at,
        audit_receipt_path=str(_audit_path(candidate_id)),
        superseded_candidate_id=supersede_id,
        idempotency_key=idempotency_key,
        replayed=False,
    )
    _write_audit_receipt(candidate_id, request, result, admission)
    return _persist_idempotency_result(idempotency_key, normalized_request, result)


def _validate_request_shape(request: Dict[str, Any]) -> None:
    if request.get("action") != ACTION_PROMOTE_CANDIDATE:
        raise ValueError("unsupported_action")
    for key in ("artifact_ref", "target_ecosystem", "target_service", "operator_confirmation"):
        if key not in request:
            raise ValueError(f"missing_{key}")
    artifact_ref = request["artifact_ref"]
    if not isinstance(artifact_ref, dict):
        raise ValueError("artifact_ref_invalid")
    for key in ("artifact_path", "repo_path", "service"):
        if not artifact_ref.get(key):
            raise ValueError(f"missing_artifact_ref_{key}")
    confirmation = request["operator_confirmation"]
    if not isinstance(confirmation, dict):
        raise ValueError("operator_confirmation_missing")
    for key in (
        "confirmed_by",
        "confirmed_at",
        "confirmed_image_digest",
        "confirmed_runtime_family",
        "confirmed_template_id",
        "confirmed_source_repo",
        "confirmed_entrypoint",
        "acknowledgements",
    ):
        if key not in confirmation:
            raise ValueError(f"missing_operator_confirmation_{key}")


def _success_result(
    request: Dict[str, Any],
    *,
    artifact: Dict[str, Any],
    candidate_id: str,
    created_at: str,
    audit_receipt_path: str,
    superseded_candidate_id: Optional[str],
    idempotency_key: str,
    replayed: bool,
) -> Dict[str, Any]:
    result = {
        "action": ACTION_PROMOTE_CANDIDATE,
        "result": RESULT_CANDIDATE_CREATED,
        "candidate_id": candidate_id,
        "idempotency_key": idempotency_key,
        "replayed": replayed,
        "artifact_ref": _result_artifact_ref(request, artifact),
        "target_ecosystem": request["target_ecosystem"],
        "target_service": request["target_service"],
        "admission_status": ADMISSION_CANDIDATE_ONLY,
        "artifact_proof_status": str(artifact.get("status") or "refused"),
        "reasons": ["candidate_record_created", "operator_confirmed", "runtime_proven"],
        "missing_prerequisites": ["deploy_pull_reference_confirmation", "source_ref_confirmation"],
        "refusal_conditions": [],
        "audit_receipt_path": audit_receipt_path,
        "created_at": created_at,
    }
    if superseded_candidate_id:
        result["superseded_candidate_id"] = superseded_candidate_id
    return result


def _refused_result(
    request: Dict[str, Any],
    *,
    created_at: str,
    refusal_conditions: list[str],
    missing_prerequisites: list[str],
    artifact: Optional[Dict[str, Any]] = None,
    admission: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    replayed: bool = False,
) -> Dict[str, Any]:
    artifact = artifact or {}
    artifact_ref = request.get("artifact_ref") if isinstance(request.get("artifact_ref"), dict) else {}
    result = {
        "action": ACTION_PROMOTE_CANDIDATE,
        "result": RESULT_REFUSED,
        "artifact_ref": {
            "artifact_path": str(artifact_ref.get("artifact_path") or ""),
            "repo_path": str(artifact_ref.get("repo_path") or ""),
            "service": str(artifact_ref.get("service") or ""),
            "image_digest": _artifact_digest(artifact) or str((request.get("operator_confirmation") or {}).get("confirmed_image_digest") or ""),
        },
        "target_ecosystem": str(request.get("target_ecosystem") or ""),
        "target_service": str(request.get("target_service") or ""),
        "admission_status": "refused",
        "artifact_proof_status": str(artifact.get("status") or "refused"),
        "reasons": sorted(set((admission or {}).get("reasons") or [])),
        "missing_prerequisites": sorted(set(missing_prerequisites)),
        "refusal_conditions": sorted(set(refusal_conditions)),
        "audit_receipt_path": "",
        "created_at": created_at,
    }
    if idempotency_key:
        result["idempotency_key"] = idempotency_key
        result["replayed"] = replayed
    return result


def _result_artifact_ref(request: Dict[str, Any], artifact: Dict[str, Any]) -> Dict[str, str]:
    artifact_ref = request["artifact_ref"]
    return {
        "artifact_path": str(artifact_ref["artifact_path"]),
        "repo_path": str(artifact_ref["repo_path"]),
        "service": str(artifact_ref["service"]),
        "image_digest": _artifact_digest(artifact),
    }


def list_candidates() -> Dict[str, Any]:
    """List locally persisted generated-build candidate records."""
    records_dir = store_root() / "candidates" / "records"
    if not records_dir.exists():
        return {"version": 1, "items": []}
    items = []
    for path in sorted(records_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items.append(_candidate_summary(record, path))
    return {"version": 1, "items": items}


def load_candidate(candidate_id: str) -> Dict[str, Any]:
    """Load one locally persisted generated-build candidate record."""
    path = _candidate_record_path(candidate_id)
    return json.loads(path.read_text(encoding="utf-8"))


def _candidate_summary(record: Dict[str, Any], path: Path) -> Dict[str, Any]:
    return {
        "candidate_id": record.get("candidate_id"),
        "status": record.get("status"),
        "target_ecosystem": record.get("target_ecosystem"),
        "target_service": record.get("target_service"),
        "image_digest": record.get("image_digest"),
        "runtime_family": record.get("runtime_family"),
        "template_id": record.get("template_id"),
        "created_by": record.get("created_by"),
        "created_at": record.get("created_at"),
        "candidate_record_path": str(path),
        "audit_receipt_path": record.get("audit_receipt_path"),
    }


def _artifact_digest(artifact: Dict[str, Any]) -> str:
    return str((artifact.get("build_proof") or {}).get("image_digest") or "")


def _idempotency_key(request: Dict[str, Any], *, context: Dict[str, Any]) -> str:
    explicit = str(request.get("idempotency_key") or "").strip()
    if explicit:
        return explicit
    normalized = _normalized_idempotency_payload(request, context=context)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return "derived-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalized_idempotency_payload(request: Dict[str, Any], *, context: Dict[str, Any]) -> Dict[str, Any]:
    context_subset = {
        "existing_build_contract_present": bool(context.get("existing_build_contract_present")),
        "existing_build_precedence_resolved": bool(context.get("existing_build_precedence_resolved")),
    }
    return {
        "action": request.get("action"),
        "artifact_ref": request.get("artifact_ref") or {},
        "target_ecosystem": request.get("target_ecosystem"),
        "target_service": request.get("target_service"),
        "operator_confirmation": request.get("operator_confirmation") or {},
        "supersede_candidate_id": request.get("supersede_candidate_id"),
        "context": context_subset,
    }


def _read_idempotency_record(idempotency_key: str) -> Optional[Dict[str, Any]]:
    path = _idempotency_path(idempotency_key)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _persist_idempotency_result(
    idempotency_key: str,
    normalized_request: Dict[str, Any],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    path = _idempotency_path(idempotency_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "idempotency_key": idempotency_key,
        "normalized_request": normalized_request,
        "result": result,
        "written_at": _now(),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return result


def _idempotency_path(idempotency_key: str) -> Path:
    return store_root() / "candidates" / "idempotency" / f"{_slug(idempotency_key)}.json"


def _candidate_id(*, target_ecosystem: str, target_service: str, image_digest: str) -> str:
    digest_short = image_digest.removeprefix("sha256:")[:12] or "unknown"
    return f"{_slug(target_ecosystem)}-{_slug(target_service)}-{digest_short}"


def _candidate_record_path(candidate_id: str) -> Path:
    return store_root() / "candidates" / "records" / f"{_slug(candidate_id)}.json"


def _active_candidate_path(target_ecosystem: str, target_service: str) -> Path:
    return store_root() / "candidates" / "active" / _slug(target_ecosystem) / f"{_slug(target_service)}.json"


def _audit_path(candidate_id: str) -> Path:
    return store_root() / "candidates" / "audit" / f"{_slug(candidate_id)}.json"


def _write_audit_receipt(candidate_id: str, request: Dict[str, Any], result: Dict[str, Any], admission: Dict[str, Any]) -> None:
    path = _audit_path(candidate_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "action": ACTION_PROMOTE_CANDIDATE,
        "candidate_id": candidate_id,
        "request": request,
        "result": result,
        "admission_policy_result": admission,
        "written_at": result["created_at"],
    }
    path.write_text(json.dumps(receipt, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _mark_candidate_superseded(active_path: Path, supersede_id: str) -> None:
    try:
        prior = json.loads(active_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    prior["status"] = "superseded"
    prior["superseded_by_request_id"] = supersede_id
    prior["superseded_at"] = _now()
    active_path.write_text(json.dumps(prior, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-") or "value"
