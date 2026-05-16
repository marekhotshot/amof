"""Read tool -- mirrors Cursor's Read tool.

Reads file contents with optional line range (offset/limit).
Returns numbered lines in format LINE_NUMBER|CONTENT.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .base import Tool, ToolResult

# Lazy import to avoid circular deps; resolved at call time
_explainer = None

def _get_explainer():
    global _explainer
    if _explainer is None:
        try:
            from ...error_explainer import ErrorExplainer
            _explainer = ErrorExplainer
        except ImportError:
            _explainer = False  # sentinel: unavailable
    return _explainer if _explainer is not False else None


class ReadTool(Tool):
    name = "Read"
    description = (
        "Reads a file from the local filesystem. Returns lines numbered "
        "starting at 1 in format LINE_NUMBER|LINE_CONTENT. Supports offset "
        "(1-indexed line start, negative counts from end) and limit (line count)."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The absolute path of the file to read.",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from. Positive values are 1-indexed. Negative values count from end.",
            },
            "limit": {
                "type": "integer",
                "description": "Number of lines to read.",
            },
        },
        "required": ["path"],
    }

    def execute(
        self,
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> ToolResult:
        file_path = Path(path)

        if not file_path.exists():
            ex = _get_explainer()
            msg = ex.file_not_found(path, file_path.parent) if ex else f"File not found: {path}"
            return ToolResult(success=False, output="", error=msg)

        if not file_path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file (is a directory?): {path}")

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except PermissionError:
            ex = _get_explainer()
            msg = ex.permission_denied(path, "read") if ex else f"Permission denied: {path}"
            return ToolResult(success=False, output="", error=msg)
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Read error: {e}")

        if not content:
            return ToolResult(success=True, output="File is empty.")

        lines = content.splitlines()
        total_lines = len(lines)

        # Apply offset
        if offset is not None:
            if offset < 0:
                start_idx = max(0, total_lines + offset)
            else:
                start_idx = max(0, offset - 1)  # 1-indexed
        else:
            start_idx = 0

        # Apply limit
        if limit is not None:
            end_idx = min(total_lines, start_idx + limit)
        else:
            end_idx = total_lines

        # Format with line numbers (right-aligned to 6 chars)
        selected = lines[start_idx:end_idx]
        numbered = []
        for i, line in enumerate(selected, start=start_idx + 1):
            numbered.append(f"{i:>6}|{line}")

        output = "\n".join(numbered)

        # Add context about truncation
        if start_idx > 0 or end_idx < total_lines:
            shown = end_idx - start_idx
            output = f"... {start_idx} lines not shown ...\n{output}"
            if end_idx < total_lines:
                output += f"\n... {total_lines - end_idx} lines not shown ..."

        return ToolResult(success=True, output=output)
