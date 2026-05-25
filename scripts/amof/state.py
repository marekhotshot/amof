"""Workspace state management (v3 - multi-ticket per ecosystem)."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .app_paths import ticket_worktrees_dir, workspace_state_file
from .manifest import resolve_workspace_root

LEGACY_STATE_DIR = Path(".amof")
LEGACY_STATE_FILE = LEGACY_STATE_DIR / "state.json"
STATE_DIR = workspace_state_file().parent
STATE_FILE = workspace_state_file()


def _resolved_state_dir() -> Path:
    if STATE_DIR.is_absolute():
        return STATE_DIR
    return resolve_workspace_root() / STATE_DIR


def _resolved_state_file() -> Path:
    if STATE_FILE.is_absolute():
        return STATE_FILE
    return resolve_workspace_root() / STATE_FILE


def _resolved_legacy_state_dir() -> Path:
    if LEGACY_STATE_DIR.is_absolute():
        return LEGACY_STATE_DIR
    return resolve_workspace_root() / LEGACY_STATE_DIR


def _resolved_legacy_state_file() -> Path:
    if LEGACY_STATE_FILE.is_absolute():
        return LEGACY_STATE_FILE
    return resolve_workspace_root() / LEGACY_STATE_FILE


def get_state() -> Dict[str, Any]:
    """Load AMOF workspace state from app-data, falling back to legacy workspace state."""
    for state_file in (_resolved_state_file(), _resolved_legacy_state_file()):
        if not state_file.exists():
            continue
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        version = state.get("version", 1)
        if version < 3:
            sys.stderr.write(f"[state] State version {version} is outdated (need v3).\n")
            sys.stderr.write("[state] Run 'amof -e <ecosystem> install' to create a fresh workspace.\n")
            return {}
        return state
    return {}


def save_state(state: Dict[str, Any]) -> None:
    """Save workspace state to AMOF app-data."""
    state_dir = _resolved_state_dir()
    state_file = _resolved_state_file()
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def create_workspace_state(
    ecosystem: str,
    workspace_branch: str,
    repos: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Create initial workspace state (version 3, multi-ticket)."""
    return {
        "version": 3,
        "ecosystem": ecosystem,
        "workspace_branch": workspace_branch,
        "active_ticket": None,
        "tickets": {},
        "repos": [
            {
                "name": r.get("name"),
                "url": r.get("url"),
                "path": r.get("path", f"repos/{r.get('name')}"),
                "branch": r.get("branch", "main"),
                "readonly": r.get("readonly", False),
                "enabled": True,
            }
            for r in repos
        ],
        "created_at": datetime.now().isoformat(),
        "last_modified": datetime.now().isoformat(),
    }


def update_state(**kwargs) -> None:
    """Update specific fields in workspace state."""
    state = get_state()
    state.update(kwargs)
    state["last_modified"] = datetime.now().isoformat()
    save_state(state)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _ticket_ecosystem(ticket: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> Optional[str]:
    payload = state or get_state()
    value = str(ticket.get("ecosystem") or payload.get("ecosystem") or "").strip()
    return value or None


def _normalize_repo_selections(repo_selections: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, str]]]:
    if not repo_selections:
        return None
    normalized: List[Dict[str, str]] = []
    for selection in repo_selections:
        if not isinstance(selection, dict):
            continue
        repo = str(selection.get("repo") or "").strip()
        mode = str(selection.get("mode") or "").strip() or "ticket_local"
        source_branch = str(selection.get("source_branch") or "").strip()
        target_branch = str(selection.get("target_branch") or "").strip()
        if not repo or not source_branch or not target_branch:
            continue
        normalized.append(
            {
                "repo": repo,
                "mode": "shared" if mode == "shared" else "ticket_local",
                "source_branch": source_branch,
                "target_branch": target_branch,
                "status": str(selection.get("status") or "ready").strip() or "ready",
            }
        )
    return normalized or None


def _normalize_plan_items(plan_items: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    if not plan_items:
        return {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for item in plan_items:
        if not isinstance(item, dict):
            continue
        plan_item_id = str(item.get("id") or "").strip()
        if not plan_item_id:
            continue
        expected_files = [
            str(path).strip()
            for path in item.get("expected_files", [])
            if str(path).strip()
        ]
        validation = [
            str(command).strip()
            for command in item.get("validation", [])
            if str(command).strip()
        ]
        status = str(item.get("status") or "pending").strip() or "pending"
        normalized[plan_item_id] = {
            "id": plan_item_id,
            "type": str(item.get("type") or "OTHER").strip() or "OTHER",
            "title": str(item.get("title") or "").strip(),
            "expected_files": expected_files,
            "validation": validation,
            "checkpoint_required": bool(item.get("checkpoint_required", False)),
            "status": status,
            "rationale": str(item.get("rationale") or "").strip() or None,
            "last_validation_receipt": item.get("last_validation_receipt"),
            "checkpoint_receipts": list(item.get("checkpoint_receipts") or []),
            "completed_at": item.get("completed_at"),
        }
    return normalized


def _default_ticket_receipts(
    *,
    preflight_receipt: Optional[Dict[str, Any]] = None,
    ticket_start_receipt: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "preflight_receipt": preflight_receipt,
        "ticket_start_receipt": ticket_start_receipt,
        "validation_receipts": [],
        "checkpoint_receipts": [],
        "readiness_receipt": None,
        "fresh_verify_receipt": None,
    }


def _ensure_ticket_receipts(ticket: Dict[str, Any]) -> Dict[str, Any]:
    receipts = ticket.get("receipts")
    if not isinstance(receipts, dict):
        receipts = _default_ticket_receipts()
        ticket["receipts"] = receipts
        return receipts
    receipts.setdefault("preflight_receipt", None)
    receipts.setdefault("ticket_start_receipt", None)
    receipts.setdefault("validation_receipts", [])
    receipts.setdefault("checkpoint_receipts", [])
    receipts.setdefault("readiness_receipt", None)
    receipts.setdefault("fresh_verify_receipt", None)
    return receipts


def _ensure_ticket_shape(ticket: Dict[str, Any]) -> Dict[str, Any]:
    ticket.setdefault("phase", "started")
    ticket.setdefault("plan_items", {})
    ticket["plan_items"] = _normalize_plan_items(list(ticket.get("plan_items", {}).values()) if isinstance(ticket.get("plan_items"), dict) else ticket.get("plan_items"))
    ticket.setdefault("planner_provenance", None)
    ticket.setdefault("dependency_ready", None)
    ticket.setdefault("deferred_reason", None)
    ticket.setdefault("killed_reason", None)
    ticket.setdefault("readiness", None)
    _ensure_ticket_receipts(ticket)
    return ticket


def get_ticket(ticket_id: str) -> Dict[str, Any]:
    state = get_state()
    ticket = dict(state.get("tickets", {}).get(ticket_id) or {})
    if not ticket:
        return {}
    return _ensure_ticket_shape(ticket)


def update_ticket(ticket_id: str, **updates: Any) -> None:
    state = get_state()
    tickets = state.get("tickets", {})
    ticket = dict(tickets.get(ticket_id) or {})
    if not ticket:
        return
    ticket.update(updates)
    ticket = _ensure_ticket_shape(ticket)
    tickets[ticket_id] = ticket
    state["tickets"] = tickets
    state["last_modified"] = _now_iso()
    save_state(state)


def record_ticket_receipt(ticket_id: str, receipt_kind: str, payload: Dict[str, Any]) -> None:
    state = get_state()
    tickets = state.get("tickets", {})
    ticket = dict(tickets.get(ticket_id) or {})
    if not ticket:
        return
    ticket = _ensure_ticket_shape(ticket)
    receipts = _ensure_ticket_receipts(ticket)
    if receipt_kind in {"preflight_receipt", "ticket_start_receipt", "readiness_receipt", "fresh_verify_receipt"}:
        receipts[receipt_kind] = payload
    elif receipt_kind in {"validation_receipts", "checkpoint_receipts"}:
        receipts[receipt_kind].append(payload)
    else:
        receipts[receipt_kind] = payload
    tickets[ticket_id] = ticket
    state["tickets"] = tickets
    state["last_modified"] = _now_iso()
    save_state(state)


def update_plan_item(ticket_id: str, plan_item_id: str, **updates: Any) -> None:
    state = get_state()
    tickets = state.get("tickets", {})
    ticket = dict(tickets.get(ticket_id) or {})
    if not ticket:
        return
    ticket = _ensure_ticket_shape(ticket)
    plan_items = dict(ticket.get("plan_items") or {})
    plan_item = dict(plan_items.get(plan_item_id) or {})
    if not plan_item:
        return
    plan_item.update(updates)
    if plan_item.get("status") in {"done", "deferred", "killed"} and not plan_item.get("completed_at"):
        plan_item["completed_at"] = _now_iso()
    plan_items[plan_item_id] = plan_item
    ticket["plan_items"] = plan_items
    tickets[ticket_id] = ticket
    state["tickets"] = tickets
    state["last_modified"] = _now_iso()
    save_state(state)


def set_ticket_phase(ticket_id: str, phase: str) -> None:
    update_ticket(ticket_id, phase=phase)


def add_ticket(
    ticket_id: str,
    repo_branches: Dict[str, str],
    ecosystem: Optional[str] = None,
    *,
    stage_id: Optional[str] = None,
    environment_id: Optional[str] = None,
    repo_selections: Optional[List[Dict[str, Any]]] = None,
    preflight_receipt: Optional[Dict[str, Any]] = None,
    ticket_start_receipt: Optional[Dict[str, Any]] = None,
    plan_items: Optional[List[Dict[str, Any]]] = None,
    planner_provenance: Optional[Dict[str, Any]] = None,
) -> None:
    """Add a ticket to state with its repo->branch mapping."""
    state = get_state()
    tickets = state.get("tickets", {})
    tickets[ticket_id] = {
        "created_at": _now_iso(),
        "repos": repo_branches,
        "worktree_base": str(ticket_worktrees_dir() / ticket_id),
        "ecosystem": ecosystem or state.get("ecosystem"),
        "stage_id": (str(stage_id).strip() or None) if stage_id is not None else None,
        "environment_id": (str(environment_id).strip() or None) if environment_id is not None else None,
        "repo_selections": _normalize_repo_selections(repo_selections),
        "phase": "started",
        "plan_items": _normalize_plan_items(plan_items),
        "planner_provenance": planner_provenance,
        "dependency_ready": None,
        "deferred_reason": None,
        "killed_reason": None,
        "readiness": None,
        "receipts": _default_ticket_receipts(
            preflight_receipt=preflight_receipt,
            ticket_start_receipt=ticket_start_receipt,
        ),
    }
    state["tickets"] = tickets
    state["active_ticket"] = ticket_id
    state["last_modified"] = _now_iso()
    save_state(state)


def remove_ticket(ticket_id: str) -> None:
    """Remove a ticket from state."""
    state = get_state()
    tickets = state.get("tickets", {})
    tickets.pop(ticket_id, None)
    if state.get("active_ticket") == ticket_id:
        remaining = list(tickets.keys())
        state["active_ticket"] = remaining[0] if remaining else None
    state["tickets"] = tickets
    state["last_modified"] = datetime.now().isoformat()
    save_state(state)


def get_ticket_repos(ticket_id: str) -> Dict[str, str]:
    """Get repo->branch mapping for a ticket."""
    state = get_state()
    ticket = state.get("tickets", {}).get(ticket_id, {})
    return ticket.get("repos", {})


def get_active_ticket(ecosystem: Optional[str] = None) -> Optional[str]:
    """Get the currently active ticket ID, or None."""
    state = get_state()
    active_ticket = state.get("active_ticket")
    if not ecosystem or not active_ticket:
        return active_ticket
    active_info = state.get("tickets", {}).get(active_ticket, {})
    return active_ticket if _ticket_ecosystem(active_info, state) == ecosystem else None


def set_active_ticket(ticket_id: Optional[str]) -> None:
    """Set the active ticket."""
    state = get_state()
    state["active_ticket"] = ticket_id
    state["last_modified"] = datetime.now().isoformat()
    save_state(state)


def get_all_tickets(ecosystem: Optional[str] = None) -> Dict[str, Any]:
    """Get all tracked tickets."""
    state = get_state()
    tickets = state.get("tickets", {})
    if not ecosystem:
        return {ticket_id: _ensure_ticket_shape(dict(info or {})) for ticket_id, info in tickets.items()}
    return {
        ticket_id: _ensure_ticket_shape(dict(info or {}))
        for ticket_id, info in tickets.items()
        if _ticket_ecosystem(info or {}, state) == ecosystem
    }


def get_workspace_info() -> Optional[Dict[str, Any]]:
    """Get workspace info if in a workspace."""
    state = get_state()
    if not state:
        return None

    tickets = state.get("tickets", {})
    return {
        "ecosystem": state.get("ecosystem"),
        "workspace_branch": state.get("workspace_branch"),
        "active_ticket": state.get("active_ticket"),
        "ticket_count": len(tickets),
        "tickets": list(tickets.keys()),
        "created_at": state.get("created_at"),
        "repos": len(state.get("repos", [])),
    }


def is_in_workspace() -> bool:
    """Check if current cwd resolves to an AMOF workspace with usable state."""
    workspace_root = resolve_workspace_root()
    if not (workspace_root / "ecosystems").exists():
        return False
    state = get_state()
    return state.get("version", 0) >= 3


def get_workspace_repos() -> List[Dict[str, Any]]:
    """Get repos from workspace state, or empty if not in workspace."""
    state = get_state()
    return state.get("repos", [])


def get_effective_repos(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Get repos to use - merge workspace state with manifest.
    
    State repos take priority (they have push tracking etc.), but repos
    added to ecosystem.yaml after the initial install are included too.
    This prevents the "missing repos" problem when the manifest evolves.
    """
    manifest_repos = [r for r in manifest.get("repos", []) if r.get("enabled", True)]

    if is_in_workspace():
        workspace_repos = get_workspace_repos()
        if workspace_repos:
            # Build lookup of state repos by name
            state_by_name = {r.get("name"): r for r in workspace_repos}
            
            # Merge: state repos first (preserves push tracking), then
            # append any manifest repos not yet in state
            merged = list(workspace_repos)
            for repo in manifest_repos:
                if repo.get("name") not in state_by_name:
                    merged.append(repo)
            return merged

    return manifest_repos


def update_repo_commit(repo_name: str, branch: str, commit: str, commit_full: str) -> None:
    """Update commit hash for a specific repo in state.json."""
    state = get_state()
    repos = state.get("repos", [])

    for repo in repos:
        if repo.get("name") == repo_name:
            repo["last_push"] = {
                "branch": branch,
                "commit": commit,
                "commit_full": commit_full,
                "pushed_at": datetime.now().isoformat(),
            }
            break

    state["repos"] = repos
    state["last_modified"] = datetime.now().isoformat()
    save_state(state)


def get_repo_commits() -> Dict[str, Dict[str, str]]:
    """Get last pushed commits for all repos."""
    state = get_state()
    result = {}

    for repo in state.get("repos", []):
        name = repo.get("name")
        if name and repo.get("last_push"):
            result[name] = repo["last_push"]

    return result
