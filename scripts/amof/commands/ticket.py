"""Ticket commands - manage ticket feature branches within a workspace using git worktrees."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..manifest import resolve_workspace_root
from ..state import (
    add_ticket,
    get_active_ticket,
    get_all_tickets,
    get_effective_repos,
    get_state,
    get_ticket,
    is_in_workspace,
    record_ticket_receipt,
    remove_ticket,
    set_active_ticket,
    set_ticket_phase,
    update_plan_item,
    update_ticket,
)
from ..utils import get_git_branch, is_git_dirty, normalize_branch_prefix, run_command
from ..worktree_manager import (
    archive_ticket_worktrees,
    get_ticket_repo_worktree_path,
    switch_to_ticket,
    update_ide_workspace,
)

_PUBLIC_BUILD_WRITE_REMOVED_DETAIL = (
    "[ticket] build-write was removed from public AMOF canonical main. "
    "Public AMOF keeps install/bootstrap/contracts only; runtime build-write "
    "flows belong to the private operating surface.\n"
)

CANONICAL_AMOF_REPO_NAME = "amof"
CANONICAL_AMOF_REMOTE = "https://github.com/marekhotshot/amof.git"
BLOCKED_REPO_NAMES = {"amof-oss"}
TERMINAL_PLAN_ITEM_STATES = {"done", "deferred", "killed"}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _normalize_remote_url(url: str) -> str:
    value = str(url or "").strip()
    if value.endswith(".git"):
        value = value[:-4]
    return value.rstrip("/")


def _repo_path(repo: Dict[str, Any], workspace_root: Path) -> Path:
    raw = Path(str(repo.get("path") or f"repos/{repo.get('name')}"))
    if raw.is_absolute():
        return raw
    return (workspace_root / raw).resolve(strict=False)


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root))
    except ValueError:
        return str(path)


def _git(repo_path: Path, *args: str) -> tuple[int, str]:
    return run_command(["git", "-C", str(repo_path), *args], cwd=repo_path)


def _parse_repo_selections(repo_selections_json: Optional[str]) -> tuple[list[dict[str, Any]], Optional[str]]:
    if not repo_selections_json:
        return [], None
    try:
        payload = json.loads(repo_selections_json)
    except json.JSONDecodeError as exc:
        return [], f"[ticket] Invalid --repo-selections payload: {exc}\n"
    if not isinstance(payload, list):
        return [], "[ticket] --repo-selections must be a JSON list.\n"
    parsed_repo_selections: List[Dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        repo_name = str(entry.get("repo") or "").strip()
        source_branch = str(entry.get("source_branch") or "").strip()
        target_branch = str(entry.get("target_branch") or "").strip()
        mode = str(entry.get("mode") or "ticket_local").strip() or "ticket_local"
        if not repo_name or not source_branch or not target_branch:
            continue
        parsed_repo_selections.append(
            {
                "repo": repo_name,
                "mode": "shared" if mode == "shared" else "ticket_local",
                "source_branch": source_branch,
                "target_branch": target_branch,
                "status": str(entry.get("status") or "ready").strip() or "ready",
            }
        )
    if not parsed_repo_selections:
        return [], "[ticket] --repo-selections did not contain any usable repo contracts.\n"
    return parsed_repo_selections, None


def _select_ticket_repos(
    manifest: Dict[str, Any],
    workspace_root: Path,
    repos_filter: Optional[str] = None,
    repo_selections_json: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[str]]:
    repos = get_effective_repos(manifest)
    parsed_repo_selections, parse_error = _parse_repo_selections(repo_selections_json)
    if parse_error:
        return [], [], parse_error
    selected_repo_names = {entry["repo"] for entry in parsed_repo_selections} or None
    if repos_filter:
        allowed = {name.strip() for name in repos_filter.split(",") if name.strip()}
        repos = [repo for repo in repos if str(repo.get("name") or "").strip() in allowed]
        if not repos:
            return [], [], f"[ticket] No matching repos found for: {repos_filter}\n"
    elif selected_repo_names is not None:
        repos = [repo for repo in repos if str(repo.get("name") or "").strip() in selected_repo_names]
        if not repos:
            return [], [], "[ticket] No writable repos matched the provided repo selections.\n"
    for repo in repos:
        repo["resolved_path"] = str(_repo_path(repo, workspace_root))
    return repos, parsed_repo_selections, None


def _plan_items_payload(plan_items_json: Optional[str], plan_items_file: Optional[str]) -> tuple[list[dict[str, Any]], Optional[str]]:
    raw_payload = plan_items_json
    if plan_items_file:
        try:
            raw_payload = Path(plan_items_file).read_text(encoding="utf-8")
        except OSError as exc:
            return [], f"[ticket] Failed to read --plan-items-file: {exc}\n"
    if not raw_payload:
        return [], "[ticket] ticket start requires --plan-items-json or --plan-items-file.\n"
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        return [], f"[ticket] Invalid plan items payload: {exc}\n"
    if isinstance(payload, dict):
        values: list[dict[str, Any]] = []
        for plan_item_id, item in payload.items():
            if not isinstance(item, dict):
                continue
            enriched = dict(item)
            enriched.setdefault("id", str(plan_item_id))
            values.append(enriched)
        payload = values
    if not isinstance(payload, list):
        return [], "[ticket] plan items payload must be a JSON list or object.\n"
    plan_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            return [], "[ticket] each plan item must be a JSON object.\n"
        plan_item_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        expected_files = [str(path).strip() for path in item.get("expected_files", []) if str(path).strip()]
        validation = [str(command).strip() for command in item.get("validation", []) if str(command).strip()]
        if not plan_item_id:
            return [], "[ticket] each plan item requires a non-empty id.\n"
        if plan_item_id in seen_ids:
            return [], f"[ticket] duplicate plan item id: {plan_item_id}\n"
        if not title:
            return [], f"[ticket] plan item {plan_item_id} requires a title.\n"
        if not expected_files:
            return [], f"[ticket] plan item {plan_item_id} requires expected_files.\n"
        if not validation:
            return [], f"[ticket] plan item {plan_item_id} requires validation commands.\n"
        plan_items.append(
            {
                "id": plan_item_id,
                "type": str(item.get("type") or "OTHER").strip() or "OTHER",
                "title": title,
                "expected_files": expected_files,
                "validation": validation,
                "checkpoint_required": bool(item.get("checkpoint_required", False)),
                "status": str(item.get("status") or "pending").strip() or "pending",
                "rationale": str(item.get("rationale") or "").strip() or None,
            }
        )
        seen_ids.add(plan_item_id)
    if not plan_items:
        return [], "[ticket] no usable plan items were provided.\n"
    return plan_items, None


def _planner_provenance(profile: Optional[str], model: Optional[str]) -> Optional[Dict[str, Any]]:
    profile_name = str(profile or "").strip()
    resolved_model = str(model or "").strip()
    if not profile_name and not resolved_model:
        return None
    return {
        "recorded_at": _now_iso(),
        "profile_name": profile_name or None,
        "resolved_model": resolved_model or None,
    }


def _is_canonical_amof_repo(repo_name: str, remote_url: str) -> bool:
    if repo_name != CANONICAL_AMOF_REPO_NAME:
        return False
    return _normalize_remote_url(remote_url) == _normalize_remote_url(CANONICAL_AMOF_REMOTE)


def _run_ticket_preflight(
    manifest: Dict[str, Any],
    ticket_id: str,
    *,
    repos_filter: Optional[str] = None,
    repo_selections_json: Optional[str] = None,
) -> tuple[bool, Dict[str, Any], list[dict[str, Any]]]:
    workspace_root = Path.cwd()
    repos, parsed_repo_selections, select_error = _select_ticket_repos(
        manifest,
        workspace_root,
        repos_filter=repos_filter,
        repo_selections_json=repo_selections_json,
    )
    receipt: Dict[str, Any] = {
        "receipt_kind": "preflight_receipt",
        "recorded_at": _now_iso(),
        "ticket_id": ticket_id,
        "allowed": False,
        "repo_checks": [],
        "blocking_issues": [],
    }
    if select_error:
        receipt["blocking_issues"].append(select_error.strip())
        return False, receipt, []
    for repo in repos:
        repo_name = str(repo.get("name") or "").strip()
        readonly = bool(repo.get("readonly", False))
        repo_path = Path(str(repo["resolved_path"]))
        repo_check: Dict[str, Any] = {
            "repo": repo_name,
            "path": str(repo_path),
            "readonly": readonly,
            "status": "ok",
            "issues": [],
            "current_branch": None,
            "origin_url": None,
            "origin_main_resolved": False,
            "is_dirty": False,
            "canonical_remote": None,
        }
        if repo_name in BLOCKED_REPO_NAMES:
            repo_check["status"] = "blocked"
            repo_check["issues"].append(f"blocked repo id: {repo_name}")
        elif readonly:
            repo_check["status"] = "skipped"
            repo_check["issues"].append("readonly repo skipped for ticket delivery")
        elif not repo_path.exists():
            repo_check["status"] = "blocked"
            repo_check["issues"].append("repo path does not exist")
        else:
            repo_check["current_branch"] = get_git_branch(repo_path)
            repo_check["is_dirty"] = is_git_dirty(repo_path)
            remote_code, remote_out = _git(repo_path, "remote", "get-url", "origin")
            if remote_code != 0 or not remote_out:
                repo_check["status"] = "blocked"
                repo_check["issues"].append("failed to resolve remote.origin.url")
            else:
                repo_check["origin_url"] = remote_out
                repo_check["canonical_remote"] = _is_canonical_amof_repo(repo_name, remote_out)
                if repo_name == CANONICAL_AMOF_REPO_NAME and not repo_check["canonical_remote"]:
                    repo_check["status"] = "blocked"
                    repo_check["issues"].append(
                        f"{repo_name} must resolve to {CANONICAL_AMOF_REMOTE}"
                    )
            origin_code, _ = _git(repo_path, "rev-parse", "--verify", "origin/main")
            if origin_code == 0:
                repo_check["origin_main_resolved"] = True
            else:
                repo_check["status"] = "blocked"
                repo_check["issues"].append("origin/main is not resolvable")
            if repo_check["is_dirty"]:
                repo_check["status"] = "blocked"
                repo_check["issues"].append("repo has unrelated local changes; use a clean canonical checkout or worktree")
        if repo_check["status"] in {"blocked"}:
            receipt["blocking_issues"].extend(
                [f"{repo_name}: {issue}" for issue in repo_check["issues"]]
            )
        receipt["repo_checks"].append(repo_check)
    receipt["allowed"] = not receipt["blocking_issues"]
    if not receipt["allowed"]:
        return False, receipt, parsed_repo_selections
    return True, receipt, parsed_repo_selections


def _print_preflight(receipt: Dict[str, Any]) -> None:
    print("[ticket] Preflight")
    print(f"  Ticket: {receipt.get('ticket_id')}")
    print(f"  Allowed: {'yes' if receipt.get('allowed') else 'no'}")
    for repo_check in receipt.get("repo_checks", []):
        issues = repo_check.get("issues") or []
        print(f"  Repo: {repo_check.get('repo')} [{repo_check.get('status')}]")
        print(f"    Path: {repo_check.get('path')}")
        if repo_check.get("origin_url"):
            print(f"    Origin: {repo_check.get('origin_url')}")
        if repo_check.get("current_branch"):
            print(f"    Branch: {repo_check.get('current_branch')}")
        print(f"    origin/main: {'yes' if repo_check.get('origin_main_resolved') else 'no'}")
        print(f"    Dirty: {'yes' if repo_check.get('is_dirty') else 'no'}")
        if issues:
            for issue in issues:
                print(f"    Issue: {issue}")


def cmd_ticket_preflight(manifest: Dict[str, Any], args: Any) -> int:
    if not is_in_workspace():
        sys.stderr.write("[ticket] Not in a workspace.\n")
        return 1
    allowed, receipt, _ = _run_ticket_preflight(
        manifest,
        args.ticket_id,
        repos_filter=getattr(args, "repos", None),
        repo_selections_json=getattr(args, "repo_selections", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(receipt, indent=2))
    else:
        _print_preflight(receipt)
    return 0 if allowed else 1


def _build_readiness(ticket: Dict[str, Any], workspace_root: Path) -> Dict[str, Any]:
    plan_items = dict(ticket.get("plan_items") or {})
    receipts = dict(ticket.get("receipts") or {})
    reasons: list[str] = []
    for plan_item_id, item in sorted(plan_items.items()):
        status = str(item.get("status") or "pending")
        if status not in TERMINAL_PLAN_ITEM_STATES:
            reasons.append(f"{plan_item_id} is not closed")
        if status == "done":
            validation_receipt = item.get("last_validation_receipt")
            if not validation_receipt or not validation_receipt.get("passed"):
                reasons.append(f"{plan_item_id} is done without a passing validation receipt")
            if item.get("checkpoint_required") and not item.get("checkpoint_receipts"):
                reasons.append(f"{plan_item_id} requires a checkpoint receipt")
    for repo_name, branch in sorted((ticket.get("repos") or {}).items()):
        wt_path = get_ticket_repo_worktree_path(workspace_root, str(ticket.get("id") or ""), repo_name)
        if not wt_path.exists():
            reasons.append(f"{repo_name} worktree is missing")
            continue
        if is_git_dirty(wt_path):
            reasons.append(f"{repo_name} worktree is dirty")
    return {
        "ready": not reasons,
        "reasons": reasons,
        "last_readiness_receipt": receipts.get("readiness_receipt"),
    }


def _status_payload(ticket_id: str, ticket: Dict[str, Any], workspace_root: Path) -> Dict[str, Any]:
    plan_items_payload = []
    for plan_item_id, item in sorted((ticket.get("plan_items") or {}).items()):
        plan_items_payload.append(
            {
                "id": plan_item_id,
                "type": item.get("type"),
                "title": item.get("title"),
                "status": item.get("status"),
                "checkpoint_required": bool(item.get("checkpoint_required")),
                "expected_files": item.get("expected_files") or [],
                "last_validation_receipt": item.get("last_validation_receipt"),
            }
        )
    repos_payload = []
    for repo_name, branch in sorted((ticket.get("repos") or {}).items()):
        wt_path = get_ticket_repo_worktree_path(workspace_root, ticket_id, repo_name)
        repos_payload.append(
            {
                "repo": repo_name,
                "branch": branch,
                "worktree_path": str(wt_path),
                "exists": wt_path.exists(),
                "dirty": wt_path.exists() and is_git_dirty(wt_path),
            }
        )
    readiness = _build_readiness({**ticket, "id": ticket_id}, workspace_root)
    return {
        "ticket_id": ticket_id,
        "phase": ticket.get("phase"),
        "planner_provenance": ticket.get("planner_provenance"),
        "preflight_receipt": (ticket.get("receipts") or {}).get("preflight_receipt"),
        "repos": repos_payload,
        "plan_items": plan_items_payload,
        "checkpoint_allowed_now": False,
        "promote_main_ready": readiness["ready"],
        "readiness_reasons": readiness["reasons"],
    }


def cmd_ticket_status(manifest: Dict[str, Any], args: Any) -> int:
    del manifest
    if not is_in_workspace():
        sys.stderr.write("[ticket] Not in a workspace.\n")
        return 1
    ticket_id = getattr(args, "ticket_id", None) or get_active_ticket()
    if not ticket_id:
        sys.stderr.write("[ticket] No active ticket.\n")
        return 1
    ticket = get_ticket(ticket_id)
    if not ticket:
        sys.stderr.write(f"[ticket] Ticket {ticket_id} not found.\n")
        return 1
    payload = _status_payload(ticket_id, ticket, Path.cwd())
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0
    print(f"[ticket] Status {ticket_id}")
    print(f"  Phase: {payload['phase']}")
    provenance = payload.get("planner_provenance") or {}
    if provenance.get("profile_name") or provenance.get("resolved_model"):
        print("  Planner provenance:")
        if provenance.get("profile_name"):
            print(f"    Profile: {provenance['profile_name']}")
        if provenance.get("resolved_model"):
            print(f"    Model: {provenance['resolved_model']}")
    preflight_receipt = payload.get("preflight_receipt") or {}
    if preflight_receipt:
        print(f"  Preflight: {'pass' if preflight_receipt.get('allowed') else 'blocked'}")
    print("  Repos:")
    for repo in payload["repos"]:
        dirty_suffix = " dirty" if repo["dirty"] else ""
        missing_suffix = " missing" if not repo["exists"] else ""
        print(f"    {repo['repo']}: {repo['branch']}{dirty_suffix}{missing_suffix}")
    print("  PlanItems:")
    for item in payload["plan_items"]:
        print(f"    {item['id']}: {item['status']} - {item['title']}")
    print(f"  Checkpoint allowed now: {'yes' if payload['checkpoint_allowed_now'] else 'no'}")
    print(f"  Ready for promote-main: {'yes' if payload['promote_main_ready'] else 'no'}")
    for reason in payload["readiness_reasons"]:
        print(f"    Reason: {reason}")
    return 0


def _ticket_dirty_files(repo_path: Path) -> set[str]:
    dirty_files: set[str] = set()
    for args in (
        ("diff", "--name-only"),
        ("diff", "--cached", "--name-only"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        code, out = _git(repo_path, *args)
        if code != 0 or not out:
            continue
        dirty_files.update(line.strip() for line in out.splitlines() if line.strip())
    return dirty_files


def _normalize_selected_files(repo_path: Path, files: list[str]) -> tuple[list[str], Optional[str]]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in files:
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve(strict=False).relative_to(repo_path.resolve(strict=False))
            except ValueError:
                return [], f"[ticket] file path escapes repo scope: {raw}\n"
        relative = candidate.as_posix()
        if relative.startswith("../"):
            return [], f"[ticket] file path escapes repo scope: {raw}\n"
        if relative not in seen:
            normalized.append(relative)
            seen.add(relative)
    if not normalized:
        return [], "[ticket] checkpoint requires one or more --file entries.\n"
    return normalized, None


def _run_validation_commands(repo_path: Path, commands: list[str]) -> Dict[str, Any]:
    results: list[dict[str, Any]] = []
    all_passed = True
    for command in commands:
        argv = shlex.split(command)
        try:
            completed = subprocess.run(
                argv,
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            results.append(
                {
                    "command": command,
                    "exit_code": 127,
                    "passed": False,
                    "stdout": "",
                    "stderr": str(exc),
                }
            )
            all_passed = False
            continue
        passed = completed.returncode == 0
        results.append(
            {
                "command": command,
                "exit_code": completed.returncode,
                "passed": passed,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
        if not passed:
            all_passed = False
    return {"passed": all_passed, "commands": results}


def cmd_ticket_checkpoint(manifest: Dict[str, Any], args: Any) -> int:
    del manifest
    if not is_in_workspace():
        sys.stderr.write("[ticket] Not in a workspace.\n")
        return 1
    ticket_id = getattr(args, "ticket_id", None) or get_active_ticket()
    if not ticket_id:
        sys.stderr.write("[ticket] checkpoint requires a ticket id or active ticket.\n")
        return 1
    ticket = get_ticket(ticket_id)
    if not ticket:
        sys.stderr.write(f"[ticket] Ticket {ticket_id} not found.\n")
        return 1
    repo_name = str(getattr(args, "repo", "") or "").strip()
    if not repo_name:
        sys.stderr.write("[ticket] checkpoint requires --repo.\n")
        return 1
    target_branch = str((ticket.get("repos") or {}).get(repo_name) or "").strip()
    if not target_branch:
        sys.stderr.write(f"[ticket] Ticket {ticket_id} has no tracked repo named {repo_name}.\n")
        return 1
    worktree_path = get_ticket_repo_worktree_path(Path.cwd(), ticket_id, repo_name)
    if not worktree_path.exists():
        sys.stderr.write(f"[ticket] Ticket worktree does not exist: {worktree_path}\n")
        return 1
    plan_item_ids = [str(value).strip() for value in getattr(args, "plan_item_ids", []) if str(value).strip()]
    if not plan_item_ids:
        sys.stderr.write("[ticket] checkpoint requires one or more --plan-item entries.\n")
        return 1
    plan_items = dict(ticket.get("plan_items") or {})
    missing_plan_items = [plan_item_id for plan_item_id in plan_item_ids if plan_item_id not in plan_items]
    if missing_plan_items:
        sys.stderr.write(f"[ticket] Unknown PlanItem ids: {', '.join(missing_plan_items)}\n")
        return 1
    selected_files, file_error = _normalize_selected_files(worktree_path, getattr(args, "files", []) or [])
    if file_error:
        sys.stderr.write(file_error)
        return 1
    allowed_files = {
        str(path).strip()
        for plan_item_id in plan_item_ids
        for path in (plan_items[plan_item_id].get("expected_files") or [])
        if str(path).strip()
    }
    outside_scope = [path for path in selected_files if path not in allowed_files]
    if outside_scope:
        sys.stderr.write(
            "[ticket] checkpoint files must stay within the referenced PlanItem scope: "
            + ", ".join(outside_scope)
            + "\n"
        )
        return 1
    dirty_files = _ticket_dirty_files(worktree_path)
    unrelated_dirty = sorted(path for path in dirty_files if path not in set(selected_files))
    if unrelated_dirty:
        sys.stderr.write(
            "[ticket] checkpoint rejected: unrelated dirty files present: "
            + ", ".join(unrelated_dirty)
            + "\n"
        )
        return 1
    if not dirty_files:
        sys.stderr.write("[ticket] checkpoint rejected: worktree is clean.\n")
        return 1
    if any(path not in dirty_files for path in selected_files):
        sys.stderr.write("[ticket] checkpoint rejected: every --file must point to a changed file.\n")
        return 1
    validation_commands = []
    seen_commands: set[str] = set()
    for plan_item_id in plan_item_ids:
        status = str(plan_items[plan_item_id].get("status") or "pending")
        if status in {"deferred", "killed"}:
            sys.stderr.write(f"[ticket] PlanItem {plan_item_id} is {status} and cannot be checkpointed.\n")
            return 1
        for command in plan_items[plan_item_id].get("validation") or []:
            if command not in seen_commands:
                validation_commands.append(command)
                seen_commands.add(command)
    validation_result = _run_validation_commands(worktree_path, validation_commands)
    validation_receipt = {
        "receipt_kind": "validation_receipt",
        "recorded_at": _now_iso(),
        "ticket_id": ticket_id,
        "repo": repo_name,
        "plan_item_ids": plan_item_ids,
        "files": selected_files,
        "passed": validation_result["passed"],
        "commands": validation_result["commands"],
    }
    record_ticket_receipt(ticket_id, "validation_receipts", validation_receipt)
    for plan_item_id in plan_item_ids:
        update_plan_item(ticket_id, plan_item_id, last_validation_receipt=validation_receipt)
    if not validation_result["passed"]:
        sys.stderr.write("[ticket] checkpoint rejected: validation failed.\n")
        return 1
    add_code, add_out = _git(worktree_path, "add", "--", *selected_files)
    if add_code != 0:
        sys.stderr.write(f"[ticket] failed to stage checkpoint files: {add_out}\n")
        return 1
    plan_item_segment = ",".join(plan_item_ids)
    summary = str(getattr(args, "message", "") or "").strip()
    if not summary:
        sys.stderr.write("[ticket] checkpoint requires --message.\n")
        return 1
    commit_message = f"[{ticket_id}][{plan_item_segment}] {summary}"
    commit_code, commit_out = _git(worktree_path, "commit", "-m", commit_message)
    if commit_code != 0:
        sys.stderr.write(f"[ticket] checkpoint commit failed: {commit_out}\n")
        return 1
    commit_short_code, commit_short = _git(worktree_path, "rev-parse", "--short", "HEAD")
    commit_full_code, commit_full = _git(worktree_path, "rev-parse", "HEAD")
    checkpoint_receipt = {
        "receipt_kind": "checkpoint_receipt",
        "recorded_at": _now_iso(),
        "ticket_id": ticket_id,
        "repo": repo_name,
        "plan_item_ids": plan_item_ids,
        "files": selected_files,
        "commit_message": commit_message,
        "commit_short": commit_short if commit_short_code == 0 else None,
        "commit_sha": commit_full if commit_full_code == 0 else None,
        "validation_receipt_recorded_at": validation_receipt["recorded_at"],
    }
    record_ticket_receipt(ticket_id, "checkpoint_receipts", checkpoint_receipt)
    for plan_item_id in plan_item_ids:
        item = dict(get_ticket(ticket_id).get("plan_items", {}).get(plan_item_id) or {})
        plan_item_checkpoints = list(item.get("checkpoint_receipts") or [])
        plan_item_checkpoints.append(checkpoint_receipt)
        update_plan_item(
            ticket_id,
            plan_item_id,
            status="done",
            checkpoint_receipts=plan_item_checkpoints,
            last_validation_receipt=validation_receipt,
        )
    readiness = _build_readiness({**get_ticket(ticket_id), "id": ticket_id}, Path.cwd())
    next_phase = "ready_for_promote" if readiness["ready"] else "in_progress"
    update_ticket(ticket_id, readiness=readiness)
    set_ticket_phase(ticket_id, next_phase)
    if readiness["ready"]:
        readiness_receipt = {
            "receipt_kind": "readiness_receipt",
            "recorded_at": _now_iso(),
            "ticket_id": ticket_id,
            "ready_for_promote_main": True,
            "reasons": [],
        }
        record_ticket_receipt(ticket_id, "readiness_receipt", readiness_receipt)
    print(f"[ticket] Checkpoint committed for {ticket_id}")
    print(f"  Repo: {repo_name}")
    print(f"  PlanItems: {', '.join(plan_item_ids)}")
    print(f"  Commit: {commit_message}")
    return 0

def cmd_ticket_start(
    manifest: Dict[str, Any],
    ticket_id: str,
    repos_filter: Optional[str] = None,
    ecosystem: Optional[str] = None,
    stage_id: Optional[str] = None,
    environment_id: Optional[str] = None,
    repo_selections_json: Optional[str] = None,
    plan_items_json: Optional[str] = None,
    plan_items_file: Optional[str] = None,
    planner_profile: Optional[str] = None,
    planner_model: Optional[str] = None,
) -> int:
    """Start ticket work - create feature branch worktrees."""
    if not is_in_workspace():
        sys.stderr.write("[ticket] Not in a workspace.\n")
        sys.stderr.write("[ticket] Run 'amof -e <ecosystem> install' first.\n")
        return 1

    state = get_state()
    tickets = state.get("tickets", {})

    if ticket_id in tickets:
        sys.stderr.write(f"[ticket] Ticket {ticket_id} already started.\n")
        sys.stderr.write(f"[ticket] Use 'amof ticket switch {ticket_id}' to switch to it.\n")
        return 1

    plan_items, plan_items_error = _plan_items_payload(plan_items_json, plan_items_file)
    if plan_items_error:
        sys.stderr.write(plan_items_error)
        return 1

    workspace_root = Path.cwd()
    repos, parsed_repo_selections, repos_error = _select_ticket_repos(
        manifest,
        workspace_root,
        repos_filter=repos_filter,
        repo_selections_json=repo_selections_json,
    )
    if repos_error:
        sys.stderr.write(repos_error)
        return 1
    allowed, preflight_receipt, _ = _run_ticket_preflight(
        manifest,
        ticket_id,
        repos_filter=repos_filter,
        repo_selections_json=repo_selections_json,
    )
    if not allowed:
        _print_preflight(preflight_receipt)
        return 1

    workspace_config = manifest.get("workspace", {})
    repo_branch_prefix = normalize_branch_prefix(
        workspace_config.get("repo_branch_prefix", "feature")
    )
    feature_branch = f"{repo_branch_prefix}/{ticket_id}"
    repo_branches: Dict[str, str] = {}
    worktree_receipts: List[Dict[str, Any]] = []
    selection_by_repo = {entry["repo"]: entry for entry in parsed_repo_selections}
    for repo in repos:
        name = repo.get("name")
        readonly = repo.get("readonly", False)
        repo_path = Path(str(repo.get("resolved_path") or _repo_path(repo, workspace_root)))
        selection = selection_by_repo.get(name)
        target_branch = str((selection or {}).get("target_branch") or feature_branch)
        create_branch = str((selection or {}).get("mode") or "ticket_local") != "shared"

        if readonly:
            print(f"[ticket] {name}: readonly, skipping")
            continue

        if not repo_path.exists():
            print(f"[ticket] {name}: not found, skipping")
            continue

        try:
            wt_path = switch_to_ticket(
                repo_path,
                target_branch,
                ticket_id,
                name,
                workspace_root,
                create_branch=create_branch,
                base_ref="origin/main",
            )
            print(f"[ticket] {name}: ready at {_display_path(wt_path, workspace_root)}")
            repo_branches[name] = target_branch
            worktree_receipts.append(
                {
                    "repo": name,
                    "branch": target_branch,
                    "worktree_path": str(wt_path),
                    "base_ref": "origin/main",
                }
            )
        except Exception as e:
            sys.stderr.write(f"[ticket] {name}: failed to create worktree: {e}\n")
            continue

    if not repo_branches:
        sys.stderr.write("[ticket] No feature branches created.\n")
        return 1

    ticket_start_receipt = {
        "receipt_kind": "ticket_start_receipt",
        "recorded_at": _now_iso(),
        "ticket_id": ticket_id,
        "created_worktrees": worktree_receipts,
        "plan_item_ids": [item["id"] for item in plan_items],
        "preflight_recorded_at": preflight_receipt["recorded_at"],
    }
    add_ticket(
        ticket_id,
        repo_branches,
        ecosystem=ecosystem,
        stage_id=stage_id,
        environment_id=environment_id,
        repo_selections=parsed_repo_selections or None,
        preflight_receipt=preflight_receipt,
        ticket_start_receipt=ticket_start_receipt,
        plan_items=plan_items,
        planner_provenance=_planner_provenance(planner_profile, planner_model),
    )
    update_ide_workspace(workspace_root, ticket_id, repos)

    print(f"\n[ticket] Started {ticket_id}")
    print(f"  Repos: {len(repo_branches)}")
    print(f"  PlanItems: {len(plan_items)}")
    for name, branch in repo_branches.items():
        print(f"    {name}: {branch}")
    return 0

def cmd_ticket_list(manifest: Dict[str, Any]) -> int:
    """List active tickets and their repo branches."""
    if not is_in_workspace():
        sys.stderr.write("[ticket] Not in a workspace.\n")
        return 1

    tickets = get_all_tickets()
    active = get_active_ticket()

    if not tickets:
        print("[ticket] No active tickets.")
        print("[ticket] Start one with: amof ticket start <ticket-id>")
        return 0

    workspace_root = Path.cwd()
    print("[ticket] Active tickets:")
    for ticket_id, info in sorted(tickets.items()):
        marker = " *" if ticket_id == active else "  "
        label = " (active)" if ticket_id == active else ""
        phase = str(info.get("phase") or "started")
        print(f"{marker} {ticket_id}{label} [{phase}]")

        repos = info.get("repos", {})
        for repo_name, branch in sorted(repos.items()):
            wt_path = get_ticket_repo_worktree_path(workspace_root, ticket_id, repo_name)
            status = ""
            if wt_path.exists():
                if is_git_dirty(wt_path):
                    status = " (dirty)"
                else:
                    status = " (ok)"
            else:
                status = " (missing worktree)"
            print(f"      {repo_name}: {branch}{status}")

    print(f"\n  Switch: amof ticket switch <ticket-id>")
    print(f"  Status: amof ticket status <ticket-id>")
    return 0

def cmd_ticket_switch(
    manifest: Dict[str, Any],
    ticket_id: str,
    ecosystem: Optional[str] = None,
) -> int:
    """Switch active ticket - setup feature branch worktrees."""
    if not is_in_workspace():
        sys.stderr.write("[ticket] Not in a workspace.\n")
        return 1

    state = get_state()
    tickets = state.get("tickets", {})
    repos = get_effective_repos(manifest)
    workspace_root = Path.cwd()

    if ticket_id == "main":
        set_active_ticket(None)
        update_ide_workspace(workspace_root, None, repos)
        print("[ticket] Switched to main (base repos)")
        return 0

    if ticket_id not in tickets:
        sys.stderr.write(f"[ticket] Ticket {ticket_id} not found.\n")
        sys.stderr.write("[ticket] Available tickets:\n")
        for tid in sorted(tickets.keys()):
            sys.stderr.write(f"  - {tid}\n")
        return 1

    current_active = get_active_ticket()
    already_active = current_active == ticket_id

    if already_active:
        print(f"[ticket] Already on {ticket_id}")
        return 0

    repos = get_effective_repos(manifest)
    target_repos = tickets[ticket_id].get("repos", {})
    workspace_root = Path.cwd()

    errors = 0
    switched = 0
    for repo in repos:
        name = repo.get("name")
        readonly = repo.get("readonly", False)
        repo_path = Path(repo.get("path", f"repos/{name}"))

        if readonly or not repo_path.exists():
            continue

        if name not in target_repos:
            continue
        target_branch = target_repos[name]

        try:
            wt_path = switch_to_ticket(repo_path, target_branch, ticket_id, name, workspace_root, create_branch=False)
            print(f"[ticket] {name}: ready at {_display_path(wt_path, workspace_root)}")
            switched += 1
        except Exception as e:
            sys.stderr.write(f"[ticket] {name}: failed to switch: {e}\n")
            errors += 1

    set_active_ticket(ticket_id)
    update_ide_workspace(workspace_root, ticket_id, repos)

    if errors:
        sys.stderr.write(f"\n[ticket] Switched to {ticket_id} with {errors} error(s)\n")
        return 1

    print(f"\n[ticket] Switched to {ticket_id}")
    return 0

def cmd_ticket_end(
    manifest: Dict[str, Any],
    ticket_id: str,
    cleanup: bool = False,
    cleanup_local: bool = False,
) -> int:
    """End ticket work - remove from state, archive worktrees."""
    if not is_in_workspace():
        sys.stderr.write("[ticket] Not in a workspace.\n")
        return 1

    state = get_state()
    tickets = state.get("tickets", {})

    if ticket_id not in tickets:
        sys.stderr.write(f"[ticket] Ticket {ticket_id} not found.\n")
        return 1

    ticket_repos = tickets[ticket_id].get("repos", {})
    repos = get_effective_repos(manifest)
    workspace_root = resolve_workspace_root()

    if cleanup or cleanup_local:
        archive_ticket_worktrees(ticket_id, workspace_root, repos)
        _cleanup_ticket_branches(repos, ticket_repos, cleanup_remote=cleanup)

    remove_ticket(ticket_id)
    
    # Update IDE workspace to point to the new active ticket (or None)
    new_active = get_active_ticket()
    update_ide_workspace(workspace_root, new_active, repos)

    print(f"\n[ticket] Ended {ticket_id}")
    if not cleanup and not cleanup_local:
        print("[ticket] Feature branches and worktrees preserved (for PRs).")
        print("[ticket] Use --cleanup to delete branches and worktrees.")
    return 0


def cmd_ticket_env_upsert(args: Any) -> int:
    """Create or update one ticket GitOps environment file via the canonical updater script."""
    scripts_root = Path(__file__).resolve().parents[2]
    repo_root = scripts_root.parent
    updater = scripts_root / "gitops" / "upsert-ticket-env.py"

    if not updater.exists():
        sys.stderr.write(f"[ticket] Updater script not found: {updater}\n")
        return 1

    command = [
        sys.executable,
        str(updater),
        "--ticket-id",
        args.ticket_id,
        "--branch",
        args.branch,
        "--commit-sha",
        args.commit_sha,
        "--host-mode",
        args.host_mode,
        "--owner-id",
        args.owner_id or "operator@amof.dev",
        "--owner-slug",
        args.owner_slug or "operator-amof-dev",
        "--owner-type",
        args.owner_type,
        "--target-revision",
        args.target_revision,
        "--summary-json",
    ]

    if getattr(args, "base_domain", None):
        command.extend(["--base-domain", args.base_domain])
    if getattr(args, "registry_base", None):
        command.extend(["--registry-base", args.registry_base])
    if getattr(args, "output", None):
        command.extend(["--output", args.output])
    if getattr(args, "dry_run", False):
        command.append("--dry-run")
    if getattr(args, "summary_json", False):
        command.append("--summary-json")

    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            sys.stdout.write(exc.stdout)
        if exc.stderr:
            sys.stderr.write(exc.stderr)
        return exc.returncode or 1

    if getattr(args, "summary_json", False):
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        return 0

    try:
        summary = json.loads(completed.stdout.strip())
    except json.JSONDecodeError:
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        sys.stderr.write("[ticket] Updater did not return valid summary JSON.\n")
        return 1

    output_path = str(summary["output_path"])
    namespace = str(summary["namespace"])
    hostname = str(summary["hostname"])
    changed = bool(summary["changed"])

    print("[ticket env] Upsert complete")
    print(f"  File: {output_path}")
    print(f"  Namespace: {namespace}")
    print(f"  Hostname: {hostname}")
    print(f"  Changed: {'yes' if changed else 'no'}")
    print("  Next:")
    print(f"    git -C {repo_root} diff -- {output_path}")
    print(f"    git -C {repo_root} add {output_path}")
    print(f"    git -C {repo_root} commit -m \"fix(gitops): update ticket env\"")
    return 0


def cmd_ticket_build_write(args: Any) -> int:
    """Public canonical main does not expose ticket build-write runtime flows."""
    sys.stderr.write(_PUBLIC_BUILD_WRITE_REMOVED_DETAIL)
    return 1

def _cleanup_ticket_branches(
    repos: List[Dict[str, Any]],
    ticket_repos: Dict[str, str],
    cleanup_remote: bool = False,
) -> None:
    """Delete feature branches for a ticket after worktrees are removed."""
    for repo_name, branch in ticket_repos.items():
        repo_path = Path(f"repos/{repo_name}")
        if not repo_path.exists():
            continue

        code, out = run_command(["git", "-C", str(repo_path), "branch", "-D", branch])
        if code == 0:
            print(f"[ticket] {repo_name}: deleted local {branch}")

        if cleanup_remote:
            code, out = run_command(
                ["git", "-C", str(repo_path), "push", "origin", "--delete", branch]
            )
            if code == 0:
                print(f"[ticket] {repo_name}: deleted remote {branch}")
