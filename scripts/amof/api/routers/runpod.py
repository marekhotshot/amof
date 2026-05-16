"""FastAPI router for the bounded Runpod Pod lifecycle provider.

Exposes the symmetrical lifecycle surface scoped to AMOF-managed pods:
``create``, ``list`` (AMOF-managed only), ``get``, ``start``, ``stop``,
``delete``. All requests require the operator-role auth guard.

T9: ``AMOF_RUNPOD_LIFECYCLE_MANAGEMENT=readonly`` gates all mutating
endpoints. GET endpoints remain available so operators can still
inspect profiles, pods, and health from the UI in cloud-dev/prod-dev.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Response

from amof.api.dependencies import require_operator_user
from amof.api.services.runpod import (
    RunpodBudgetCapExceeded,
    RunpodClient,
    RunpodClientError,
    RunpodHttpError,
    RunpodNotConfigured,
    RunpodProfileError,
    RunpodSiblingConflict,
    RunpodTtlExceeded,
    list_profiles,
    load_profile,
    project_pod_status,
)
from amof.api.services.runpod_heavy_lane import evaluate_heavy_lane_status, resolve_profile

router = APIRouter(prefix="/runpod", tags=["runpod"])


def _require_write_mode() -> None:
    """Refuse mutating endpoints when the control plane runs in readonly mode."""

    mode = str(os.environ.get("AMOF_RUNPOD_LIFECYCLE_MANAGEMENT") or "write").strip().lower()
    if mode == "readonly":
        raise HTTPException(
            status_code=403,
            detail=(
                "RunPod lifecycle mutations are disabled on this control plane "
                "(AMOF_RUNPOD_LIFECYCLE_MANAGEMENT=readonly). Drive pod "
                "create/stop/delete from a local control plane instead."
            ),
        )


def _client() -> RunpodClient:
    try:
        return RunpodClient()
    except RunpodNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _translate(exc: BaseException) -> HTTPException:
    if isinstance(exc, RunpodNotConfigured):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, RunpodProfileError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RunpodTtlExceeded):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RunpodSiblingConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RunpodBudgetCapExceeded):
        return HTTPException(status_code=402, detail=str(exc))
    if isinstance(exc, RunpodHttpError):
        return HTTPException(
            status_code=exc.status_code,
            detail={
                "method": exc.method,
                "path": exc.path,
                "upstream_status": exc.status_code,
                "upstream_body": exc.body,
            },
        )
    if isinstance(exc, RunpodClientError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=500, detail=f"Unexpected runpod error: {exc!r}")


@router.get("/heavy-lane/profile")
def heavy_lane_profile(_user=Depends(require_operator_user)) -> Dict[str, Any]:
    """Return the canonical AMOF-owned RunPod heavy-lane profile."""
    return resolve_profile()


@router.get("/heavy-lane/health")
def heavy_lane_health(_user=Depends(require_operator_user)) -> Dict[str, Any]:
    """Health-check the canonical RunPod heavy lane without running inference."""
    return evaluate_heavy_lane_status()


@router.get("/profiles")
def list_profile_catalog(_user=Depends(require_operator_user)) -> List[Dict[str, Any]]:
    """List AMOF RunPod profiles materialized in the runtime profiles dir."""
    try:
        return list_profiles()
    except RunpodClientError as exc:
        raise _translate(exc) from exc


@router.post("/pods", status_code=201)
def create_pod(
    payload: Dict[str, Any] = Body(...),
    _user=Depends(require_operator_user),
) -> Dict[str, Any]:
    _require_write_mode()
    profile_name = str((payload or {}).get("profile") or "").strip()
    if not profile_name:
        raise HTTPException(status_code=400, detail="Field 'profile' is required.")
    client = _client()
    try:
        profile = load_profile(profile_name, client.settings)
        pod = client.create_pod(profile)
    except (RunpodClientError, HTTPException) as exc:
        if isinstance(exc, HTTPException):
            raise
        raise _translate(exc) from exc
    return project_pod_status(pod)


@router.get("/pods/{pod_id}/health")
def pod_health(pod_id: str, _user=Depends(require_operator_user)) -> Dict[str, Any]:
    """Per-pod endpoint health check without running inference."""
    client = _client()
    try:
        pod = client.get_pod(pod_id)
        env = pod.get("env") or {}
        profile_name = str(env.get("AMOF_PROFILE") or "").strip() if isinstance(env, dict) else ""
        if not profile_name:
            raise HTTPException(
                status_code=400,
                detail="Pod is not AMOF-managed (no AMOF_PROFILE marker); cannot resolve health.",
            )
        profile = load_profile(profile_name, client.settings)
        return client.health_check_endpoint(pod_id, profile)
    except HTTPException:
        raise
    except RunpodClientError as exc:
        raise _translate(exc) from exc


@router.post("/pods/{pod_id}/mark-usable")
def mark_pod_usable(
    pod_id: str,
    payload: Dict[str, Any] = Body(...),
    _user=Depends(require_operator_user),
) -> Dict[str, Any]:
    """Record an operator-visible usable/unusable projection for this pod."""
    _require_write_mode()
    usable_raw = (payload or {}).get("usable")
    if not isinstance(usable_raw, bool):
        raise HTTPException(status_code=400, detail="Field 'usable' must be boolean.")
    reason = str((payload or {}).get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Field 'reason' is required.")
    client = _client()
    try:
        return client.mark_usable(pod_id, usable=usable_raw, reason=reason)
    except RunpodClientError as exc:
        raise _translate(exc) from exc


@router.get("/pods")
def list_pods(_user=Depends(require_operator_user)) -> List[Dict[str, Any]]:
    client = _client()
    try:
        pods = client.list_amof_pods()
    except RunpodClientError as exc:
        raise _translate(exc) from exc
    return [project_pod_status(pod) for pod in pods]


@router.get("/pods/{pod_id}")
def get_pod(pod_id: str, _user=Depends(require_operator_user)) -> Dict[str, Any]:
    client = _client()
    try:
        pod = client.get_pod(pod_id)
    except RunpodClientError as exc:
        raise _translate(exc) from exc
    return project_pod_status(pod)


@router.post("/pods/{pod_id}/start")
def start_pod(pod_id: str, _user=Depends(require_operator_user)) -> Dict[str, Any]:
    _require_write_mode()
    client = _client()
    try:
        pod = client.start_pod(pod_id)
    except RunpodClientError as exc:
        raise _translate(exc) from exc
    return project_pod_status(pod)


@router.post("/pods/{pod_id}/stop")
def stop_pod(pod_id: str, _user=Depends(require_operator_user)) -> Dict[str, Any]:
    _require_write_mode()
    client = _client()
    try:
        pod = client.stop_pod(pod_id)
    except RunpodClientError as exc:
        raise _translate(exc) from exc
    return project_pod_status(pod)


@router.delete("/pods/{pod_id}", status_code=204)
def delete_pod(pod_id: str, _user=Depends(require_operator_user)) -> Response:
    _require_write_mode()
    client = _client()
    try:
        client.delete_pod(pod_id)
    except RunpodClientError as exc:
        raise _translate(exc) from exc
    return Response(status_code=204)


@router.post("/gc")
def gc(
    payload: Optional[Dict[str, Any]] = Body(default=None),
    _user=Depends(require_operator_user),
) -> Dict[str, Any]:
    """Workspace-scoped zombie sweep across AMOF-managed pods.

    Body is optional. ``dry_run`` defaults to ``True`` (never deletes
    without explicit opt-in). ``overshoot_seconds`` controls how far
    past TTL a pod must be before it counts as a zombie (default 3600).
    """
    body = payload or {}
    dry_run = bool(body.get("dry_run", True))
    if not dry_run:
        _require_write_mode()
    overshoot = int(body.get("overshoot_seconds") or 3600)
    client = _client()
    try:
        actions = client.garbage_collect(dry_run=dry_run, overshoot_seconds=overshoot)
    except RunpodClientError as exc:
        raise _translate(exc) from exc
    return {
        "dry_run": dry_run,
        "overshoot_seconds": overshoot,
        "count": len(actions),
        "actions": actions,
    }


@router.post("/ttl/enforce")
def ttl_enforce(
    payload: Optional[Dict[str, Any]] = Body(default=None),
    _user=Depends(require_operator_user),
) -> Dict[str, Any]:
    """Run the TTL enforcer once. ``dry_run`` defaults to ``True``."""
    body = payload or {}
    dry_run = bool(body.get("dry_run", True))
    if not dry_run:
        _require_write_mode()
    client = _client()
    try:
        actions = client.enforce_ttl(dry_run=dry_run)
    except RunpodClientError as exc:
        raise _translate(exc) from exc
    return {
        "dry_run": dry_run,
        "count": len(actions),
        "actions": actions,
    }
