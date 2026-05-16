"""Archive command - finish workspace, preserve repo branches, save state."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..utils import (
    get_git_branch,
    get_git_commit,
    get_git_toplevel,
    get_main_worktree_root,
    is_linked_worktree,
    run_command,
)
from ..state import get_state, get_effective_repos, get_all_tickets
from .workspace import get_workspace_filename


def _cleanup_feature_branches(repos, tickets: Dict[str, Any], dry_run: bool = False) -> None:
    """Delete feature branches for all tracked tickets."""
    branches_to_delete = set()
    for ticket_id, info in tickets.items():
        for repo_name, branch in info.get("repos", {}).items():
            branches_to_delete.add((repo_name, branch))

    for repo_name, branch in sorted(branches_to_delete):
        repo_path = Path(f"repos/{repo_name}")
        if not repo_path.exists():
            continue

        if dry_run:
            print(f"  - {repo_name}: {branch}")
            continue

        current = get_git_branch(repo_path)
        if current == branch:
            manifest_branch = "main"
            for r in repos:
                if r.get("name") == repo_name:
                    manifest_branch = r.get("branch", "main")
                    break
            run_command(["git", "-C", str(repo_path), "checkout", manifest_branch])

        code, _ = run_command(["git", "-C", str(repo_path), "branch", "-D", branch])
        if code == 0:
            print(f"[archive] {repo_name}: deleted local {branch}")

        code, _ = run_command(["git", "-C", str(repo_path), "push", "origin", "--delete", branch])
        if code == 0:
            print(f"[archive] {repo_name}: deleted remote {branch}")


def cmd_archive(
    manifest: Dict[str, Any],
    message: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
    ecosystem: Optional[str] = None,
    delete_workspace: bool = False,
    cleanup_features: bool = False,
) -> int:
    """Archive workspace: push, save state, optionally delete branches."""
    state = get_state()
    if not state:
        sys.stderr.write("[archive] No workspace state found.\n")
        return 1

    repos = get_effective_repos(manifest)
    eco = state.get("ecosystem", ecosystem or "default")
    tickets = get_all_tickets()

    current_branch = get_git_branch(Path("."))
    if not current_branch or not current_branch.startswith("workspace/"):
        sys.stderr.write(f"[archive] Not on a workspace branch (current: {current_branch})\n")
        return 1

    worktree_root = get_git_toplevel()
    main_root = get_main_worktree_root()
    in_worktree = is_linked_worktree()

    if dry_run:
        print("[dry-run] Would archive workspace:")
        print(f"  Ecosystem: {eco}")
        print(f"  Workspace branch: {current_branch}")
        if in_worktree and worktree_root:
            print(f"  Worktree: {worktree_root}")
        print(f"  Tickets: {len(tickets)}")
        if delete_workspace:
            print(f"  Would DELETE workspace branch")
        else:
            print(f"  Would KEEP workspace branch")
        if cleanup_features and tickets:
            print(f"  Would DELETE feature branches:")
            _cleanup_feature_branches(repos, tickets, dry_run=True)
        print("\nRun without --dry-run to execute.")
        return 0

    if not force:
        print(f"[archive] This will:")
        print(f"  1. Push all changes")
        print(f"  2. Save archive to ecosystems/{eco}/archives/")
        print(f"  3. {'DELETE' if delete_workspace else 'KEEP'} workspace branch")
        print(f"  4. {'DELETE' if cleanup_features else 'KEEP'} feature branches")
        if in_worktree and worktree_root:
            print(f"  5. Remove worktree at {worktree_root}")
        confirm = input("Continue? [y/N]: ")
        if confirm.lower() != "y":
            print("[archive] Cancelled")
            return 0

    print("[archive] Pushing all changes...")
    archive_data = {
        "ecosystem": eco,
        "archived_at": datetime.now().isoformat(),
        "workspace_branch": current_branch,
        "message": message,
        "tickets": list(tickets.keys()),
        "repos": [],
    }

    run_command(["git", "push", "-u", "origin", current_branch])

    for repo in repos:
        name = repo.get("name")
        readonly = repo.get("readonly", False)
        repo_path = Path(repo.get("path", name))

        if not repo_path.exists():
            continue

        branch = get_git_branch(repo_path)
        commit = get_git_commit(repo_path)

        archive_data["repos"].append({
            "name": name,
            "branch": branch,
            "commit": commit,
            "readonly": readonly,
        })

        if not readonly and branch:
            run_command(["git", "-C", str(repo_path), "push", "-u", "origin", branch])
            print(f"[archive] {name}: pushed {branch}")

    archive_dir = Path(f"ecosystems/{eco}/archives")
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_file = archive_dir / f"{eco}.json"

    with archive_file.open("w", encoding="utf-8") as f:
        json.dump(archive_data, f, indent=2)

    run_command(["git", "add", str(archive_file)])
    run_command(["git", "commit", "-m", f"Archive workspace {eco}"])
    run_command(["git", "push"])

    if cleanup_features and tickets:
        _cleanup_feature_branches(repos, tickets)

    workspace_branch = current_branch

    if in_worktree and worktree_root and main_root:
        # --- Worktree mode: remove the linked worktree ---
        for cleanup_path in [Path("repos"), Path("context"), Path(".amof")]:
            abs_path = worktree_root / cleanup_path
            if abs_path.exists():
                shutil.rmtree(abs_path)

        for ws_file in worktree_root.glob("amof.*.code-workspace"):
            ws_file.unlink()

        wt_str = str(worktree_root)
        os.chdir(main_root)

        code, out = run_command(["git", "worktree", "remove", "--force", wt_str])
        if code != 0:
            sys.stderr.write(f"[archive] Failed to remove worktree: {out}\n")
            run_command(["git", "worktree", "prune"])
            if Path(wt_str).exists():
                shutil.rmtree(wt_str)

        if delete_workspace:
            run_command(["git", "branch", "-D", workspace_branch])
            run_command(["git", "push", "origin", "--delete", workspace_branch])
            print(f"[archive] Deleted workspace branch")

        print(f"\n[archive] Workspace archived!")
        print(f"[archive] You are in the main repo at: {main_root}")
    else:
        # --- Legacy mode ---
        for cleanup_path in [Path("repos"), Path("context"), Path(".amof")]:
            if cleanup_path.exists():
                shutil.rmtree(cleanup_path)

        for ws_file in Path(".").glob("amof.*.code-workspace"):
            ws_file.unlink()

        run_command(["git", "checkout", "main"])

        if delete_workspace:
            run_command(["git", "branch", "-D", workspace_branch])
            run_command(["git", "push", "origin", "--delete", workspace_branch])
            print(f"[archive] Deleted workspace branch")

        print(f"\n[archive] Workspace archived!")
        print(f"[archive] You are now on main.")

    return 0


def cmd_archive_list(manifest: Dict[str, Any]) -> int:
    """List archived workspaces."""
    ecosystem = manifest.get("ecosystem", "default")
    archive_dir = Path(f"ecosystems/{ecosystem}/archives")

    if not archive_dir.exists():
        print(f"[archive] No archives found for: {ecosystem}")
        return 0

    for archive_file in sorted(archive_dir.glob("*.json"), reverse=True):
        try:
            with archive_file.open() as f:
                data = json.load(f)
            print(f"  {archive_file.stem}")
            print(f"    Archived: {data.get('archived_at', '?')[:16]}")
            print(f"    Tickets: {', '.join(data.get('tickets', [])) or 'none'}")
        except Exception:
            print(f"  {archive_file.name}: (error)")

    return 0
