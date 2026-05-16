"""Actor commands - manage actors/customers in ecosystem manifest."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ..manifest import get_manifest_path


def cmd_actor(args, manifest: Dict[str, Any], ecosystem: Optional[str] = None) -> int:
    """Manage actors in ecosystem manifest."""
    if args.actor_cmd == "add":
        return cmd_actor_add(args.id, args.name, args.role, args.status, ecosystem)
    elif args.actor_cmd == "list":
        return cmd_actor_list(manifest)
    elif args.actor_cmd == "update":
        return cmd_actor_update(args.id, args.status, ecosystem)
    else:
        print("Usage: amof -e <ecosystem> actor {add,list,update}")
        return 1


def cmd_actor_add(actor_id: str, name: str | None, role: str, status: str, ecosystem: Optional[str] = None) -> int:
    """Add an actor to the ecosystem manifest."""
    manifest_path = get_manifest_path(ecosystem)
    if not manifest_path.exists():
        sys.stderr.write(f"[actor] Manifest not found at {manifest_path}\n")
        return 1
    
    content = manifest_path.read_text()
    
    actor_entry = f'''
  - id: {actor_id}
    name: {name or actor_id.upper()}
    role: {role}
    status: {status}
    description: ""
    apps: []
    paths:
      helm_values: "helm-values/{actor_id}-values.yaml"
      env_config: "env/{actor_id}-config.json5"
      stack: "lib/common/apps/stacks/{actor_id}/"
'''
    
    if "actors: []" in content:
        content = content.replace("actors: []", f"actors:{actor_entry}")
    elif "actors:" in content:
        lines = content.split("\n")
        new_lines = []
        in_actors = False
        for line in lines:
            new_lines.append(line)
            if line.strip().startswith("actors:"):
                in_actors = True
            elif in_actors and line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                new_lines.insert(-1, actor_entry.rstrip())
                in_actors = False
        if in_actors:
            new_lines.append(actor_entry.rstrip())
        content = "\n".join(new_lines)
    
    manifest_path.write_text(content)
    print(f"[actor] Added {actor_id} to {manifest_path}")
    print(f"[actor] Edit the manifest to fill in details")
    return 0


def cmd_actor_list(manifest: Dict[str, Any]) -> int:
    """List actors in the ecosystem manifest."""
    actors = manifest.get("actors", [])
    
    if not actors:
        print("No actors defined in this ecosystem.")
        print("Add one with: amof -e <ecosystem> actor add <id>")
        return 0
    
    print(f"{'ID':<12} {'NAME':<15} {'ROLE':<12} {'STATUS':<12}")
    print("-" * 55)
    
    for actor in actors:
        print(f"{actor.get('id', ''):<12} {actor.get('name', ''):<15} "
              f"{actor.get('role', ''):<12} {actor.get('status', ''):<12}")
    
    return 0


def cmd_actor_update(actor_id: str, status: str, ecosystem: Optional[str] = None) -> int:
    """Update actor status in manifest."""
    manifest_path = get_manifest_path(ecosystem)
    if not manifest_path.exists():
        sys.stderr.write(f"[actor] Manifest not found at {manifest_path}\n")
        return 1
    
    content = manifest_path.read_text()
    
    lines = content.split("\n")
    in_actor = False
    updated = False
    
    for i, line in enumerate(lines):
        if f"id: {actor_id}" in line:
            in_actor = True
        elif in_actor and line.strip().startswith("status:"):
            lines[i] = line.split(":")[0] + f": {status}"
            updated = True
            in_actor = False
        elif in_actor and line.strip().startswith("- id:"):
            in_actor = False
    
    if not updated:
        sys.stderr.write(f"[actor] Actor {actor_id} not found\n")
        return 1
    
    manifest_path.write_text("\n".join(lines))
    print(f"[actor] Updated {actor_id} status to {status}")
    return 0
