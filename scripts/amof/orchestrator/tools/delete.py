"""Delete tool -- mirrors Cursor's Delete tool.

Deletes a file at the specified path with safety checks.
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


class DeleteTool(Tool):
    name = "Delete"
    description = (
        "Deletes a file at the specified path. Fails gracefully if the file "
        "doesn't exist or cannot be deleted."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The absolute path of the file to delete.",
            },
        },
        "required": ["path"],
    }

    def execute(self, path: str) -> ToolResult:
        file_path = Path(path)

        if not file_path.exists():
            ex = _get_explainer()
            msg = ex.file_not_found(path, file_path.parent) if ex else f"File not found: {path}"
            return ToolResult(success=False, output="", error=msg)

        if not file_path.is_file():
            return ToolResult(
                success=False, output="", error=f"Not a file (use rmdir for directories): {path}"
            )

        try:
            file_path.unlink()
        except PermissionError:
            ex = _get_explainer()
            msg = ex.permission_denied(path, "delete") if ex else f"Permission denied: {path}"
            return ToolResult(success=False, output="", error=msg)
        except Exception as e:
            return ToolResult(
                success=False, output="", error=f"Delete error: {e}"
            )

        return ToolResult(success=True, output=f"Deleted {path}")
