"""Shell tool -- mirrors Cursor's Shell tool.

Executes shell commands via subprocess with timeout and working directory support.
Safety checks (blocked commands, dangerous patterns) are loaded from
.amof/rules/guardrails.yaml — no hardcoded lists in Python.
"""

from __future__ import annotations

import logging
import os
import re
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)

# Known stderr noise patterns from kubectl that inflate context without value
_KUBECTL_NOISE_RE = re.compile(
    r"E\d{4}\s+\d+:\d+:\d+\.\d+\s+\d+\s+memcache\.go:\d+\].*"
)


def _filter_kubectl_stderr(output: str) -> str:
    """Remove known kubectl stderr noise lines that waste context tokens.

    Only applied when the command looks like a kubectl/helm command.
    """
    lines = output.split("\n")
    filtered = []
    noise_count = 0
    for line in lines:
        if _KUBECTL_NOISE_RE.match(line.strip()):
            noise_count += 1
        else:
            filtered.append(line)
    result = "\n".join(filtered)
    if noise_count > 0:
        result = result.rstrip() + f"\n[{noise_count} kubectl stderr warnings filtered]"
    return result

_explainer = None

def _get_explainer():
    global _explainer
    if _explainer is None:
        try:
            from ...error_explainer import ErrorExplainer
            _explainer = ErrorExplainer
        except ImportError:
            _explainer = False
    return _explainer if _explainer is not False else None


class ShellTool(Tool):
    name = "Shell"
    description = (
        "Executes a command in a shell session. The working directory can be "
        "specified. Commands that exceed the timeout are terminated. "
        "Dangerous commands are blocked per .amof/rules/guardrails.yaml."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command to execute.",
            },
            "working_directory": {
                "type": "string",
                "description": "The directory to execute the command in. Defaults to workspace root.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds. Default 30.",
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        default_cwd: Optional[str] = None,
        stop_checker: Optional[Callable[[], bool]] = None,
    ):
        self._default_cwd = default_cwd
        self._stop_checker = stop_checker

    def execute(
        self,
        command: str,
        working_directory: Optional[str] = None,
        timeout: Optional[int] = 30,
    ) -> ToolResult:
        # Note: blocked_commands check is now handled by Guardrails.check_shell()
        # which is called by ToolRegistry._check_guardrails() before execute().
        # ShellTool.execute() no longer does its own safety checks — config-driven.

        # Strip trailing & to prevent background processes
        clean_command = command.rstrip()
        if clean_command.endswith("&"):
            clean_command = clean_command[:-1].rstrip()

        cwd = working_directory or self._default_cwd
        if cwd and not Path(cwd).is_dir():
            return ToolResult(
                success=False,
                output="",
                error=f"Working directory does not exist: {cwd}",
            )

        timeout_secs = timeout or 30
        start = time.monotonic()

        try:
            if self._stop_checker and self._stop_checker():
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Command cancelled before start due to stop request ({elapsed_ms}ms elapsed)",
                    cancelled=True,
                )

            proc = subprocess.Popen(
                clean_command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=None,  # inherit current env
                start_new_session=True,  # own process group for clean kills
            )
            assert proc.stdout is not None
            os.set_blocking(proc.stdout.fileno(), False)

            deadline_at = time.monotonic() + timeout_secs
            output_chunks: list[bytes] = []
            stop_terminate_requested_at: Optional[float] = None
            stop_kill_requested = False
            cancelled = False
            timed_out = False

            def _drain_pending() -> None:
                try:
                    chunk = proc.stdout.read()
                except (BlockingIOError, ValueError):
                    chunk = None
                if chunk:
                    output_chunks.append(chunk)

            while True:
                ready, _, _ = select.select([proc.stdout], [], [], 0.2)
                if ready:
                    _drain_pending()

                if self._stop_checker and self._stop_checker() and proc.poll() is None:
                    cancelled = True
                    if stop_terminate_requested_at is None:
                        proc.terminate()
                        stop_terminate_requested_at = time.monotonic()
                    elif (
                        not stop_kill_requested
                        and time.monotonic() - stop_terminate_requested_at >= 5.0
                    ):
                        proc.kill()
                        stop_kill_requested = True

                if (
                    not cancelled
                    and proc.poll() is None
                    and time.monotonic() >= deadline_at
                ):
                    timed_out = True
                    proc.terminate()
                    stop_terminate_requested_at = time.monotonic()

                if proc.poll() is not None:
                    _drain_pending()
                    break

            return_code = proc.wait()
            elapsed_ms = int((time.monotonic() - start) * 1000)
            output = b"".join(output_chunks).decode("utf-8", errors="replace").rstrip()
            proc.stdout.close()

            # Filter kubectl/helm noise to save context tokens
            cmd_base = clean_command.split()[0] if clean_command.strip() else ""
            if cmd_base in ("kubectl", "helm", "k"):
                output = _filter_kubectl_stderr(output)

            if cancelled:
                return ToolResult(
                    success=False,
                    output=(
                        f"Exit code: 130\n\n{output}\n\n"
                        f"Command cancelled after {elapsed_ms}ms due to stop request."
                    ).strip(),
                    error="Command cancelled due to stop request",
                    cancelled=True,
                )

            if timed_out:
                return ToolResult(
                    success=False,
                    output=(
                        f"Exit code: 124\n\n{output}\n\n"
                        f"Command timed out after {timeout_secs}s ({elapsed_ms}ms elapsed)."
                    ).strip(),
                    error=f"Command timed out after {timeout_secs}s ({elapsed_ms}ms elapsed)",
                )

            if proc.returncode == 0:
                return ToolResult(
                    success=True,
                    output=f"Exit code: 0\n\n{output}\n\nCommand completed in {elapsed_ms}ms.",
                )
            else:
                return ToolResult(
                    success=False,
                    output=f"Exit code: {return_code}\n\n{output}\n\nCommand completed in {elapsed_ms}ms.",
                    error=f"Command exited with code {return_code}",
                )
        except FileNotFoundError:
            cmd_name = clean_command.split()[0] if clean_command.strip() else "unknown"
            ex = _get_explainer()
            msg = ex.command_not_found(cmd_name) if ex else f"Command not found: {cmd_name}"
            return ToolResult(success=False, output="", error=msg)
        except Exception as e:
            ex = _get_explainer()
            msg = ex.wrap_error(e, f"Executing: {clean_command[:80]}") if ex else f"Shell execution error: {type(e).__name__}: {e}"
            return ToolResult(success=False, output="", error=msg)
