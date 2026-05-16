import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, Request

from amof.app_paths import logs_dir, queue_dir, runs_dir
from amof.api.auth import (
    AuthUser,
    StepUpStore,
    SupabaseAuthService,
    require_authenticated_user as _require_authenticated_user,
    require_operator_user as _require_operator_user,
    require_step_up_user as _require_step_up_user,
    resolve_internal_control_user,
)
from amof.api.command_builder import get_workspace_root
from amof.orchestrator.lifecycle import EcosystemManager
from amof.api.run_store import RunStore
from amof.api.run_manager import RunManager
from amof.logs import StructuredLogStore
from amof.queue import QueueDispatcher, QueueStore

ecosystem_manager = EcosystemManager()
_workspace_root = get_workspace_root()


def _env_path(env_var: str, default_relative: str):
    raw = (os.environ.get(env_var) or "").strip()
    if raw:
        return _workspace_root / raw if not raw.startswith("/") else Path(raw)
    return _workspace_root / default_relative


_run_store = RunStore(str(os.environ.get("AMOF_RUNS_DIR") or runs_dir()))
_queue_store = QueueStore(Path(os.environ.get("AMOF_QUEUE_DIR") or queue_dir()))
_log_store = StructuredLogStore(Path(os.environ.get("AMOF_STRUCTURED_LOGS_DIR") or logs_dir()))
run_manager = RunManager(store=_run_store, queue_store=_queue_store, log_store=_log_store)
queue_dispatcher = QueueDispatcher(run_manager=run_manager, queue_store=_queue_store)
auth_service = SupabaseAuthService()
step_up_store = StepUpStore()

def get_ecosystem_manager():
    return ecosystem_manager

def get_run_manager():
    return run_manager


def get_queue_dispatcher():
    return queue_dispatcher


def get_auth_service() -> SupabaseAuthService:
    return auth_service


def get_step_up_store() -> StepUpStore:
    return step_up_store


def require_authenticated_user(
    request: Request,
    auth_service: SupabaseAuthService = Depends(get_auth_service),
) -> Optional[AuthUser]:
    return _require_authenticated_user(request, auth_service)


def resolve_control_or_authenticated_user(
    request: Request,
    auth_service: SupabaseAuthService = Depends(get_auth_service),
) -> Optional[AuthUser]:
    internal_user = resolve_internal_control_user(request, auth_service)
    if internal_user is not None:
        return internal_user
    return _require_authenticated_user(request, auth_service)


def require_step_up_user(
    request: Request,
    current_user: Optional[AuthUser] = Depends(resolve_control_or_authenticated_user),
    auth_service: SupabaseAuthService = Depends(get_auth_service),
    step_up_store: StepUpStore = Depends(get_step_up_store),
) -> Optional[AuthUser]:
    return _require_step_up_user(request, current_user, auth_service, step_up_store)


def require_operator_user(
    request: Request,
    current_user: Optional[AuthUser] = Depends(resolve_control_or_authenticated_user),
    auth_service: SupabaseAuthService = Depends(get_auth_service),
) -> Optional[AuthUser]:
    return _require_operator_user(request, current_user, auth_service)
