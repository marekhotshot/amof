"""StrReplace tool -- mirrors Cursor's StrReplace tool.

Performs exact string replacement in files with uniqueness checking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .base import Tool, ToolResult

_explainer = None
MAX_REPLACE_ALL_OCCURRENCES = 20
MAX_STR_REPLACE_ADDED_BYTES = 1_000
MAX_STR_REPLACE_GROWTH_MULTIPLIER = 5
MAX_REPLACEMENT_SIZE_RATIO = 20

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

        validation_error = self._validate_search_text(old_string)
        if validation_error:
            return ToolResult(success=False, output="", error=validation_error)

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
                error=f"invalid_strreplace_old_not_found: old_string not found in {path}",
            )

        if count > 1 and not replace_all:
            return ToolResult(
                success=False,
                output="",
                error=f"invalid_strreplace_old_multiple: old_string found {count} times in {path}. Provide more context to make it unique.",
            )

        replacements = count if replace_all else 1
        growth_error = self._validate_projected_growth(
            content=content,
            old_string=old_string,
            new_string=new_string,
            replacements=replacements,
            replace_all=bool(replace_all),
        )
        if growth_error:
            return ToolResult(success=False, output="", error=growth_error)

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        try:
            file_path.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Write error: {e}")

        return ToolResult(
            success=True,
            output=f"Replaced {replacements} occurrence(s) in {path}",
        )

    @staticmethod
    def _validate_search_text(old_string: str) -> Optional[str]:
        if old_string == "":
            return (
                "invalid_strreplace_old_empty: old_string must be non-empty. "
                "Use a unique exact snippet from the target file."
            )
        if old_string.strip() == "":
            return (
                "invalid_strreplace_old_whitespace: old_string cannot be whitespace-only. "
                "Use surrounding non-whitespace context."
            )
        return None

    @staticmethod
    def _validate_projected_growth(
        *,
        content: str,
        old_string: str,
        new_string: str,
        replacements: int,
        replace_all: bool,
    ) -> Optional[str]:
        if replace_all and replacements > MAX_REPLACE_ALL_OCCURRENCES:
            return (
                "invalid_strreplace_replace_all_too_many: "
                f"replace_all would modify {replacements} occurrences; "
                f"maximum is {MAX_REPLACE_ALL_OCCURRENCES}."
            )

        if len(new_string) > max(
            len(old_string) * MAX_REPLACEMENT_SIZE_RATIO,
            len(old_string) + MAX_STR_REPLACE_ADDED_BYTES,
        ):
            return (
                "invalid_strreplace_replacement_too_large: replacement is too large "
                "relative to old_string. Use a smaller targeted edit or an explicit "
                "full-file rewrite flow."
            )

        projected_added = max(len(new_string) - len(old_string), 0) * replacements
        projected_size = len(content) + (len(new_string) - len(old_string)) * replacements
        growth_limit = max(
            len(content) * MAX_STR_REPLACE_GROWTH_MULTIPLIER,
            len(content) + MAX_STR_REPLACE_ADDED_BYTES,
        )
        if projected_added > MAX_STR_REPLACE_ADDED_BYTES or projected_size > growth_limit:
            return (
                "invalid_strreplace_growth: replacement would grow the file too much "
                f"before verification ({len(content)}->{projected_size} bytes)."
            )

        return None
