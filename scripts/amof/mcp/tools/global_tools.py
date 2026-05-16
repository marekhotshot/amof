"""Global-scope MCP tools: ecosystem listing, server status, active runs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from amof.mcp.server import register_tool, get_session
from amof.mcp.session import SessionContext
from amof.mcp.decorators import mode_aware
from amof.mcp.formatters import format_response, format_table, format_error, text_content
from amof.mcp._ecosystem_helpers import get_available_ecosystems, load_ecosystem_manifest


@mode_aware(safety="read-only")
def _list_ecosystems(session: SessionContext, args: Dict[str, Any]) -> Any:
    names = get_available_ecosystems()
    if not names:
        return format_response(
            session,
            "No ecosystems found.",
            details="Create one with: `amof ecosystem create <name>`",
        )

    rows = []
    for name in names:
        try:
            m = load_ecosystem_manifest(name)
            repo_count = len(m.get("repos", []))
            provisioner = m.get("provisioner", "-")
            rows.append([name, str(repo_count), str(provisioner)])
        except Exception:
            rows.append([name, "?", "?"])

    table = format_table(["Ecosystem", "Repos", "Provisioner"], rows)
    actions = [f"amof_use_ecosystem({n})" for n in names[:3]]
    return format_response(
        session,
        f"{len(names)} ecosystem(s) found.",
        details=table,
        suggested_actions=actions,
    )


@mode_aware(safety="read-only")
def _get_server_status(session: SessionContext, args: Dict[str, Any]) -> Any:
    pid_file = Path(".amof/server.pid")
    api_base = os.environ.get("AMOF_API_BASE", "http://localhost:8000")

    info = {"api_base": api_base, "pid_file": str(pid_file)}
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            info["pid"] = pid
            info["running"] = _pid_alive(pid)
        except Exception:
            info["running"] = False
    else:
        info["running"] = False

    status_str = "running" if info.get("running") else "stopped"
    details = "\n".join(f"  {k}: {v}" for k, v in info.items())
    return format_response(
        session,
        f"Control plane: {status_str}",
        details=details,
        suggested_actions=["amof_list_ecosystems"],
    )


@mode_aware(safety="read-only")
def _get_active_runs(session: SessionContext, args: Dict[str, Any]) -> Any:
    try:
        from amof.api.dependencies import run_manager
        active = run_manager.list_runs(status="running", limit=20)
    except Exception:
        return format_response(session, "No active runs (run manager not available).")

    if not active:
        return format_response(
            session, "No active runs.",
            suggested_actions=["amof_list_ecosystems"],
        )

    rows = []
    for r in active:
        rows.append([
            r.run_id[:8],
            r.ecosystem,
            r.action,
            r.status,
            r.created_at[:19] if r.created_at else "-",
        ])

    table = format_table(["Run", "Ecosystem", "Action", "Status", "Created"], rows)
    return format_response(
        session,
        f"{len(active)} active run(s).",
        details=table,
        suggested_actions=["amof_get_run_status"],
    )


def _pid_alive(pid: int) -> bool:
    """Check if a process with given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ── Registration ──

register_tool(
    "amof_list_ecosystems",
    "List all available ecosystems with repo count and provisioner.",
    _list_ecosystems,
)

register_tool(
    "amof_get_server_status",
    "Get AMOF control plane server status (PID, port, health).",
    _get_server_status,
)

register_tool(
    "amof_get_active_runs",
    "List currently running agent tasks across all ecosystems.",
    _get_active_runs,
)
