"""Glob tool -- mirrors Cursor's Glob tool.

Finds files matching a glob pattern, sorted by modification time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from .base import Tool, ToolResult


class GlobTool(Tool):
    name = "Glob"
    description = (
        "Search for files matching a glob pattern. Returns matching file "
        "paths sorted by modification time (newest first). Patterns not "
        'starting with "**/" are auto-prepended with "**/" for recursive search.'
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "glob_pattern": {
                "type": "string",
                "description": "The glob pattern to match files against.",
            },
            "target_directory": {
                "type": "string",
                "description": "Directory to search in. Defaults to workspace root.",
            },
        },
        "required": ["glob_pattern"],
    }

    def execute(
        self,
        glob_pattern: str,
        target_directory: Optional[str] = None,
    ) -> ToolResult:
        search_dir = Path(target_directory) if target_directory else Path(".")

        if not search_dir.is_dir():
            return ToolResult(
                success=False,
                output="",
                error=f"Directory not found: {search_dir}",
            )

        # Auto-prepend **/ for recursive search
        pattern = glob_pattern
        if not pattern.startswith("**/"):
            pattern = f"**/{pattern}"

        try:
            matches = list(search_dir.glob(pattern))
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Glob error: {e}",
            )

        # Filter to files only
        files = [m for m in matches if m.is_file()]

        # Sort by modification time (newest first)
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        if not files:
            return ToolResult(success=True, output="No files matched the pattern.")

        # Format output
        lines = []
        for f in files:
            try:
                rel = f.relative_to(search_dir)
            except ValueError:
                rel = f
            lines.append(str(rel))

        return ToolResult(success=True, output="\n".join(lines))
