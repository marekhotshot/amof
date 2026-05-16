"""Uninstall the locally installed AMOF CLI without deleting the repo."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .doctor import _detect_layout


def _resolve_repo_root(start_path: Path | None = None) -> Path:
    layout_mode, workspace = _detect_layout(start_path)
    if layout_mode == "split_workspace":
        return (workspace / "repos" / "amof").resolve()
    return workspace.resolve()


def _interactive_shell_amof_resolution(shell_bin: str | None = None) -> tuple[str | None, str | None]:
    shell = shell_bin or shutil.which("bash") or sys.executable
    completed = subprocess.run(
        [shell, "-ic", "type -t amof 2>/dev/null || true"],
        capture_output=True,
        text=True,
        check=False,
    )
    detected_type = (completed.stdout or "").strip().splitlines()
    resolution_type = detected_type[-1].strip() if detected_type else ""
    if resolution_type not in {"function", "alias"}:
        return None, None
    detail = subprocess.run(
        [shell, "-ic", "type amof 2>/dev/null || true"],
        capture_output=True,
        text=True,
        check=False,
    )
    return resolution_type, (detail.stdout or detail.stderr or "").strip() or None


def cmd_uninstall(
    args: Any | None = None,
    *,
    repo_root: Path | None = None,
    python_executable: str | None = None,
) -> int:
    target_root = (repo_root or _resolve_repo_root()).resolve()
    local_venv = target_root / ".venv"
    egg_info = target_root / "scripts" / "amof.egg-info"
    python_bin = python_executable or sys.executable

    print(f"[uninstall] Repo root: {target_root}")
    completed = subprocess.run(
        [python_bin, "-m", "pip", "uninstall", "-y", "amof"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        sys.stderr.write(output.strip() + "\n" if output.strip() else "[uninstall] pip uninstall failed\n")
        return 1
    if output.strip():
        print(output.strip())

    if egg_info.exists():
        shutil.rmtree(egg_info, ignore_errors=True)
        print(f"[uninstall] Removed generated metadata: {egg_info}")

    if local_venv.exists():
        shutil.rmtree(local_venv, ignore_errors=True)
        print(f"[uninstall] Removed local virtualenv: {local_venv}")

    resolution_type, resolution_detail = _interactive_shell_amof_resolution()
    if resolution_type:
        print(f"[uninstall] WARN: leftover shell {resolution_type} named 'amof' detected")
        if resolution_detail:
            print(f"[uninstall] WARN: {resolution_detail}")
        print("[uninstall] WARN: remove the old shell override or open a fresh shell.")

    print("[uninstall] AMOF CLI removed. Repo contents were left intact.")
    return 0
