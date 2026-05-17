"""Composite read-only file inspection tool.

Lets a worker inspect a small set of related files in one tool call while
preserving the same edit evidence semantics as individual Read calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Tool, ToolResult


class InspectFilesTool(Tool):
    name = "InspectFiles"
    description = (
        "Read several files in one structured, read-only call. Use this when a task "
        "requires inspecting related source and test files before editing, for example "
        "app.py and tests/test_app.py. Returns numbered file sections."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths to inspect. Keep this focused to files needed for the task.",
            },
            "offset": {
                "type": "integer",
                "description": "Optional 1-indexed line start applied to each file. Negative values count from end.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional number of lines to read from each file.",
            },
        },
        "required": ["paths"],
    }

    def execute(
        self,
        paths: List[str],
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> ToolResult:
        if not isinstance(paths, list) or not paths:
            return ToolResult(success=False, output="", error="invalid_inspectfiles_paths: paths must be a non-empty list")
        if len(paths) > 8:
            return ToolResult(success=False, output="", error="invalid_inspectfiles_paths: inspect at most 8 files per call")

        sections: List[str] = []
        inspected_files: List[str] = []
        for raw_path in paths:
            if not isinstance(raw_path, str) or not raw_path.strip():
                return ToolResult(success=False, output="", error="invalid_inspectfiles_path: each path must be a non-empty string")

            file_path = Path(raw_path)
            if not file_path.exists():
                return ToolResult(success=False, output="", error=f"File not found: {raw_path}")
            if not file_path.is_file():
                return ToolResult(success=False, output="", error=f"Not a file (is a directory?): {raw_path}")

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except PermissionError:
                return ToolResult(success=False, output="", error=f"Permission denied: {raw_path}")
            except Exception as e:
                return ToolResult(success=False, output="", error=f"Read error: {raw_path}: {e}")

            inspected_files.append(raw_path)
            sections.append(_format_file_section(raw_path, content, offset=offset, limit=limit))

        return ToolResult(
            success=True,
            output="\n\n".join(sections),
            metadata={
                "inspected_files": inspected_files,
                "inspected_file_count": len(inspected_files),
            },
        )


def _format_file_section(
    path: str,
    content: str,
    *,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    header = f"==> {path} <=="
    if not content:
        return f"{header}\nFile is empty."

    lines = content.splitlines()
    total_lines = len(lines)
    if offset is not None:
        if offset < 0:
            start_idx = max(0, total_lines + offset)
        else:
            start_idx = max(0, offset - 1)
    else:
        start_idx = 0

    if limit is not None:
        end_idx = min(total_lines, start_idx + limit)
    else:
        end_idx = total_lines

    selected = lines[start_idx:end_idx]
    numbered = [f"{i:>6}|{line}" for i, line in enumerate(selected, start=start_idx + 1)]
    body = "\n".join(numbered)
    if start_idx > 0 or end_idx < total_lines:
        body = f"... {start_idx} lines not shown ...\n{body}"
        if end_idx < total_lines:
            body += f"\n... {total_lines - end_idx} lines not shown ..."
    return f"{header}\n{body}"
