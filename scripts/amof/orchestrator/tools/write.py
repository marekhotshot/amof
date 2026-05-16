"""Write tool -- mirrors Cursor's Write tool.

Creates or overwrites a file with given contents.
Creates parent directories as needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .base import Tool, ToolResult

_explainer = None

def _get_explainer():
    global _explainer
    if _explainer is None:
        try:
            from ...error_explainer import ErrorExplainer
            _explainer = ErrorExplainer
        except ImportError:
            _explainer = False
    return _explainer if _explainer is not False else None


class WriteTool(Tool):
    name = "Write"
    description = (
        "Writes a file to the local filesystem. Overwrites existing file "
        "if one exists. Creates parent directories as needed."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The absolute path of the file to write.",
            },
            "contents": {
                "type": "string",
                "description": "The contents to write to the file.",
            },
        },
        "required": ["path", "contents"],
    }

    def execute(self, path: str, contents: str) -> ToolResult:
        file_path = Path(path)

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(contents, encoding="utf-8")
        except PermissionError:
            ex = _get_explainer()
            msg = ex.permission_denied(path, "write") if ex else f"Permission denied: {path}"
            return ToolResult(success=False, output="", error=msg)
        except Exception as e:
            ex = _get_explainer()
            msg = ex.wrap_error(e, f"Writing to {path}") if ex else f"Write error: {e}"
            return ToolResult(success=False, output="", error=msg)

        return ToolResult(
            success=True,
            output=f"Wrote {len(contents)} bytes to {path}",
        )
