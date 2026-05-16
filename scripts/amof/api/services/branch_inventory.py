from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from amof.api.command_builder import get_workspace_root
from amof.commands.status import status_data
from amof.manifest import load_manifest
from amof.state import get_all_tickets, get_effective_repos
from amof.utils import run_command

OPERATIONAL_LONG_LIVED_BRANCHES = {"main", "dev"}


def _resolve_repo_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return get_workspace_root() / path


def _run_git_repo_command(repo_path: Path, args: list[str]) -> Tuple[int, str]:
    return run_command(
        ["git", "-c", f"safe.directory={repo_path}", *args],
        cwd=repo_path,
    )


def _branch_sort_key(branch: str) -> Tuple[int, str]:
    priority = {
        "main": 0,
        "dev": 1,
        "master": 2,
    }
    return (priority.get(branch, 50), branch)


def _is_git_repo_root(repo_path: Path) -> bool:
    if not repo_path.exists():
        return False
    code, _ = _run_git_repo_command(repo_path, ["rev-parse", "--show-toplevel"])
    return code == 0


def _list_repo_branches(repo_path: Path) -> Set[str]:
    if not _is_git_repo_root(repo_path):
        return set()

    code, out = _run_git_repo_command(
        repo_path,
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"],
    )
    branches: Set[str] = set()
    if code == 0:
        for line in out.splitlines():
            branch = line.strip()
            if not branch or branch in {"HEAD", "origin", "origin/HEAD"}:
                continue
            if branch.startswith("origin/"):
                branch = branch[len("origin/") :]
            if branch:
                branches.add(branch)
    if branches:
        return branches

    code, out = _run_git_repo_command(
        repo_path,
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"],
    )
    if code != 0:
        return set()
    for line in out.splitlines():
        branch = line.strip()
        if branch:
            branches.add(branch)
    return branches


def _get_repo_current_branch(repo_path: Path) -> Optional[str]:
    code, out = _run_git_repo_command(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if code == 0 and out:
        return out.strip()
    return None


def _tracked_ticket_branches_for_repo(repo_name: str, ecosystem: str) -> Set[str]:
    tracked: Set[str] = set()
    tickets = get_all_tickets(ecosystem=ecosystem)
    for ticket in tickets.values():
        repo_branch = str((ticket or {}).get("repos", {}).get(repo_name) or "").strip()
        if repo_branch.startswith("feat/"):
            tracked.add(repo_branch)
    return tracked


def _filter_operational_branches(branches: Set[str], repo_name: str, ecosystem: str) -> Set[str]:
    tracked_ticket_branches = _tracked_ticket_branches_for_repo(repo_name, ecosystem)
    return {
        branch
        for branch in branches
        if branch in OPERATIONAL_LONG_LIVED_BRANCHES or branch in tracked_ticket_branches
    }


def _default_branch_for_repo(branches: Set[str], manifest_branch: Optional[str]) -> Optional[str]:
    if "dev" in branches:
        return "dev"
    if "main" in branches:
        return "main"
    if manifest_branch and manifest_branch in branches:
        return manifest_branch
    return sorted(branches, key=_branch_sort_key)[0] if branches else manifest_branch


def build_branch_truth(ecosystem: str) -> Dict[str, Any]:
    try:
        manifest = load_manifest(ecosystem)
    except Exception:
        return {
            "branch_summary": None,
            "repo_statuses": [],
            "available_branches": [],
            "repo_branch_inventory": [],
        }

    summary = status_data(manifest)
    repo_rows = summary.get("repos") or []
    repo_statuses = []
    for row in repo_rows:
        status_text = str(row.get("status") or "")
        repo_statuses.append(
            {
                "repo": row.get("repo", ""),
                "branch": row.get("branch", ""),
                "commit": row.get("commit", ""),
                "mode": row.get("mode", ""),
                "status": status_text,
                "tracking_branch": None,
                "ahead": 0,
                "behind": 0,
                "dirty": "DIRTY" in status_text,
                "fetch_state": "local",
                "fetched_at": None,
                "fetch_message": None,
            }
        )

    discovered_operational_branches: Set[str] = set()
    repo_branch_inventory = []
    for repo in get_effective_repos(manifest):
        repo_name = str(repo.get("name") or "")
        repo_path = _resolve_repo_path(repo.get("path", f"repos/{repo_name}"))
        manifest_branch = str(repo.get("branch") or "main").strip() or "main"
        raw_branches = _list_repo_branches(repo_path)
        operational_branches = _filter_operational_branches(raw_branches, repo_name, ecosystem)

        if not repo_path.exists() or not _is_git_repo_root(repo_path):
            inventory_status = "missing"
        elif operational_branches:
            inventory_status = "available"
        else:
            current_branch = _get_repo_current_branch(repo_path)
            inventory_status = "uninitialized" if not current_branch else "available"

        discovered_operational_branches.update(operational_branches)
        sorted_operational_branches = sorted(operational_branches, key=_branch_sort_key)
        repo_branch_inventory.append(
            {
                "repo": repo_name,
                "manifest_branch": manifest_branch,
                "default_branch": _default_branch_for_repo(operational_branches, manifest_branch),
                "status": inventory_status,
                "readonly": bool(repo.get("readonly", False)),
                "branches": sorted_operational_branches,
                "raw_branches": sorted(raw_branches, key=_branch_sort_key),
                "protected_branches": [branch for branch in sorted_operational_branches if branch == "main"],
            }
        )

    available_branches = sorted(discovered_operational_branches, key=_branch_sort_key)

    return {
        "branch_summary": {
            "repo_count": len(repo_statuses),
            "ahead_count": 0,
            "behind_count": 0,
            "dirty_count": sum(1 for repo in repo_statuses if repo["dirty"]),
            "wrong_branch_count": sum(1 for repo in repo_statuses if "WRONG_BRANCH" in repo["status"]),
            "fetch_state": "local",
            "fetched_at": None,
            "fetch_message": None,
        },
        "repo_statuses": repo_statuses,
        "available_branches": available_branches,
        "repo_branch_inventory": repo_branch_inventory,
    }
