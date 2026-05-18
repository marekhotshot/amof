"""InsertAfter tool -- safer bounded insertion after an exact anchor."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .base import Tool, ToolResult
from .str_replace import (
    MAX_STR_REPLACE_ADDED_BYTES,
    MAX_STR_REPLACE_GROWTH_MULTIPLIER,
)


class InsertAfterTool(Tool):
    name = "InsertAfter"
    description = (
        "Inserts text immediately after a unique anchor string in an existing file. "
        "Use this for small additions after reading the file and copying the anchor exactly."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The path to the file to modify.",
            },
            "anchor_string": {
                "type": "string",
                "description": "The exact existing text after which content should be inserted.",
            },
            "content_to_insert": {
                "type": "string",
                "description": "The new text to insert immediately after anchor_string.",
            },
        },
        "required": ["path", "anchor_string", "content_to_insert"],
    }

    def execute(self, path: str, anchor_string: str, content_to_insert: str) -> ToolResult:
        if anchor_string == "":
            return ToolResult(
                success=False,
                output="",
                error="invalid_insertafter_anchor_empty: anchor_string must be non-empty.",
            )
        if anchor_string.strip() == "":
            return ToolResult(
                success=False,
                output="",
                error="invalid_insertafter_anchor_whitespace: anchor_string cannot be whitespace-only.",
            )
        if content_to_insert == "":
            return ToolResult(
                success=False,
                output="",
                error="invalid_insertafter_content_empty: content_to_insert must be non-empty.",
            )

        file_path = Path(path)
        if not file_path.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if not file_path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            return ToolResult(success=False, output="", error=f"Read error: {exc}")

        count = content.count(anchor_string)
        if count == 0:
            return ToolResult(
                success=False,
                output="",
                error=f"invalid_insertafter_anchor_not_found: anchor_string not found in {path}",
            )
        if count > 1:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "invalid_insertafter_anchor_multiple: "
                    f"anchor_string found {count} times in {path}. Provide a unique anchor."
                ),
            )

        projected_size = len(content) + len(content_to_insert)
        growth_limit = max(
            len(content) * MAX_STR_REPLACE_GROWTH_MULTIPLIER,
            len(content) + MAX_STR_REPLACE_ADDED_BYTES,
        )
        if len(content_to_insert) > MAX_STR_REPLACE_ADDED_BYTES or projected_size > growth_limit:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "invalid_insertafter_growth: insertion would grow the file too much "
                    f"before verification ({len(content)}->{projected_size} bytes)."
                ),
            )

        insert_at = content.index(anchor_string) + len(anchor_string)
        new_content = content[:insert_at] + content_to_insert + content[insert_at:]

        try:
            file_path.write_text(new_content, encoding="utf-8")
        except Exception as exc:
            return ToolResult(success=False, output="", error=f"Write error: {exc}")

        return ToolResult(success=True, output=f"Inserted {len(content_to_insert)} bytes in {path}")
