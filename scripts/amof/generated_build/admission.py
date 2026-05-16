"""Read-only generated-build admission policy evaluator.

The evaluator maps a stored generated-build artifact to the machine
readable result defined by
`contracts/generated-build-admission-policy.schema.json`.

It is intentionally side-effect free: no artifact mutation, no deploy
state, no release admission, no Docker execution.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


POLICY_RESULT_KIND = "generated_build_admission_policy_result"
ADMISSION_EVIDENCE_ONLY = "evidence_only"
ADMISSION_CANDIDATE_ONLY = "candidate_only"
ADMISSION_DEPLOY_ADMITTED = "deploy_admitted"
ADMISSION_REFUSED = "refused"

PROOF_REFUSED = "refused"
PROOF_PROPOSED = "proposed"
PROOF_BUILD_PROVEN = "build_proven"
PROOF_RUNTIME_PROVEN = "runtime_proven"

ADMISSION_ENABLED_FAMILIES = {"python", "node", "go"}
ACTIVE_TEMPLATE_IDS = {
    "python-uvicorn-distroless-v1",
    "node-express-distroless-v1",
    "go-stdlib-distroless-v1",
}
ACCEPTED_LIVENESS_SIGNALS = {"port_open", "healthcheck_ok", "log_pattern_seen"}
BLOCKING_RISK_FLAGS = {
    "multi_language_signals",
    "custom_toolchain_heuristic",
    "existing_build_contract_present",
}


def evaluate_admission(
    artifact: Dict[str, Any],
    *,
    artifact_path: Optional[str | Path] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a read-only generated-build admission policy result."""
    context = dict(context or {})
    reasons: list[str] = []
    missing: list[str] = []
    refusals: list[str] = []

    proof_status = str(artifact.get("status") or PROOF_REFUSED)
    evidence = _evidence_summary(artifact)

    existing_present = bool(context.get("existing_build_contract_present"))
    existing_resolved = bool(context.get("existing_build_precedence_resolved"))
    operator_confirmation = context.get("operator_confirmation")
    deploy_pull_reference_present = bool(context.get("deploy_pull_reference_present"))
    source_ref_confirmed = bool(context.get("source_ref_confirmed"))

    precedence_decision = "no_existing_build_contract"
    if existing_present and not existing_resolved:
        precedence_decision = "existing_build_wins"
        reasons.extend(["generated_artifact_exists", "existing_build_contract_detected"])
        refusals.append("existing_build_contract_present")
        missing.append("existing_build_contract_retirement_decision")
        return _result(
            artifact,
            artifact_path=artifact_path,
            proof_status=proof_status,
            admission_status=ADMISSION_REFUSED,
            reasons=reasons,
            missing=missing,
            refusals=refusals,
            precedence_decision=precedence_decision,
            evidence=evidence,
        )

    if existing_present and existing_resolved:
        precedence_decision = "generated_build_selected_by_policy"
        reasons.append("existing_build_precedence_resolved")

    if proof_status != PROOF_RUNTIME_PROVEN:
        if proof_status == PROOF_REFUSED:
            refusals.append("artifact_proof_status_refused")
        else:
            missing.append("runtime_proven")
        return _result(
            artifact,
            artifact_path=artifact_path,
            proof_status=proof_status,
            admission_status=ADMISSION_EVIDENCE_ONLY if proof_status != PROOF_REFUSED else ADMISSION_REFUSED,
            reasons=reasons or ["evidence_recorded"],
            missing=missing,
            refusals=refusals,
            precedence_decision=precedence_decision,
            evidence=evidence,
        )

    candidate_missing, candidate_refusals, candidate_reasons = _candidate_checks(artifact)
    reasons.extend(candidate_reasons)
    missing.extend(candidate_missing)
    refusals.extend(candidate_refusals)

    if refusals:
        return _result(
            artifact,
            artifact_path=artifact_path,
            proof_status=proof_status,
            admission_status=ADMISSION_REFUSED,
            reasons=reasons,
            missing=missing,
            refusals=refusals,
            precedence_decision=precedence_decision,
            evidence=evidence,
        )

    if missing:
        return _result(
            artifact,
            artifact_path=artifact_path,
            proof_status=proof_status,
            admission_status=ADMISSION_EVIDENCE_ONLY,
            reasons=reasons,
            missing=missing,
            refusals=[],
            precedence_decision=precedence_decision,
            evidence=evidence,
        )

    deploy_missing: list[str] = []
    deploy_reasons: list[str] = []
    if not isinstance(operator_confirmation, dict) or not operator_confirmation.get("confirmed"):
        deploy_missing.append("operator_confirmation")
    else:
        digest = _image_digest(artifact)
        confirmed_digest = operator_confirmation.get("confirmed_image_digest")
        if digest and confirmed_digest != digest:
            return _result(
                artifact,
                artifact_path=artifact_path,
                proof_status=proof_status,
                admission_status=ADMISSION_REFUSED,
                reasons=reasons + ["operator_confirmation_present"],
                missing=[],
                refusals=["operator_confirmation_digest_mismatch"],
                precedence_decision=precedence_decision,
                evidence=evidence,
                operator_confirmation=operator_confirmation,
            )
        deploy_reasons.append("operator_confirmed")
    if not deploy_pull_reference_present:
        deploy_missing.append("deploy_pull_reference_confirmation")
    else:
        deploy_reasons.append("deploy_pull_reference_present")
    if not source_ref_confirmed:
        deploy_missing.append("source_ref_confirmation")
    else:
        deploy_reasons.append("source_ref_confirmed")
    if existing_present and not existing_resolved:
        deploy_missing.append("existing_build_precedence_resolution")

    if deploy_missing:
        return _result(
            artifact,
            artifact_path=artifact_path,
            proof_status=proof_status,
            admission_status=ADMISSION_CANDIDATE_ONLY,
            reasons=reasons,
            missing=deploy_missing,
            refusals=[],
            precedence_decision=precedence_decision,
            evidence=evidence,
        )

    return _result(
        artifact,
        artifact_path=artifact_path,
        proof_status=proof_status,
        admission_status=ADMISSION_DEPLOY_ADMITTED,
        reasons=reasons + deploy_reasons,
        missing=[],
        refusals=[],
        precedence_decision=precedence_decision,
        evidence=evidence,
        operator_confirmation=operator_confirmation,
    )


def _candidate_checks(artifact: Dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    missing: list[str] = []
    refusals: list[str] = []
    reasons: list[str] = []

    if artifact.get("build_contract_kind") != "generated":
        refusals.append("build_contract_kind_not_generated")
    else:
        reasons.append("generated_build_contract")

    if artifact.get("confidence") != "accepted":
        refusals.append("confidence_not_accepted")
    else:
        reasons.append("confidence_accepted")

    runtime_family = str(artifact.get("runtime_family") or "")
    if runtime_family not in ADMISSION_ENABLED_FAMILIES:
        refusals.append("runtime_family_not_admission_enabled")
    else:
        reasons.append("first_wave_runtime_family")

    template = artifact.get("dockerfile_template") or {}
    template_id = str(template.get("id") or "")
    if template_id not in ACTIVE_TEMPLATE_IDS:
        refusals.append("dockerfile_template_missing_or_unknown")
    else:
        reasons.append("known_template")

    if not template.get("rendered_path"):
        missing.append("rendered_path")
    else:
        reasons.append("template_rendered")

    if not _concrete_image_output(artifact):
        missing.append("concrete_image_output")
    else:
        reasons.append("concrete_image_output")

    if not _valid_digest(_image_digest(artifact)):
        missing.append("build_digest")
    else:
        reasons.append("image_digest_recorded")

    runtime_proof = artifact.get("runtime_proof") or {}
    liveness = runtime_proof.get("liveness_signal")
    if liveness not in ACCEPTED_LIVENESS_SIGNALS:
        missing.append("accepted_liveness_signal")
    else:
        reasons.append("runtime_proven")

    risk_flags = set(artifact.get("risk_flags") or [])
    blocking = sorted(risk_flags & BLOCKING_RISK_FLAGS)
    if blocking:
        refusals.extend(f"blocking_risk_flag_present:{flag}" for flag in blocking)

    return missing, refusals, reasons


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
    operator_confirmation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result = {
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
    if operator_confirmation is not None:
        result["operator_confirmation"] = operator_confirmation
    return result


def _evidence_summary(artifact: Dict[str, Any]) -> Dict[str, Any]:
    runtime_proof = artifact.get("runtime_proof") or {}
    return {
        "build_contract_kind": str(artifact.get("build_contract_kind") or ""),
        "runtime_family": str(artifact.get("runtime_family") or ""),
        "confidence": str(artifact.get("confidence") or ""),
        "template_id": str((artifact.get("dockerfile_template") or {}).get("id") or ""),
        "has_rendered_path": bool((artifact.get("dockerfile_template") or {}).get("rendered_path")),
        "has_build_proof": bool(artifact.get("build_proof")),
        "has_runtime_proof": bool(artifact.get("runtime_proof")),
        "liveness_signal": runtime_proof.get("liveness_signal"),
        "risk_flags": list(artifact.get("risk_flags") or []),
    }


def _image_digest(artifact: Dict[str, Any]) -> str:
    return str((artifact.get("build_proof") or {}).get("image_digest") or "")


def _valid_digest(value: str) -> bool:
    return bool(re.match(r"^sha256:[0-9a-f]{64}$", value or ""))


def _concrete_image_output(artifact: Dict[str, Any]) -> bool:
    for row in artifact.get("image_outputs") or []:
        push = str(row.get("push_image") or "")
        pull = str(row.get("pull_image") or "")
        if push and pull and "unrendered" not in push and "refused" not in push and "unrendered" not in pull and "refused" not in pull:
            return True
    return False
