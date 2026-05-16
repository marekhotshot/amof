"""Release-scope MCP tools: status, validate, log, bump, promote."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from amof.mcp.server import register_tool, get_confirmations
from amof.mcp.session import SessionContext
from amof.mcp.decorators import mode_aware
from amof.mcp.formatters import format_response, format_table, format_error, format_kv


@mode_aware(safety="read-only")
def _release_status(session: SessionContext, args: Dict[str, Any]) -> Any:
    """Show current version, latest tag, and pre-release stage."""
    try:
        result = _run_cli_capture(["release", "status"])
    except Exception as exc:
        return format_error(session, f"Release status failed: {exc}")

    return format_response(
        session,
        "Release status",
        details=result,
        suggested_actions=["amof_release_validate", "amof_release_log"],
    )


@mode_aware(safety="read-only")
def _release_validate(session: SessionContext, args: Dict[str, Any]) -> Any:
    """Run pre-release validation checks."""
    try:
        result = _run_cli_capture(["release", "validate"])
    except Exception as exc:
        return format_error(session, f"Release validation failed: {exc}")

    return format_response(
        session,
        "Release validation",
        details=result,
        suggested_actions=["amof_release_bump", "amof_release_promote"],
    )


@mode_aware(safety="read-only")
def _release_log(session: SessionContext, args: Dict[str, Any]) -> Any:
    """Show release history (tags and changelog)."""
    limit = args.get("limit", 10)
    try:
        result = _run_cli_capture(["release", "log", "--limit", str(limit)])
    except Exception as exc:
        return format_error(session, f"Release log failed: {exc}")

    return format_response(
        session,
        "Release log",
        details=result,
    )


@mode_aware(safety="dangerous", confirm="preview-confirm")
def _release_bump(session: SessionContext, args: Dict[str, Any]) -> Any:
    """Bump version (patch/minor/major) with optional pre-release stage."""
    bump_type = args.get("bump_type", "patch")
    pre = args.get("pre_release")

    if bump_type not in ("patch", "minor", "major"):
        return format_error(session, f"Invalid bump type: {bump_type}. Use patch, minor, or major.")

    cli_args = ["release", bump_type]
    if pre:
        cli_args.append(f"--{pre}")

    if args.get("_dry_run"):
        cli_args.append("--dry-run")
        try:
            result = _run_cli_capture(cli_args)
        except Exception as exc:
            return format_error(session, f"Dry run failed: {exc}")
        return format_response(
            session,
            f"[plan] Release bump preview ({bump_type})",
            details=result,
            suggested_actions=["amof_set_mode(execute)", "amof_release_status"],
        )

    description = f"Bump version: {bump_type}" + (f" --{pre}" if pre else "")
    confirmations = get_confirmations()

    def _execute() -> Any:
        cli_args_exec = ["release", bump_type, "--yes"]
        if pre:
            cli_args_exec.append(f"--{pre}")
        try:
            result = _run_cli_capture(cli_args_exec)
        except Exception as exc:
            return format_error(session, f"Release bump failed: {exc}")
        return format_response(session, f"Release bumped: {bump_type}", details=result)

    try:
        preview = _run_cli_capture(cli_args + ["--dry-run"])
    except Exception:
        preview = "(preview unavailable)"

    entry = confirmations.create(
        tool_name="amof_release_bump",
        description=description,
        preview=preview,
        execute_fn=_execute,
        confirm_type="simple",
    )

    return format_response(
        session,
        f"Confirm: {description}",
        details=f"Preview:\n{preview}\n\nToken: {entry.token}\n→ amof_confirm(token=\"{entry.token}\")",
        suggested_actions=[f"amof_confirm({entry.token})", "amof_cancel_confirm"],
    )


@mode_aware(safety="dangerous", confirm="preview-confirm")
def _release_promote(session: SessionContext, args: Dict[str, Any]) -> Any:
    """Promote pre-release to next stage (alpha -> beta -> rc -> stable)."""
    target = args.get("target")

    cli_args = ["release", "promote"]
    if target:
        cli_args.append(f"--{target}")

    if args.get("_dry_run"):
        cli_args.append("--dry-run")
        try:
            result = _run_cli_capture(cli_args)
        except Exception as exc:
            return format_error(session, f"Dry run failed: {exc}")
        return format_response(
            session,
            f"[plan] Release promote preview",
            details=result,
            suggested_actions=["amof_set_mode(execute)"],
        )

    description = "Promote release" + (f" to {target}" if target else "")
    confirmations = get_confirmations()

    def _execute() -> Any:
        exec_args = ["release", "promote", "--yes"]
        if target:
            exec_args.append(f"--{target}")
        try:
            result = _run_cli_capture(exec_args)
        except Exception as exc:
            return format_error(session, f"Promote failed: {exc}")
        return format_response(session, "Release promoted.", details=result)

    try:
        preview = _run_cli_capture(cli_args + ["--dry-run"])
    except Exception:
        preview = "(preview unavailable)"

    entry = confirmations.create(
        tool_name="amof_release_promote",
        description=description,
        preview=preview,
        execute_fn=_execute,
        confirm_type="simple",
    )

    return format_response(
        session,
        f"Confirm: {description}",
        details=f"Preview:\n{preview}\n\nToken: {entry.token}\n→ amof_confirm(token=\"{entry.token}\")",
        suggested_actions=[f"amof_confirm({entry.token})", "amof_cancel_confirm"],
    )


def _run_cli_capture(cli_args: List[str]) -> str:
    """Run an AMOF CLI command and capture stdout+stderr."""
    script = Path("scripts/amof.py")
    cmd = [sys.executable, str(script)] + cli_args
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and not output.strip():
        output = f"(exit code {proc.returncode})"
    return output.strip()


# ── Registration ──

register_tool(
    "amof_release_status",
    "Show current release version, latest tag, and pre-release stage.",
    _release_status,
)

register_tool(
    "amof_release_validate",
    "Run pre-release validation checks (clean tree, changelog, etc.).",
    _release_validate,
)

register_tool(
    "amof_release_log",
    "Show release history: recent tags and changelog entries.",
    _release_log,
    params={
        "properties": {
            "limit": {"type": "integer", "description": "Number of entries (default 10)"},
        },
    },
)

register_tool(
    "amof_release_bump",
    "Bump the release version (patch/minor/major) with optional pre-release stage.",
    _release_bump,
    params={
        "properties": {
            "bump_type": {
                "type": "string",
                "description": "Version bump type",
                "enum": ["patch", "minor", "major"],
            },
            "pre_release": {
                "type": "string",
                "description": "Pre-release stage: alpha, beta, rc",
                "enum": ["alpha", "beta", "rc"],
            },
        },
        "required": ["bump_type"],
    },
)

register_tool(
    "amof_release_promote",
    "Promote current pre-release to next stage (alpha->beta->rc->stable).",
    _release_promote,
    params={
        "properties": {
            "target": {
                "type": "string",
                "description": "Target stage: beta, rc, or omit for stable",
                "enum": ["beta", "rc"],
            },
        },
    },
)
