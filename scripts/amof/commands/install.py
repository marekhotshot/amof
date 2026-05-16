"""Install command - bootstrap a clean ecosystem workspace using git worktrees."""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from ..utils import (
    run_command,
    get_main_worktree_root,
    get_worktree_dir,
    is_linked_worktree,
    list_worktrees,
)
from ..state import create_workspace_state, save_state
from .sync import cmd_sync
from .workspace import cmd_workspace, get_workspace_filename, setup_shell_aliases


def rollback_install(worktree_path: Path, workspace_branch: str, main_root: Path) -> None:
    """Rollback a failed install by removing the worktree and branch."""
    print("\n[install] Rolling back...")

    # Clean up generated content inside the worktree
    for cleanup_path in [worktree_path / "repos", worktree_path / "context", worktree_path / ".amof"]:
        if cleanup_path.exists():
            shutil.rmtree(cleanup_path)

    for ws_file in worktree_path.glob("amof.*.code-workspace"):
        ws_file.unlink()

    # Remove the worktree, then delete the branch
    os.chdir(main_root)
    run_command(["git", "worktree", "remove", "--force", str(worktree_path)])
    run_command(["git", "branch", "-D", workspace_branch])
    print("[rollback] Done. You are in the main repo.")


def cmd_install(
    manifest: Dict[str, Any],
    push: bool = False,
    dry_run: bool = False,
    ecosystem: str | None = None,
) -> int:
    """Bootstrap a clean ecosystem workspace as a git worktree."""
    if not ecosystem:
        sys.stderr.write("[install] Ecosystem name is required.\n")
        sys.stderr.write("[install] Usage: amof -e <ecosystem> install\n")
        return 1

    all_repos = manifest.get("repos", [])
    repos = [r for r in all_repos if r.get("enabled", True)]

    if not repos:
        print("[install] No enabled repositories in manifest (empty workspace).")

    # Resolve the main repo root (works from main or any linked worktree)
    main_root = get_main_worktree_root()
    if main_root is None:
        sys.stderr.write("[install] Not in a git repository.\n")
        return 1

    workspace_branch = f"workspace/{ecosystem}"
    worktree_path = get_worktree_dir(ecosystem, main_root)

    # Check if a worktree for this ecosystem already exists
    for wt in list_worktrees(main_root):
        if wt.get("branch") == workspace_branch:
            sys.stderr.write(f"[install] Workspace '{workspace_branch}' already exists.\n")
            sys.stderr.write(f"[install] Worktree at: {wt.get('path')}\n")
            sys.stderr.write("[install] cd there and run 'amof sync' to update, or 'amof discard' to start fresh.\n")
            return 1

    if dry_run:
        print("[dry-run] Would perform:")
        print(f"  1. Create worktree: {worktree_path}")
        print(f"  2. On branch: {workspace_branch}")
        print(f"  3. Sync {len(repos)} repositories")
        for repo in repos:
            mode = "RO" if repo.get("readonly", False) else "RW"
            print(f"     - {repo.get('name')} ({mode})")
        print("  4. Commit workspace state")
        if push:
            print("  5. Push to origin")
        print(f"  6. Generate {get_workspace_filename(ecosystem)}")
        print("\nRun without --dry-run to execute.")
        return 0

    print(f"[install] Creating workspace: {workspace_branch}")
    print(f"[install] Worktree path: {worktree_path}")

    # Ensure parent directory exists
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Create worktree with a new branch based on current HEAD
    code, out = run_command(
        ["git", "worktree", "add", "-b", workspace_branch, str(worktree_path)],
        cwd=main_root,
    )
    if code != 0:
        sys.stderr.write(f"[install] Failed to create worktree: {out}\n")
        return 1

    # Switch into the worktree for all subsequent operations
    os.chdir(worktree_path)

    # Seed the new workspace state before the first sync so repo resolution
    # uses this ecosystem's manifest instead of inherited tracked state.
    state = create_workspace_state(
        ecosystem=ecosystem,
        workspace_branch=workspace_branch,
        repos=repos,
    )
    save_state(state)

    print("[install] Syncing repositories...")
    if cmd_sync(manifest) != 0:
        sys.stderr.write("[install] Sync failed!\n")
        rollback_install(worktree_path, workspace_branch, main_root)
        return 1

    # Auto-profile repos after sync
    try:
        from .profile import cmd_profile
        print("[install] Generating repo profiles...")
        cmd_profile(manifest, repo_name=None, all_repos=True)
    except Exception as e:
        sys.stderr.write(f"[install] Profile generation failed (non-fatal): {e}\n")

    # Auto-index codebase (Merkle tree + LLM descriptions if API key available)
    # Scope is bounded by the ecosystem manifest: only enabled, on-disk repos
    # listed in ecosystem.yaml participate. This prevents one ecosystem's
    # indexing from silently picking up sibling ecosystems' files.
    try:
        from ..orchestrator.indexer import CodebaseIndexer
        from ..orchestrator.manifest_scope import resolve_scope

        repos_root = Path("repos")
        index_dir = Path(f"ecosystems/{ecosystem}/index")

        scope = resolve_scope(manifest, Path.cwd(), ecosystem=ecosystem)
        if scope.is_empty():
            print(
                f"[install] Indexing scope is empty for ecosystem={ecosystem} "
                f"(skipped={scope.skipped})."
            )
        else:
            print(
                f"[install] Indexing scope: {scope.repo_count} repo(s) "
                f"({', '.join(p.name for p in scope.repo_roots)})"
            )

        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if api_key and not scope.is_empty():
            print("[install] Indexing codebase...")
            try:
                from ..orchestrator.llm.anthropic import AnthropicClient
                indexer_llm = AnthropicClient(
                    api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
                    model=os.environ.get("AMOF_PLANNER_MODEL", "claude-sonnet-4-5"),
                )
                indexer = CodebaseIndexer(
                    indexer_llm=indexer_llm,
                    repos_root=repos_root,
                    index_dir=index_dir,
                    repo_roots=scope.repo_roots,
                    ecosystem_name=ecosystem,
                )
                idx = indexer.index(force=True)
                print(f"[install] Indexed {idx.file_count} files (${idx.indexing_cost:.4f})")
            except Exception as ie:
                sys.stderr.write(f"[install] LLM indexing failed (non-fatal): {ie}\n")
                indexer = CodebaseIndexer(
                    indexer_llm=type("Stub", (), {"model_name": lambda s: "stub"})(),
                    repos_root=repos_root,
                    index_dir=index_dir,
                    repo_roots=scope.repo_roots,
                    ecosystem_name=ecosystem,
                )
                indexer.build_tree_only()
                print("[install] Merkle tree saved (index will be created on first 'amof agent' run)")
        elif not scope.is_empty():
            print("[install] Skipping codebase index (no API key). Will index on first 'amof agent' run.")
            indexer = CodebaseIndexer(
                indexer_llm=type("Stub", (), {"model_name": lambda s: "stub"})(),
                repos_root=repos_root,
                index_dir=index_dir,
                repo_roots=scope.repo_roots,
                ecosystem_name=ecosystem,
            )
            indexer.build_tree_only()
    except Exception as e:
        sys.stderr.write(f"[install] Indexing setup failed (non-fatal): {e}\n")

    print("[install] Saving workspace state...")
    state["last_modified"] = datetime.now().isoformat()
    save_state(state)

    run_command(["git", "add", "-A"])
    code, out = run_command(["git", "commit", "-m", f"Initialize workspace for {ecosystem}"])
    if code != 0 and "nothing to commit" not in out:
        sys.stderr.write(f"[install] Commit failed: {out}\n")
        return 1

    if push:
        print(f"[install] Pushing {workspace_branch}...")
        code, out = run_command(["git", "push", "-u", "origin", workspace_branch])
        if code != 0:
            sys.stderr.write(f"[install] Push failed: {out}\n")

    cmd_workspace(manifest, ecosystem)
    setup_shell_aliases()

    ws_file = get_workspace_filename(ecosystem)

    print(f"\n[install] Workspace ready!")
    print(f"  Ecosystem: {ecosystem}")
    print(f"  Branch:    {workspace_branch}")
    print(f"  Worktree:  {worktree_path}")
    print(f"  Repos:     {len(repos)}")
    print(f"\n  Enter workspace:  cd {worktree_path}")
    print(f"  Start a ticket:   amof ticket start <TICKET-ID>")
    print(f"  Open in Cursor:   cursor {worktree_path / ws_file}")
    return 0
