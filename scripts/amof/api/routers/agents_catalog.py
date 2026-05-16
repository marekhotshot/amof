"""Agents catalog API: list available agent personas from prompts/agents or .cursor/agents."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from amof.api.command_builder import get_workspace_root

router = APIRouter(prefix="/agents", tags=["agents"])

# Frontmatter pattern: ---\n...name: x\n...description: y...
_FRONTMATTER = re.compile(
    r"^---\s*\n(.*?)\n---",
    re.DOTALL,
)
_NAME = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
_DESC = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


def _parse_agent_md(path: Path) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = _FRONTMATTER.match(text)
    if not m:
        return {"name": path.stem, "description": "", "source": str(path)}
    block = m.group(1)
    name_match = _NAME.search(block)
    desc_match = _DESC.search(block)
    return {
        "name": (name_match.group(1).strip() if name_match else path.stem),
        "description": (desc_match.group(1).strip() if desc_match else ""),
        "source": path.name,
    }


@router.get("")
def list_agents() -> Dict[str, List[Dict[str, Any]]]:
    """List available agent personas from prompts/agents and .cursor/agents."""
    root = get_workspace_root()
    seen: set[str] = set()
    agents: List[Dict[str, Any]] = []

    for subdir in ("prompts/agents", ".cursor/agents"):
        dir_path = root / subdir
        if not dir_path.is_dir():
            continue
        for path in sorted(dir_path.glob("*.md")):
            parsed = _parse_agent_md(path)
            if parsed and parsed["name"] not in seen:
                seen.add(parsed["name"])
                agents.append(parsed)

    return {"agents": agents}
