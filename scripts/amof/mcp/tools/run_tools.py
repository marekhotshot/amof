"""Run-scope MCP tools: list, status, logs, summarize."""

from __future__ import annotations

from typing import Any, Dict, List

from amof.mcp.server import register_tool
from amof.mcp.session import SessionContext
from amof.mcp.decorators import mode_aware, requires_scope
from amof.mcp.formatters import format_response, format_table, format_error, format_kv


@mode_aware(safety="read-only")
def _list_runs(session: SessionContext, args: Dict[str, Any]) -> Any:
    ecosystem = args.get("ecosystem") or session.selected_ecosystem
    status_filter = args.get("status")
    limit = args.get("limit", 20)

    try:
        from amof.api.dependencies import run_manager
        runs = run_manager.list_runs(
            ecosystem=ecosystem,
            status=status_filter,
            limit=limit,
        )
    except Exception as exc:
        return format_error(session, f"Cannot query runs: {exc}")

    if not runs:
        return format_response(session, "No runs found.", suggested_actions=["amof_list_ecosystems"])

    rows = []
    for r in runs:
        rows.append([
            r.run_id[:8],
            r.ecosystem,
            r.action,
            r.status,
            (r.created_at or "")[:19],
        ])

    table = format_table(["Run", "Ecosystem", "Action", "Status", "Created"], rows)
    return format_response(
        session,
        f"{len(runs)} run(s) found.",
        details=table,
        suggested_actions=["amof_get_run_status"],
    )


@mode_aware(safety="read-only")
def _get_run_status(session: SessionContext, args: Dict[str, Any]) -> Any:
    run_id = args.get("run_id") or session.selected_run_id
    if not run_id:
        return format_error(session, "Missing run_id. Provide it or enter a run scope.")

    try:
        from amof.api.dependencies import run_manager
        run = run_manager.get_run(run_id)
    except Exception as exc:
        return format_error(session, f"Cannot get run: {exc}")

    if not run:
        return format_error(session, f"Run not found: {run_id}")

    kv = format_kv([
        ("Run ID", run.run_id),
        ("Ecosystem", run.ecosystem),
        ("Action", run.action),
        ("Status", run.status),
        ("Created", run.created_at or "-"),
        ("Started", run.started_at or "-"),
        ("Finished", run.finished_at or "-"),
        ("Exit code", str(run.exit_code) if run.exit_code is not None else "-"),
        ("Events", str(len(run.events))),
    ])

    return format_response(
        session,
        f"Run {run.run_id[:8]}: {run.status} ({run.action})",
        details=kv,
        suggested_actions=["amof_get_run_logs"],
    )


@mode_aware(safety="read-only")
def _get_run_logs(session: SessionContext, args: Dict[str, Any]) -> Any:
    run_id = args.get("run_id") or session.selected_run_id
    if not run_id:
        return format_error(session, "Missing run_id.")

    tail = args.get("tail", 50)
    level_filter = args.get("level")

    try:
        from amof.api.dependencies import run_manager
        run = run_manager.get_run(run_id)
    except Exception as exc:
        return format_error(session, f"Cannot get run: {exc}")

    if not run:
        return format_error(session, f"Run not found: {run_id}")

    events = run.events
    if level_filter:
        events = [e for e in events if e.level == level_filter]

    events = events[-tail:]

    if not events:
        return format_response(session, f"No log events for run {run_id[:8]}.")

    lines = []
    for e in events:
        ts = e.timestamp[:19] if e.timestamp else ""
        lines.append(f"{ts} [{e.level}] {e.message}")

    return format_response(
        session,
        f"Last {len(events)} events for run {run_id[:8]}",
        details="\n".join(lines),
    )


@mode_aware(safety="read-only")
def _summarize_run(session: SessionContext, args: Dict[str, Any]) -> Any:
    run_id = args.get("run_id") or session.selected_run_id
    if not run_id:
        return format_error(session, "Missing run_id.")

    try:
        from amof.api.dependencies import run_manager
        run = run_manager.get_run(run_id)
    except Exception as exc:
        return format_error(session, f"Cannot get run: {exc}")

    if not run:
        return format_error(session, f"Run not found: {run_id}")

    errors = [e for e in run.events if e.level == "error"]
    log_count = len([e for e in run.events if e.type == "log"])

    parts = [
        f"Action: {run.action} on {run.ecosystem}",
        f"Status: {run.status}",
        f"Duration: {_duration(run.started_at, run.finished_at)}",
        f"Log events: {log_count}",
    ]

    if errors:
        parts.append(f"Errors ({len(errors)}):")
        for e in errors[:5]:
            parts.append(f"  - {e.message[:120]}")

    if run.exit_code is not None and run.exit_code != 0:
        parts.append(f"Exit code: {run.exit_code}")

    return format_response(
        session,
        f"Summary: {run.action} - {run.status}",
        details="\n".join(parts),
    )


def _duration(start: str, end: str) -> str:
    if not start or not end:
        return "-"
    try:
        from datetime import datetime
        s = datetime.fromisoformat(start.rstrip("Z"))
        e = datetime.fromisoformat(end.rstrip("Z"))
        delta = e - s
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
    except Exception:
        return "-"


# ── Registration ──

register_tool(
    "amof_list_runs",
    "List runs with optional filters for ecosystem, status, and limit.",
    _list_runs,
    params={
        "properties": {
            "ecosystem": {"type": "string", "description": "Filter by ecosystem name"},
            "status": {"type": "string", "description": "Filter by status: queued, running, success, failed"},
            "limit": {"type": "integer", "description": "Max runs to return (default 20)"},
        },
    },
)

register_tool(
    "amof_get_run_status",
    "Get detailed status for a specific run.",
    _get_run_status,
    params={
        "properties": {
            "run_id": {"type": "string", "description": "Run ID (or uses current run scope)"},
        },
    },
)

register_tool(
    "amof_get_run_logs",
    "Get log events for a run with optional tail and level filtering.",
    _get_run_logs,
    params={
        "properties": {
            "run_id": {"type": "string", "description": "Run ID (or uses current run scope)"},
            "tail": {"type": "integer", "description": "Number of recent events (default 50)"},
            "level": {"type": "string", "description": "Filter by level: info, error, etc."},
        },
    },
)

register_tool(
    "amof_summarize_run",
    "Get a summary of a run: action, duration, errors, exit code.",
    _summarize_run,
    params={
        "properties": {
            "run_id": {"type": "string", "description": "Run ID (or uses current run scope)"},
        },
    },
)
