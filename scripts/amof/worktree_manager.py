import os
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Optional

from .app_paths import ensure_canonical_repo_write_allowed, ticket_worktrees_dir
from .state import get_ticket_repos

logger = logging.getLogger(__name__)

def get_ticket_worktree_base(workspace_root: Path, ticket_id: str) -> Path:
    """Returns the base directory for a ticket's worktrees."""
    _ = workspace_root
    return ticket_worktrees_dir() / ticket_id

def get_ticket_repo_worktree_path(workspace_root: Path, ticket_id: str, repo_name: str) -> Path:
    """Returns the specific worktree path for a repo under a ticket."""
    return get_ticket_worktree_base(workspace_root, ticket_id) / repo_name

def _run_git(repo_path: Path, args: list, check: bool = True) -> subprocess.CompletedProcess:
    """Helper to run git commands inside a repo."""
    resolved_repo_path = repo_path.resolve()
    cmd = ["git", "-c", f"safe.directory={resolved_repo_path}", "-C", str(resolved_repo_path)] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)

def switch_to_ticket(
    repo_path: Path,
    branch_name: str,
    ticket_id: str,
    repo_name: str,
    workspace_root: Path,
    create_branch: bool = True,
    base_ref: str = "HEAD",
) -> Path:
    """
    Creates or re-uses a git worktree for the specific branch and ticket.
    Returns the path to the newly created (or existing) worktree.
    """
    wt_path = get_ticket_repo_worktree_path(workspace_root, ticket_id, repo_name)
    ensure_canonical_repo_write_allowed(
        operation="create ticket worktree",
        target_path=wt_path,
        base=workspace_root,
    )
    
    # 1. If worktree directory already exists, ensure it's valid
    if wt_path.exists() and (wt_path / ".git").exists():
        logger.debug(f"Worktree already exists at {wt_path}")
        return wt_path

    # Ensure parent directory exists
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    # 2. Check if branch exists locally
    local_check = _run_git(repo_path, ["show-ref", "--verify", f"refs/heads/{branch_name}"], check=False)
    branch_exists_locally = local_check.returncode == 0

    # 3. Check if branch exists on remote
    remote_check = _run_git(repo_path, ["ls-remote", "--heads", "origin", branch_name], check=False)
    branch_exists_remote = bool(remote_check.stdout.strip())

    try:
        if branch_exists_locally:
            # Branch exists locally: create worktree checked out to it
            _run_git(repo_path, ["worktree", "add", str(wt_path), branch_name])
            logger.info(f"Created worktree for existing local branch '{branch_name}' at {wt_path}")
        elif branch_exists_remote:
            # Branch only on remote: create worktree tracking remote branch
            _run_git(repo_path, ["worktree", "add", "--track", "-b", branch_name, str(wt_path), f"origin/{branch_name}"])
            logger.info(f"Created worktree tracking origin/{branch_name} at {wt_path}")
        else:
            if create_branch:
                # Branch does not exist: create new branch and worktree from the requested base ref.
                _run_git(repo_path, ["worktree", "add", "-b", branch_name, str(wt_path), base_ref])
                logger.info(f"Created new branch '{branch_name}' from {base_ref} at {wt_path}")
            else:
                raise RuntimeError(f"Branch '{branch_name}' does not exist and create_branch=False")
                
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to create worktree: {e.stderr}")
        raise RuntimeError(f"Git worktree creation failed for {repo_name}: {e.stderr}")

    return wt_path

def archive_ticket_worktrees(ticket_id: str, workspace_root: Path, repos: list[dict] | None = None) -> None:
    """
    Removes the git worktrees for a ticket and deletes the directory.
    """
    wt_base = get_ticket_worktree_base(workspace_root, ticket_id)
    if not wt_base.exists():
        return
    resolved_wt_base = wt_base.resolve()
    expected_repo_names = set(get_ticket_repos(ticket_id).keys())
    repo_paths = {
        str(repo.get("name") or "").strip(): (workspace_root / str(repo.get("path") or f"repos/{repo.get('name')}")).resolve()
        for repo in (repos or [])
        if str(repo.get("name") or "").strip()
    }
    removable_dirs: list[Path] = []

    for repo_dir in sorted((path for path in wt_base.iterdir() if path.is_dir()), key=lambda item: item.name):
        repo_name = repo_dir.name
        resolved_repo_dir = repo_dir.resolve()
        try:
            resolved_repo_dir.relative_to(resolved_wt_base)
        except ValueError:
            logger.warning("Skipping ticket worktree cleanup outside expected base: %s", repo_dir)
            continue
        if expected_repo_names and repo_name not in expected_repo_names:
            logger.warning("Skipping unexpected ticket worktree path with no tracked owner: %s", repo_dir)
            continue
        repo_path = repo_paths.get(repo_name)
        if repo_path is None:
            logger.warning("Skipping ticket worktree cleanup without canonical repo mapping: %s", repo_dir)
            continue
        if not repo_path.exists():
            logger.warning("Skipping ticket worktree cleanup because canonical repo path is missing: %s", repo_path)
            continue
        if repo_path == resolved_repo_dir:
            logger.warning("Refusing to remove canonical repo path as a ticket worktree: %s", repo_dir)
            continue
        if not (repo_dir / ".git").exists():
            logger.warning("Ticket worktree path is missing .git metadata; skipping git removal and preserving path: %s", repo_dir)
            continue
        try:
            _run_git(repo_path, ["worktree", "remove", "--force", str(resolved_repo_dir)])
            logger.info("Removed ticket worktree %s via owning repo %s", repo_dir, repo_path)
            removable_dirs.append(repo_dir)
        except subprocess.CalledProcessError as e:
            logger.warning(
                "Failed to cleanly remove ticket worktree %s via owning repo %s: %s",
                repo_dir,
                repo_path,
                e.stderr,
            )

    for repo_dir in removable_dirs:
        if repo_dir.exists():
            try:
                shutil.rmtree(repo_dir)
            except Exception as e:
                logger.warning("Failed to delete removed ticket worktree path %s: %s", repo_dir, e)

    remaining_dirs = [path for path in wt_base.iterdir() if path.exists()]
    if remaining_dirs:
        logger.warning("Preserving ticket worktree base because unresolved paths remain: %s", wt_base)
        return
    try:
        shutil.rmtree(wt_base)
        logger.info(f"Deleted ticket worktree directory {wt_base}")
    except Exception as e:
        logger.warning(f"Failed to delete {wt_base}: {e}")

import json

def update_ide_workspace(workspace_root: Path, ticket_id: Optional[str], repos: list) -> None:
    """
    Updates the .code-workspace file to point to the correct worktrees.
    If ticket_id is None, it reverts to the base repos/ folders.
    """
    workspace_files = list(workspace_root.glob("*.code-workspace"))
    if not workspace_files:
        return
        
    repo_names = {r.get("name") for r in repos}
    
    for ws_file in workspace_files:
        try:
            with open(ws_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            if "folders" not in data:
                continue
                
            changed = False
            for folder in data["folders"]:
                name = folder.get("name")
                if name in repo_names:
                    # Determine target path
                    if ticket_id:
                        target_path = str(get_ticket_worktree_base(workspace_root, ticket_id) / name)
                    else:
                        target_path = f"repos/{name}"
                        
                    if folder.get("path") != target_path:
                        folder["path"] = target_path
                        changed = True
                        
            if changed:
                with open(ws_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                logger.debug(f"Updated IDE workspace file: {ws_file.name}")
        except Exception as e:
            logger.warning(f"Failed to update workspace file {ws_file}: {e}")
