"""Sync command - synchronize repositories from manifest."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from ..utils import run_command, run_with_retry
from ..state import get_effective_repos, is_in_workspace, get_state
from ..worktree_manager import switch_to_ticket


def _load_dotenv(env_path: Path) -> None:
    """Load .env into os.environ (only set if not already set). Handles KEY=val and export KEY=val."""
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if val.startswith("'") and val.endswith("'") or val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass


def _github_https_auth_url(url: str, token: str) -> str:
    """Return HTTPS URL with token for GitHub (e.g. for fetch/clone)."""
    if not url.startswith("https://") or "github.com" not in url:
        return url
    # https://github.com/org/repo.git -> https://TOKEN@github.com/org/repo.git
    if "@" in url.split("//", 1)[-1]:
        return url  # already has auth
    return url.replace("https://", f"https://{token}@", 1)


def cmd_sync(manifest: Dict[str, Any], only: set[str] | None = None) -> int:
    """Synchronize repositories defined in ecosystem.yaml."""
    # Load .env from cwd or from platform root (where script lives: scripts/amof/commands/sync.py)
    for env_path in [Path.cwd() / ".env", Path(__file__).resolve().parent.parent.parent.parent / ".env"]:
        _load_dotenv(env_path)
        if os.environ.get("GITHUB_TOKEN"):
            break

    repos = get_effective_repos(manifest)
    if not repos:
        print("[sync] No repositories defined (or none enabled). Nothing to sync.")
        return 0

    token = os.environ.get("GITHUB_TOKEN")
    overall = 0
    for repo in repos:
        name = repo.get("name")
        if only and name not in only:
            continue
        url = repo.get("url")
        branch = repo.get("branch", "main")
        path = Path(repo.get("path", name))

        if not name or not url:
            sys.stderr.write("Skipping repo with missing name or url in manifest.\n")
            overall = 1
            continue

        clone_or_fetch_url = _github_https_auth_url(url, token) if token else url
        actions: List[str] = []
        if not path.exists():
            # Clone with retry (network operation)
            code, out = run_with_retry(["git", "clone", clone_or_fetch_url, str(path)])
            if code != 0:
                sys.stderr.write(f"[sync:{name}] clone failed: {out}\n")
                overall = 1
                continue
            actions.append("cloned")
            # Ensure origin URL is stored without token
            run_command(["git", "-C", str(path), "remote", "set-url", "origin", url])
        else:
            # Temporarily set origin URL with token for fetch/pull if GitHub HTTPS
            if token and url.startswith("https://") and "github.com" in url:
                run_command(["git", "-C", str(path), "remote", "set-url", "origin", clone_or_fetch_url])
            code, out = run_with_retry(["git", "-C", str(path), "fetch", "--all"])
            if code != 0:
                sys.stderr.write(f"[sync:{name}] fetch failed: {out}\n")
                if "could not read Username" in out or "Authentication failed" in out:
                    sys.stderr.write("[sync] Hint: for private GitHub repos, run 'source .env' first (GITHUB_TOKEN) or set GITHUB_TOKEN.\n")
                overall = 1
                if token and url.startswith("https://") and "github.com" in url:
                    run_command(["git", "-C", str(path), "remote", "set-url", "origin", url])
                continue
            actions.append("fetched")

        code, out = run_command(["git", "-C", str(path), "checkout", branch])
        if code != 0:
            sys.stderr.write(f"[sync:{name}] checkout failed: {out}\n")
            overall = 1
            continue
        actions.append(f"checked out {branch}")

        # Pull with retry (network operation)
        code, out = run_with_retry(["git", "-C", str(path), "pull", "origin", branch])
        if code != 0:
            sys.stderr.write(f"[sync:{name}] pull failed: {out}\n")
            overall = 1
            continue
        actions.append("updated")

        # If in workspace with active ticket, checkout the feature branch for this repo
        if is_in_workspace() and not repo.get("readonly", False):
            state = get_state()
            active_ticket = state.get("active_ticket")
            if active_ticket:
                ticket_repos = state.get("tickets", {}).get(active_ticket, {}).get("repos", {})
                feature_branch = ticket_repos.get(name)
                if feature_branch:
                    workspace_root = Path.cwd()
                    try:
                        switch_to_ticket(path, feature_branch, active_ticket, name, workspace_root)
                        actions.append(f"worktree ready for {feature_branch}")
                    except Exception as e:
                        sys.stderr.write(f"[sync:{name}] failed to create worktree: {e}\n")

        print(f"[sync] {name} ({path}): {', '.join(actions)}")

        # Restore origin URL without token (we set it for fetch/pull when using GITHUB_TOKEN)
        if path.exists() and token and url.startswith("https://") and "github.com" in url:
            run_command(["git", "-C", str(path), "remote", "set-url", "origin", url])

    # Auto-profile repos after sync
    try:
        from .profile import cmd_profile
        print("[sync] Updating repo profiles...")
        cmd_profile(manifest, repo_name=None, all_repos=True)
    except Exception as e:
        sys.stderr.write(f"[sync] Profile generation failed (non-fatal): {e}\n")

    return overall

