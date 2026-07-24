"""LS tool -- mirrors Cursor's LS tool.

Lists files and directories in a given path, excluding dot-files/dirs.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Tool, ToolResult, resolve_tool_path


class LSTool(Tool):
    name = "LS"
    description = (
        "Lists files and directories in a given path. Excludes dot-files "
        "and dot-directories. Supports ignore glob patterns. Prefer the "
        "absolute materialized Repository Path from the mission; the bare "
        "alias /workspace is rewritten to the runner workspace root."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "target_directory": {
                "type": "string",
                "description": (
                    "Absolute path to directory to list. Use the materialized "
                    "Repository Path (for example under "
                    "/var/lib/amof/share/workspaces/...). Bare /workspace is "
                    "rewritten to the runner workspace root."
                ),
            },
            "ignore_globs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Glob patterns to ignore.",
            },
        },
        "required": ["target_directory"],
    }

    def __init__(self, workspace_root: Optional[Path] = None) -> None:
        self._workspace_root = workspace_root

    def execute(
        self,
        target_directory: str,
        ignore_globs: Optional[List[str]] = None,
    ) -> ToolResult:
        dir_path = resolve_tool_path(target_directory, workspace_root=self._workspace_root)
        display_path = str(dir_path)

        if not dir_path.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"Directory not found: {display_path}",
            )

        if not dir_path.is_dir():
            return ToolResult(
                success=False,
                output="",
                error=f"Not a directory: {display_path}",
            )

        ignore_patterns = ignore_globs or []
        # Auto-prepend **/ to patterns that don't start with it
        expanded_patterns = []
        for p in ignore_patterns:
            if not p.startswith("**/"):
                expanded_patterns.append(f"**/{p}")
            expanded_patterns.append(p)

        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return ToolResult(
                success=False,
                output="",
                error=f"Permission denied: {display_path}",
            )

        lines = [f"{display_path}/"]
        for entry in entries:
            # Skip dot-files and dot-dirs
            if entry.name.startswith("."):
                continue

            rel_name = entry.name
            # Check ignore patterns
            if any(fnmatch(rel_name, p) or fnmatch(f"**/{rel_name}", p) for p in expanded_patterns):
                continue

            if entry.is_dir():
                lines.append(f"  {rel_name}/")
                # Show one level of children
                try:
                    children = sorted(entry.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                    for child in children:
                        if child.name.startswith("."):
                            continue
                        suffix = "/" if child.is_dir() else ""
                        lines.append(f"    {child.name}{suffix}")
                except PermissionError:
                    lines.append("    (permission denied)")
            else:
                lines.append(f"  {rel_name}")

        return ToolResult(success=True, output="\n".join(lines))
