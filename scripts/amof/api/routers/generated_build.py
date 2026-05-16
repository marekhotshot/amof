"""Generated-build artifact and local candidate endpoints.

This router exposes the local `.amof/generated-builds` store introduced
by UP9-4. Artifact and admission-preview endpoints are intentionally
read-only: no detection, rendering, build-proof, runtime-proof, deploy,
or release admission happens here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from amof.api.dependencies import require_step_up_user
from amof.api.auth import AuthUser
from amof.generated_build.admission import evaluate_admission
from amof.generated_build.candidate import list_candidates, load_candidate, promote_candidate
from amof.generated_build.release_admission import evaluate_release_admission_preview
from amof.generated_build.store import artifact_path_for, load_artifact, load_index


router = APIRouter(prefix="/generated-builds", tags=["generated-builds"])
control_router = APIRouter(prefix="/generated-builds", tags=["generated-builds", "control"])


@router.get("")
def list_generated_build_artifacts() -> dict:
    """List locally persisted generated-build artifacts."""
    return load_index()


@router.get("/artifact")
def get_generated_build_artifact(
    repo_path: str = Query(..., description="Repository root path used when artifact was persisted."),
    service: Optional[str] = Query(None, description="Optional service name; defaults to root."),
) -> dict:
    """Return one locally persisted generated-build artifact.

    The returned artifact is the exact stored JSON: proof status is not
    altered and deployability is not implied.
    """
    try:
        return load_artifact(Path(repo_path), service=service)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Generated-build artifact not found") from exc


@router.get("/admission-preview")
def preview_generated_build_admission(
    repo_path: str = Query(..., description="Repository root path used when artifact was persisted."),
    service: Optional[str] = Query(None, description="Optional service name; defaults to root."),
    existing_build_present: bool = Query(False, description="Assert an existing-build contract conflict."),
    existing_build_resolved: bool = Query(False, description="Assert existing-build precedence is resolved for generated lane."),
    operator_confirmed: bool = Query(False, description="Provide minimal operator confirmation context."),
    confirmed_by: str = Query("operator", description="Operator id/email for confirmation context."),
    deploy_pull_reference_present: bool = Query(False, description="Assert deploy-pull reference is present."),
    source_ref_confirmed: bool = Query(False, description="Assert source repo/ref is confirmed."),
) -> dict:
    """Preview generated-build admission policy for one stored artifact.

    This is read-only. It loads the stored artifact and evaluates policy
    against optional context flags. It never mutates the artifact, never
    writes deploy/release state, and never admits anything into live
    flows.
    """
    try:
        artifact = load_artifact(Path(repo_path), service=service)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Generated-build artifact not found") from exc

    context = {
        "existing_build_contract_present": existing_build_present,
        "existing_build_precedence_resolved": existing_build_resolved,
        "deploy_pull_reference_present": deploy_pull_reference_present,
        "source_ref_confirmed": source_ref_confirmed,
    }
    if operator_confirmed:
        digest = str((artifact.get("build_proof") or {}).get("image_digest") or "")
        context["operator_confirmation"] = {
            "confirmed": True,
            "confirmed_by": confirmed_by,
            "confirmed_at": "1970-01-01T00:00:00Z",
            "confirmed_image_digest": digest,
            "target_service": service or artifact.get("service") or "root",
        }

    return evaluate_admission(
        artifact,
        artifact_path=artifact_path_for(Path(repo_path), service=service),
        context=context,
    )


@router.get("/candidates")
def list_generated_build_candidates() -> dict:
    """List local candidate records without mutating generated-build state."""
    return list_candidates()


@router.get("/candidates/{candidate_id}")
def get_generated_build_candidate(candidate_id: str) -> dict:
    """Return one local candidate record."""
    try:
        return load_candidate(candidate_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Generated-build candidate not found") from exc


@router.get("/candidates/{candidate_id}/release-admission-preview")
def preview_generated_build_release_admission(
    candidate_id: str,
    release_target: Optional[str] = Query(None, description="Readonly release target/context label."),
    source_ref_confirmed: bool = Query(False, description="Assert source ref has been reviewed for this preview."),
    deploy_pull_reference_present: bool = Query(False, description="Assert deploy-pull reference exists for this preview."),
    chart_contract_present: bool = Query(False, description="Assert chart/release contract has been reviewed for this preview."),
    operator_preview_only_acknowledged: bool = Query(False, description="Assert operator understands this is preview-only."),
    existing_release_candidate_present: bool = Query(False, description="Assert an existing release candidate conflict."),
) -> dict:
    """Preview release-admission policy for one local candidate record.

    Read-only: this does not create release admission, deploy admission,
    chart state, lifecycle state, or candidate state.
    """
    try:
        candidate = load_candidate(candidate_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Generated-build candidate not found") from exc
    return evaluate_release_admission_preview(
        candidate,
        context={
            "release_target": release_target,
            "source_ref_confirmed": source_ref_confirmed,
            "deploy_pull_reference_present": deploy_pull_reference_present,
            "chart_contract_present": chart_contract_present,
            "operator_preview_only_acknowledged": operator_preview_only_acknowledged,
            "existing_release_candidate_present": existing_release_candidate_present,
        },
    )


@control_router.post("/candidates/promote")
def promote_generated_build_candidate(
    request_body: dict = Body(...),
    existing_build_present: bool = Query(False, description="Assert an existing-build contract conflict."),
    existing_build_resolved: bool = Query(False, description="Assert existing-build precedence is resolved for generated lane."),
    current_user: AuthUser | None = Depends(require_step_up_user),
) -> dict:
    """Promote a runtime-proven generated-build artifact to local candidate state.

    This is control-only and local-only. It writes candidate store state,
    an active pointer, an audit receipt, and idempotency metadata; it does
    not write deploy, release, chart, lifecycle, or artifact proof state.
    """
    context = {
        "existing_build_contract_present": existing_build_present,
        "existing_build_precedence_resolved": existing_build_resolved,
    }
    if current_user is not None:
        context["authenticated_principal"] = current_user.to_dict()
    return promote_candidate(request_body, context=context)
