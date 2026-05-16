"""Server lifecycle MCP tools: start, stop, restart, status."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

from amof.mcp.server import register_tool, get_confirmations
from amof.mcp.session import SessionContext
from amof.mcp.decorators import mode_aware
from amof.mcp.formatters import format_response, format_error, format_kv


def _get_pid_file() -> Path:
    return Path(".amof/server.pid")


def _get_server_pid() -> int:
    pf = _get_pid_file()
    if pf.exists():
        try:
            return int(pf.read_text().strip())
        except ValueError:
            return 0
    return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


@mode_aware(safety="read-only")
def _server_status(session: SessionContext, args: Dict[str, Any]) -> Any:
    pid = _get_server_pid()
    alive = _pid_alive(pid)
    api_base = os.environ.get("AMOF_API_BASE", "http://localhost:8000")

    kv = format_kv([
        ("Status", "running" if alive else "stopped"),
        ("PID", str(pid) if pid else "-"),
        ("API", api_base),
        ("PID file", str(_get_pid_file())),
    ])

    actions = ["amof_server_start"] if not alive else ["amof_server_stop", "amof_server_restart"]
    return format_response(
        session,
        f"Server: {'running' if alive else 'stopped'}",
        details=kv,
        suggested_actions=actions,
    )


@mode_aware(safety="safe-write")
def _server_start(session: SessionContext, args: Dict[str, Any]) -> Any:
    pid = _get_server_pid()
    if _pid_alive(pid):
        return format_response(session, f"Server already running (PID {pid}).")

    host = args.get("host", "127.0.0.1")
    port = args.get("port", 8000)

    try:
        platform_root = Path.cwd()
        scripts_dir = platform_root / "scripts"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(scripts_dir) + os.pathsep + env.get("PYTHONPATH", "")
        env["AMOF_CWD"] = str(platform_root)

        proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "amof.api.main:app",
                "--host", host,
                "--port", str(port),
            ],
            cwd=str(platform_root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        _get_pid_file().parent.mkdir(parents=True, exist_ok=True)
        _get_pid_file().write_text(str(proc.pid))

        time.sleep(1)
        if proc.poll() is not None:
            return format_error(session, f"Server failed to start (exit {proc.returncode}).")

        return format_response(
            session,
            f"Server started on http://{host}:{port} (PID {proc.pid})",
            suggested_actions=["amof_get_server_status"],
        )
    except Exception as exc:
        return format_error(session, f"Failed to start server: {exc}")


@mode_aware(safety="safe-write", confirm="simple")
def _server_stop(session: SessionContext, args: Dict[str, Any]) -> Any:
    pid = _get_server_pid()
    if not _pid_alive(pid):
        _get_pid_file().unlink(missing_ok=True)
        return format_response(session, "Server is not running.")

    confirmations = get_confirmations()

    def _execute() -> Any:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
            _get_pid_file().unlink(missing_ok=True)
            return format_response(session, f"Server stopped (PID {pid}).")
        except Exception as exc:
            return format_error(session, f"Failed to stop server: {exc}")

    entry = confirmations.create(
        tool_name="amof_server_stop",
        description=f"Stop AMOF server (PID {pid})",
        preview=f"Will send SIGTERM to PID {pid}",
        execute_fn=_execute,
        confirm_type="simple",
    )

    return format_response(
        session,
        f"Confirm: stop server (PID {pid})?",
        details=f'Token: {entry.token}\n→ amof_confirm(token="{entry.token}")',
        suggested_actions=[f"amof_confirm({entry.token})", "amof_cancel_confirm"],
    )


@mode_aware(safety="safe-write", confirm="simple")
def _server_restart(session: SessionContext, args: Dict[str, Any]) -> Any:
    pid = _get_server_pid()
    confirmations = get_confirmations()

    def _execute() -> Any:
        if _pid_alive(pid):
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
            _get_pid_file().unlink(missing_ok=True)
            time.sleep(1)
        return _server_start(session, args)

    entry = confirmations.create(
        tool_name="amof_server_restart",
        description="Restart AMOF server",
        preview=f"Will stop PID {pid} then start new server",
        execute_fn=_execute,
        confirm_type="simple",
    )

    return format_response(
        session,
        f"Confirm: restart server?",
        details=f'Token: {entry.token}\n→ amof_confirm(token="{entry.token}")',
        suggested_actions=[f"amof_confirm({entry.token})"],
    )


# ── Registration ──

register_tool(
    "amof_server_status",
    "Check if the AMOF API server is running (PID, port).",
    _server_status,
)

register_tool(
    "amof_server_start",
    "Start the AMOF API server (background uvicorn process).",
    _server_start,
    params={
        "properties": {
            "host": {"type": "string", "description": "Host to bind (default: 127.0.0.1)"},
            "port": {"type": "integer", "description": "Port to bind (default: 8000)"},
        },
    },
)

register_tool(
    "amof_server_stop",
    "Stop the running AMOF API server. Requires confirmation.",
    _server_stop,
)

register_tool(
    "amof_server_restart",
    "Restart the AMOF API server. Requires confirmation.",
    _server_restart,
    params={
        "properties": {
            "host": {"type": "string", "description": "Host to bind (default: 127.0.0.1)"},
            "port": {"type": "integer", "description": "Port to bind (default: 8000)"},
        },
    },
)
