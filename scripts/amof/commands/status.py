"""Status command - show repository status."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

from ..api.command_builder import get_workspace_root
from ..utils import (
    get_git_branch,
    get_git_commit,
    get_git_toplevel,
    is_git_dirty,
    is_linked_worktree,
    normalize_branch_prefix,
)
from ..state import get_workspace_info, get_effective_repos, get_repo_commits


def status_data(manifest: Dict[str, Any], only: set[str] | None = None) -> Dict[str, Any]:
    """Compute workspace and repo status as structured data (no side effects).
    Used by CLI for printing and by API for JSON responses.
    """
    ws_info = get_workspace_info()
    repos = get_effective_repos(manifest)
    if not repos:
        return {"workspace": ws_info, "repos": [], "last_pushed": {}}

    workspace_config = manifest.get("workspace", {})
    repo_branch_prefix = normalize_branch_prefix(
        workspace_config.get("repo_branch_prefix", "feature")
    )
    manifest_ecosystem = str(manifest.get("ecosystem") or manifest.get("name") or "").strip() or None
    workspace_ecosystem = str(ws_info.get("ecosystem") or "").strip() if ws_info else ""
    active_ticket = ws_info.get("active_ticket") if ws_info and workspace_ecosystem == manifest_ecosystem else None
    expected_feature_branch = f"{repo_branch_prefix}/{active_ticket}" if active_ticket else None
    repo_commits = get_repo_commits()

    rows: list[Dict[str, Any]] = []
    for repo in repos:
        name = repo.get("name")
        if only and name not in only:
            continue
        manifest_branch = repo.get("branch", "main")
        readonly = repo.get("readonly", False)
        mode = "RO" if readonly else "RW"
        path_value = str(repo.get("path", name))
        path = Path(path_value)
        if not path.is_absolute():
            path = get_workspace_root() / path

        if not path.exists():
            rows.append({"repo": name, "branch": "-", "commit": "-", "mode": mode, "status": "MISSING"})
            continue

        current_branch = get_git_branch(path) or "?"
        current_commit = get_git_commit(path) or "?"
        dirty = is_git_dirty(path)

        status = "OK"
        if readonly:
            if current_branch != manifest_branch:
                status = "WRONG_BRANCH"
        else:
            if expected_feature_branch:
                if current_branch != expected_feature_branch:
                    status = "WRONG_BRANCH"
            else:
                is_feature_branch = current_branch.startswith(f"{repo_branch_prefix}/")
                if current_branch != manifest_branch and not is_feature_branch:
                    status = "WRONG_BRANCH"

        if dirty:
            status = "DIRTY" if status == "OK" else f"{status}+DIRTY"
        last_push = repo_commits.get(name, {})
        if last_push:
            last_commit = last_push.get("commit", "")
            if current_commit != last_commit and current_commit != "?":
                status = "UNPUSHED" if status == "OK" else f"{status}+UNPUSHED"

        rows.append({
            "repo": name,
            "branch": current_branch,
            "commit": current_commit,
            "mode": mode,
            "status": status,
        })

    last_pushed = {
        name: {
            "branch": info.get("branch"),
            "commit": info.get("commit"),
            "pushed_at": info.get("pushed_at", "")[:16].replace("T", " "),
        }
        for name, info in repo_commits.items()
    }
    return {"workspace": ws_info, "repos": rows, "last_pushed": last_pushed}


def cmd_status(manifest: Dict[str, Any], only: set[str] | None = None) -> int:
    """Show repository status."""
    data = status_data(manifest, only)
    ws_info = data["workspace"]
    if ws_info:
        eco = ws_info.get('ecosystem', '?')
        active = ws_info.get('active_ticket') or 'none'
        ticket_count = ws_info.get('ticket_count', 0)
        print(f"Workspace: {eco} (active ticket: {active}, {ticket_count} tracked)")
        print(f"Branch: {ws_info.get('workspace_branch', '?')}")
        if is_linked_worktree():
            wt_root = get_git_toplevel()
            if wt_root:
                print(f"Worktree: {wt_root}")
        print()

    repos_data = data["repos"]
    if not repos_data:
        sys.stderr.write("No repositories defined (or none enabled).\n")
        return 1

    max_repo_width = max(len("REPO"), max((len(r["repo"]) for r in repos_data), default=0), 22)
    header = f"{'REPO':<{max_repo_width}}{'BRANCH':<30}{'COMMIT':<10}{'MODE':<6}{'STATUS'}"
    print(header)
    print("-" * len(header))
    for r in repos_data:
        print(f"{r['repo']:<{max_repo_width}}{r['branch']:<30}{r['commit']:<10}{r['mode']:<6}{r['status']}")

    last_pushed = data.get("last_pushed") or {}
    if last_pushed:
        print("\nLast pushed commits:")
        for name, info in last_pushed.items():
            print(f"  {name}: {info.get('branch')} @ {info.get('commit')} ({info.get('pushed_at', '')})")

    return 0

