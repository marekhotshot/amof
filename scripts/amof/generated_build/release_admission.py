"""Read-only release-admission preview for generated-build candidates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .store import load_artifact


RESULT_KIND = "generated_build_release_admission_preview"
STATUS_CANDIDATE_ONLY = "release_candidate_only"
STATUS_ADMITTED_PREVIEW = "release_admitted_preview"
STATUS_REFUSED = "refused"
STATUS_UNAVAILABLE = "unavailable"

BLOCKING_RISK_FLAGS = {
    "blocking_risk_flag_present",
    "polyglot_repo_no_per_service_map",
}


def evaluate_release_admission_preview(
    candidate: Dict[str, Any],
    *,
    artifact: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate release-admission readiness without writing any state."""
    context = dict(context or {})
    candidate_id = str(candidate.get("candidate_id") or "")
    artifact_ref = candidate.get("artifact_ref") if isinstance(candidate.get("artifact_ref"), dict) else {}
    loaded_artifact = artifact
    unavailable: list[str] = []

    if loaded_artifact is None:
        try:
            loaded_artifact = load_artifact(str(artifact_ref.get("repo_path") or ""), service=str(artifact_ref.get("service") or "root"))
        except FileNotFoundError:
            unavailable.append("artifact_not_found")
        except (ValueError, TypeError):
            unavailable.append("artifact_record_invalid")

    loaded_artifact = loaded_artifact or {}
    reasons: list[str] = []
    missing: list[str] = []
    refusals: list[str] = []

    if not candidate_id:
        unavailable.append("candidate_record_invalid")

    if candidate.get("status") != "candidate_only":
        refusals.append("candidate_status_not_candidate_only")
    else:
        reasons.append("candidate_record_present")

    candidate_admission_status = str(((candidate.get("admission_policy_result") or {}).get("admission_status")) or "")
    if candidate_admission_status != "candidate_only":
        refusals.append("candidate_admission_not_candidate_only")
    else:
        reasons.append("candidate_only")

    artifact_digest = str((loaded_artifact.get("build_proof") or {}).get("image_digest") or "")
    candidate_digest = str(candidate.get("image_digest") or artifact_ref.get("image_digest") or "")
    if loaded_artifact:
        if loaded_artifact.get("build_contract_kind") != "generated":
            refusals.append("build_contract_kind_not_generated")
        if loaded_artifact.get("status") != "runtime_proven":
            refusals.append("artifact_status_below_runtime_proven")
        else:
            reasons.append("runtime_proven")
        if loaded_artifact.get("confidence") != "accepted":
            refusals.append("confidence_not_accepted")
        if not (loaded_artifact.get("runtime_proof") or {}).get("liveness_signal"):
            refusals.append("runtime_proof_missing")
        if candidate_digest and artifact_digest and candidate_digest != artifact_digest:
            refusals.append("candidate_artifact_digest_mismatch")
        if _has_blocking_risk_flags(loaded_artifact):
            refusals.append("blocking_risk_flag_present")

    if context.get("existing_release_candidate_present"):
        refusals.append("existing_release_candidate_present")
        reasons.append("existing_release_candidate_conflict")
    if context.get("release_or_deploy_mutation_requested"):
        refusals.append("release_or_deploy_mutation_requested")

    if not context.get("release_target"):
        missing.append("release_target")
    else:
        reasons.append("release_target_present")
    if not context.get("source_ref_confirmed"):
        missing.append("source_ref_confirmation")
    else:
        reasons.append("source_ref_confirmed")
    if not context.get("deploy_pull_reference_present"):
        missing.append("deploy_pull_reference_confirmation")
    else:
        reasons.append("deploy_pull_reference_present")
    if not context.get("chart_contract_present"):
        missing.append("chart_contract_review")
    else:
        reasons.append("chart_contract_present")
    if not context.get("operator_preview_only_acknowledged"):
        missing.append("operator_preview_only_acknowledgement")
    else:
        reasons.append("operator_preview_only_acknowledged")

    if unavailable:
        status = STATUS_UNAVAILABLE
    elif refusals:
        status = STATUS_REFUSED
    elif missing:
        status = STATUS_CANDIDATE_ONLY
    else:
        status = STATUS_ADMITTED_PREVIEW

    return {
        "result_kind": RESULT_KIND,
        "candidate_id": candidate_id,
        "candidate_status": str(candidate.get("status") or ""),
        "artifact_ref": {
            "artifact_path": str(artifact_ref.get("artifact_path") or ""),
            "repo_path": str(artifact_ref.get("repo_path") or ""),
            "service": str(artifact_ref.get("service") or ""),
            "image_digest": candidate_digest or artifact_digest,
        },
        "target_ecosystem": str(candidate.get("target_ecosystem") or ""),
        "target_service": str(candidate.get("target_service") or ""),
        "image_digest": candidate_digest or artifact_digest,
        "artifact_proof_status": str(loaded_artifact.get("status") or ""),
        "candidate_admission_status": candidate_admission_status,
        "release_admission_preview_status": status,
        "would_create_release_admission": False,
        "would_create_deploy_admission": False,
        "reasons": sorted(set(reasons)),
        "missing_prerequisites": sorted(set(missing if status != STATUS_REFUSED else [])),
        "refusal_conditions": sorted(set(refusals or unavailable)),
        "evidence_summary": _evidence_summary(loaded_artifact),
        "preview_context": _preview_context(context),
        "evaluated_at": _now(),
    }


def _evidence_summary(artifact: Dict[str, Any]) -> Dict[str, Any]:
    runtime_proof = artifact.get("runtime_proof") or {}
    return {
        "build_contract_kind": str(artifact.get("build_contract_kind") or ""),
        "runtime_family": str(artifact.get("runtime_family") or ""),
        "confidence": str(artifact.get("confidence") or ""),
        "template_id": str((artifact.get("dockerfile_template") or {}).get("id") or ""),
        "has_build_proof": bool(artifact.get("build_proof")),
        "has_runtime_proof": bool(runtime_proof),
        "liveness_signal": runtime_proof.get("liveness_signal"),
        "risk_flags": list(artifact.get("risk_flags") or []),
    }


def _preview_context(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "release_target": context.get("release_target") or None,
        "source_ref_confirmed": bool(context.get("source_ref_confirmed")),
        "deploy_pull_reference_present": bool(context.get("deploy_pull_reference_present")),
        "chart_contract_present": bool(context.get("chart_contract_present")),
        "operator_preview_only_acknowledged": bool(context.get("operator_preview_only_acknowledged")),
        "existing_release_candidate_present": bool(context.get("existing_release_candidate_present")),
    }


def _has_blocking_risk_flags(artifact: Dict[str, Any]) -> bool:
    return bool(BLOCKING_RISK_FLAGS.intersection(set(artifact.get("risk_flags") or [])))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
