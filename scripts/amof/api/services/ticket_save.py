from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException

from amof.api.command_builder import get_workspace_root
from amof.api.run_manager import RUN_STATUS_FAILED, RUN_STATUS_RUNNING, RUN_STATUS_SUCCESS, RunManager
from amof.manifest import get_ecosystem_root, load_manifest
from amof.state import get_all_tickets, get_effective_repos
from amof.utils import get_git_branch, get_git_commit, get_git_commit_full, is_git_dirty, run_command


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    normalized = normalized.strip("-")
    return normalized or "ticket"


def _repo_path(workspace_root: Path, repo_entry: Dict[str, Any]) -> Path:
    raw = Path(str(repo_entry.get("path") or f"repos/{repo_entry.get('name')}"))
    return raw if raw.is_absolute() else workspace_root / raw


def _list_dirty_files(repo_path: Path) -> list[str]:
    code, out = run_command(
        ["git", "-c", f"safe.directory={repo_path.resolve()}", "status", "--porcelain", "--", ":(exclude).amof"],
        cwd=repo_path,
    )
    if code != 0 or not out:
        return []
    dirty_files: list[str] = []
    for line in out.splitlines():
        entry = line[3:].strip()
        if entry:
            dirty_files.append(entry)
    return dirty_files


def _ticket_info(ecosystem: str, ticket_id: str) -> Dict[str, Any]:
    if ticket_id == "main":
        raise HTTPException(status_code=400, detail="Main is not a ticketable save target.")
    ticket = get_all_tickets(ecosystem=ecosystem).get(ticket_id)
    if not isinstance(ticket, dict):
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found in {ecosystem}.")
    return ticket


def _resolve_control_repo(ticket: Dict[str, Any]) -> Optional[str]:
    repo_selections = ticket.get("repo_selections") if isinstance(ticket.get("repo_selections"), list) else []
    for selection in repo_selections:
        if not isinstance(selection, dict):
            continue
        if str(selection.get("mode") or "") == "shared":
            repo_name = str(selection.get("repo") or "").strip()
            if repo_name:
                return repo_name
    repos = ticket.get("repos") if isinstance(ticket.get("repos"), dict) else {}
    if len(repos) == 1:
        return next(iter(repos.keys()))
    if len(repo_selections) == 1 and isinstance(repo_selections[0], dict):
        repo_name = str(repo_selections[0].get("repo") or "").strip()
        return repo_name or None
    return None


def _build_repo_states(workspace_root: Path, ecosystem: str, ticket: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    manifest = load_manifest(ecosystem)
    repos = get_effective_repos(manifest)
    repo_by_name = {
        str(repo.get("name") or "").strip(): repo
        for repo in repos
        if str(repo.get("name") or "").strip()
    }
    ticket_repos = ticket.get("repos") if isinstance(ticket.get("repos"), dict) else {}
    selection_by_repo = {
        str(entry.get("repo") or "").strip(): entry
        for entry in (ticket.get("repo_selections") if isinstance(ticket.get("repo_selections"), list) else [])
        if isinstance(entry, dict) and str(entry.get("repo") or "").strip()
    }

    repo_states: Dict[str, Dict[str, Any]] = {}
    for repo_name, ticket_branch in sorted(ticket_repos.items()):
        repo_entry = dict(repo_by_name.get(repo_name) or {"name": repo_name, "path": f"repos/{repo_name}"})
        repo_path = _repo_path(workspace_root, repo_entry)
        exists = repo_path.exists()
        dirty_files = _list_dirty_files(repo_path) if exists else []
        selection = selection_by_repo.get(repo_name) or {}
        repo_states[repo_name] = {
            "name": repo_name,
            "path": str(repo_path.relative_to(workspace_root) if repo_path.is_relative_to(workspace_root) else repo_path),
            "branch": get_git_branch(repo_path) if exists else None,
            "commit": get_git_commit(repo_path) if exists else None,
            "commit_full": get_git_commit_full(repo_path) if exists else None,
            "dirty": bool(dirty_files) if exists else False,
            "dirty_summary": (
                f"{len(dirty_files)} dirty file{'s' if len(dirty_files) != 1 else ''}"
                if dirty_files
                else ("clean" if exists else "missing")
            ),
            "dirty_file_count": len(dirty_files),
            "dirty_files": dirty_files,
            "readonly": bool(repo_entry.get("readonly", False)),
            "exists": exists,
            "mode": str(selection.get("mode") or "").strip() or None,
            "source_branch": str(selection.get("source_branch") or "").strip() or None,
            "target_branch": str(selection.get("target_branch") or ticket_branch or "").strip() or None,
            "selection_status": str(selection.get("status") or "").strip() or None,
        }
    return repo_states


def _audit_dir(workspace_root: Path, ecosystem: str) -> Path:
    return get_ecosystem_root(ecosystem, base=str(workspace_root)) / "audit"


def _latest_ticket_save_record(workspace_root: Path, ecosystem: str, ticket_id: str) -> Optional[Dict[str, Any]]:
    audit_dir = _audit_dir(workspace_root, ecosystem)
    if not audit_dir.exists():
        return None
    candidates = sorted(audit_dir.glob("*-ticket-save-*.json"), reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (
            isinstance(payload, dict)
            and payload.get("record_type") == "ticket_save"
            and payload.get("ecosystem") == ecosystem
            and payload.get("ticket_id") == ticket_id
        ):
            return payload
    return None


def _build_tag(ticket_id: str, timestamp: datetime) -> str:
    return f"{_slugify(ticket_id)}-snapshot-{timestamp.strftime('%Y%m%d-%H%M%S')}"


def _changed_repos(previous: Optional[Dict[str, Any]], repo_states: Dict[str, Dict[str, Any]]) -> list[str]:
    if not isinstance(previous, dict):
        return []
    previous_states = previous.get("repo_states") if isinstance(previous.get("repo_states"), dict) else {}
    changed: list[str] = []
    for repo_name, state in repo_states.items():
        previous_state = previous_states.get(repo_name) if isinstance(previous_states, dict) else None
        if not isinstance(previous_state, dict):
            changed.append(repo_name)
            continue
        comparable = (
            previous_state.get("branch"),
            previous_state.get("commit_full"),
            bool(previous_state.get("dirty")),
            previous_state.get("dirty_file_count"),
        )
        current = (
            state.get("branch"),
            state.get("commit_full"),
            bool(state.get("dirty")),
            state.get("dirty_file_count"),
        )
        if comparable != current:
            changed.append(repo_name)
    return changed


def build_ticket_save_options(ecosystem: str, ticket_id: str) -> Dict[str, Any]:
    workspace_root = get_workspace_root()
    ticket = _ticket_info(ecosystem, ticket_id)
    repo_states = _build_repo_states(workspace_root, ecosystem, ticket)
    latest_record = _latest_ticket_save_record(workspace_root, ecosystem, ticket_id)
    control_repo = _resolve_control_repo(ticket)
    now = datetime.now(timezone.utc)
    notices: list[Dict[str, Any]] = []

    if latest_record is None:
        notices.append(
            {
                "code": "no_previous_save_tag",
                "level": "info",
                "message": "No previous ecosystem save tag was found for this ticket yet.",
                "blocking": False,
            }
        )
    if control_repo is None:
        notices.append(
            {
                "code": "control_repo_unresolved",
                "level": "warning",
                "message": "Control repo could not be resolved from the tracked ticket metadata. The snapshot can still capture current repo truth.",
                "blocking": False,
            }
        )
    if any(bool(state.get("dirty")) for state in repo_states.values()):
        notices.append(
            {
                "code": "uncommitted_changes_present",
                "level": "warning",
                "message": "Uncommitted changes are present. The snapshot will record dirty-file evidence without forcing micro-commits.",
                "blocking": False,
            }
        )

    tag = _build_tag(ticket_id, now)
    return {
        "ecosystem": ecosystem,
        "ticket_id": ticket_id,
        "control_repo": control_repo,
        "control_repo_status": "resolved" if control_repo else "unresolved",
        "current_tag": latest_record.get("tag") if latest_record else None,
        "current_version": latest_record.get("version") if latest_record else None,
        "options": [
            {
                "id": "workspace_snapshot",
                "label": "Workspace snapshot",
                "description": "Record branch, commit, and dirty-file evidence for the tracked ticket without forcing commit, push, or tag churn.",
                "version": "workspace_snapshot",
                "tag": tag,
            }
        ],
        "notices": notices,
        "repo_states": repo_states,
    }


def perform_ticket_save(ecosystem: str, ticket_id: str, option_id: str, expected_current_tag: Optional[str]) -> Dict[str, Any]:
    options = build_ticket_save_options(ecosystem, ticket_id)
    selected_option = next((entry for entry in options["options"] if entry["id"] == option_id), None)
    if selected_option is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_ticket_save_option", "message": f"Unsupported save strategy: {option_id}"},
        )
    current_tag = options.get("current_tag")
    if (expected_current_tag or None) != (current_tag or None):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ticket_save_conflict",
                "message": "Capture options are stale. Reload the modal and try again.",
                "current_tag": current_tag,
            },
        )

    workspace_root = get_workspace_root()
    latest_record = _latest_ticket_save_record(workspace_root, ecosystem, ticket_id)
    repo_states = options["repo_states"]
    changed_repos = _changed_repos(latest_record, repo_states)
    timestamp = datetime.now(timezone.utc)
    audit_dir = _audit_dir(workspace_root, ecosystem)
    audit_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{timestamp.strftime('%Y-%m-%d-%H%M%S')}-ticket-save-{_slugify(ticket_id)}.json"
    record_path = audit_dir / filename
    record = {
        "record_type": "ticket_save",
        "saved_at": timestamp.isoformat(),
        "ecosystem": ecosystem,
        "ticket_id": ticket_id,
        "tag": selected_option["tag"],
        "version": selected_option["version"],
        "save_kind": selected_option["id"],
        "control_repo": options.get("control_repo"),
        "control_repo_status": options.get("control_repo_status"),
        "previous_tag": current_tag,
        "changed_repos": changed_repos,
        "repo_states": repo_states,
        "notices": options.get("notices") or [],
    }
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return {
        "tag": selected_option["tag"],
        "version": selected_option["version"],
        "save_kind": selected_option["id"],
        "control_repo": options.get("control_repo"),
        "record_path": str(record_path.relative_to(workspace_root)),
        "changed_repos": changed_repos,
        "repo_states": {
            name: {
                "branch": state.get("branch"),
                "commit": state.get("commit"),
                "commit_full": state.get("commit_full"),
            }
            for name, state in repo_states.items()
        },
    }


def run_ticket_save(
    run_manager: RunManager,
    run_id: str,
    ecosystem: str,
    ticket_id: str,
    option_id: str,
    expected_current_tag: Optional[str],
) -> None:
    run_manager.update_status(run_id, RUN_STATUS_RUNNING)
    run_manager.append_event(
        run_id,
        level="info",
        type="ticket_save_started",
        message=f"Saving ticket context for {ticket_id}.",
        payload={"ticket_id": ticket_id, "option_id": option_id},
    )
    try:
        result = perform_ticket_save(ecosystem, ticket_id, option_id, expected_current_tag)
        run_manager.append_event(
            run_id,
            level="info",
            type="ticket_save_result",
            message=f"Saved {ticket_id} as {result['tag']}.",
            payload=result,
        )
        run_manager.update_status(run_id, RUN_STATUS_SUCCESS, exit_code=0)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        run_manager.append_event(
            run_id,
            level="error",
            type="ticket_save_error",
            message=str(detail.get("message") or detail.get("code") or "Save failed."),
            payload=detail,
        )
        run_manager.update_status(run_id, RUN_STATUS_FAILED, exit_code=1)
    except Exception as exc:  # pragma: no cover - defensive fallback
        run_manager.append_event(
            run_id,
            level="error",
            type="ticket_save_error",
            message=str(exc),
            payload={"message": str(exc)},
        )
        run_manager.update_status(run_id, RUN_STATUS_FAILED, exit_code=1)
