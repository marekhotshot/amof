"""Bounded repo-adoption control API."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from amof.api.dependencies import require_step_up_user
from amof.api.services.repo_adoption_service import (
    RepoAdoptionRequest,
    RepoAdoptionResponse,
    http_analyze_repo_adoption,
)


router = APIRouter(prefix="/repo-adoption", tags=["repo-adoption", "control"])


@router.post("/analyses", dependencies=[Depends(require_step_up_user)])
def create_repo_adoption_analysis(body: RepoAdoptionRequest) -> RepoAdoptionResponse:
    return http_analyze_repo_adoption(body)
