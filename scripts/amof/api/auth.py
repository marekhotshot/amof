"""Server-validated platform auth and step-up reauthorization."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException, Request, Response


AMOF_AUTH_COOKIE = "amof_access_token"
DEFAULT_STEP_UP_TTL_SECONDS = 600
INTERNAL_CONTROL_CREDENTIAL_HEADER = "x-amof-internal-control-credential"
INTERNAL_CONTROL_ACTOR_HEADER = "x-amof-internal-control-actor"
INTERNAL_CONTROL_PATH_PREFIX = "/api/v1/control"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str = "") -> List[str]:
    raw = os.environ.get(name)
    source = raw if raw is not None else default
    values = [item.strip().lower() for item in source.split(",") if item.strip()]
    deduped: List[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _now_unix() -> float:
    return time.time()


def _iso_at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _auth_detail(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"code": code, "message": message}
    payload.update(extra)
    return payload


def _request_is_secure(request: Request) -> bool:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded_proto:
        return forwarded_proto == "https"
    return request.url.scheme == "https"


def clear_auth_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        AMOF_AUTH_COOKIE,
        path="/",
        secure=_request_is_secure(request),
        samesite="lax",
    )


def set_auth_cookie(response: Response, request: Request, access_token: str) -> None:
    response.set_cookie(
        AMOF_AUTH_COOKIE,
        access_token,
        httponly=True,
        secure=_request_is_secure(request),
        samesite="lax",
        path="/",
    )


def _extract_roles(payload: Dict[str, Any]) -> List[str]:
    roles: List[str] = []
    app_metadata = payload.get("app_metadata")
    if isinstance(app_metadata, dict):
        raw_roles = app_metadata.get("roles")
        if isinstance(raw_roles, list):
            roles.extend(str(item).strip().lower() for item in raw_roles if str(item).strip())
        elif isinstance(raw_roles, str) and raw_roles.strip():
            roles.append(raw_roles.strip().lower())
        raw_role = app_metadata.get("role")
        if isinstance(raw_role, str) and raw_role.strip():
            roles.append(raw_role.strip().lower())
    top_level_role = payload.get("role")
    if isinstance(top_level_role, str) and top_level_role.strip():
        roles.append(top_level_role.strip().lower())
    deduped: List[str] = []
    for role in roles:
        if role not in deduped:
            deduped.append(role)
    return deduped


@dataclass
class AuthUser:
    user_id: str
    email: Optional[str]
    display_name: Optional[str]
    roles: List[str]
    app_metadata: Dict[str, Any]
    user_metadata: Dict[str, Any]
    auth_source: str = "supabase_session"
    principal_type: str = "human_user"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.user_id,
            "email": self.email,
            "display_name": self.display_name,
            "roles": list(self.roles),
            "app_metadata": dict(self.app_metadata),
            "user_metadata": dict(self.user_metadata),
            "auth_source": self.auth_source,
            "principal_type": self.principal_type,
        }


class StepUpStore:
    """Short-lived, server-side step-up grants keyed to the current access token."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}

    def _token_hash(self, access_token: str) -> str:
        return hashlib.sha256(access_token.encode("utf-8")).hexdigest()

    def _cleanup_locked(self) -> None:
        now = _now_unix()
        expired = [token_hash for token_hash, payload in self._entries.items() if float(payload.get("expires_at_unix") or 0) <= now]
        for token_hash in expired:
            self._entries.pop(token_hash, None)

    def grant(self, access_token: str, user_id: str, ttl_seconds: int) -> Dict[str, Any]:
        expires_at_unix = _now_unix() + max(ttl_seconds, 1)
        entry = {
            "user_id": user_id,
            "expires_at_unix": expires_at_unix,
            "expires_at": _iso_at(expires_at_unix),
        }
        with self._lock:
            self._cleanup_locked()
            self._entries[self._token_hash(access_token)] = entry
        return dict(entry)

    def status(self, access_token: str, user_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._cleanup_locked()
            entry = self._entries.get(self._token_hash(access_token))
            if not entry or str(entry.get("user_id") or "") != user_id:
                return None
            return dict(entry)

    def revoke(self, access_token: Optional[str] = None, user_id: Optional[str] = None) -> None:
        with self._lock:
            self._cleanup_locked()
            if access_token:
                self._entries.pop(self._token_hash(access_token), None)
            if user_id:
                for token_hash, entry in list(self._entries.items()):
                    if str(entry.get("user_id") or "") == user_id:
                        self._entries.pop(token_hash, None)


class SupabaseAuthService:
    """Validate Supabase sessions on the control plane."""

    def __init__(self) -> None:
        self.public_url = (os.environ.get("AMOF_SUPABASE_PUBLIC_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or "").strip().rstrip("/")
        self.anon_key = (os.environ.get("AMOF_SUPABASE_ANON_KEY") or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY") or "").strip()
        self.service_role_key = (
            os.environ.get("AMOF_SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or ""
        ).strip()
        self.auth_enabled = _env_flag("AMOF_AUTH_ENABLED", default=False)
        self.strict_roles = _env_flag("AMOF_AUTH_STRICT_ROLES", default=False)
        self.enforce_operator_roles = _env_flag("AMOF_AUTH_ENFORCE_OPERATOR_ROLES", default=False)
        self.viewer_roles = _env_csv("AMOF_AUTH_VIEWER_ROLES", default="authenticated,admin,operator,service_role")
        self.viewer_emails = _env_csv("AMOF_AUTH_VIEWER_EMAILS", default="")
        self.operator_roles = _env_csv("AMOF_AUTH_OPERATOR_ROLES", default="admin,operator,service_role")
        self.operator_emails = _env_csv("AMOF_AUTH_OPERATOR_EMAILS", default="")
        raw_step_up_ttl = os.environ.get("AMOF_AUTH_STEP_UP_TTL_SECONDS") or str(DEFAULT_STEP_UP_TTL_SECONDS)
        try:
            parsed_step_up_ttl = int(raw_step_up_ttl)
        except ValueError:
            parsed_step_up_ttl = DEFAULT_STEP_UP_TTL_SECONDS
        self.step_up_ttl_seconds = max(parsed_step_up_ttl, 60)
        self.n8n_base_url = (os.environ.get("AMOF_N8N_BASE_URL") or "").strip()
        self.n8n_base_path = (os.environ.get("AMOF_N8N_BASE_PATH") or "").strip() or "/"
        self.n8n_internal_url = (os.environ.get("AMOF_N8N_INTERNAL_URL") or "http://n8n:5678").strip().rstrip("/")
        self.n8n_operator_email = (os.environ.get("AMOF_N8N_OPERATOR_EMAIL") or "").strip()
        self.n8n_operator_password = os.environ.get("AMOF_N8N_OPERATOR_PASSWORD") or ""
        self.n8n_auth_cookie_name = (os.environ.get("AMOF_N8N_AUTH_COOKIE_NAME") or "amof_n8n_session").strip()
        self.audit_log_path = Path((os.environ.get("AMOF_AUTH_AUDIT_LOG_PATH") or ".amof/auth-role-audit.jsonl").strip())
        self.internal_control_auth_enabled = _env_flag("AMOF_INTERNAL_CONTROL_AUTH_ENABLED", default=False)
        self.internal_control_credential = (os.environ.get("AMOF_INTERNAL_CONTROL_CREDENTIAL") or "").strip()
        self.internal_control_principal_id = (
            os.environ.get("AMOF_INTERNAL_CONTROL_PRINCIPAL_ID") or "internal_orchestrator"
        ).strip() or "internal_orchestrator"
        self.internal_control_principal_name = (
            os.environ.get("AMOF_INTERNAL_CONTROL_PRINCIPAL_NAME") or self.internal_control_principal_id
        ).strip() or self.internal_control_principal_id
        self.internal_control_principal_email = (os.environ.get("AMOF_INTERNAL_CONTROL_PRINCIPAL_EMAIL") or "").strip() or None
        self.internal_control_allow_step_up_actions = _env_flag(
            "AMOF_INTERNAL_CONTROL_ALLOW_STEP_UP_ACTIONS",
            default=False,
        )
        self._timeout = 10

    def configured(self) -> bool:
        return bool(self.public_url and self.anon_key)

    def _ensure_runtime(self) -> None:
        if not self.configured():
            raise HTTPException(
                status_code=503,
                detail=_auth_detail(
                    "auth_unconfigured",
                    "Platform auth is enabled, but Supabase verification is not fully configured on the control plane.",
                ),
            )

    def _request_headers(self, access_token: Optional[str] = None) -> Dict[str, str]:
        headers = {"apikey": self.anon_key, "Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _admin_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.service_role_key}",
            "apikey": self.service_role_key,
            "Content-Type": "application/json",
        }

    def _user_matches(self, user: AuthUser, roles: List[str], emails: List[str]) -> bool:
        if user.email and user.email.strip().lower() in emails:
            return True
        return bool(set(user.roles) & set(roles))

    def _match_sources(self, user: AuthUser, roles: List[str], emails: List[str]) -> Dict[str, bool]:
        normalized_email = (user.email or "").strip().lower()
        return {
            "allowlist": bool(normalized_email and normalized_email in emails),
            "supabase_roles": bool(set(user.roles) & set(roles)),
        }

    @staticmethod
    def _source_label(sources: Dict[str, bool]) -> str:
        if sources.get("allowlist") and sources.get("supabase_roles"):
            return "both"
        if sources.get("allowlist"):
            return "allowlist"
        if sources.get("supabase_roles"):
            return "supabase_roles"
        return "none"

    def is_console_user(self, user: Optional[AuthUser]) -> bool:
        if user is None:
            return False
        if user.auth_source == "internal_control":
            return True
        return self._user_matches(user, self.viewer_roles, self.viewer_emails)

    def is_operator_user(self, user: Optional[AuthUser]) -> bool:
        if user is None:
            return False
        if user.auth_source == "internal_control":
            return True
        if self.enforce_operator_roles:
            return self._user_matches(user, self.operator_roles, self.operator_emails)
        return self.is_console_user(user)

    def permissions_for(self, user: Optional[AuthUser]) -> Dict[str, bool]:
        if not self.auth_enabled:
            return {
                "can_access_console": True,
                "can_operate": True,
                "can_manage_settings": True,
                "can_launch_n8n": bool(self.n8n_base_url),
                "can_launch_supabase": bool(self.public_url),
            }
        can_access_console = self.is_console_user(user)
        can_operate = self.is_operator_user(user)
        return {
            "can_access_console": can_access_console,
            "can_operate": can_operate,
            "can_manage_settings": can_operate,
            "can_launch_n8n": can_operate and bool(self.n8n_base_url),
            "can_launch_supabase": can_operate and bool(self.public_url),
        }

    def has_n8n_session_bridge(self) -> bool:
        return bool(self.n8n_base_url and self.n8n_operator_email and self.n8n_operator_password)

    def n8n_login_url(self) -> str:
        return f"{self.n8n_internal_url}/rest/login"

    def verify_access_token(self, access_token: str) -> AuthUser:
        self._ensure_runtime()
        response = requests.get(
            f"{self.public_url}/auth/v1/user",
            headers=self._request_headers(access_token),
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=401,
                detail=_auth_detail("auth_required", "Your AMOF session is missing or expired. Sign in again to continue."),
            )
        payload = response.json()
        if not isinstance(payload, dict) or not str(payload.get("id") or "").strip():
            raise HTTPException(
                status_code=401,
                detail=_auth_detail("invalid_session", "Supabase returned an invalid user payload for this session."),
            )
        roles = _extract_roles(payload)
        user = AuthUser(
            user_id=str(payload["id"]),
            email=str(payload.get("email") or "").strip() or None,
            display_name=(
                str((payload.get("user_metadata") or {}).get("display_name") or "").strip()
                or str((payload.get("user_metadata") or {}).get("nick") or "").strip()
                or str(payload.get("email") or "").strip()
                or None
            ),
            roles=roles,
            app_metadata=dict(payload.get("app_metadata") or {}),
            user_metadata=dict(payload.get("user_metadata") or {}),
        )
        if self.strict_roles and not self.is_console_user(user):
            raise HTTPException(
                status_code=403,
                detail=_auth_detail("forbidden", "This account is authenticated but does not have AMOF operator access."),
            )
        return user

    def verify_password(self, email: str, password: str) -> None:
        self._ensure_runtime()
        response = requests.post(
            f"{self.public_url}/auth/v1/token?grant_type=password",
            headers=self._request_headers(),
            json={"email": email, "password": password},
            timeout=self._timeout,
        )
        payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.status_code != 200 or not isinstance(payload, dict) or not str(payload.get("access_token") or "").strip():
            raise HTTPException(
                status_code=403,
                detail=_auth_detail("step_up_failed", "Password reauthorization failed. Check your password and try again."),
            )

    def capabilities(self) -> Dict[str, Any]:
        internal_control_enabled = self.internal_control_auth_enabled and bool(self.internal_control_credential)
        return {
            "n8n_launch_url": "/api/v1/auth/n8n/launch" if self.n8n_base_url else None,
            "supabase_launch_url": "/api/v1/auth/supabase/launch" if self.public_url else None,
            "n8n_session_bridge": self.has_n8n_session_bridge(),
            "step_up_ttl_seconds": self.step_up_ttl_seconds,
            "operator_authorization_mode": "strict" if self.enforce_operator_roles else "compat",
            "internal_control_auth_enabled": internal_control_enabled,
            "internal_control_step_up_mode": (
                "allowed" if self.internal_control_allow_step_up_actions else "denied"
            ),
            "arena": {
                "published": False,
                "reason": "Agent Arena is gated in this deployment because the Arena backend contract is not published.",
            },
            "director": {
                "published": True,
            },
        }

    def _ensure_admin_runtime(self) -> None:
        self._ensure_runtime()
        if self.service_role_key:
            return
        raise HTTPException(
            status_code=503,
            detail=_auth_detail(
                "users_unconfigured",
                "Operator user-management view is not configured on the control plane.",
                missing=["service_role_key"],
            ),
        )

    def _admin_list_users_page(self, page: int, per_page: int) -> Dict[str, Any]:
        self._ensure_admin_runtime()
        response = requests.get(
            f"{self.public_url}/auth/v1/admin/users",
            headers=self._admin_headers(),
            params={"page": page, "per_page": per_page},
            timeout=self._timeout,
        )
        if response.status_code != 200:
            detail = response.text.strip() or f"status {response.status_code}"
            raise HTTPException(
                status_code=502,
                detail=_auth_detail(
                    "users_fetch_failed",
                    "AMOF could not load Supabase users for the operator surface.",
                    status_code=response.status_code,
                    upstream_detail=detail,
                ),
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=502,
                detail=_auth_detail(
                    "users_fetch_failed",
                    "Supabase returned an invalid user listing payload.",
                ),
            )
        return payload

    def _admin_get_user(self, user_id: str) -> Dict[str, Any]:
        self._ensure_admin_runtime()
        response = requests.get(
            f"{self.public_url}/auth/v1/admin/users/{user_id}",
            headers=self._admin_headers(),
            timeout=self._timeout,
        )
        if response.status_code != 200:
            detail = response.text.strip() or f"status {response.status_code}"
            raise HTTPException(
                status_code=502 if response.status_code >= 500 else response.status_code,
                detail=_auth_detail(
                    "user_fetch_failed",
                    "AMOF could not load this Supabase user.",
                    status_code=response.status_code,
                    upstream_detail=detail,
                ),
            )
        payload = response.json()
        user_payload = payload.get("user") if isinstance(payload, dict) else None
        if isinstance(user_payload, dict):
            return user_payload
        if isinstance(payload, dict):
            return payload
        raise HTTPException(
            status_code=502,
            detail=_auth_detail(
                "user_fetch_failed",
                "Supabase returned an invalid user payload.",
            ),
        )

    def _admin_update_user(self, user_id: str, app_metadata: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_admin_runtime()
        response = requests.put(
            f"{self.public_url}/auth/v1/admin/users/{user_id}",
            headers=self._admin_headers(),
            json={"app_metadata": app_metadata},
            timeout=self._timeout,
        )
        if response.status_code != 200:
            detail = response.text.strip() or f"status {response.status_code}"
            raise HTTPException(
                status_code=502 if response.status_code >= 500 else response.status_code,
                detail=_auth_detail(
                    "user_update_failed",
                    "AMOF could not update operator access for this user.",
                    status_code=response.status_code,
                    upstream_detail=detail,
                ),
            )
        payload = response.json()
        user_payload = payload.get("user") if isinstance(payload, dict) else None
        if isinstance(user_payload, dict):
            return user_payload
        if isinstance(payload, dict):
            return payload
        raise HTTPException(
            status_code=502,
            detail=_auth_detail(
                "user_update_failed",
                "Supabase returned an invalid user update payload.",
            ),
        )

    def _list_supabase_users(self) -> List[Dict[str, Any]]:
        users: List[Dict[str, Any]] = []
        page = 1
        per_page = 200
        while True:
            payload = self._admin_list_users_page(page=page, per_page=per_page)
            raw_users = payload.get("users")
            page_users = raw_users if isinstance(raw_users, list) else []
            users.extend(user for user in page_users if isinstance(user, dict))
            if len(page_users) < per_page:
                break
            page += 1
        return users

    def _auth_user_from_admin_payload(self, payload: Dict[str, Any]) -> AuthUser:
        user_metadata = dict(payload.get("user_metadata") or {})
        email = str(payload.get("email") or "").strip() or None
        display_name = (
            str(user_metadata.get("display_name") or "").strip()
            or str(user_metadata.get("nick") or "").strip()
            or email
            or None
        )
        return AuthUser(
            user_id=str(payload.get("id") or "").strip(),
            email=email,
            display_name=display_name,
            roles=_extract_roles(payload),
            app_metadata=dict(payload.get("app_metadata") or {}),
            user_metadata=user_metadata,
        )

    def _access_row(self, granted: bool, sources: Dict[str, bool]) -> Dict[str, Any]:
        return {
            "granted": granted,
            "sources": sources,
            "source_of_truth": self._source_label(sources),
        }

    def _surface_user_row(self, user: AuthUser, exists_in_supabase: bool = True) -> Dict[str, Any]:
        viewer_sources = self._match_sources(user, self.viewer_roles, self.viewer_emails)
        operator_sources = self._match_sources(user, self.operator_roles, self.operator_emails)
        can_access_console = self.is_console_user(user)
        can_operate = self.is_operator_user(user)
        effective_roles: List[str] = []
        if can_access_console:
            effective_roles.append("viewer")
        if can_operate:
            effective_roles.append("operator")
        return {
            "id": user.user_id or None,
            "email": user.email,
            "display_name": user.display_name,
            "exists_in_supabase": exists_in_supabase,
            "supabase_roles": list(user.roles),
            "effective_roles": effective_roles,
            "access": {
                "console": self._access_row(can_access_console, viewer_sources),
                "operator": self._access_row(can_operate, operator_sources),
            },
        }

    def _read_audit_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.audit_log_path.exists():
            return []
        events: List[Dict[str, Any]] = []
        try:
            with self.audit_log_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw = line.strip()
                    if not raw:
                        continue
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        events.append(payload)
        except Exception:
            return []
        if limit <= 0:
            return events
        return events[-limit:]

    def _append_audit_event(self, event: Dict[str, Any]) -> None:
        try:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
        except Exception:
            return

    def operator_users_surface(self) -> Dict[str, Any]:
        users = [self._surface_user_row(self._auth_user_from_admin_payload(payload)) for payload in self._list_supabase_users()]
        seen_emails = {
            str(user.get("email") or "").strip().lower()
            for user in users
            if str(user.get("email") or "").strip()
        }

        allowlist_only_rows: List[Dict[str, Any]] = []
        for email in sorted(set(self.viewer_emails + self.operator_emails)):
            normalized_email = email.strip().lower()
            if not normalized_email or normalized_email in seen_emails:
                continue
            allowlist_user = AuthUser(
                user_id="",
                email=normalized_email,
                display_name=None,
                roles=[],
                app_metadata={},
                user_metadata={},
            )
            allowlist_only_rows.append(self._surface_user_row(allowlist_user, exists_in_supabase=False))

        all_rows = sorted(
            users + allowlist_only_rows,
            key=lambda row: (
                0 if row.get("exists_in_supabase") else 1,
                str(row.get("email") or "").lower(),
                str(row.get("id") or ""),
            ),
        )
        recent_events = list(reversed(self._read_audit_events(limit=20)))
        latest_by_target: Dict[str, Dict[str, Any]] = {}
        for event in recent_events:
            target_key = str(event.get("target_user_id") or event.get("target_email") or "").strip().lower()
            if target_key and target_key not in latest_by_target:
                latest_by_target[target_key] = event
        for row in all_rows:
            target_key = str(row.get("id") or row.get("email") or "").strip().lower()
            row["latest_operator_assignment_event"] = latest_by_target.get(target_key)
        return {
            "users": all_rows,
            "summary": {
                "total_rows": len(all_rows),
                "supabase_users": sum(1 for row in all_rows if row.get("exists_in_supabase")),
                "allowlist_only_rows": sum(1 for row in all_rows if not row.get("exists_in_supabase")),
                "viewer_count": sum(1 for row in all_rows if "viewer" in (row.get("effective_roles") or [])),
                "operator_count": sum(1 for row in all_rows if "operator" in (row.get("effective_roles") or [])),
            },
            "authorization": {
                "operator_authorization_mode": "strict" if self.enforce_operator_roles else "compat",
                "viewer_roles": list(self.viewer_roles),
                "viewer_allowlist_emails": list(self.viewer_emails),
                "operator_roles": list(self.operator_roles),
                "operator_allowlist_emails": list(self.operator_emails),
            },
            "recent_operator_assignment_events": recent_events,
        }

    @staticmethod
    def _normalized_role_list(app_metadata: Dict[str, Any]) -> List[str]:
        raw_roles = app_metadata.get("roles")
        roles: List[str] = []
        if isinstance(raw_roles, list):
            roles.extend(str(item).strip().lower() for item in raw_roles if str(item).strip())
        elif isinstance(raw_roles, str) and raw_roles.strip():
            roles.append(raw_roles.strip().lower())
        deduped: List[str] = []
        for role in roles:
            if role not in deduped:
                deduped.append(role)
        return deduped

    def set_operator_access(self, target_user_id: str, grant: bool, actor: AuthUser) -> Dict[str, Any]:
        target_payload = self._admin_get_user(target_user_id)
        target_user = self._auth_user_from_admin_payload(target_payload)
        if not target_user.user_id:
            raise HTTPException(
                status_code=404,
                detail=_auth_detail("user_not_found", "Supabase did not return a valid target user."),
            )
        if not grant and actor.user_id == target_user.user_id:
            raise HTTPException(
                status_code=400,
                detail=_auth_detail(
                    "self_removal_blocked",
                    "Remove your own operator access through product tooling is blocked to avoid accidental lockout.",
                ),
            )

        before_row = self._surface_user_row(target_user)
        next_app_metadata = dict(target_payload.get("app_metadata") or {})
        next_roles = self._normalized_role_list(next_app_metadata)
        if grant:
            if "operator" not in next_roles:
                next_roles.append("operator")
        else:
            next_roles = [role for role in next_roles if role != "operator"]
            if str(next_app_metadata.get("role") or "").strip().lower() == "operator":
                next_app_metadata["role"] = "authenticated"
        next_app_metadata["roles"] = next_roles
        updated_payload = self._admin_update_user(target_user.user_id, next_app_metadata)
        updated_user = self._auth_user_from_admin_payload(updated_payload)
        after_row = self._surface_user_row(updated_user)
        event = {
            "event_type": "operator_access_granted" if grant else "operator_access_removed",
            "at": datetime.now(timezone.utc).isoformat(),
            "actor_user_id": actor.user_id,
            "actor_email": actor.email,
            "target_user_id": updated_user.user_id,
            "target_email": updated_user.email,
            "before_effective_roles": before_row.get("effective_roles") or [],
            "after_effective_roles": after_row.get("effective_roles") or [],
            "before_operator_source": ((before_row.get("access") or {}).get("operator") or {}).get("source_of_truth"),
            "after_operator_source": ((after_row.get("access") or {}).get("operator") or {}).get("source_of_truth"),
        }
        self._append_audit_event(event)
        after_row["latest_operator_assignment_event"] = event
        return {
            "user": after_row,
            "audit_event": event,
        }

    def establish_n8n_session(self) -> requests.cookies.RequestsCookieJar:
        if not self.has_n8n_session_bridge():
            return requests.cookies.RequestsCookieJar()
        response = requests.post(
            self.n8n_login_url(),
            headers={"Content-Type": "application/json"},
            json={
                "emailOrLdapLoginId": self.n8n_operator_email,
                "password": self.n8n_operator_password,
            },
            timeout=self._timeout,
            allow_redirects=False,
        )
        if response.status_code not in {200, 204}:
            raise HTTPException(
                status_code=502,
                detail=_auth_detail(
                    "n8n_bridge_failed",
                    "AMOF could not establish an n8n operator session right now.",
                    status_code=response.status_code,
                ),
            )
        if not response.cookies:
            raise HTTPException(
                status_code=502,
                detail=_auth_detail(
                    "n8n_bridge_failed",
                    "n8n login succeeded but did not return a browser session cookie.",
                ),
            )
        return response.cookies


def extract_access_token(request: Request) -> Optional[str]:
    authorization = (request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    cookie_token = request.cookies.get(AMOF_AUTH_COOKIE)
    if isinstance(cookie_token, str) and cookie_token.strip():
        return cookie_token.strip()
    return None


def _is_control_request(request: Request) -> bool:
    return request.url.path.startswith(INTERNAL_CONTROL_PATH_PREFIX)


def _normalize_internal_control_actor(raw_value: Optional[str]) -> Optional[str]:
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.strip().lower()
    if not normalized:
        return None
    normalized = re.sub(r"[^a-z0-9._:/-]+", "-", normalized)
    normalized = normalized.strip("-")
    if not normalized:
        return None
    return normalized[:128]


def _internal_control_audit_context(
    request: Request,
    auth_service: SupabaseAuthService,
    user: AuthUser,
) -> Dict[str, Any]:
    actor_label = _normalize_internal_control_actor(request.headers.get(INTERNAL_CONTROL_ACTOR_HEADER))
    internal_step_up_allowed = bool(getattr(auth_service, "internal_control_allow_step_up_actions", False))
    return {
        "auth_path": "internal_control_credential",
        "principal_id": user.user_id,
        "principal_type": user.principal_type,
        "auth_source": user.auth_source,
        "step_up_policy": (
            "internal_allowed" if internal_step_up_allowed else "internal_denied"
        ),
        "actor_label": actor_label,
    }


def resolve_internal_control_user(
    request: Request,
    auth_service: SupabaseAuthService,
) -> Optional[AuthUser]:
    if not auth_service.auth_enabled or not _is_control_request(request):
        return None
    credential = (request.headers.get(INTERNAL_CONTROL_CREDENTIAL_HEADER) or "").strip()
    if not credential:
        return None
    configured_credential = str(getattr(auth_service, "internal_control_credential", "") or "").strip()
    if not bool(getattr(auth_service, "internal_control_auth_enabled", False)) or not configured_credential:
        raise HTTPException(
            status_code=401,
            detail=_auth_detail(
                "internal_control_auth_disabled",
                "Internal control credential auth is disabled for this control-plane deployment.",
            ),
        )
    if not hmac.compare_digest(credential, configured_credential):
        raise HTTPException(
            status_code=401,
            detail=_auth_detail(
                "internal_control_auth_invalid",
                "Internal control credential is invalid for this control-plane deployment.",
            ),
        )
    user = AuthUser(
        user_id=str(getattr(auth_service, "internal_control_principal_id", "internal_orchestrator") or "internal_orchestrator"),
        email=getattr(auth_service, "internal_control_principal_email", None),
        display_name=str(
            getattr(auth_service, "internal_control_principal_name", None)
            or getattr(auth_service, "internal_control_principal_id", "internal_orchestrator")
            or "internal_orchestrator"
        ),
        roles=["internal_control"],
        app_metadata={},
        user_metadata={},
        auth_source="internal_control",
        principal_type="internal_operator",
    )
    request.state.auth_user = user
    request.state.auth_access_token = None
    request.state.auth_step_up = None
    request.state.auth_audit = _internal_control_audit_context(request, auth_service, user)
    return user


def current_step_up_status(
    request: Request,
    step_up_store: StepUpStore,
    user: Optional[AuthUser],
) -> Optional[Dict[str, Any]]:
    if user is None:
        return None
    access_token = getattr(request.state, "auth_access_token", None) or extract_access_token(request)
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    status = step_up_store.status(access_token, user.user_id)
    request.state.auth_step_up = status
    return status


def build_auth_session_payload(
    request: Request,
    auth_service: SupabaseAuthService,
    step_up_store: StepUpStore,
    user: Optional[AuthUser],
) -> Dict[str, Any]:
    step_up = current_step_up_status(request, step_up_store, user)
    return {
        "authenticated": bool(user),
        "auth_enabled": auth_service.auth_enabled,
        "user": user.to_dict() if user else None,
        "permissions": auth_service.permissions_for(user),
        "capabilities": auth_service.capabilities(),
        "step_up": {
            "active": bool(step_up),
            "expires_at": step_up.get("expires_at") if step_up else None,
        },
    }


def clear_n8n_cookie(response: Response, request: Request, auth_service: SupabaseAuthService) -> None:
    if not auth_service.n8n_auth_cookie_name:
        return
    response.delete_cookie(
        auth_service.n8n_auth_cookie_name,
        path="/",
        secure=_request_is_secure(request),
        samesite="lax",
    )
    base_path = "/" + auth_service.n8n_base_path.strip("/")
    if base_path != "/":
        response.delete_cookie(
            auth_service.n8n_auth_cookie_name,
            path=base_path,
            secure=_request_is_secure(request),
            samesite="lax",
        )


def apply_n8n_session_cookies(
    response: Response,
    request: Request,
    auth_service: SupabaseAuthService,
    cookies: requests.cookies.RequestsCookieJar,
) -> None:
    default_path = "/" + auth_service.n8n_base_path.strip("/")
    if default_path == "//":
        default_path = "/"
    for cookie in cookies:
        cookie_path = cookie.path or default_path
        response.set_cookie(
            cookie.name,
            cookie.value,
            path=cookie_path,
            secure=_request_is_secure(request),
            httponly=True,
            samesite="lax",
        )


def require_authenticated_user(request: Request, auth_service: SupabaseAuthService) -> Optional[AuthUser]:
    access_token = extract_access_token(request)
    if not auth_service.auth_enabled:
        request.state.auth_user = None
        request.state.auth_access_token = access_token
        request.state.auth_step_up = None
        return None
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail=_auth_detail("auth_required", "Your AMOF session is missing or expired. Sign in again to continue."),
        )
    user = auth_service.verify_access_token(access_token)
    request.state.auth_user = user
    request.state.auth_access_token = access_token
    request.state.auth_step_up = None
    request.state.auth_audit = {
        "auth_path": "supabase_access_token",
        "principal_id": user.user_id,
        "principal_type": user.principal_type,
        "auth_source": user.auth_source,
        "step_up_policy": "user_step_up_required",
    }
    return user


def require_operator_user(
    request: Request,
    current_user: Optional[AuthUser],
    auth_service: SupabaseAuthService,
) -> Optional[AuthUser]:
    if not auth_service.auth_enabled:
        return current_user
    if current_user is None:
        raise HTTPException(
            status_code=401,
            detail=_auth_detail("auth_required", "Your AMOF session is missing or expired. Sign in again to continue."),
        )
    if not auth_service.is_operator_user(current_user):
        raise HTTPException(
            status_code=403,
            detail=_auth_detail("operator_required", "Operator authorization is required for this AMOF surface."),
        )
    request.state.auth_user = current_user
    return current_user


def require_step_up_user(
    request: Request,
    current_user: Optional[AuthUser],
    auth_service: SupabaseAuthService,
    step_up_store: StepUpStore,
) -> Optional[AuthUser]:
    if not auth_service.auth_enabled:
        return current_user
    operator_user = require_operator_user(request, current_user, auth_service)
    if operator_user is not None and operator_user.auth_source == "internal_control":
        if not bool(getattr(auth_service, "internal_control_allow_step_up_actions", False)):
            raise HTTPException(
                status_code=403,
                detail=_auth_detail(
                    "internal_control_step_up_denied",
                    "Internal control principal is not allowed to execute step-up-gated control actions.",
                ),
            )
        request.state.auth_step_up = {
            "active": True,
            "mode": "internal_control_policy",
            "policy": "allowed",
        }
        audit = dict(getattr(request.state, "auth_audit", {}) or {})
        audit["step_up_policy"] = "internal_allowed"
        request.state.auth_audit = audit
        return operator_user
    access_token = getattr(request.state, "auth_access_token", None) or extract_access_token(request)
    if not isinstance(access_token, str) or not access_token.strip() or operator_user is None:
        raise HTTPException(
            status_code=401,
            detail=_auth_detail("auth_required", "Your AMOF session is missing or expired. Sign in again to continue."),
        )
    status = step_up_store.status(access_token, operator_user.user_id)
    request.state.auth_step_up = status
    if not status:
        raise HTTPException(
            status_code=403,
            detail=_auth_detail(
                "step_up_required",
                "Re-enter your password before continuing with this AMOF operator action.",
            ),
        )
    return operator_user
