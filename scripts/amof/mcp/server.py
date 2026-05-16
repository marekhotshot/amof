"""AMOF MCP Server entry point.

Wires the protocol layer, session context, confirmation store,
and tool modules into a running MCP server.

Launch: ``python -m amof.mcp.server`` (stdio transport)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from amof.mcp import __version__
from amof.mcp.protocol import McpServer, McpError
from amof.mcp.session import SessionContext, ScopeError
from amof.mcp.confirmation import ConfirmationStore
from amof.mcp.formatters import format_response, format_error, text_content


_session = SessionContext()
_confirmations = ConfirmationStore()
_server = McpServer(name="amof-mcp", version=__version__)


def get_session() -> SessionContext:
    return _session


def get_confirmations() -> ConfirmationStore:
    return _confirmations


def _wrap_handler(fn: Callable) -> Callable[[Dict[str, Any]], Any]:
    """Adapt a ``(session, args) -> content`` handler into the raw MCP handler."""

    def handler(args: Dict[str, Any]) -> Any:
        try:
            return fn(_session, args)
        except ScopeError as exc:
            return format_error(_session, str(exc))
        except McpError:
            raise
        except Exception as exc:
            return format_error(_session, f"Unexpected error: {exc}")

    return handler


def register_tool(
    name: str,
    description: str,
    handler: Callable,
    params: Optional[Dict[str, Any]] = None,
) -> None:
    """Register a tool with the MCP server, wrapping it with session injection."""
    _server.register_tool(name, description, _wrap_handler(handler), params=params)


# ── Core scope / session tools (always available) ──


def _scope_info(session: SessionContext, args: Dict[str, Any]) -> Any:
    return format_response(
        session,
        f"Scope: {session.current_scope} | Mode: {session.mode}",
        details="\n".join(
            f"  {k}: {v}" for k, v in session.to_dict().items()
        ),
        suggested_actions=["amof_use_ecosystem", "amof_scope_back"],
    )


def _scope_back(session: SessionContext, args: Dict[str, Any]) -> Any:
    if session.go_back():
        return format_response(
            session,
            "Navigated back.",
            suggested_actions=["amof_scope_info"],
        )
    return format_response(session, "Already at global scope.")


def _use_ecosystem(session: SessionContext, args: Dict[str, Any]) -> Any:
    name = args.get("name", "").strip()
    if not name:
        return format_error(session, "Missing required argument: name")

    from amof.mcp._ecosystem_helpers import validate_ecosystem_exists

    error = validate_ecosystem_exists(name)
    if error:
        return format_error(session, error)

    session.enter_ecosystem(name)
    return format_response(
        session,
        f"Entered ecosystem: {name}",
        suggested_actions=["amof_get_ecosystem_status", "amof_describe_ecosystem", "amof_ticket_list"],
    )


def _scope_global(session: SessionContext, args: Dict[str, Any]) -> Any:
    session.go_global()
    return format_response(
        session,
        "Reset to global scope.",
        suggested_actions=["amof_list_ecosystems", "amof_get_active_runs"],
    )


def _set_mode(session: SessionContext, args: Dict[str, Any]) -> Any:
    mode = args.get("mode", "").strip()
    if session.set_mode(mode):
        return format_response(session, f"Mode set to: {mode}")
    return format_error(
        session,
        f"Invalid mode: {mode!r}. Must be one of: ask, plan, execute.",
    )


def _confirm(session: SessionContext, args: Dict[str, Any]) -> Any:
    token = args.get("token", "").strip()
    typed_value = args.get("typed_value")
    if not token:
        return format_error(session, "Missing required argument: token")

    entry = _confirmations.consume(token, typed_value=typed_value)
    if not entry:
        pending = _confirmations.list_pending()
        if pending:
            tokens = ", ".join(p.token for p in pending)
            return format_error(
                session,
                f"Invalid or expired token. Active tokens: {tokens}",
            )
        return format_error(session, "No pending confirmations.")

    result = entry.execute_fn()
    return result


def _cancel_confirm(session: SessionContext, args: Dict[str, Any]) -> Any:
    token = args.get("token", "").strip()
    if not token:
        return format_error(session, "Missing required argument: token")
    if _confirmations.cancel(token):
        return format_response(session, "Confirmation cancelled.")
    return format_error(session, "Token not found or already expired.")


def _register_core_tools() -> None:
    """Register the built-in scope and session tools."""
    register_tool(
        "amof_scope_info",
        "Show current scope, mode, and breadcrumb.",
        _scope_info,
    )
    register_tool(
        "amof_scope_back",
        "Navigate back to the previous scope.",
        _scope_back,
    )
    register_tool(
        "amof_use_ecosystem",
        "Enter an ecosystem scope by name.",
        _use_ecosystem,
        params={
            "properties": {"name": {"type": "string", "description": "Ecosystem name"}},
            "required": ["name"],
        },
    )
    register_tool(
        "amof_scope_global",
        "Reset scope to global.",
        _scope_global,
    )
    register_tool(
        "amof_set_mode",
        "Set the interaction mode: ask (read-only), plan (preview), execute (mutate).",
        _set_mode,
        params={
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "One of: ask, plan, execute",
                    "enum": ["ask", "plan", "execute"],
                }
            },
            "required": ["mode"],
        },
    )
    register_tool(
        "amof_confirm",
        "Confirm a pending dangerous action by providing the confirmation token.",
        _confirm,
        params={
            "properties": {
                "token": {"type": "string", "description": "Confirmation token"},
                "typed_value": {
                    "type": "string",
                    "description": "For type-confirm: the value to type to confirm",
                },
            },
            "required": ["token"],
        },
    )
    register_tool(
        "amof_cancel_confirm",
        "Cancel a pending confirmation.",
        _cancel_confirm,
        params={
            "properties": {
                "token": {"type": "string", "description": "Confirmation token to cancel"},
            },
            "required": ["token"],
        },
    )


def create_server() -> McpServer:
    """Build and return the fully-wired MCP server (but don't start it)."""
    _register_core_tools()

    from amof.mcp.tools import global_tools
    from amof.mcp.tools import ecosystem_tools
    from amof.mcp.tools import run_tools
    from amof.mcp.tools import release_tools
    from amof.mcp.tools import server_tools
    from amof.mcp.tools import telemetry_tools

    return _server


def main() -> None:
    """Entry point: create server and run the stdio loop."""
    workspace_root = os.environ.get("AMOF_CWD", os.getcwd())
    os.chdir(workspace_root)
    sys.stderr.write(f"[mcp] workspace: {workspace_root}\n")

    server = create_server()
    server.run()


if __name__ == "__main__":
    main()
