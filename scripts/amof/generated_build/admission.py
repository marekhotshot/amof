"""Public generated-build admission contract envelope.

The public distribution keeps the admission-preview command/API callable,
but it does not publish admission policy internals. Evaluation therefore
fails closed and returns a stable, side-effect-free result envelope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


POLICY_RESULT_KIND = "generated_build_admission_policy_result"
ADMISSION_EVIDENCE_ONLY = "evidence_only"
ADMISSION_CANDIDATE_ONLY = "candidate_only"
ADMISSION_DEPLOY_ADMITTED = "deploy_admitted"
ADMISSION_REFUSED = "refused"

PROOF_REFUSED = "refused"


def evaluate_admission(
    artifact: Dict[str, Any],
    *,
    artifact_path: Optional[str | Path] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a fail-closed public generated-build admission result."""
    _ = context
    proof_status = str(artifact.get("status") or PROOF_REFUSED)
    return _result(
        artifact,
        artifact_path=artifact_path,
        proof_status=proof_status,
        admission_status=ADMISSION_REFUSED,
        reasons=["public_contract_only"],
        missing=["admission_result_from_authorized_workflow"],
        refusals=["admission_not_evaluated_in_public_distribution"],
        precedence_decision="not_evaluated",
        evidence=_evidence_summary(artifact),
    )


def _result(
    artifact: Dict[str, Any],
    *,
    artifact_path: Optional[str | Path],
    proof_status: str,
    admission_status: str,
    reasons: list[str],
    missing: list[str],
    refusals: list[str],
    precedence_decision: str,
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "policy_result_kind": POLICY_RESULT_KIND,
        "artifact_ref": {
            "artifact_path": str(artifact_path or ""),
            "repo_path": str((artifact.get("source_repo") or {}).get("host_path") or ""),
            "service": str(artifact.get("service") or "root"),
            "image_digest": _image_digest(artifact) or "",
        },
        "artifact_proof_status": proof_status,
        "admission_status": admission_status,
        "reasons": sorted(set(reasons)),
        "missing_prerequisites": sorted(set(missing)),
        "refusal_conditions": sorted(set(refusals)),
        "precedence_decision": precedence_decision,
        "evidence_summary": evidence,
        "evaluated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _evidence_summary(artifact: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "artifact_record_present": bool(artifact),
        "has_build_proof": bool(artifact.get("build_proof")),
        "has_runtime_proof": bool(artifact.get("runtime_proof")),
        "has_image_digest": bool(_image_digest(artifact)),
    }


def _image_digest(artifact: Dict[str, Any]) -> str:
    return str((artifact.get("build_proof") or {}).get("image_digest") or "")
