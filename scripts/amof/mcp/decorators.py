"""MCP tool decorators: mode enforcement and scope requirements.

These wrap tool handlers to apply cross-cutting policies before
the handler body runs.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Dict, List, Optional, Sequence

from amof.mcp.session import SessionContext, ScopeError, Scope
from amof.mcp.protocol import McpError


def mode_aware(
    safety: str = "read-only",
    confirm: Optional[str] = None,
) -> Callable:
    """Decorator that enforces mode policy on a tool handler.

    Args:
        safety: One of ``read-only``, ``safe-write``, ``dangerous``.
        confirm: Confirmation type for dangerous tools:
                 ``simple``, ``preview-confirm``, ``type-confirm``, or ``None``.

    The decorated function receives ``(session, args)`` and returns MCP content.
    """

    def decorator(fn: Callable) -> Callable:
        fn._mcp_safety = safety  # type: ignore[attr-defined]
        fn._mcp_confirm = confirm  # type: ignore[attr-defined]

        @functools.wraps(fn)
        def wrapper(session: SessionContext, args: Dict[str, Any]) -> Any:
            mode = session.mode

            if safety == "read-only":
                return fn(session, args)

            if safety == "safe-write":
                if mode == "ask":
                    raise McpError(
                        -1,
                        f"This tool modifies state. Switch to plan or execute mode first. "
                        f"(current mode: {mode})",
                    )
                return fn(session, args)

            if safety == "dangerous":
                if mode == "ask":
                    raise McpError(
                        -1,
                        f"Dangerous operation blocked in ask mode. Switch to execute mode.",
                    )
                if mode == "plan":
                    args["_dry_run"] = True
                return fn(session, args)

            return fn(session, args)

        return wrapper

    return decorator


def requires_scope(*scopes: Scope) -> Callable:
    """Decorator that ensures the session is in one of the required scopes.

    Usage::

        @requires_scope("ecosystem", "run")
        def my_tool(session, args):
            eco = session.requires_ecosystem()
            ...
    """

    def decorator(fn: Callable) -> Callable:

        @functools.wraps(fn)
        def wrapper(session: SessionContext, args: Dict[str, Any]) -> Any:
            if session.current_scope not in scopes:
                allowed = ", ".join(scopes)
                raise McpError(
                    -1,
                    f"This tool requires scope: {allowed}. "
                    f"Current scope: {session.current_scope}. "
                    f"Navigate with amof_use_ecosystem / amof_scope_back.",
                )
            return fn(session, args)

        return wrapper

    return decorator
