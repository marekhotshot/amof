"""Spin command: deploy or destroy infrastructure via provisioner."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


def cmd_spin(manifest: Dict[str, Any], action: str, ecosystem: str) -> int:
    """Run provisioner (e.g. aws-spin) for deploy or destroy.

    Reads provisioner from manifest, constructs path to provisioners/<name>/spin.sh,
    and executes it with ACTION and ecosystem context. Streams stdout/stderr in real-time.

    Root for ecosystems/ and provisioners/ is AMOF_CWD (if set) else Path.cwd().
    The amof shell alias sets AMOF_CWD to the directory where you ran the command,
    so spin works from worktrees even when Python runs from the main repo.
    """
    provisioner = manifest.get("provisioner")
    if not provisioner:
        sys.stderr.write("Error: No provisioner defined in ecosystem.yaml\n")
        sys.stderr.write("Add: provisioner: aws-spin\n")
        return 1

    if action not in ("deploy", "destroy"):
        sys.stderr.write("Error: ACTION must be 'deploy' or 'destroy'\n")
        return 1

    # Prefer AMOF_CWD (set by amof alias to the dir where user ran the command)
    # so worktrees work when the alias cd's to the main repo to run Python.
    root_cwd = os.environ.get("AMOF_CWD")
    root = Path(root_cwd).resolve() if root_cwd else Path.cwd()
    eco_path = root / "ecosystems" / ecosystem
    if not eco_path.exists() or not eco_path.is_dir():
        sys.stderr.write(f"Error: Ecosystem path not found: {eco_path}\n")
        return 1

    spin_script = root / "provisioners" / provisioner / "spin.sh"
    if not spin_script.exists() or not spin_script.is_file():
        sys.stderr.write(f"Error: Provisioner script not found: {spin_script}\n")
        sys.stderr.write(f"Expected provisioners/{provisioner}/spin.sh\n")
        return 1

    # Use bash if script is not executable (e.g. in some CI environments)
    # k3d provisioner expects ecosystem name as the second argument.
    # Existing provisioners still accept ecosystem path.
    if provisioner == "k3d":
        provisioner_arg = ecosystem
    else:
        provisioner_arg = str(eco_path.resolve())

    if spin_script.stat().st_mode & 0o111:
        cmd = [str(spin_script), action, provisioner_arg]
    else:
        cmd = ["bash", str(spin_script), action, provisioner_arg]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(root),
    )
    for line in proc.stdout or []:
        sys.stdout.write(line)
        sys.stdout.flush()
    proc.wait()
    return proc.returncode
