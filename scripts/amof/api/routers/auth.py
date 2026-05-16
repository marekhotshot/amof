from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from amof.api.auth import (
    AMOF_AUTH_COOKIE,
    AuthUser,
    StepUpStore,
    SupabaseAuthService,
    apply_n8n_session_cookies,
    build_auth_session_payload,
    clear_auth_cookie,
    clear_n8n_cookie,
    extract_access_token,
    set_auth_cookie,
)
from amof.api.dependencies import get_auth_service, get_step_up_store, require_authenticated_user, require_step_up_user
from amof.api.dependencies import require_operator_user
from amof.api.models.auth import AuthSessionRequest, StepUpRequest


router = APIRouter(prefix="/auth", tags=["auth"])


def _optional_current_user(request: Request, auth_service: SupabaseAuthService) -> Optional[AuthUser]:
    access_token = extract_access_token(request)
    if not access_token or not auth_service.auth_enabled:
        request.state.auth_user = None
        request.state.auth_access_token = None
        request.state.auth_step_up = None
        return None
    user = auth_service.verify_access_token(access_token)
    request.state.auth_user = user
    request.state.auth_access_token = access_token
    return user


@router.get("/session")
def get_auth_session(
    request: Request,
    auth_service: SupabaseAuthService = Depends(get_auth_service),
    step_up_store: StepUpStore = Depends(get_step_up_store),
) -> Dict[str, Any]:
    user = _optional_current_user(request, auth_service)
    return build_auth_session_payload(request, auth_service, step_up_store, user)


@router.post("/session")
def create_auth_session(
    body: AuthSessionRequest,
    request: Request,
    response: Response,
    auth_service: SupabaseAuthService = Depends(get_auth_service),
    step_up_store: StepUpStore = Depends(get_step_up_store),
) -> Dict[str, Any]:
    user = auth_service.verify_access_token(body.access_token)
    existing_token = request.cookies.get(AMOF_AUTH_COOKIE)
    if existing_token and existing_token != body.access_token:
        step_up_store.revoke(access_token=existing_token)
    set_auth_cookie(response, request, body.access_token)
    request.state.auth_user = user
    request.state.auth_access_token = body.access_token
    request.state.auth_step_up = None
    return build_auth_session_payload(request, auth_service, step_up_store, user)


@router.delete("/session")
def delete_auth_session(
    request: Request,
    response: Response,
    auth_service: SupabaseAuthService = Depends(get_auth_service),
    step_up_store: StepUpStore = Depends(get_step_up_store),
) -> Dict[str, Any]:
    access_token = extract_access_token(request)
    if access_token:
        step_up_store.revoke(access_token=access_token)
    clear_auth_cookie(response, request)
    clear_n8n_cookie(response, request, auth_service)
    request.state.auth_user = None
    request.state.auth_access_token = None
    request.state.auth_step_up = None
    return build_auth_session_payload(request, auth_service, step_up_store, None)


@router.post("/reauth")
def reauthorize_step_up(
    body: StepUpRequest,
    request: Request,
    current_user: Optional[AuthUser] = Depends(require_operator_user),
    auth_service: SupabaseAuthService = Depends(get_auth_service),
    step_up_store: StepUpStore = Depends(get_step_up_store),
) -> Dict[str, Any]:
    if current_user is None or not current_user.email:
        raise HTTPException(status_code=400, detail="Authenticated users must include an email for password reauthorization.")
    access_token = getattr(request.state, "auth_access_token", None) or extract_access_token(request)
    if not isinstance(access_token, str) or not access_token.strip():
        raise HTTPException(status_code=401, detail="Access token missing from authenticated request.")
    auth_service.verify_password(current_user.email, body.password)
    step_up = step_up_store.grant(access_token, current_user.user_id, auth_service.step_up_ttl_seconds)
    request.state.auth_step_up = step_up
    return build_auth_session_payload(request, auth_service, step_up_store, current_user)


@router.get("/n8n/launch")
def launch_n8n(
    request: Request,
    current_user: Optional[AuthUser] = Depends(require_step_up_user),
    auth_service: SupabaseAuthService = Depends(get_auth_service),
) -> RedirectResponse:
    del current_user
    if not auth_service.n8n_base_url:
        raise HTTPException(status_code=404, detail="n8n operator access is not configured for this environment.")
    response = RedirectResponse(auth_service.n8n_base_url, status_code=307)
    if auth_service.has_n8n_session_bridge():
        cookies = auth_service.establish_n8n_session()
        apply_n8n_session_cookies(response, request, auth_service, cookies)
    return response


@router.get("/supabase/launch")
def launch_supabase(
    current_user: Optional[AuthUser] = Depends(require_step_up_user),
    auth_service: SupabaseAuthService = Depends(get_auth_service),
) -> RedirectResponse:
    del current_user
    if not auth_service.public_url:
        raise HTTPException(status_code=404, detail="Supabase operator access is not configured for this environment.")
    return RedirectResponse(auth_service.public_url, status_code=307)
