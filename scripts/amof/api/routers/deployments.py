"""Deployments overview API: list deployments per ecosystem with promote/rollback."""

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from amof.cli import get_available_ecosystems

router = APIRouter(prefix="/deployments", tags=["deployments"])


@router.get("")
def list_deployments() -> Dict[str, List[Dict[str, Any]]]:
    """List deployment status per ecosystem.
    Returns placeholder structure; can be extended to query spin/k8s or state files.
    """
    ecosystems = get_available_ecosystems()
    deployments: List[Dict[str, Any]] = []
    for name in ecosystems:
        deployments.append({
            "ecosystem": name,
            "environment": "dev",
            "version": None,
            "status": "unknown",
            "last_deployed": None,
        })
    return {"deployments": deployments}
