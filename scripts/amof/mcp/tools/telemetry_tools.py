"""Observability and telemetry MCP tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from amof.app_paths import runs_dir
from amof.mcp.server import register_tool
from amof.mcp.session import SessionContext
from amof.mcp.decorators import mode_aware
from amof.mcp.formatters import format_response, format_table, format_error, format_kv


@mode_aware(safety="read-only")
def _global_telemetry(session: SessionContext, args: Dict[str, Any]) -> Any:
    """Aggregate telemetry across recent runs."""
    try:
        from amof.api.dependencies import run_manager
        all_runs = run_manager.list_runs(limit=200)
    except Exception:
        return format_response(session, "Telemetry unavailable (run manager not loaded).")

    total = len(all_runs)
    by_status = {}
    by_eco = {}
    for r in all_runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_eco[r.ecosystem] = by_eco.get(r.ecosystem, 0) + 1

    status_rows = [[s, str(c)] for s, c in sorted(by_status.items())]
    eco_rows = [[e, str(c)] for e, c in sorted(by_eco.items(), key=lambda x: -x[1])]

    status_table = format_table(["Status", "Count"], status_rows)
    eco_table = format_table(["Ecosystem", "Runs"], eco_rows)

    return format_response(
        session,
        f"Telemetry: {total} total runs",
        details=f"By status:\n{status_table}\n\nBy ecosystem:\n{eco_table}",
        suggested_actions=["amof_get_active_runs", "amof_list_runs"],
    )


@mode_aware(safety="read-only")
def _agent_stats(session: SessionContext, args: Dict[str, Any]) -> Any:
    """Load agent session telemetry from the AMOF app-data runs directory."""
    telemetry_runs_dir = runs_dir()
    if not telemetry_runs_dir.exists():
        return format_response(session, "No agent session data found.")

    sessions = []
    for d in sorted(telemetry_runs_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        telem_file = d / "telemetry.json"
        if telem_file.exists():
            try:
                data = json.loads(telem_file.read_text(encoding="utf-8"))
                sessions.append({
                    "session_id": d.name[:8],
                    "model": data.get("model", "-"),
                    "cost": data.get("total_cost", 0),
                    "tokens_in": data.get("tokens_in", 0),
                    "tokens_out": data.get("tokens_out", 0),
                    "duration": data.get("duration_s", 0),
                })
            except Exception:
                continue

    if not sessions:
        return format_response(session, "No agent telemetry data found.")

    sessions = sessions[:20]
    rows = [
        [
            s["session_id"],
            s["model"],
            f"${s['cost']:.4f}" if s["cost"] else "-",
            str(s["tokens_in"]),
            str(s["tokens_out"]),
            f"{s['duration']:.0f}s" if s["duration"] else "-",
        ]
        for s in sessions
    ]

    table = format_table(
        ["Session", "Model", "Cost", "Tokens In", "Tokens Out", "Duration"],
        rows,
    )

    total_cost = sum(s["cost"] for s in sessions)
    return format_response(
        session,
        f"Agent stats: {len(sessions)} recent sessions, total cost: ${total_cost:.4f}",
        details=table,
    )


# ── Registration ──

register_tool(
    "amof_global_telemetry",
    "Show aggregate telemetry: run counts by status and ecosystem.",
    _global_telemetry,
)

register_tool(
    "amof_agent_stats",
    "Show agent session stats: cost, tokens, and model usage from the AMOF app-data runs directory.",
    _agent_stats,
)
