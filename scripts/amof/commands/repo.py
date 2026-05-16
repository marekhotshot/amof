"""Repo command - add/manage repositories in manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..manifest import write_manifest, get_manifest_path
from ..state import get_state, save_state, is_in_workspace, get_effective_repos
from ..utils import run_command, get_git_branch, run_with_retry, normalize_branch_prefix
from .sync import cmd_sync


def upsert_repo(
    manifest: Dict[str, Any],
    name: str,
    url: str,
    branch: str,
    path: str | None,
    include: List[str],
    exclude: List[str],
    replace: bool,
) -> None:
    """Add or update a repository in the manifest."""
    repo_entry = {
        "name": name,
        "url": url,
        "branch": branch,
        "path": path or f"repos/{name}",
    }
    if include:
        repo_entry["include"] = include
    if exclude:
        repo_entry["exclude"] = exclude

    repos = manifest.setdefault("repos", [])
    existing_idx = next((idx for idx, r in enumerate(repos) if r.get("name") == name), None)
    if existing_idx is not None:
        if not replace:
            sys.stderr.write(
                f"Repository '{name}' already exists in manifest; use --replace to overwrite.\n"
            )
            sys.exit(1)
        repos[existing_idx] = repo_entry
    else:
        repos.append(repo_entry)


def cmd_add_repo(args: argparse.Namespace, manifest: Dict[str, Any], ecosystem: Optional[str] = None) -> int:
    """Add a repository to the manifest."""
    upsert_repo(
        manifest,
        name=args.name,
        url=args.url,
        branch=args.branch,
        path=args.path,
        include=args.include,
        exclude=args.exclude,
        replace=args.replace,
    )

    write_manifest(manifest, ecosystem)
    print(f"Added {args.name} to manifest at {get_manifest_path(ecosystem)}")

    if args.sync:
        result = cmd_sync(manifest, only={args.name})
        
        # If in workspace and repo is not readonly, ensure feature branch exists
        if result == 0 and is_in_workspace() and not args.readonly:
            state = get_state()
            feature_branch = state.get("feature_branch")
            if feature_branch:
                repo_path = Path(args.path or f"repos/{args.name}")
                if repo_path.exists():
                    # Check if feature branch exists locally
                    code, _ = run_command(["git", "-C", str(repo_path), "rev-parse", "--verify", feature_branch])
                    if code == 0:
                        # Switch to existing feature branch
                        code, out = run_command(["git", "-C", str(repo_path), "checkout", feature_branch])
                        if code == 0:
                            print(f"[add-repo] ✓ Switched to {feature_branch}")
                        else:
                            sys.stderr.write(f"[add-repo] Warning: failed to checkout {feature_branch}: {out}\n")
                    else:
                        # Check if remote branch exists
                        remote_branch = f"origin/{feature_branch}"
                        code, _ = run_command(["git", "-C", str(repo_path), "rev-parse", "--verify", remote_branch])
                        if code == 0:
                            # Create local branch tracking remote
                            print(f"[add-repo] Creating feature branch {feature_branch} (tracking {remote_branch})...")
                            code, out = run_command(["git", "-C", str(repo_path), "checkout", "-b", feature_branch, remote_branch])
                            if code == 0:
                                print(f"[add-repo] ✓ Created and checked out {feature_branch}")
                            else:
                                sys.stderr.write(f"[add-repo] Warning: failed to create feature branch: {out}\n")
                        else:
                            # Create feature branch from current branch
                            print(f"[add-repo] Creating feature branch {feature_branch}...")
                            code, out = run_command(["git", "-C", str(repo_path), "checkout", "-b", feature_branch])
                            if code == 0:
                                print(f"[add-repo] ✓ Created and checked out {feature_branch}")
                            else:
                                sys.stderr.write(f"[add-repo] Warning: failed to create feature branch: {out}\n")
        
        return result
    return 0


def cmd_repo_promote(manifest: Dict[str, Any], repo_name: str, ecosystem: Optional[str] = None) -> int:
    """Promote a readonly repo to writable by creating a feature branch.
    
    This allows making changes to a repo that was initially cloned as readonly.
    """
    if not is_in_workspace():
        sys.stderr.write("[promote] Not in a workspace. Run from a workspace branch.\n")
        return 1
    
    state = get_state()
    ticket_id = state.get("ticket_id", "unknown")
    workspace_config = manifest.get("workspace", {})
    repo_branch_prefix = normalize_branch_prefix(
        workspace_config.get("repo_branch_prefix", "feature")
    )
    
    # Find the repo in state
    repos = state.get("repos", [])
    repo = None
    repo_idx = None
    for idx, r in enumerate(repos):
        if r.get("name") == repo_name:
            repo = r
            repo_idx = idx
            break
    
    if not repo:
        sys.stderr.write(f"[promote] Repository '{repo_name}' not found in workspace\n")
        sys.stderr.write("[promote] Available repos:\n")
        for r in repos:
            sys.stderr.write(f"  - {r.get('name')} ({'RO' if r.get('readonly') else 'RW'})\n")
        return 1
    
    if not repo.get("readonly", False):
        sys.stderr.write(f"[promote] Repository '{repo_name}' is already writable (RW)\n")
        return 1
    
    repo_path = Path(repo.get("path", f"repos/{repo_name}"))
    if not repo_path.exists():
        sys.stderr.write(f"[promote] Repository path not found: {repo_path}\n")
        sys.stderr.write("[promote] Run 'amof sync' first to clone the repository.\n")
        return 1
    
    # Create feature branch
    feature_branch = f"{repo_branch_prefix}/{ticket_id}"
    current_branch = get_git_branch(repo_path)
    
    print(f"[promote] Promoting {repo_name} from readonly to writable...")
    print(f"[promote] Creating branch: {feature_branch}")
    
    code, out = run_command(["git", "-C", str(repo_path), "checkout", "-b", feature_branch])
    if code != 0:
        # Branch might already exist
        if "already exists" in out:
            print(f"[promote] Branch {feature_branch} already exists, switching to it...")
            code, out = run_command(["git", "-C", str(repo_path), "checkout", feature_branch])
            if code != 0:
                sys.stderr.write(f"[promote] Failed to switch to branch: {out}\n")
                return 1
        else:
            sys.stderr.write(f"[promote] Failed to create branch: {out}\n")
            return 1
    
    # Update state to mark repo as writable and record the new branch
    repos[repo_idx]["readonly"] = False
    repos[repo_idx]["original_readonly"] = True  # Track that it was promoted
    repos[repo_idx]["promoted_branch"] = feature_branch  # Track the feature branch
    repos[repo_idx]["promoted_from"] = current_branch  # Track what branch it was promoted from
    state["repos"] = repos
    save_state(state)
    
    print(f"[promote] ✓ {repo_name} is now writable on branch {feature_branch}")
    print(f"[promote] You can now make changes and push with 'amof push'")
    return 0


def cmd_repo_cleanup(manifest: Dict[str, Any]) -> int:
    """Delete feature branches that have no commits (unchanged repos).
    
    This cleans up branches that were created but never used.
    """
    if not is_in_workspace():
        sys.stderr.write("[cleanup] Not in a workspace. Run from a workspace branch.\n")
        return 1
    
    state = get_state()
    workspace_config = manifest.get("workspace", {})
    repo_branch_prefix = normalize_branch_prefix(
        workspace_config.get("repo_branch_prefix", "feature")
    )
    
    repos = state.get("repos", [])
    cleaned = []
    skipped = []
    
    for repo in repos:
        name = repo.get("name")
        readonly = repo.get("readonly", False)
        repo_path = Path(repo.get("path", f"repos/{name}"))
        
        if readonly:
            continue
        
        if not repo_path.exists():
            continue
        
        current_branch = get_git_branch(repo_path)
        if not current_branch or not current_branch.startswith(f"{repo_branch_prefix}/"):
            continue
        
        # Get the manifest branch (base branch)
        manifest_branch = repo.get("branch", "main")
        
        # Check if there are any commits ahead of base
        code, out = run_command([
            "git", "-C", str(repo_path), 
            "rev-list", "--count", f"{manifest_branch}..{current_branch}"
        ])
        
        if code != 0:
            # Might not have the remote branch, skip
            skipped.append((name, "could not compare branches"))
            continue
        
        try:
            commit_count = int(out.strip())
        except ValueError:
            skipped.append((name, "invalid commit count"))
            continue
        
        if commit_count == 0:
            # No commits, safe to delete
            print(f"[cleanup] {name}: no commits on {current_branch}, cleaning up...")
            
            # Switch to manifest branch
            code, out = run_command(["git", "-C", str(repo_path), "checkout", manifest_branch])
            if code != 0:
                skipped.append((name, f"failed to checkout {manifest_branch}"))
                continue
            
            # Delete the feature branch
            code, out = run_command(["git", "-C", str(repo_path), "branch", "-D", current_branch])
            if code == 0:
                cleaned.append(name)
            else:
                skipped.append((name, f"failed to delete branch: {out}"))
        else:
            skipped.append((name, f"{commit_count} commits, keeping"))
    
    print()
    if cleaned:
        print(f"[cleanup] Cleaned {len(cleaned)} branches:")
        for name in cleaned:
            print(f"  ✓ {name}")
    else:
        print("[cleanup] No empty branches to clean up")
    
    if skipped:
        print(f"\n[cleanup] Skipped {len(skipped)} repos:")
        for name, reason in skipped:
            print(f"  - {name}: {reason}")
    
    return 0

