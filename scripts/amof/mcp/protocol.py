"""Minimal MCP (JSON-RPC 2.0 over stdio) protocol implementation.

Implements the subset of MCP needed for tool serving:
  initialize -> tools/list -> tools/call

No external dependencies beyond the Python stdlib.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

MCP_PROTOCOL_VERSION = "2024-11-05"


class McpError(Exception):
    """MCP-level error returned to the client."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _read_message() -> Optional[Dict[str, Any]]:
    """Read one JSON-RPC message from stdin (newline-delimited)."""
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    return json.loads(line)


def _write_message(msg: Dict[str, Any]) -> None:
    """Write one JSON-RPC message to stdout."""
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _ok_response(id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _error_response(id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": err}


ToolSchema = Dict[str, Any]
ToolHandler = Callable[[Dict[str, Any]], Any]


class McpServer:
    """Lightweight MCP server over stdio (JSON-RPC 2.0, newline-delimited).

    Usage::

        server = McpServer(name="amof", version="0.1.0")

        @server.tool("amof_list_ecosystems", description="...", params={...})
        def list_ecosystems(args):
            return [{"type": "text", "text": "..."}]

        server.run()
    """

    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self._tools: Dict[str, Tuple[ToolSchema, ToolHandler]] = {}
        self._initialized = False
        self._on_initialize: Optional[Callable[[], None]] = None

    def tool(
        self,
        name: str,
        description: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Callable:
        """Decorator to register an MCP tool."""
        schema: ToolSchema = {
            "name": name,
            "description": description,
        }
        if params:
            schema["inputSchema"] = {
                "type": "object",
                "properties": params.get("properties", {}),
                "required": params.get("required", []),
            }
        else:
            schema["inputSchema"] = {"type": "object", "properties": {}}

        def decorator(fn: ToolHandler) -> ToolHandler:
            self._tools[name] = (schema, fn)
            return fn

        return decorator

    def register_tool(
        self,
        name: str,
        description: str,
        handler: ToolHandler,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Imperative registration (alternative to decorator)."""
        schema: ToolSchema = {
            "name": name,
            "description": description,
        }
        if params:
            schema["inputSchema"] = {
                "type": "object",
                "properties": params.get("properties", {}),
                "required": params.get("required", []),
            }
        else:
            schema["inputSchema"] = {"type": "object", "properties": {}}
        self._tools[name] = (schema, handler)

    def on_initialize(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Hook called when the client sends `initialize`."""
        self._on_initialize = fn
        return fn

    def _handle_initialize(self, id: Any, params: Dict[str, Any]) -> None:
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": self.name,
                "version": self.version,
            },
        }
        _write_message(_ok_response(id, result))
        self._initialized = True
        if self._on_initialize:
            self._on_initialize()

    def _handle_tools_list(self, id: Any, params: Dict[str, Any]) -> None:
        tools = [schema for schema, _ in self._tools.values()]
        _write_message(_ok_response(id, {"tools": tools}))

    def _handle_tools_call(self, id: Any, params: Dict[str, Any]) -> None:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in self._tools:
            _write_message(
                _error_response(id, METHOD_NOT_FOUND, f"Unknown tool: {tool_name}")
            )
            return

        _, handler = self._tools[tool_name]
        try:
            result = handler(arguments)
            if isinstance(result, list):
                content = result
            elif isinstance(result, str):
                content = [{"type": "text", "text": result}]
            elif isinstance(result, dict) and "content" in result:
                content = result["content"]
                is_error = result.get("isError", False)
                _write_message(
                    _ok_response(id, {"content": content, "isError": is_error})
                )
                return
            else:
                content = [{"type": "text", "text": json.dumps(result, indent=2)}]
            _write_message(_ok_response(id, {"content": content}))
        except McpError as exc:
            _write_message(
                _ok_response(
                    id,
                    {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                )
            )
        except Exception as exc:
            tb = traceback.format_exc()
            sys.stderr.write(f"[mcp] Tool error in {tool_name}: {tb}\n")
            _write_message(
                _ok_response(
                    id,
                    {
                        "content": [
                            {"type": "text", "text": f"Internal error: {exc}"}
                        ],
                        "isError": True,
                    },
                )
            )

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method", "")
        id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            self._handle_initialize(id, params)
        elif method == "initialized":
            pass  # notification, no response
        elif method == "tools/list":
            self._handle_tools_list(id, params)
        elif method == "tools/call":
            self._handle_tools_call(id, params)
        elif method == "notifications/cancelled":
            pass
        elif method == "ping":
            _write_message(_ok_response(id, {}))
        elif id is not None:
            _write_message(
                _error_response(id, METHOD_NOT_FOUND, f"Unknown method: {method}")
            )

    def run(self) -> None:
        """Main event loop: read stdin, dispatch, write stdout. Blocks forever."""
        sys.stderr.write(f"[mcp] {self.name} v{self.version} ready (stdio)\n")
        while True:
            try:
                msg = _read_message()
                if msg is None:
                    break
                self._dispatch(msg)
            except json.JSONDecodeError as exc:
                _write_message(
                    _error_response(None, PARSE_ERROR, f"Parse error: {exc}")
                )
            except KeyboardInterrupt:
                break
            except Exception as exc:
                sys.stderr.write(f"[mcp] Unhandled: {exc}\n")
                traceback.print_exc(file=sys.stderr)
        sys.stderr.write("[mcp] Server stopped.\n")
