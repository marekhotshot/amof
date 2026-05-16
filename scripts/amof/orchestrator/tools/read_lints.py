"""ReadLints tool -- run configured linters on files and return diagnostics.

Mirrors Cursor's ReadLints tool. Uses the LinterRunner for all linter
execution. All linter definitions come from .amof/rules/linters.yaml.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .base import Tool, ToolResult
from ..linter import LinterRunner


class ReadLintsTool(Tool):
    name = "ReadLints"
    description = (
        "Run configured linters on specified files or directories and return "
        "diagnostics. If no paths are provided, returns an error asking for paths. "
        "Supports Python (ruff), YAML (yamllint), shell (shellcheck), and any "
        "linters configured in .amof/rules/linters.yaml."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "File or directory paths to lint. For directories, all "
                    "files with matching extensions are linted."
                ),
            },
        },
        "required": [],
    }

    def __init__(self, linter: Optional[LinterRunner] = None) -> None:
        self._linter = linter

    def execute(self, paths: Optional[List[str]] = None) -> ToolResult:
        if self._linter is None:
            return ToolResult(
                success=False,
                output="",
                error="Linter not configured. Ensure .amof/rules/linters.yaml exists.",
            )

        if not paths:
            return ToolResult(
                success=False,
                output="",
                error="No paths provided. Specify files or directories to lint.",
            )

        # Expand directories to individual files
        file_paths: List[str] = []
        for p in paths:
            if os.path.isdir(p):
                for root, _dirs, files in os.walk(p):
                    for f in files:
                        file_paths.append(os.path.join(root, f))
            elif os.path.isfile(p):
                file_paths.append(p)
            else:
                # Path doesn't exist — include anyway, lint_file will skip it
                file_paths.append(p)

        if not file_paths:
            return ToolResult(
                success=True,
                output="No files found at the specified paths.",
            )

        # Run linters
        results = self._linter.lint_files(file_paths)

        if not results:
            return ToolResult(
                success=True,
                output="No linter diagnostics found.",
            )

        formatted = self._linter.format_diagnostics(results)
        total_issues = sum(len(v) for v in results.values())

        return ToolResult(
            success=True,
            output=f"{total_issues} diagnostic(s) in {len(results)} file(s):\n\n{formatted}",
        )
