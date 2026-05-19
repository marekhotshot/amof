"""Shared utility functions for AMOF."""

from __future__ import annotations

import fnmatch
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, List, Tuple


def run_command(
    args: List[str],
    cwd: Path | None = None,
    timeout_seconds: float | None = None,
) -> Tuple[int, str]:
    """Run a shell command and return (exit_code, output)."""
    try:
        process = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds,
        )
        return process.returncode, process.stdout.strip()
    except subprocess.TimeoutExpired as exc:
        output = str(exc.stdout or "").strip()
        timeout_note = f"timed out after {timeout_seconds:g}s" if timeout_seconds else "timed out"
        if output:
            return 124, f"{output}\n{timeout_note}".strip()
        return 124, timeout_note


def run_with_retry(
    args: List[str],
    cwd: Path | None = None,
    max_retries: int = 3,
    backoff_factor: float = 2.0,
    initial_delay: float = 1.0,
) -> Tuple[int, str]:
    """Run a command with exponential backoff retry.
    
    Args:
        args: Command arguments
        cwd: Working directory
        max_retries: Maximum number of retries (default: 3)
        backoff_factor: Multiplier for delay between retries (default: 2.0)
        initial_delay: Initial delay in seconds (default: 1.0)
    
    Returns:
        Tuple of (exit_code, output)
    """
    delay = initial_delay
    last_code = 0
    last_out = ""
    
    for attempt in range(max_retries + 1):
        code, out = run_command(args, cwd)
        
        if code == 0:
            return code, out
        
        last_code = code
        last_out = out
        
        # Check if error is retryable (network-related)
        retryable_errors = [
            "Could not resolve host",
            "Connection refused",
            "Connection timed out",
            "Network is unreachable",
            "SSL certificate problem",
            "Failed to connect",
            "Could not read from remote",
            "unexpected disconnect",
        ]
        
        is_retryable = any(err.lower() in out.lower() for err in retryable_errors)
        
        if not is_retryable or attempt == max_retries:
            break
        
        sys.stderr.write(f"[retry] Attempt {attempt + 1}/{max_retries} failed, retrying in {delay:.1f}s...\n")
        time.sleep(delay)
        delay *= backoff_factor
    
    return last_code, last_out


def prepare_patterns(patterns: Iterable[str]) -> List[str]:
    """Prepare glob patterns for matching."""
    prepared = []
    for pattern in patterns:
        if pattern.endswith("/"):
            prepared.append(pattern + "**")
        else:
            prepared.append(pattern)
    return prepared


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    """Check if path matches any of the given patterns."""
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _git_repo_args(path: Path, *args: str) -> List[str]:
    resolved = path.resolve()
    return ["git", "-c", f"safe.directory={resolved}", *args]


def get_git_branch(path: Path) -> str | None:
    """Get current git branch for a repository."""
    code, out = run_command(_git_repo_args(path, "rev-parse", "--abbrev-ref", "HEAD"), cwd=path)
    if code == 0:
        return out
    return None


def get_git_commit(path: Path) -> str | None:
    """Get current git commit hash (short) for a repository."""
    code, out = run_command(_git_repo_args(path, "rev-parse", "--short", "HEAD"), cwd=path)
    if code == 0:
        return out
    return None


def get_git_commit_full(path: Path) -> str | None:
    """Get current git commit hash (full) for a repository."""
    code, out = run_command(_git_repo_args(path, "rev-parse", "HEAD"), cwd=path)
    if code == 0:
        return out
    return None


def is_git_dirty(path: Path) -> bool:
    """Check if repository has uncommitted changes.
    
    Excludes .amof/ from dirty checks — AMOF creates this folder in repos
    for state tracking, and it should never count as a real change.
    """
    code, out = run_command(_git_repo_args(path, "status", "--porcelain", "--", ":(exclude).amof"), cwd=path)
    return code == 0 and bool(out)


def ensure_dir(path: Path) -> None:
    """Ensure a directory exists."""
    path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Git worktree helpers
# ---------------------------------------------------------------------------

def get_git_toplevel(path: Path | None = None) -> Path | None:
    """Get the top-level directory of the git working tree at *path*."""
    if path is None:
        args = ["git", "rev-parse", "--show-toplevel"]
    else:
        args = _git_repo_args(path, "rev-parse", "--show-toplevel")
    code, out = run_command(args, cwd=path)
    if code == 0 and out:
        return Path(out)
    return None


def get_main_worktree_root(path: Path | None = None) -> Path | None:
    """Return the root of the **main** worktree (the original clone).

    Works from inside any linked worktree by parsing ``git worktree list``.
    """
    if path is None:
        args = ["git", "worktree", "list", "--porcelain"]
    else:
        args = _git_repo_args(path, "worktree", "list", "--porcelain")
    code, out = run_command(args, cwd=path)
    if code != 0:
        return None
    # First "worktree <path>" line is always the main worktree.
    for line in out.splitlines():
        if line.startswith("worktree "):
            return Path(line.split(" ", 1)[1])
    return None


def is_linked_worktree(path: Path | None = None) -> bool:
    """Return True if *path* is inside a **linked** (non-main) git worktree.

    Linked worktrees have a ``.git`` *file* (not directory) that points back
    to the main repository's ``.git/worktrees/<name>/`` directory.
    """
    toplevel = get_git_toplevel(path)
    if toplevel is None:
        return False
    dot_git = toplevel / ".git"
    # Linked worktrees have a .git *file*; the main worktree has a .git *dir*.
    return dot_git.is_file()


def list_worktrees(path: Path | None = None) -> list[dict[str, str]]:
    """List all worktrees for the repository.

    Returns a list of dicts with keys: ``path``, ``branch``, ``head``, ``bare``.
    """
    if path is None:
        args = ["git", "worktree", "list", "--porcelain"]
    else:
        args = _git_repo_args(path, "worktree", "list", "--porcelain")
    code, out = run_command(args, cwd=path)
    if code != 0:
        return []

    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line.split(" ", 1)[1]}
        elif line.startswith("HEAD "):
            current["head"] = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            # branch refs/heads/workspace/my-project -> workspace/my-project
            ref = line.split(" ", 1)[1]
            current["branch"] = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line == "bare":
            current["bare"] = "true"
        elif line == "detached":
            current["branch"] = "(detached)"
    if current:
        worktrees.append(current)
    return worktrees


def get_worktree_dir(ecosystem: str, main_root: Path | None = None) -> Path:
    """Return the conventional worktree directory for an ecosystem.

    Layout: ``<main_root>/worktrees/<ecosystem>``
    """
    if main_root is None:
        main_root = get_main_worktree_root() or Path(".")
    return main_root / "worktrees" / ecosystem


def get_ecosystem_from_branch(path: Path | None = None) -> str | None:
    """Infer ecosystem name from the current ``workspace/<eco>`` branch."""
    branch = get_git_branch(path or Path("."))
    if branch and branch.startswith("workspace/"):
        return branch.split("/", 1)[1]
    return None


def get_ecosystem_from_path() -> str | None:
    """Infer ecosystem from worktree directory path.

    Uses ``AMOF_CWD`` (set by the shell wrapper) or ``cwd`` to detect if
    we are inside ``$AMOF_ROOT/worktrees/<eco>/...``.  If the matching
    ``ecosystems/<eco>/ecosystem.yaml`` exists, returns ``<eco>``.
    """
    import os

    amof_root = os.environ.get("AMOF_ROOT", "")
    cwd = os.environ.get("AMOF_CWD", os.getcwd())

    if not amof_root:
        return None

    prefix = amof_root.rstrip("/") + "/worktrees/"
    if not cwd.startswith(prefix):
        return None

    rel = cwd[len(prefix):]
    eco_name = rel.split("/", 1)[0]
    if not eco_name:
        return None

    # Verify that an ecosystem manifest actually exists for this name
    manifest_path = Path("ecosystems") / eco_name / "ecosystem.yaml"
    if manifest_path.exists():
        return eco_name

    return None


def normalize_branch_prefix(prefix: str | None, default: str = "feature") -> str:
    """Normalize branch prefix values from manifest config.

    Converts values like "feat/" to "feat" to prevent accidental double slashes
    when callers build branch names as "<prefix>/<ticket>".
    """
    value = (prefix or default).strip()
    value = value.rstrip("/")
    return value or default

