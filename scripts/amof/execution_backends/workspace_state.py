"""Workspace resolution and Git change accounting for execution backends."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

def _workspace_for(selection: Any, manifest: dict[str, Any]) -> Path:
    if selection.readable_root:
        path = Path(selection.readable_root).expanduser().resolve(strict=False)
        if path.is_dir():
            return path
    if selection.writable_roots:
        first_scope = Path(selection.writable_roots[0]).resolve(strict=False)
        if first_scope.is_dir():
            return first_scope
        for parent in first_scope.parents:
            if parent.is_dir():
                return parent
    repos = manifest.get("repos")
    if isinstance(repos, list):
        for item in repos:
            if isinstance(item, dict):
                path = Path(str(item.get("path") or "")).expanduser().resolve(strict=False)
                if path.is_dir():
                    return path
    return Path.cwd().resolve(strict=False)


def _workspace_repo_roots(workspace: Path) -> list[Path]:
    """Git roots governed by a workspace: the workspace itself when it is a
    repository, else its direct child repositories (multi-target dispatch
    workspaces materialize one pinned checkout per target)."""
    if (workspace / ".git").exists():
        return [workspace]
    if not workspace.is_dir():
        return []
    return sorted(
        child for child in workspace.iterdir() if (child / ".git").exists()
    )


def _changed_paths(workspace: Path) -> list[str]:
    paths: list[str] = []
    for repo_root in _workspace_repo_roots(workspace):
        completed = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines():
            item = line[3:].strip()
            if item:
                paths.append(item)
    return list(dict.fromkeys(paths))


def _changed_paths_delta(before: list[str], after: list[str]) -> list[str]:
    before_set = {item for item in before if item}
    after_set = {item for item in after if item}
    return sorted(after_set - before_set)


def _read_only_unclean_workspace_message(preexisting_changed_paths: list[str]) -> str:
    """Describe the unclean workspace without claiming tracked-only when untracked may be present.

    `_changed_paths` uses `git status --short --untracked-files=all`, so the sample
    may include modified tracked files and/or untracked paths.
    """
    paths = [str(item).strip() for item in preexisting_changed_paths if str(item).strip()]
    if not paths:
        return (
            "Read-only run blocked before execution because the workspace is not clean."
        )
    sample = ", ".join(paths[:5])
    more = f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""
    return (
        "Read-only run blocked before execution because the workspace has pre-existing "
        f"changes (modified and/or untracked): {sample}{more}."
    )


def _restore_read_only_paths(workspace: Path, paths: list[str]) -> list[str]:
    restored: list[str] = []
    if not paths:
        return restored
    for repo_root in _workspace_repo_roots(workspace):
        for rel_path in sorted({item for item in paths if item}):
            target = repo_root / rel_path
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", rel_path],
                cwd=str(repo_root),
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            ).returncode == 0
            if tracked:
                dirty = subprocess.run(
                    ["git", "status", "--short", "--untracked-files=all", "--", rel_path],
                    cwd=str(repo_root),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                ).stdout.strip()
                if not dirty:
                    continue
                subprocess.run(
                    ["git", "restore", "--staged", "--worktree", "--", rel_path],
                    cwd=str(repo_root),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                restored.append(rel_path)
                continue
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
                restored.append(rel_path)
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                restored.append(rel_path)
    return sorted(dict.fromkeys(restored))


