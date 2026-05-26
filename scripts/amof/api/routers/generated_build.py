"""Generated-build artifact and local candidate endpoints.

This router exposes the local `.amof/generated-builds` store. Public
admission and candidate endpoints remain callable, but do not publish
admission or release decision internals.
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
) -> dict:
    """Return the public generated-build admission contract for one artifact."""
    try:
        artifact = load_artifact(Path(repo_path), service=service)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Generated-build artifact not found") from exc

    return evaluate_admission(
        artifact,
        artifact_path=artifact_path_for(Path(repo_path), service=service),
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
) -> dict:
    """Return the public release-admission preview contract for one candidate."""
    try:
        candidate = load_candidate(candidate_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Generated-build candidate not found") from exc
    return evaluate_release_admission_preview(candidate)


@control_router.post("/candidates/promote")
def promote_generated_build_candidate(
    request_body: dict = Body(...),
    current_user: AuthUser | None = Depends(require_step_up_user),
) -> dict:
    """Return the fail-closed public candidate promotion contract."""
    _ = current_user
    return promote_candidate(request_body)
