"""MCP Suggested Actions engine.

Given a tool invocation and its result, compute a ranked list of contextual
next-step actions for the MCP client to surface to the user.

See docs/mcp-suggested-actions.md for the full design specification.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ── Mode hierarchy (lower = more permissive) ────────────────────

_MODE_RANK = {"ask": 0, "plan": 1, "execute": 2}

_SAFETY_MIN_MODE = {
    "read-only": "ask",
    "safe-write": "plan",
    "dangerous-write": "execute",
}

MAX_ACTIONS = 5
MAX_ACTIONS_INFO_QUERY = 3


@dataclass
class SuggestedAction:
    id: str
    label: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    scope: str = "global"
    mode_required: str = "ask"
    safety: str = "read-only"
    confirm: bool = False
    priority: int = 3
    description: str = ""
    condition: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v or k in ("confirm", "priority")}


# ── Action factory helpers ────────────────────────────────────────

def _a(
    id: str,
    label: str,
    tool: str,
    *,
    args: Optional[Dict[str, Any]] = None,
    scope: str = "global",
    mode_required: str = "ask",
    safety: str = "read-only",
    confirm: bool = False,
    priority: int = 3,
    description: str = "",
    condition: str = "",
) -> SuggestedAction:
    return SuggestedAction(
        id=id,
        label=label,
        tool=tool,
        args=args or {},
        scope=scope,
        mode_required=mode_required,
        safety=safety,
        confirm=confirm,
        priority=priority,
        description=description,
        condition=condition,
    )


# ── Ecosystem-scoped action constructors ──────────────────────────

def _eco_status(eco: str) -> SuggestedAction:
    return _a("eco_status", "Check status", "amof_get_ecosystem_status",
              args={"ecosystem": eco}, scope="ecosystem", priority=2,
              description="Show repo branches, commits, and dirty/unpushed flags")


def _eco_sync(eco: str) -> SuggestedAction:
    return _a("eco_sync", "Sync repos", "amof_sync_ecosystem",
              args={"ecosystem": eco}, scope="ecosystem",
              mode_required="plan", safety="safe-write", priority=3,
              description="Pull latest changes from all remotes")


def _eco_push(eco: str, priority: int = 1) -> SuggestedAction:
    return _a("eco_push", "Push changes", "amof_push_ecosystem",
              args={"ecosystem": eco}, scope="ecosystem",
              mode_required="execute", safety="dangerous-write", confirm=True,
              priority=priority,
              description="Push workspace and all repo feature branches to remote")


def _ticket_start(eco: str) -> SuggestedAction:
    return _a("ticket_start", "Start ticket", "amof_ticket_start",
              args={"ecosystem": eco}, scope="ecosystem",
              mode_required="plan", safety="safe-write", priority=1,
              description="Create feature branches for a new ticket")


def _ticket_list(eco: str) -> SuggestedAction:
    return _a("ticket_list", "List tickets", "amof_ticket_list",
              args={"ecosystem": eco}, scope="ecosystem", priority=3,
              description="Show all tracked tickets and their branches")


def _ticket_switch(eco: str, ticket_id: Optional[str] = None) -> SuggestedAction:
    args: Dict[str, Any] = {"ecosystem": eco}
    if ticket_id:
        args["ticket_id"] = ticket_id
    label = f"Switch to {ticket_id}" if ticket_id else "Switch ticket"
    return _a("ticket_switch", label, "amof_ticket_switch",
              args=args, scope="ecosystem",
              mode_required="plan", safety="safe-write", priority=1,
              description="Checkout feature branches for a different ticket")


# ── Run-scoped action constructors ────────────────────────────────

def _run_status(run_id: str) -> SuggestedAction:
    return _a("run_status", "Refresh status", "amof_get_run_status",
              args={"run_id": run_id}, scope="run", priority=1,
              description="Get current run status and metadata")


def _run_logs(run_id: str) -> SuggestedAction:
    return _a("run_logs", "Show logs", "amof_get_run_logs",
              args={"run_id": run_id}, scope="run", priority=2,
              description="Retrieve log output for this run")


def _run_summary(run_id: str) -> SuggestedAction:
    return _a("run_summary", "View summary", "amof_summarize_run",
              args={"run_id": run_id}, scope="run", priority=1,
              description="Human-readable summary of run outcome")


def _back_to_ecosystem(eco: str) -> SuggestedAction:
    return _a("back_ecosystem", "Back to ecosystem", "amof_get_ecosystem_status",
              args={"ecosystem": eco}, scope="ecosystem", priority=4,
              description="Return to ecosystem status view")


# ── Release-scoped action constructors ────────────────────────────

def _release_status() -> SuggestedAction:
    return _a("release_status", "Release status", "amof_release_status",
              scope="release", priority=2,
              description="Show current version, stage, and next-version options")


def _release_validate() -> SuggestedAction:
    return _a("release_validate", "Validate release", "amof_release_validate",
              scope="release", priority=3,
              description="Run pre-release validation checks")


def _release_log() -> SuggestedAction:
    return _a("release_log", "Release history", "amof_release_log",
              scope="release", priority=3,
              description="Show release audit trail")


def _release_promote(target: str) -> SuggestedAction:
    labels = {"beta": "Promote to beta", "rc": "Promote to RC", "stable": "Promote to stable"}
    return _a(f"release_promote_{target}", labels.get(target, f"Promote to {target}"),
              "amof_release_promote",
              args={"target": target if target != "stable" else None},
              scope="release", mode_required="execute", safety="dangerous-write",
              confirm=True, priority=1,
              description=f"Promote current pre-release to {target}")


def _release_bump_alpha() -> SuggestedAction:
    return _a("release_bump_alpha", "Start alpha", "amof_release_bump",
              args={"part": "patch", "pre": "alpha"},
              scope="release", mode_required="execute", safety="dangerous-write",
              confirm=True, priority=1,
              description="Begin new alpha pre-release cycle")


# ── Global/server actions ─────────────────────────────────────────

def _list_ecosystems() -> SuggestedAction:
    return _a("list_ecosystems", "List ecosystems", "amof_list_ecosystems",
              priority=1, description="Show all available ecosystems")


def _server_status() -> SuggestedAction:
    return _a("server_status", "Server status", "amof_get_server_status",
              scope="server", priority=2,
              description="Check if the control plane server is running")


def _active_runs() -> SuggestedAction:
    return _a("active_runs", "Show active runs", "amof_get_active_runs",
              priority=3, description="List runs across all ecosystems")


# ── Static action sets ────────────────────────────────────────────

_GLOBAL_DEFAULT = [_list_ecosystems, _server_status, _active_runs]

_RELEASE_SETS = {
    "alpha":  [lambda: _release_promote("beta"), _release_status, _release_validate],
    "beta":   [lambda: _release_promote("rc"), _release_validate, _release_log],
    "rc":     [lambda: _release_promote("stable"), _release_validate, _release_log],
    "stable": [_release_log, _release_status],
}


# ── Read-only tool set (for info query throttling) ────────────────

_INFO_TOOLS = frozenset({
    "amof_list_ecosystems",
    "amof_describe_ecosystem",
    "amof_get_ecosystem_status",
    "amof_ticket_list",
    "amof_get_run_logs",
    "amof_get_run_status",
    "amof_summarize_run",
    "amof_release_status",
    "amof_release_log",
    "amof_release_validate",
    "amof_get_server_status",
    "amof_get_global_telemetry",
    "amof_get_active_runs",
    "amof_validate_manifest",
})

_DESTRUCTIVE_TOOLS = frozenset({
    "amof_discard_ecosystem",
    "amof_spin_destroy",
    "amof_archive_ecosystem",
})

_ASYNC_ACTION_TOOLS = frozenset({
    "amof_install_ecosystem",
    "amof_sync_ecosystem",
    "amof_push_ecosystem",
    "amof_spin_deploy",
    "amof_spin_destroy",
    "amof_ticket_start",
    "amof_ticket_switch",
    "amof_ticket_end",
    "amof_validate_manifest",
    "amof_archive_ecosystem",
    "amof_discard_ecosystem",
})


# ── Core inference engine ─────────────────────────────────────────


def _filter_by_mode(actions: List[SuggestedAction], mode: str) -> List[SuggestedAction]:
    mode_rank = _MODE_RANK.get(mode, 0)
    return [a for a in actions if _MODE_RANK.get(a.mode_required, 0) <= mode_rank]


def _deduplicate(actions: List[SuggestedAction]) -> List[SuggestedAction]:
    seen: Dict[str, SuggestedAction] = {}
    for a in actions:
        if a.id not in seen or a.priority < seen[a.id].priority:
            seen[a.id] = a
    return sorted(seen.values(), key=lambda x: x.priority)


def _suppress_just_invoked(actions: List[SuggestedAction], tool_name: str, run_status: Optional[str] = None) -> List[SuggestedAction]:
    result = []
    for a in actions:
        if a.tool == tool_name:
            if tool_name == "amof_get_run_status" and run_status == "running":
                result.append(a)
                continue
            continue
        result.append(a)
    return result


def _infer_from_ecosystem_status(result: Dict[str, Any], eco: str) -> List[SuggestedAction]:
    """Derive actions from amof_get_ecosystem_status output."""
    actions: List[SuggestedAction] = []
    repos = result.get("repos", [])
    workspace = result.get("workspace") or {}
    active_ticket = workspace.get("active_ticket")

    statuses = [r.get("status", "OK") for r in repos]
    status_str = " ".join(statuses)

    has_wrong_branch = "WRONG_BRANCH" in status_str
    has_unpushed = "UNPUSHED" in status_str
    has_dirty = "DIRTY" in status_str
    has_missing = "MISSING" in status_str

    if has_wrong_branch and active_ticket:
        actions.append(_ticket_switch(eco, active_ticket))
        actions[-1].condition = "WRONG_BRANCH detected"

    if has_missing:
        a = _eco_sync(eco)
        a.priority = 1
        a.condition = "MISSING repo detected"
        actions.append(a)

    if has_unpushed:
        a = _eco_push(eco, priority=1)
        a.condition = "UNPUSHED changes detected"
        actions.append(a)

    if has_dirty and not has_unpushed:
        a = _eco_push(eco, priority=2)
        a.condition = "DIRTY repos detected"
        actions.append(a)

    if not active_ticket and workspace.get("ticket_count", 0) == 0:
        a = _ticket_start(eco)
        a.condition = "No active ticket"
        actions.append(a)

    if not actions:
        actions = [_eco_status(eco), _eco_sync(eco)]
        if active_ticket:
            actions.append(_eco_push(eco, priority=3))
        else:
            actions.append(_ticket_start(eco))

    # Pad with standard ecosystem actions so the user always has >=3 options.
    # Include read-only options so ask-mode filtering doesn't leave an empty set.
    existing_ids = {a.id for a in actions}
    padding = [
        ("eco_status", lambda: _eco_status(eco)),
        ("eco_sync", lambda: _eco_sync(eco)),
        ("ticket_list", lambda: _ticket_list(eco)),
    ]
    for pid, factory in padding:
        if pid not in existing_ids and len(actions) < 4:
            actions.append(factory())

    return actions


def _infer_from_run_status(result: Dict[str, Any], eco: Optional[str]) -> List[SuggestedAction]:
    """Derive actions from amof_get_run_status output."""
    run_id = result.get("run_id", "")
    status = result.get("status", "")

    if status == "running":
        return [_run_status(run_id), _run_logs(run_id)]
    elif status == "success":
        actions = [_run_summary(run_id), _run_logs(run_id)]
        if eco:
            actions.append(_back_to_ecosystem(eco))
        return actions
    elif status == "failed":
        actions = [_run_logs(run_id), _run_summary(run_id)]
        if eco:
            actions.append(_back_to_ecosystem(eco))
        return actions
    else:
        return [_run_status(run_id)]


def _infer_from_release_status(result: Dict[str, Any]) -> List[SuggestedAction]:
    """Derive actions from amof_release_status output."""
    stage = result.get("stage", "stable")
    dirty = result.get("dirty", False)
    version_drift = result.get("version_drift", False)
    commits = result.get("commits_since_tag", 0)

    actions: List[SuggestedAction] = []

    if dirty:
        a = _eco_push("")
        a.label = "Commit/push first"
        a.priority = 1
        a.condition = "Uncommitted changes block release"
        actions.append(a)

    if version_drift:
        a = _release_validate()
        a.priority = 1
        a.condition = "Version drift detected"
        actions.append(a)

    if stage in _RELEASE_SETS:
        for factory in _RELEASE_SETS[stage]:
            actions.append(factory())
    elif commits > 0:
        actions.append(_release_bump_alpha())
        actions.append(_release_status())
    else:
        actions.append(_release_log())
        actions.append(_release_status())

    return actions


def _infer_from_release_validate(result: Dict[str, Any]) -> List[SuggestedAction]:
    """Derive actions from amof_release_validate output."""
    ok = result.get("ok", True)
    if not ok:
        return [_release_status()]
    return [_release_status(), _release_log()]


def _default_for_async_action(result: Dict[str, Any], eco: Optional[str]) -> List[SuggestedAction]:
    """After an async action (returns run_id), suggest monitoring tools."""
    run_id = result.get("run_id") or result.get("task_id", "")
    if not run_id:
        return []
    return [_run_status(run_id), _run_logs(run_id)]


# ── Public API ────────────────────────────────────────────────────


def get_suggested_actions(
    tool_name: str,
    tool_result: Dict[str, Any],
    current_mode: str = "execute",
    session_ecosystem: Optional[str] = None,
    session_scope: str = "global",
) -> List[SuggestedAction]:
    """Compute suggested next actions after a tool invocation.

    Args:
        tool_name: The MCP tool that was just invoked.
        tool_result: The JSON-serialisable result dict from the tool.
        current_mode: Current interaction mode: "ask", "plan", or "execute".
        session_ecosystem: Currently active ecosystem (if any).
        session_scope: Current scope context.

    Returns:
        Ordered list of SuggestedAction, max 5 items, mode-filtered.
    """
    eco = session_ecosystem or ""
    actions: List[SuggestedAction] = []

    # Post-destructive cooldown: only suggest returning to global
    if tool_name in _DESTRUCTIVE_TOOLS:
        return [_list_ecosystems()]

    # Async actions: suggest monitoring
    if tool_name in _ASYNC_ACTION_TOOLS:
        return _default_for_async_action(tool_result, eco)

    # Tool-specific inference
    if tool_name == "amof_get_ecosystem_status":
        actions = _infer_from_ecosystem_status(tool_result, eco)

    elif tool_name in ("amof_get_run_status", "amof_get_run_logs", "amof_summarize_run"):
        run_eco = tool_result.get("ecosystem", eco)
        actions = _infer_from_run_status(tool_result, run_eco)

    elif tool_name == "amof_release_status":
        actions = _infer_from_release_status(tool_result)

    elif tool_name == "amof_release_validate":
        actions = _infer_from_release_validate(tool_result)

    elif tool_name in ("amof_release_bump", "amof_release_promote"):
        if tool_result.get("dry_run"):
            actions = [_release_status(), _release_validate()]
        else:
            actions = [_release_log(), _release_status()]

    elif tool_name == "amof_list_ecosystems":
        if tool_result.get("ecosystems"):
            first = tool_result["ecosystems"][0]
            first_name = first.get("name", "") if isinstance(first, dict) else str(first)
            if first_name:
                actions.append(
                    _a("switch_ecosystem", f"Open {first_name}", "amof_switch_ecosystem",
                       args={"ecosystem": first_name}, priority=1,
                       description=f"Switch to {first_name} ecosystem")
                )
        actions.append(_server_status())
        actions.append(_active_runs())

    elif tool_name == "amof_switch_ecosystem":
        new_eco = tool_result.get("active_ecosystem", eco)
        actions = [_eco_status(new_eco), _eco_sync(new_eco), _ticket_list(new_eco)]

    elif tool_name == "amof_describe_ecosystem":
        actions = [_eco_status(eco), _eco_sync(eco)]

    elif tool_name in ("amof_ticket_list", "amof_ticket_start", "amof_ticket_switch", "amof_ticket_end"):
        actions = [_eco_status(eco), _ticket_list(eco)]

    elif tool_name == "amof_get_server_status":
        running = tool_result.get("running", False)
        if running:
            actions.append(
                _a("server_restart", "Restart server", "amof_server_restart",
                   scope="server", mode_required="plan", safety="safe-write", priority=2,
                   description="Restart the AMOF control plane server")
            )
        else:
            actions.append(
                _a("server_start", "Start server", "amof_server_start",
                   scope="server", mode_required="plan", safety="safe-write", priority=1,
                   description="Start the AMOF control plane server")
            )
        actions.append(_list_ecosystems())

    elif tool_name == "amof_validate_manifest":
        actions = [_eco_status(eco), _a("eco_describe", "Describe ecosystem", "amof_describe_ecosystem",
                                         args={"ecosystem": eco}, scope="ecosystem", priority=2,
                                         description="Show full ecosystem metadata")]

    elif tool_name == "amof_create_journal":
        actions = [_eco_status(eco)]

    else:
        if session_scope == "global":
            actions = [fn() for fn in _GLOBAL_DEFAULT]
        elif session_scope == "ecosystem" and eco:
            actions = [_eco_status(eco), _eco_sync(eco), _ticket_list(eco)]
        elif session_scope == "release":
            actions = [_release_status(), _release_log()]
        elif session_scope == "server":
            actions = [_server_status(), _list_ecosystems()]
        else:
            actions = [fn() for fn in _GLOBAL_DEFAULT]

    # Apply suppression: same-action
    run_status = tool_result.get("status") if "run_id" in tool_result else None
    actions = _suppress_just_invoked(actions, tool_name, run_status)

    # Apply mode gating
    actions = _filter_by_mode(actions, current_mode)

    # Deduplicate and sort
    actions = _deduplicate(actions)

    # Post-filter fallback: if all actions were filtered, supply safe defaults
    if not actions:
        if session_scope == "ecosystem" and eco:
            actions = [_ticket_list(eco)]
        elif session_scope == "release":
            actions = [_release_log()]
        elif session_scope == "run":
            pass  # empty is acceptable if run_id context is lost
        else:
            actions = [_list_ecosystems()]
        actions = _suppress_just_invoked(actions, tool_name)

    # Info query throttling
    is_info_query = tool_name in _INFO_TOOLS
    limit = MAX_ACTIONS_INFO_QUERY if is_info_query else MAX_ACTIONS
    actions = actions[:limit]

    return actions


def format_actions_text(actions: List[SuggestedAction]) -> str:
    """Format actions for text-based MCP clients."""
    if not actions:
        return ""
    labels = " ".join(f"[{a.label}]" for a in actions)
    return f"Next: {labels}"


def actions_to_meta(actions: List[SuggestedAction]) -> Dict[str, Any]:
    """Serialize actions for the _meta.suggested_actions field."""
    return {"suggested_actions": [a.to_dict() for a in actions]}
