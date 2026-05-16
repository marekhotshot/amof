"""StrReplace tool -- mirrors Cursor's StrReplace tool.

Performs exact string replacement in files with uniqueness checking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

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


class StrReplaceTool(Tool):
    name = "StrReplace"
    description = (
        "Performs exact string replacements in files. The old_string must be "
        "unique in the file (unless replace_all is true). Preserves indentation."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The absolute path to the file to modify.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact text to replace.",
            },
            "new_string": {
                "type": "string",
                "description": "The text to replace it with.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "If true, replace all occurrences. Default false.",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    def execute(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: Optional[bool] = False,
    ) -> ToolResult:
        file_path = Path(path)

        if not file_path.exists():
            ex = _get_explainer()
            msg = ex.file_not_found(path, file_path.parent) if ex else f"File not found: {path}"
            return ToolResult(success=False, output="", error=msg)

        if not file_path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        if old_string == new_string:
            return ToolResult(
                success=False, output="", error="old_string and new_string are identical"
            )

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Read error: {e}")

        count = content.count(old_string)

        if count == 0:
            return ToolResult(
                success=False,
                output="",
                error=f"old_string not found in {path}",
            )

        if count > 1 and not replace_all:
            return ToolResult(
                success=False,
                output="",
                error=f"old_string found {count} times in {path}. Use replace_all=true or provide more context to make it unique.",
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        try:
            file_path.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Write error: {e}")

        replacements = count if replace_all else 1
        return ToolResult(
            success=True,
            output=f"Replaced {replacements} occurrence(s) in {path}",
        )
