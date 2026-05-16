"""Initialize AMOF metadata for an existing repository."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..app_config import adopt_repo_binding


def _run_git(path: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _detect_git_root(path: Path) -> Path | None:
    output = _run_git(path, "rev-parse", "--show-toplevel")
    if not output:
        return None
    return Path(output).resolve(strict=False)


def _detect_current_ref(git_root: Path) -> str:
    branch = _run_git(git_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != "HEAD":
        return branch
    ref = _run_git(git_root, "rev-parse", "--short", "HEAD")
    return ref or "main"


def _normalize_ecosystem_name(raw: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw.strip()).strip("-._").lower()
    if not normalized:
        raise ValueError("ecosystem name is required")
    return normalized


def cmd_init(args: Any) -> int:
    """Adopt an existing git repository into AMOF app-data."""

    if not getattr(args, "adopt", None):
        sys.stderr.write("[init] Nothing to initialize. Run: amof init --adopt .\n")
        return 1
    if bool(getattr(args, "write_local", False)):
        sys.stderr.write("[init] --write-local is not implemented yet; default adoption uses AMOF app-data only.\n")
        return 1

    target = Path(str(getattr(args, "adopt"))).expanduser().resolve(strict=False)
    if not target.exists():
        sys.stderr.write(f"[init] Path does not exist: {target}\n")
        return 1

    git_root = _detect_git_root(target)
    if git_root is None:
        sys.stderr.write(f"[init] Path is not inside a git repository: {target}\n")
        sys.stderr.write("Run this from a git checkout, then retry: amof init --adopt .\n")
        return 1

    repo_name = git_root.name
    try:
        ecosystem_name = _normalize_ecosystem_name(getattr(args, "name", None) or repo_name)
    except ValueError as exc:
        sys.stderr.write(f"[init] {exc}\n")
        return 1

    default_ref = _detect_current_ref(git_root)
    try:
        entry = adopt_repo_binding(
            git_root=git_root,
            ecosystem=ecosystem_name,
            repo_name=repo_name,
            default_ref=default_ref,
        )
    except ValueError as exc:
        sys.stderr.write(f"[init] {exc}\n")
        return 1

    print(f"[init] Adopted repository: {entry['git_root']}")
    print(f"[init] Ecosystem: {entry['ecosystem']}")
    print(f"[init] Manifest source: {entry['manifest_source']}")
    print()
    print("Next commands:")
    print("  amof doctor")
    print('  amof agent --plan "Inspect this repo"')
    return 0
