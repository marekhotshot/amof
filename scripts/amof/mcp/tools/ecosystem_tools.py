"""Ecosystem-scope MCP tools: describe, status, validate, tickets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from amof.mcp.server import register_tool, get_confirmations
from amof.mcp.session import SessionContext
from amof.mcp.decorators import mode_aware, requires_scope
from amof.mcp.formatters import (
    format_response, format_table, format_error, format_kv, text_content,
)
from amof.mcp._ecosystem_helpers import load_ecosystem_manifest


@mode_aware(safety="read-only")
@requires_scope("ecosystem")
def _describe_ecosystem(session: SessionContext, args: Dict[str, Any]) -> Any:
    eco = session.requires_ecosystem()
    try:
        manifest = load_ecosystem_manifest(eco)
    except Exception as exc:
        return format_error(session, f"Cannot load manifest: {exc}")

    repos = manifest.get("repos", [])
    repo_rows = []
    for r in repos:
        name = r.get("name", "?")
        branch = r.get("branch", "main")
        ro = "yes" if r.get("readonly") else "no"
        repo_rows.append([name, branch, ro])

    table = format_table(["Repo", "Branch", "Readonly"], repo_rows) if repo_rows else "(no repos)"

    workspace = manifest.get("workspace", {})
    provisioner = manifest.get("provisioner", "-")

    kv = format_kv([
        ("Ecosystem", eco),
        ("Repos", str(len(repos))),
        ("Provisioner", str(provisioner)),
        ("Branch prefix", workspace.get("branch_prefix", "-")),
    ])

    return format_response(
        session,
        f"Ecosystem: {eco} ({len(repos)} repos)",
        details=kv + "\n\n" + table,
        suggested_actions=["amof_get_ecosystem_status", "amof_ticket_list", "amof_validate_manifest"],
    )


@mode_aware(safety="read-only")
@requires_scope("ecosystem")
def _get_ecosystem_status(session: SessionContext, args: Dict[str, Any]) -> Any:
    eco = session.requires_ecosystem()
    from amof.state import get_state, get_active_ticket

    state = get_state()
    if not state:
        return format_response(
            session,
            f"No workspace state for {eco}. Run install first.",
            suggested_actions=["amof_install_ecosystem"],
        )

    active_ticket = get_active_ticket()
    repos = state.get("repos", [])
    repo_rows = []
    for r in repos:
        name = r.get("name", "?")
        mode = "RO" if r.get("readonly") else "RW"
        lp = r.get("last_push", {})
        commit = lp.get("commit", "-")[:8] if lp else "-"
        branch = lp.get("branch", r.get("branch", "-"))
        repo_rows.append([name, branch, commit, mode])

    table = format_table(["Repo", "Branch", "Commit", "Mode"], repo_rows)

    kv = format_kv([
        ("Ecosystem", state.get("ecosystem", eco)),
        ("Workspace branch", state.get("workspace_branch", "-")),
        ("Active ticket", active_ticket or "(none)"),
        ("Tickets", str(len(state.get("tickets", {})))),
    ])

    return format_response(
        session,
        f"Status: {eco} | ticket: {active_ticket or 'none'}",
        details=kv + "\n\n" + table,
        suggested_actions=["amof_sync_ecosystem", "amof_ticket_list"],
    )


@mode_aware(safety="read-only")
@requires_scope("ecosystem")
def _validate_manifest(session: SessionContext, args: Dict[str, Any]) -> Any:
    eco = session.requires_ecosystem()
    strict = args.get("strict", False)

    try:
        manifest = load_ecosystem_manifest(eco)
    except Exception as exc:
        return format_error(session, f"Cannot load manifest: {exc}")

    from amof.manifest import validate_manifest

    errors = validate_manifest(manifest, detailed=True, strict=strict)
    if not errors:
        return format_response(
            session,
            f"Manifest valid ({eco}).",
            suggested_actions=["amof_describe_ecosystem"],
        )

    details = "\n".join(f"  {e}" for e in errors)
    return format_response(
        session,
        f"Manifest has {len(errors)} issue(s).",
        details=details,
    )


@mode_aware(safety="read-only")
@requires_scope("ecosystem")
def _ticket_list(session: SessionContext, args: Dict[str, Any]) -> Any:
    from amof.state import get_all_tickets, get_active_ticket

    tickets = get_all_tickets()
    active = get_active_ticket()

    if not tickets:
        return format_response(
            session,
            "No tickets tracked.",
            suggested_actions=["amof_ticket_start"],
        )

    rows = []
    for tid, info in tickets.items():
        is_active = "yes" if tid == active else ""
        repo_count = str(len(info.get("repos", {})))
        created = info.get("created_at", "-")[:10]
        rows.append([tid, is_active, repo_count, created])

    table = format_table(["Ticket", "Active", "Repos", "Created"], rows)
    return format_response(
        session,
        f"{len(tickets)} ticket(s), active: {active or 'none'}",
        details=table,
        suggested_actions=["amof_ticket_start", "amof_ticket_switch"],
    )


# ── Phase 2: Write operations (subprocess-based) ──

@mode_aware(safety="safe-write")
@requires_scope("ecosystem")
def _install_ecosystem(session: SessionContext, args: Dict[str, Any]) -> Any:
    return _run_subprocess_action(session, "install", "Install")


@mode_aware(safety="safe-write")
@requires_scope("ecosystem")
def _sync_ecosystem(session: SessionContext, args: Dict[str, Any]) -> Any:
    return _run_subprocess_action(session, "sync", "Sync")


@mode_aware(safety="dangerous", confirm="simple")
@requires_scope("ecosystem")
def _push_ecosystem(session: SessionContext, args: Dict[str, Any]) -> Any:
    if args.get("_dry_run"):
        return format_response(
            session,
            "[plan] Would push all repo branches to remote.",
            suggested_actions=["amof_set_mode(execute)"],
        )
    return _run_dangerous_action(
        session, "push", "Push all branches to remote",
        confirm_type="simple",
    )


@mode_aware(safety="dangerous", confirm="simple")
@requires_scope("ecosystem")
def _spin_deploy(session: SessionContext, args: Dict[str, Any]) -> Any:
    if args.get("_dry_run"):
        return format_response(
            session,
            "[plan] Would deploy spin environment.",
            suggested_actions=["amof_set_mode(execute)"],
        )
    return _run_dangerous_action(
        session, "spin/deploy", "Deploy spin environment",
        confirm_type="simple",
    )


@mode_aware(safety="dangerous", confirm="type-confirm")
@requires_scope("ecosystem")
def _spin_destroy(session: SessionContext, args: Dict[str, Any]) -> Any:
    eco = session.requires_ecosystem()
    if args.get("_dry_run"):
        return format_response(
            session,
            "[plan] Would destroy spin environment.",
            suggested_actions=["amof_set_mode(execute)"],
        )
    return _run_dangerous_action(
        session, "spin/destroy", "Destroy spin environment",
        confirm_type="type-confirm", type_target=eco,
    )


@mode_aware(safety="dangerous", confirm="preview-confirm")
@requires_scope("ecosystem")
def _archive_ecosystem(session: SessionContext, args: Dict[str, Any]) -> Any:
    if args.get("_dry_run"):
        return format_response(
            session,
            "[plan] Would archive workspace (push + save state + delete workspace branch).",
            suggested_actions=["amof_set_mode(execute)"],
        )
    return _run_dangerous_action(
        session, "archive", "Archive workspace",
        confirm_type="simple",
    )


@mode_aware(safety="dangerous", confirm="type-confirm")
@requires_scope("ecosystem")
def _discard_ecosystem(session: SessionContext, args: Dict[str, Any]) -> Any:
    eco = session.requires_ecosystem()
    if args.get("_dry_run"):
        return format_response(
            session,
            "[plan] Would discard workspace (delete everything, no push).",
            suggested_actions=["amof_set_mode(execute)"],
        )
    return _run_dangerous_action(
        session, "discard", "Discard workspace permanently",
        confirm_type="type-confirm", type_target=eco,
    )


@mode_aware(safety="safe-write")
@requires_scope("ecosystem")
def _ticket_start(session: SessionContext, args: Dict[str, Any]) -> Any:
    ticket_id = args.get("ticket_id", "").strip()
    if not ticket_id:
        return format_error(session, "Missing required argument: ticket_id")
    return _run_subprocess_action(session, "ticket-start", "Start ticket", ticket_id=ticket_id)


@mode_aware(safety="safe-write")
@requires_scope("ecosystem")
def _ticket_switch(session: SessionContext, args: Dict[str, Any]) -> Any:
    ticket_id = args.get("ticket_id", "").strip()
    if not ticket_id:
        return format_error(session, "Missing required argument: ticket_id")
    return _run_subprocess_action(session, "ticket-switch", "Switch ticket", ticket_id=ticket_id)


@mode_aware(safety="safe-write")
@requires_scope("ecosystem")
def _ticket_end(session: SessionContext, args: Dict[str, Any]) -> Any:
    ticket_id = args.get("ticket_id", "").strip()
    if not ticket_id:
        return format_error(session, "Missing required argument: ticket_id")
    return _run_subprocess_action(session, "ticket-end", "End ticket", ticket_id=ticket_id)


# ── Helpers ──

def _run_subprocess_action(
    session: SessionContext,
    action: str,
    label: str,
    ticket_id: str = "",
) -> Any:
    """Launch a CLI action via RunManager and return the run_id."""
    eco = session.requires_ecosystem()
    from amof.api.command_builder import get_workspace_root, build_command
    from amof.api.dependencies import run_manager

    root = get_workspace_root()

    if action == "ticket-start":
        from amof.api.command_builder import build_ticket_start_command
        cmd, cwd = build_ticket_start_command(root, eco, ticket_id)
    elif action == "ticket-switch":
        from amof.api.command_builder import build_ticket_switch_command
        cmd, cwd = build_ticket_switch_command(root, eco, ticket_id)
    elif action == "ticket-end":
        from amof.api.command_builder import build_ticket_end_command
        cmd, cwd = build_ticket_end_command(root, eco, ticket_id)
    else:
        cmd, cwd = build_command(root, action, eco)

    run_id = run_manager.create_run(eco, action, cmd)

    import subprocess
    import threading

    def _execute() -> None:
        run_manager.update_status(run_id, "running")
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd),
                capture_output=True, text=True, timeout=300,
            )
            for line in (proc.stdout or "").splitlines():
                run_manager.append_log(run_id, line)
            for line in (proc.stderr or "").splitlines():
                run_manager.append_log(run_id, f"[stderr] {line}")
            status = "success" if proc.returncode == 0 else "failed"
            run_manager.update_status(run_id, status, exit_code=proc.returncode)
        except Exception as exc:
            run_manager.append_log(run_id, f"[error] {exc}")
            run_manager.update_status(run_id, "failed", exit_code=-1)

    t = threading.Thread(target=_execute, daemon=True)
    t.start()

    return format_response(
        session,
        f"{label} started. Run ID: {run_id[:8]}",
        details=f"  run_id: {run_id}\n  command: {' '.join(cmd)}",
        suggested_actions=["amof_get_run_status", "amof_get_run_logs"],
    )


def _run_dangerous_action(
    session: SessionContext,
    action: str,
    description: str,
    confirm_type: str = "simple",
    type_target: str = "",
) -> Any:
    """Stage a dangerous action behind confirmation."""
    eco = session.requires_ecosystem()
    confirmations = get_confirmations()

    def _execute() -> Any:
        return _run_subprocess_action(session, action, description)

    entry = confirmations.create(
        tool_name=f"amof_{action.replace('/', '_')}",
        description=description,
        preview=f"Will run: amof -e {eco} {action}",
        execute_fn=_execute,
        confirm_type=confirm_type,
        type_target=type_target or None,
    )

    parts = [f"Confirm: {description}"]
    parts.append(f"  Preview: amof -e {eco} {action}")
    parts.append(f"  Token: {entry.token}")
    if confirm_type == "type-confirm":
        parts.append(f"  To confirm, type: {type_target}")
        parts.append(f'  → amof_confirm(token="{entry.token}", typed_value="{type_target}")')
    else:
        parts.append(f'  → amof_confirm(token="{entry.token}")')

    return format_response(
        session,
        f"Confirmation required for: {description}",
        details="\n".join(parts),
        suggested_actions=[f'amof_confirm({entry.token})', "amof_cancel_confirm"],
    )


# ── Registration ──

register_tool(
    "amof_describe_ecosystem",
    "Show ecosystem manifest details: repos, provisioner, branch prefix.",
    _describe_ecosystem,
)

register_tool(
    "amof_get_ecosystem_status",
    "Show workspace status: active ticket, repo branches, push state.",
    _get_ecosystem_status,
)

register_tool(
    "amof_validate_manifest",
    "Validate the ecosystem manifest schema.",
    _validate_manifest,
    params={
        "properties": {"strict": {"type": "boolean", "description": "Treat warnings as errors"}},
    },
)

register_tool(
    "amof_ticket_list",
    "List all tracked tickets and which is active.",
    _ticket_list,
)

register_tool(
    "amof_install_ecosystem",
    "Install the ecosystem workspace (clone repos, create branches).",
    _install_ecosystem,
)

register_tool(
    "amof_sync_ecosystem",
    "Sync repos with their upstream branches.",
    _sync_ecosystem,
)

register_tool(
    "amof_push_ecosystem",
    "Push all repo feature branches to remote. Requires confirmation.",
    _push_ecosystem,
)

register_tool(
    "amof_spin_deploy",
    "Deploy the spin environment. Requires confirmation.",
    _spin_deploy,
)

register_tool(
    "amof_spin_destroy",
    "Destroy the spin environment. Requires type-to-confirm.",
    _spin_destroy,
)

register_tool(
    "amof_archive_ecosystem",
    "Archive workspace: push, save state, delete workspace branch. Requires confirmation.",
    _archive_ecosystem,
)

register_tool(
    "amof_discard_ecosystem",
    "Discard workspace permanently. Requires type-to-confirm.",
    _discard_ecosystem,
)

register_tool(
    "amof_ticket_start",
    "Start a new ticket (creates feature branches in repos).",
    _ticket_start,
    params={
        "properties": {"ticket_id": {"type": "string", "description": "Ticket ID (e.g. PROJ-123)"}},
        "required": ["ticket_id"],
    },
)

register_tool(
    "amof_ticket_switch",
    "Switch to an existing ticket.",
    _ticket_switch,
    params={
        "properties": {"ticket_id": {"type": "string", "description": "Ticket ID to switch to"}},
        "required": ["ticket_id"],
    },
)

register_tool(
    "amof_ticket_end",
    "End a ticket (can optionally clean up branches).",
    _ticket_end,
    params={
        "properties": {"ticket_id": {"type": "string", "description": "Ticket ID to end"}},
        "required": ["ticket_id"],
    },
)
