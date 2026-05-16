from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends

from amof.api.auth import AuthUser, SupabaseAuthService
from amof.api.dependencies import get_auth_service, require_operator_user


router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
def list_users(
    current_user: Optional[AuthUser] = Depends(require_operator_user),
    auth_service: SupabaseAuthService = Depends(get_auth_service),
) -> Dict[str, Any]:
    del current_user
    return auth_service.operator_users_surface()
