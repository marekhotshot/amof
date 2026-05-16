"""Ecosystem commands - manage persistent branch templates."""

from __future__ import annotations

import sys
from pathlib import Path

from ..utils import run_command


def cmd_ecosystem(args) -> int:
    """Manage ecosystems."""
    if args.ecosystem_cmd == "create":
        return cmd_ecosystem_create(args.name, args.from_branch)
    elif args.ecosystem_cmd == "list":
        return cmd_ecosystem_list()
    else:
        print("Usage: amof ecosystem {create,list}")
        return 1


def cmd_ecosystem_create(name: str, from_branch: str = "main") -> int:
    """Create a new ecosystem branch."""
    branch_name = f"feature/ecosystem-{name}"
    
    code, out = run_command(["git", "branch", "--list", branch_name])
    if out.strip():
        sys.stderr.write(f"[ecosystem] Branch {branch_name} already exists\n")
        return 1
    
    print(f"[ecosystem] Creating {branch_name} from {from_branch}...")
    code, out = run_command(["git", "checkout", "-b", branch_name, from_branch])
    if code != 0:
        sys.stderr.write(f"[ecosystem] Failed to create branch: {out}\n")
        return 1
    
    ecosystem_dir = Path(f"ecosystems/{name}")
    ecosystem_dir.mkdir(parents=True, exist_ok=True)
    
    manifest_file = ecosystem_dir / "ecosystem.yaml"
    manifest_file.write_text(f'''# Ecosystem: {name}
name: {name}
description: ""

actors: []

components: {{}}
''')
    
    (ecosystem_dir / "playbooks").mkdir(exist_ok=True)
    (ecosystem_dir / "kb").mkdir(exist_ok=True)
    (ecosystem_dir / "diagrams").mkdir(exist_ok=True)
    
    (ecosystem_dir / "playbooks" / "README.md").write_text(
        "# Playbooks\n\nStep-by-step guides for this ecosystem.\n"
    )
    (ecosystem_dir / "kb" / "README.md").write_text(
        "# Knowledge Base\n\nReference documentation for this ecosystem.\n"
    )
    
    print(f"[ecosystem] Created ecosystem structure in {ecosystem_dir}")
    print(f"[ecosystem] Edit {manifest_file} to configure actors")
    
    run_command(["git", "add", str(ecosystem_dir)])
    run_command(["git", "commit", "-m", f"Create ecosystem: {name}"])
    
    print(f"\n[ecosystem] Ecosystem {name} created on branch {branch_name}")
    return 0


def cmd_ecosystem_list() -> int:
    """List available ecosystems (folders with ecosystem.yaml)."""
    ecosystems_dir = Path("ecosystems")

    from amof.manifest import list_available_ecosystems

    ecosystems = list_available_ecosystems()
    
    if not ecosystems:
        print("No ecosystems found.")
        print("Create ecosystem folder: ecosystems/<name>/ecosystem.yaml")
        return 0
    
    print("Available ecosystems:")
    for name in sorted(ecosystems):
        manifest_path = ecosystems_dir / name / "ecosystem.yaml"
        # Try to read description from manifest
        desc = ""
        try:
            content = manifest_path.read_text()
            for line in content.splitlines():
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"\'')
                    break
        except Exception:
            pass
        
        if desc:
            print(f"  {name:20} - {desc}")
        else:
            print(f"  {name}")
    
    return 0

