"""Grep tool -- mirrors Cursor's Grep tool.

Wraps ripgrep (rg) for fast regex search across files.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional

from .base import Tool, ToolResult


class GrepTool(Tool):
    name = "Grep"
    description = (
        "Search for a regex pattern across files using ripgrep. "
        "Supports file type filtering, glob patterns, context lines, "
        "and multiple output modes."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search in. Defaults to workspace root.",
            },
            "glob": {
                "type": "string",
                "description": 'Glob pattern to filter files (e.g. "*.py", "*.{ts,tsx}").',
            },
            "type": {
                "type": "string",
                "description": "File type to search (e.g. py, js, rust, go, java).",
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "Output mode. Default: content.",
            },
            "-i": {
                "type": "boolean",
                "description": "Case insensitive search.",
            },
            "-A": {
                "type": "integer",
                "description": "Lines to show after each match.",
            },
            "-B": {
                "type": "integer",
                "description": "Lines to show before each match.",
            },
            "-C": {
                "type": "integer",
                "description": "Lines to show before and after each match.",
            },
            "multiline": {
                "type": "boolean",
                "description": "Enable multiline matching.",
            },
            "head_limit": {
                "type": "integer",
                "description": "Limit number of results.",
            },
        },
        "required": ["pattern"],
    }

    def execute(self, pattern: str, **kwargs: Any) -> ToolResult:
        cmd: List[str] = ["rg", "--color=never"]

        # Output mode
        output_mode = kwargs.get("output_mode", "content")
        if output_mode == "files_with_matches":
            cmd.append("--files-with-matches")
        elif output_mode == "count":
            cmd.extend(["--count", "--sort=path"])

        # Case insensitive
        if kwargs.get("-i"):
            cmd.append("-i")

        # Context lines
        if kwargs.get("-A"):
            cmd.extend(["-A", str(kwargs["-A"])])
        if kwargs.get("-B"):
            cmd.extend(["-B", str(kwargs["-B"])])
        if kwargs.get("-C"):
            cmd.extend(["-C", str(kwargs["-C"])])

        # Multiline
        if kwargs.get("multiline"):
            cmd.extend(["-U", "--multiline-dotall"])

        # Line numbers (for content mode)
        if output_mode == "content":
            cmd.append("--line-number")

        # Glob filter
        if kwargs.get("glob"):
            cmd.extend(["--glob", kwargs["glob"]])

        # Type filter
        if kwargs.get("type"):
            cmd.extend(["--type", kwargs["type"]])

        # Head limit
        head_limit = kwargs.get("head_limit")
        if head_limit and output_mode == "content":
            cmd.extend(["-m", str(head_limit)])

        # Pattern and path
        cmd.extend(["--", pattern])
        search_path = kwargs.get("path", ".")
        cmd.append(search_path)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if proc.returncode == 0:
                output = proc.stdout.rstrip()
                if head_limit and output_mode in ("files_with_matches", "count"):
                    lines = output.split("\n")
                    output = "\n".join(lines[:head_limit])
                return ToolResult(success=True, output=output)
            elif proc.returncode == 1:
                return ToolResult(success=True, output="No matches found.")
            else:
                return ToolResult(
                    success=False,
                    output=proc.stderr.rstrip(),
                    error=f"ripgrep exited with code {proc.returncode}",
                )
        except (FileNotFoundError, PermissionError):
            # Fallback: try GNU grep (rg missing or not executable)
            return self._fallback_grep(pattern, kwargs)
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error="Grep search timed out after 30s",
            )

    def _fallback_grep(self, pattern: str, kwargs: Any) -> ToolResult:
        """Fall back to GNU grep when ripgrep is not available."""
        cmd: List[str] = ["grep", "-rn", "--color=never"]

        if kwargs.get("-i"):
            cmd.append("-i")

        if kwargs.get("-A"):
            cmd.extend(["-A", str(kwargs["-A"])])
        if kwargs.get("-B"):
            cmd.extend(["-B", str(kwargs["-B"])])
        if kwargs.get("-C"):
            cmd.extend(["-C", str(kwargs["-C"])])

        output_mode = kwargs.get("output_mode", "content")
        if output_mode == "files_with_matches":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")

        if kwargs.get("glob"):
            cmd.extend(["--include", kwargs["glob"]])

        cmd.extend(["--", pattern])
        search_path = kwargs.get("path", ".")
        cmd.append(search_path)

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                output = proc.stdout.rstrip()
                return ToolResult(
                    success=True,
                    output=f"(using GNU grep fallback — install ripgrep for better results)\n{output}",
                )
            elif proc.returncode == 1:
                return ToolResult(success=True, output="No matches found.")
            else:
                return ToolResult(
                    success=False,
                    output=proc.stderr.rstrip(),
                    error=f"grep exited with code {proc.returncode}",
                )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                output="",
                error="Neither ripgrep (rg) nor GNU grep found. Install ripgrep: https://github.com/BurntSushi/ripgrep",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="Grep search timed out after 30s")
