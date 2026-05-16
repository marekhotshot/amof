"""Uninstall the locally installed AMOF CLI without deleting the repo."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .doctor import _detect_layout
from .update import InstallInfo, detect_install_method


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
    install_info: InstallInfo | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    info = install_info or detect_install_method()
    yes = bool(getattr(args, "yes", False)) if args is not None else False
    if info.method == "pipx":
        pipx = which("pipx")
        print("[uninstall] Detected pipx-managed AMOF install.")
        print("[uninstall] Repo contents and AMOF app-data will be left intact.")
        if not pipx:
            sys.stderr.write("[uninstall] pipx is not on PATH, so AMOF will not run pip uninstall inside the venv.\n")
            sys.stderr.write("[uninstall] Run this after pipx is available:\n")
            sys.stderr.write("  pipx uninstall amof\n")
            return 1
        if not yes and not _confirm("Proceed with pipx uninstall amof?"):
            print("[uninstall] Cancelled.")
            return 1
        completed = runner(
            [pipx, "uninstall", "amof"],
            capture_output=True,
            text=True,
            check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            sys.stderr.write(output.strip() + "\n" if output.strip() else "[uninstall] pipx uninstall failed\n")
            return completed.returncode or 1
        if output.strip():
            print(output.strip())
        print("[uninstall] AMOF CLI removed via pipx. Repo contents and AMOF app-data were left intact.")
        return 0

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


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}
