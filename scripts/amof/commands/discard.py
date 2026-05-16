"""Discard command - delete workspace worktree and feature branches."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

from ..utils import (
    get_git_branch,
    get_git_toplevel,
    get_main_worktree_root,
    is_linked_worktree,
    run_command,
)
from ..state import get_state, get_effective_repos, get_all_tickets


def cmd_discard(
    manifest: Dict[str, Any],
    force: bool = False,
    dry_run: bool = False,
    ecosystem: str | None = None,
) -> int:
    """Delete workspace worktree and all feature branches."""
    repos = get_effective_repos(manifest)
    tickets = get_all_tickets()

    current_branch = get_git_branch(Path("."))
    if not current_branch or not current_branch.startswith("workspace/"):
        sys.stderr.write(f"[discard] Not on a workspace branch (current: {current_branch})\n")
        return 1

    worktree_root = get_git_toplevel()
    main_root = get_main_worktree_root()
    in_worktree = is_linked_worktree()

    all_feature_branches: Dict[str, set] = {}
    for ticket_id, info in tickets.items():
        for repo_name, branch in info.get("repos", {}).items():
            all_feature_branches.setdefault(repo_name, set()).add(branch)

    if dry_run:
        print("[dry-run] Would delete:")
        print(f"  - Workspace branch: {current_branch}")
        if in_worktree and worktree_root:
            print(f"  - Worktree directory: {worktree_root}")
        for repo_name, branches in sorted(all_feature_branches.items()):
            for branch in sorted(branches):
                print(f"  - {repo_name}: {branch}")
        print("  - repos/, context/, .amof/, workspace files")
        print("\nRun without --dry-run to execute.")
        return 0

    if not force:
        print(f"[discard] This will delete:")
        print(f"  - Workspace branch: {current_branch}")
        if in_worktree and worktree_root:
            print(f"  - Worktree directory: {worktree_root}")
        for repo_name, branches in sorted(all_feature_branches.items()):
            for branch in sorted(branches):
                print(f"  - {repo_name}: {branch}")
        confirm = input("Are you sure? [y/N]: ")
        if confirm.lower() != "y":
            print("[discard] Cancelled")
            return 0

    # Delete feature branches inside the repos
    print("[discard] Deleting feature branches...")
    for repo_name, branches in all_feature_branches.items():
        repo_path = Path(f"repos/{repo_name}")
        if not repo_path.exists():
            continue

        manifest_branch = "main"
        for r in repos:
            if r.get("name") == repo_name:
                manifest_branch = r.get("branch", "main")
                break

        for branch in branches:
            current = get_git_branch(repo_path)
            if current == branch:
                run_command(["git", "-C", str(repo_path), "checkout", manifest_branch])

            code, _ = run_command(["git", "-C", str(repo_path), "branch", "-D", branch])
            if code == 0:
                print(f"[discard] {repo_name}: deleted {branch}")

    workspace_branch = current_branch

    if in_worktree and worktree_root and main_root:
        # --- Worktree mode: remove the linked worktree, then delete the branch ---
        # Clean up gitignored content first (repos are separate clones)
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
            sys.stderr.write(f"[discard] Failed to remove worktree: {out}\n")
            # Try harder: prune and retry
            run_command(["git", "worktree", "prune"])
            # If directory still exists, remove it manually
            if Path(wt_str).exists():
                shutil.rmtree(wt_str)

        code, _ = run_command(["git", "branch", "-D", workspace_branch])
        if code == 0:
            print(f"[discard] Deleted branch {workspace_branch}")

        print(f"\n[discard] Workspace discarded. You are in the main repo at: {main_root}")
    else:
        # --- Legacy mode (running from main worktree with branch checkout) ---
        for cleanup_path in [Path("repos"), Path("context"), Path(".amof")]:
            if cleanup_path.exists():
                shutil.rmtree(cleanup_path)

        for ws_file in Path(".").glob("amof.*.code-workspace"):
            ws_file.unlink()

        run_command(["git", "checkout", "main"])
        run_command(["git", "branch", "-D", workspace_branch])

        print("\n[discard] Workspace discarded. You are on main.")

    return 0
