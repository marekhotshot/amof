"""Ticket commands - manage ticket feature branches within a workspace using git worktrees."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils import get_git_branch, run_command, is_git_dirty, normalize_branch_prefix
from ..manifest import resolve_workspace_root
from ..state import (
    get_state,
    get_effective_repos,
    is_in_workspace,
    add_ticket,
    remove_ticket,
    get_active_ticket,
    set_active_ticket,
    get_all_tickets,
)
from ..worktree_manager import switch_to_ticket, archive_ticket_worktrees, get_ticket_repo_worktree_path, update_ide_workspace

_PUBLIC_BUILD_WRITE_REMOVED_DETAIL = (
    "[ticket] build-write was removed from public AMOF canonical main. "
    "Public AMOF keeps install/bootstrap/contracts only; runtime build-write "
    "flows belong to the private operating surface.\n"
)

def cmd_ticket_start(
    manifest: Dict[str, Any],
    ticket_id: str,
    repos_filter: Optional[str] = None,
    ecosystem: Optional[str] = None,
    stage_id: Optional[str] = None,
    environment_id: Optional[str] = None,
    repo_selections_json: Optional[str] = None,
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

    repos = get_effective_repos(manifest)
    workspace_config = manifest.get("workspace", {})
    repo_branch_prefix = normalize_branch_prefix(
        workspace_config.get("repo_branch_prefix", "feature")
    )
    feature_branch = f"{repo_branch_prefix}/{ticket_id}"
    parsed_repo_selections: List[Dict[str, Any]] = []
    selected_repo_names = None
    if repo_selections_json:
        try:
            payload = json.loads(repo_selections_json)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"[ticket] Invalid --repo-selections payload: {exc}\n")
            return 1
        if not isinstance(payload, list):
            sys.stderr.write("[ticket] --repo-selections must be a JSON list.\n")
            return 1
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
        selected_repo_names = {entry["repo"] for entry in parsed_repo_selections}
        if not parsed_repo_selections:
            sys.stderr.write("[ticket] --repo-selections did not contain any usable repo contracts.\n")
            return 1

    if repos_filter:
        allowed = set(r.strip() for r in repos_filter.split(","))
        repos = [r for r in repos if r.get("name") in allowed]
        if not repos:
            sys.stderr.write(f"[ticket] No matching repos found for: {repos_filter}\n")
            return 1
    elif selected_repo_names is not None:
        repos = [r for r in repos if r.get("name") in selected_repo_names]
        if not repos:
            sys.stderr.write("[ticket] No writable repos matched the provided repo selections.\n")
            return 1

    workspace_root = Path.cwd()
    repo_branches: Dict[str, str] = {}
    selection_by_repo = {entry["repo"]: entry for entry in parsed_repo_selections}
    for repo in repos:
        name = repo.get("name")
        readonly = repo.get("readonly", False)
        repo_path = Path(repo.get("path", f"repos/{name}"))
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
            )
            print(f"[ticket] {name}: ready at {wt_path.relative_to(workspace_root)}")
            repo_branches[name] = target_branch
        except Exception as e:
            sys.stderr.write(f"[ticket] {name}: failed to create worktree: {e}\n")
            continue

    if not repo_branches:
        sys.stderr.write("[ticket] No feature branches created.\n")
        return 1

    # add_ticket will now also store worktree_base internally if state.py is updated
    add_ticket(
        ticket_id,
        repo_branches,
        ecosystem=ecosystem,
        stage_id=stage_id,
        environment_id=environment_id,
        repo_selections=parsed_repo_selections or None,
    )
    update_ide_workspace(workspace_root, ticket_id, repos)

    print(f"\n[ticket] Started {ticket_id}")
    print(f"  Repos: {len(repo_branches)}")
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
        print(f"{marker} {ticket_id}{label}")

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
            print(f"[ticket] {name}: ready at {wt_path.relative_to(workspace_root)}")
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
