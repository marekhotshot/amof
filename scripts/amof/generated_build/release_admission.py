"""Public release-admission preview contract for generated-build candidates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

RESULT_KIND = "generated_build_release_admission_preview"
STATUS_CANDIDATE_ONLY = "release_candidate_only"
STATUS_ADMITTED_PREVIEW = "release_admitted_preview"
STATUS_REFUSED = "refused"
STATUS_UNAVAILABLE = "unavailable"


def evaluate_release_admission_preview(
    candidate: Dict[str, Any],
    *,
    artifact: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a fail-closed public release-admission preview envelope."""
    _ = context
    candidate_id = str(candidate.get("candidate_id") or "")
    artifact_ref = candidate.get("artifact_ref") if isinstance(candidate.get("artifact_ref"), dict) else {}
    loaded_artifact = artifact or {}
    artifact_digest = str((loaded_artifact.get("build_proof") or {}).get("image_digest") or "")
    candidate_digest = str(candidate.get("image_digest") or artifact_ref.get("image_digest") or "")

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
        "candidate_admission_status": str(((candidate.get("admission_policy_result") or {}).get("admission_status")) or ""),
        "release_admission_preview_status": STATUS_UNAVAILABLE,
        "would_create_release_admission": False,
        "would_create_deploy_admission": False,
        "reasons": ["public_contract_only"],
        "missing_prerequisites": ["release_admission_result_from_authorized_workflow"],
        "refusal_conditions": ["release_admission_not_evaluated_in_public_distribution"],
        "evidence_summary": _evidence_summary(loaded_artifact),
        "preview_context": {"public_contract_only": True},
        "evaluated_at": _now(),
    }


def _evidence_summary(artifact: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "artifact_record_present": bool(artifact),
        "has_build_proof": bool(artifact.get("build_proof")),
        "has_runtime_proof": bool(artifact.get("runtime_proof")),
        "has_image_digest": bool((artifact.get("build_proof") or {}).get("image_digest")),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
