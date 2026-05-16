"""Response formatting for MCP tool outputs.

Every tool response follows the pattern:

    [breadcrumb] Summary line (1 sentence)

    Key details (structured table/list)

    Next: [Action1] [Action2] [Action3]
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from amof.mcp.session import SessionContext


def text_content(text: str) -> List[Dict[str, str]]:
    """Wrap a string as MCP text content."""
    return [{"type": "text", "text": text}]


def format_response(
    session: SessionContext,
    summary: str,
    details: str = "",
    suggested_actions: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """Build a standard MCP response with breadcrumb, details, and next actions."""
    parts = [f"{session.breadcrumb()} {summary}"]
    if details:
        parts.append("")
        parts.append(details)
    if suggested_actions:
        parts.append("")
        parts.append("Next: " + "  ".join(f"[{a}]" for a in suggested_actions))
    return text_content("\n".join(parts))


def format_table(
    headers: List[str],
    rows: List[List[str]],
    align: Optional[List[str]] = None,
) -> str:
    """Render a markdown table.

    Args:
        headers: Column header labels.
        rows: List of rows (each row is a list of cell strings).
        align: Per-column alignment: ``"l"``, ``"r"``, ``"c"``, or ``None``.
    """
    if not rows:
        return ""

    col_count = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < col_count:
                widths[i] = max(widths[i], len(cell))

    def pad(val: str, width: int, a: str = "l") -> str:
        if a == "r":
            return val.rjust(width)
        if a == "c":
            return val.center(width)
        return val.ljust(width)

    alignments = align or ["l"] * col_count

    header_line = "| " + " | ".join(
        pad(h, widths[i], alignments[i]) for i, h in enumerate(headers)
    ) + " |"

    sep_parts = []
    for i in range(col_count):
        a = alignments[i] if align else "l"
        bar = "-" * widths[i]
        if a == "r":
            bar = bar[:-1] + ":"
        elif a == "c":
            bar = ":" + bar[1:-1] + ":"
        sep_parts.append(bar)
    sep_line = "| " + " | ".join(sep_parts) + " |"

    body_lines = []
    for row in rows:
        cells = []
        for i in range(col_count):
            val = row[i] if i < len(row) else ""
            cells.append(pad(val, widths[i], alignments[i]))
        body_lines.append("| " + " | ".join(cells) + " |")

    return "\n".join([header_line, sep_line] + body_lines)


def format_kv(pairs: List[tuple]) -> str:
    """Render key-value pairs as a compact list."""
    if not pairs:
        return ""
    max_key = max(len(str(k)) for k, _ in pairs)
    return "\n".join(f"  {str(k).ljust(max_key)}  {v}" for k, v in pairs)


def format_json_block(data: Any, label: str = "") -> str:
    """Render data as a labeled JSON code block."""
    prefix = f"**{label}**\n" if label else ""
    return prefix + "```json\n" + json.dumps(data, indent=2) + "\n```"


def format_error(
    session: SessionContext,
    message: str,
    hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an MCP error response with breadcrumb."""
    parts = [f"{session.breadcrumb()} Error: {message}"]
    if hint:
        parts.append(f"\nHint: {hint}")
    return {
        "content": text_content("\n".join(parts)),
        "isError": True,
    }


def enrich_with_suggested_actions(
    session: SessionContext,
    tool_name: str,
    tool_result: Any,
    response_text: str,
) -> str:
    """Append suggested actions line if the engine produces any.

    Falls back gracefully if the engine isn't available or throws.
    """
    try:
        from amof.api.suggested_actions import get_suggested_actions, format_actions_text

        result_dict = tool_result if isinstance(tool_result, dict) else {}
        actions = get_suggested_actions(
            tool_name=tool_name,
            tool_result=result_dict,
            current_mode=session.mode,
            session_ecosystem=session.selected_ecosystem,
            session_scope=session.current_scope,
        )
        actions_text = format_actions_text(actions)
        if actions_text and actions_text not in response_text:
            return response_text.rstrip() + "\n\n" + actions_text
    except Exception:
        pass
    return response_text
