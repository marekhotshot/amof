"""PR command - create pull requests for all changed repos."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..state import get_state, is_in_workspace, save_state
from ..utils import run_command, get_git_branch, get_git_commit


def get_bitbucket_auth() -> tuple[str, str] | None:
    """Get Bitbucket credentials from environment."""
    user = os.environ.get("BITBUCKET_USER")
    token = os.environ.get("BITBUCKET_TOKEN")
    if not user or not token:
        return None
    return (user, token)


def get_bitbucket_url() -> str:
    """Get Bitbucket base URL from environment."""
    return os.environ.get("BITBUCKET_URL", "https://bitbucket.example.com")


def parse_bitbucket_repo_info(ssh_url: str) -> tuple[str, str] | None:
    """Parse project key and repo slug from SSH URL.
    
    Examples:
        ssh://git@bitbucket.example.com:7999/pdtool/amof.git -> (pdtool, amof)
        git@bitbucket.org:myproject/myrepo.git -> (myproject, myrepo)
    """
    # Handle ssh:// format
    if ssh_url.startswith("ssh://"):
        # ssh://git@bitbucket.example.com:7999/pdtool/amof.git
        parts = ssh_url.split("/")
        if len(parts) >= 2:
            project = parts[-2]
            repo = parts[-1].replace(".git", "")
            return (project, repo)
    
    # Handle git@ format
    if ssh_url.startswith("git@"):
        # git@bitbucket.org:myproject/myrepo.git
        if ":" in ssh_url:
            path_part = ssh_url.split(":")[1]
            parts = path_part.replace(".git", "").split("/")
            if len(parts) >= 2:
                return (parts[-2], parts[-1])
    
    return None


def create_pull_request(
    project: str,
    repo_slug: str,
    title: str,
    description: str,
    source_branch: str,
    target_branch: str,
    reviewers: List[str] | None = None,
) -> dict | None:
    """Create a pull request via Bitbucket REST API.
    
    Returns the created PR data or None on failure.
    """
    auth = get_bitbucket_auth()
    if not auth:
        sys.stderr.write("[pr] Bitbucket credentials not configured\n")
        sys.stderr.write("[pr] Set BITBUCKET_USER and BITBUCKET_TOKEN in .env\n")
        return None
    
    base_url = get_bitbucket_url()
    api_url = f"{base_url}/rest/api/1.0/projects/{project}/repos/{repo_slug}/pull-requests"
    
    # Build request body
    body = {
        "title": title,
        "description": description,
        "state": "OPEN",
        "open": True,
        "closed": False,
        "fromRef": {
            "id": f"refs/heads/{source_branch}",
            "repository": {
                "slug": repo_slug,
                "project": {"key": project.upper()},
            },
        },
        "toRef": {
            "id": f"refs/heads/{target_branch}",
            "repository": {
                "slug": repo_slug,
                "project": {"key": project.upper()},
            },
        },
    }
    
    if reviewers:
        body["reviewers"] = [{"user": {"name": r}} for r in reviewers]
    
    # Make request
    try:
        credentials = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Basic {credentials}",
        }
        
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")
        
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        try:
            error_json = json.loads(error_body)
            errors = error_json.get("errors", [])
            for err in errors:
                msg = err.get("message", str(err))
                # Check if PR already exists
                if "already exists" in msg.lower() or "duplicate" in msg.lower():
                    sys.stderr.write(f"[pr] PR already exists for {source_branch}\n")
                    return {"existing": True, "message": msg}
                sys.stderr.write(f"[pr] API error: {msg}\n")
        except json.JSONDecodeError:
            sys.stderr.write(f"[pr] HTTP {e.code}: {error_body[:200]}\n")
        return None
    
    except urllib.error.URLError as e:
        sys.stderr.write(f"[pr] Connection error: {e.reason}\n")
        return None
    except Exception as e:
        sys.stderr.write(f"[pr] Error: {e}\n")
        return None


def get_commit_messages(repo_path: Path, base_branch: str, feature_branch: str) -> List[str]:
    """Get commit messages between base and feature branch."""
    code, out = run_command([
        "git", "-C", str(repo_path),
        "log", "--format=%s", f"{base_branch}..{feature_branch}"
    ])
    if code != 0:
        return []
    return [line.strip() for line in out.strip().split("\n") if line.strip()]


def cmd_pr(
    manifest: Dict[str, Any],
    reviewers: List[str] | None = None,
    dry_run: bool = False,
) -> int:
    """Create pull requests for all repos with pushed changes."""
    if not is_in_workspace():
        sys.stderr.write("[pr] Not in a workspace. Run from a workspace branch.\n")
        return 1
    
    state = get_state()
    if not state:
        sys.stderr.write("[pr] No workspace state found\n")
        return 1
    
    ticket_id = state.get("ticket_id", "unknown")
    repos = state.get("repos", [])
    
    # Check credentials unless dry run
    if not dry_run:
        auth = get_bitbucket_auth()
        if not auth:
            sys.stderr.write("[pr] Bitbucket credentials not configured\n")
            sys.stderr.write("[pr] Set BITBUCKET_USER and BITBUCKET_TOKEN in .env\n")
            sys.stderr.write("[pr] Use --dry-run to preview without credentials\n")
            return 1
    
    created_prs = []
    skipped = []
    failed = []
    
    for repo in repos:
        name = repo.get("name")
        readonly = repo.get("readonly", False)
        repo_path = Path(repo.get("path", f"repos/{name}"))
        url = repo.get("url", "")
        
        # Skip readonly repos
        if readonly:
            skipped.append((name, "readonly"))
            continue
        
        if not repo_path.exists():
            skipped.append((name, "not cloned"))
            continue
        
        # Get current branch
        current_branch = get_git_branch(repo_path)
        if not current_branch:
            skipped.append((name, "no branch"))
            continue
        
        # Get base branch from manifest
        base_branch = repo.get("branch", "main")
        
        # Skip if on base branch
        if current_branch == base_branch:
            skipped.append((name, "on base branch"))
            continue
        
        # Check for commits
        code, out = run_command([
            "git", "-C", str(repo_path),
            "rev-list", "--count", f"{base_branch}..{current_branch}"
        ])
        if code != 0:
            skipped.append((name, "cannot compare branches"))
            continue
        
        try:
            commit_count = int(out.strip())
        except ValueError:
            skipped.append((name, "invalid commit count"))
            continue
        
        if commit_count == 0:
            skipped.append((name, "no commits"))
            continue
        
        # Parse repo info from URL
        repo_info = parse_bitbucket_repo_info(url)
        if not repo_info:
            skipped.append((name, f"cannot parse URL: {url}"))
            continue
        
        project, repo_slug = repo_info
        
        # Generate PR title and description
        title = f"{ticket_id} {name}"
        
        # Get commit messages for description
        commit_msgs = get_commit_messages(repo_path, base_branch, current_branch)
        description = f"## {ticket_id}\n\n"
        description += f"Feature branch: `{current_branch}`\n"
        description += f"Target: `{base_branch}`\n\n"
        if commit_msgs:
            description += "### Changes\n\n"
            for msg in commit_msgs[:10]:  # Limit to 10 commits
                description += f"- {msg}\n"
            if len(commit_msgs) > 10:
                description += f"\n... and {len(commit_msgs) - 10} more commits\n"
        
        if dry_run:
            print(f"[pr] Would create PR for {name}:")
            print(f"     Project: {project}")
            print(f"     Repo: {repo_slug}")
            print(f"     Title: {title}")
            print(f"     {current_branch} → {base_branch}")
            print(f"     Commits: {commit_count}")
            print()
            created_prs.append((name, "dry-run", None))
            continue
        
        print(f"[pr] Creating PR for {name}...")
        result = create_pull_request(
            project=project,
            repo_slug=repo_slug,
            title=title,
            description=description,
            source_branch=current_branch,
            target_branch=base_branch,
            reviewers=reviewers,
        )
        
        if result:
            if result.get("existing"):
                skipped.append((name, "PR already exists"))
            else:
                pr_id = result.get("id")
                pr_link = result.get("links", {}).get("self", [{}])[0].get("href", "")
                if not pr_link:
                    pr_link = f"{get_bitbucket_url()}/projects/{project}/repos/{repo_slug}/pull-requests/{pr_id}"
                
                print(f"[pr] ✓ {name}: PR #{pr_id} created")
                print(f"     {pr_link}")
                
                # Update state with PR info
                for r in state.get("repos", []):
                    if r.get("name") == name:
                        r["pr_id"] = pr_id
                        r["pr_url"] = pr_link
                        break
                
                created_prs.append((name, pr_id, pr_link))
        else:
            failed.append((name, "API error"))
    
    # Save updated state
    if not dry_run and created_prs:
        save_state(state)
    
    # Summary
    print()
    if created_prs:
        if dry_run:
            print(f"[pr] Would create {len(created_prs)} PR(s)")
        else:
            print(f"[pr] Created {len(created_prs)} PR(s)")
    
    if skipped:
        print(f"[pr] Skipped {len(skipped)} repo(s):")
        for name, reason in skipped:
            print(f"     - {name}: {reason}")
    
    if failed:
        print(f"[pr] Failed {len(failed)} repo(s):")
        for name, reason in failed:
            print(f"     - {name}: {reason}")
        return 1
    
    return 0

