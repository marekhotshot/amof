"""Shared AMOF CLI entrypoint.

This keeps the historical ``scripts/amof.py`` launcher and the installed
package entrypoint (``amof``, ``python -m amof``) on the same code path.
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

from amof.cli import parse_args
from amof.manifest import list_available_ecosystems, load_manifest
from amof.state import get_state

NO_ECOSYSTEM_COMMANDS = {
    "ecosystem",
    "check",
    "context",
    "open",
    "ticket",
    "manifest",
    "generated-build",
    "release",
    "troubleshoot",
    "doctor",
    "bootstrap",
    "paths",
    "setup",
    "chat",
    "intake",
    "runner",
    "execution",
    "loop",
    "runs",
    "update",
    "uninstall",
    "shell",
    "preview",
    "help",
    "init",
    "eval",
    "workspace",
    "server",
    "mcp",
    "director",
    "promote-main",
    "promote-main-revert",
    "handoff",
}


def _resolve_root_shell_ecosystem(explicit_ecosystem: str | None) -> str | None:
    """Resolve an ecosystem for root-shell commands that historically used state."""
    if explicit_ecosystem:
        return explicit_ecosystem
    state = get_state()
    if state and state.get("ecosystem"):
        return str(state["ecosystem"])
    ecosystems = list_available_ecosystems()
    if len(ecosystems) == 1:
        return ecosystems[0]
    return None


def _is_operational_context_command(args) -> bool:
    action = str(getattr(args, "service", "") or "").strip()
    return action in {
        "current",
        "list",
        "show",
        "use",
        "doctor",
        "add",
        "prompt",
        "banner",
    }


def _current_git_root() -> Path | None:
    try:
        from amof.utils import get_git_toplevel

        root = get_git_toplevel()
    except Exception:
        return None
    if not root:
        return None
    return Path(root).resolve(strict=False)


def _resolve_adopted_repo_ecosystem() -> str | None:
    git_root = _current_git_root()
    if git_root is None:
        return None
    try:
        from amof.app_config import get_repo_binding_for_git_root

        binding = get_repo_binding_for_git_root(git_root)
    except Exception:
        return None
    if not binding:
        return None
    ecosystem = str(binding.get("ecosystem") or "").strip()
    return ecosystem or None


def _resolve_default_ecosystem_from_cwd_config() -> str | None:
    agent_config = Path(".amof/agent.yaml")
    if not agent_config.exists():
        return None
    for line in agent_config.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("default_ecosystem:"):
            val = line.split(":", 1)[1].strip()
            if " #" in val:
                val = val[: val.index(" #")].rstrip()
            if val:
                return val
    return None


def _write_ecosystem_resolution_failure() -> None:
    sys.stderr.write("Error: no ecosystem resolved for this AMOF command.\n")
    git_root = _current_git_root()
    if git_root is not None:
        ecosystem_name = git_root.name
        sys.stderr.write(f"Detected git root: {git_root}\n")
        sys.stderr.write(f"Run: amof init --adopt . --name {ecosystem_name}\n")
        sys.stderr.write(
            f'Then: amof agent -e {ecosystem_name} --plan "Inspect this repo" --no-follow-up\n'
        )
    else:
        sys.stderr.write("Run from a git checkout and then: amof init --adopt .\n")
    sys.stderr.write("Usage: amof -e <ecosystem> <command>\n\n")

    ecosystems_dir = Path("ecosystems")
    if ecosystems_dir.exists():
        sys.stderr.write("Available ecosystems:\n")
        for eco in ecosystems_dir.iterdir():
            if eco.is_dir() and (eco / "ecosystem.yaml").exists():
                sys.stderr.write(f"  - {eco.name}\n")


def _lazy_command(module_name: str, attr_name: str):
    def _runner(*args, **kwargs):
        module = import_module(f"amof.commands.{module_name}")
        return getattr(module, attr_name)(*args, **kwargs)

    return _runner


cmd_spin = _lazy_command("spin", "cmd_spin")
cmd_sync = _lazy_command("sync", "cmd_sync")
cmd_status = _lazy_command("status", "cmd_status")
cmd_context = _lazy_command("context", "cmd_context")
cmd_operational_context = _lazy_command(
    "operational_context", "cmd_operational_context"
)
cmd_add_repo = _lazy_command("repo", "cmd_add_repo")
cmd_init = _lazy_command("init", "cmd_init")
cmd_repo_promote = _lazy_command("repo", "cmd_repo_promote")
cmd_repo_cleanup = _lazy_command("repo", "cmd_repo_cleanup")
cmd_install = _lazy_command("install", "cmd_install")
cmd_paths = _lazy_command("paths", "cmd_paths")
cmd_workspace = _lazy_command("workspace", "cmd_workspace")
cmd_workspace_list = _lazy_command("workspace", "cmd_workspace_list")
cmd_workspace_registry_list = _lazy_command("workspace", "cmd_workspace_registry_list")
cmd_workspace_register = _lazy_command("workspace", "cmd_workspace_register")
cmd_workspace_show = _lazy_command("workspace", "cmd_workspace_show")
cmd_workspace_materialize_run = _lazy_command(
    "workspace", "cmd_workspace_materialize_run"
)
cmd_workspace_materialize_from_intake = _lazy_command(
    "workspace", "cmd_workspace_materialize_from_intake"
)
cmd_open = _lazy_command("workspace", "cmd_open")
cmd_push = _lazy_command("workspace", "cmd_push")
cmd_discard = _lazy_command("discard", "cmd_discard")
cmd_archive = _lazy_command("archive", "cmd_archive")
cmd_archive_list = _lazy_command("archive", "cmd_archive_list")
cmd_ecosystem = _lazy_command("ecosystem", "cmd_ecosystem")
cmd_actor = _lazy_command("actor", "cmd_actor")
cmd_check = _lazy_command("check", "cmd_check")
cmd_pr = _lazy_command("pr", "cmd_pr")
cmd_jira = _lazy_command("jira", "cmd_jira")
cmd_kb = _lazy_command("kb", "cmd_kb")
cmd_profile = _lazy_command("profile", "cmd_profile")
cmd_chat = _lazy_command("chat", "cmd_chat")
cmd_intake = _lazy_command("intake", "cmd_intake")
cmd_runner = _lazy_command("runner", "cmd_runner")
cmd_execution = _lazy_command("execution", "cmd_execution")
cmd_loop = _lazy_command("loop", "cmd_loop")
cmd_runs = _lazy_command("runs", "cmd_runs")
cmd_agent = _lazy_command("agent_cmd", "cmd_agent")
cmd_manifest = _lazy_command("manifest_cmd", "cmd_manifest")
cmd_release = _lazy_command("release", "cmd_release")
cmd_troubleshoot = _lazy_command("troubleshoot", "cmd_troubleshoot")
cmd_doctor = _lazy_command("doctor", "cmd_doctor")
cmd_bootstrap = _lazy_command("bootstrap", "cmd_bootstrap")
cmd_help = _lazy_command("help_cmd", "cmd_help")
cmd_setup = _lazy_command("setup", "cmd_setup")
cmd_update = _lazy_command("update", "cmd_update")
cmd_uninstall = _lazy_command("uninstall", "cmd_uninstall")
cmd_shell = _lazy_command("shell", "cmd_shell")
cmd_director = _lazy_command("director", "cmd_director")
cmd_director_action = _lazy_command("director_action", "cmd_director_action")
cmd_ticket_preflight = _lazy_command("ticket", "cmd_ticket_preflight")
cmd_ticket_start = _lazy_command("ticket", "cmd_ticket_start")
cmd_ticket_list = _lazy_command("ticket", "cmd_ticket_list")
cmd_ticket_status = _lazy_command("ticket", "cmd_ticket_status")
cmd_ticket_checkpoint = _lazy_command("ticket", "cmd_ticket_checkpoint")
cmd_ticket_switch = _lazy_command("ticket", "cmd_ticket_switch")
cmd_ticket_end = _lazy_command("ticket", "cmd_ticket_end")
cmd_ticket_env_upsert = _lazy_command("ticket", "cmd_ticket_env_upsert")
cmd_promote_main = _lazy_command("promote_main", "cmd_promote_main")
cmd_promote_main_revert = _lazy_command("promote_main", "cmd_promote_main_revert")
cmd_handoff = _lazy_command("handoff", "cmd_handoff")


def main() -> None:
    """Main entry point."""
    args = parse_args()
    ecosystem = getattr(args, "ecosystem", None)

    if args.command in NO_ECOSYSTEM_COMMANDS:
        if args.command == "ecosystem":
            sys.exit(cmd_ecosystem(args))
        if args.command == "check":
            manifest = load_manifest(ecosystem) if ecosystem else {"repos": []}
            sys.exit(cmd_check(manifest))
        if args.command == "open":
            eco = getattr(args, "open_ecosystem", None) or ecosystem
            sys.exit(cmd_open(eco))
        if args.command == "manifest":
            sys.exit(cmd_manifest(ecosystem, args))
        if args.command == "generated-build":
            from amof.generated_build.__main__ import _main as generated_build_main

            gb_argv = [getattr(args, "generated_build_cmd")]
            if getattr(args, "repo_path", None):
                gb_argv.append(getattr(args, "repo_path"))
            if getattr(args, "image", None):
                gb_argv.extend(["--image", getattr(args, "image")])
            if getattr(args, "output", None):
                gb_argv.extend(["--output", getattr(args, "output")])
            if getattr(args, "service", None):
                gb_argv.extend(["--service", getattr(args, "service")])
            if (
                getattr(args, "timeout", None) is not None
                and getattr(args, "generated_build_cmd", None) == "runtime-proof"
            ):
                gb_argv.extend(["--timeout", str(getattr(args, "timeout"))])
            sys.exit(generated_build_main(gb_argv))
        if args.command == "help":
            sys.exit(cmd_help(getattr(args, "topic", None)))
        if args.command == "init":
            sys.exit(cmd_init(args))
        if args.command == "troubleshoot":
            manifest = load_manifest(ecosystem) if ecosystem else {"repos": []}
            sys.exit(cmd_troubleshoot(manifest))
        if args.command == "doctor":
            sys.exit(cmd_doctor(args))
        if args.command == "bootstrap":
            sys.exit(cmd_bootstrap(args))
        if args.command == "paths":
            sys.exit(cmd_paths(args))
        if args.command == "setup":
            sys.exit(cmd_setup(args))
        if args.command == "chat":
            sys.exit(cmd_chat(args))
        if args.command == "intake":
            sys.exit(cmd_intake(args))
        if args.command == "runner":
            sys.exit(cmd_runner(args))
        if args.command == "execution":
            sys.exit(cmd_execution(args))
        if args.command == "loop":
            sys.exit(cmd_loop(args))
        if args.command == "runs":
            sys.exit(cmd_runs(args))
        if args.command == "update":
            sys.exit(cmd_update(args))
        if args.command == "uninstall":
            sys.exit(cmd_uninstall(args))
        if args.command == "shell":
            sys.exit(cmd_shell(args))
        if args.command == "handoff":
            sys.exit(cmd_handoff(args))
        if args.command == "preview":
            from amof.commands.preview import cmd_preview

            sys.exit(cmd_preview(args))
        if args.command == "context":
            if _is_operational_context_command(args):
                sys.exit(cmd_operational_context(args))
            eco = (
                ecosystem
                or _resolve_adopted_repo_ecosystem()
                or _resolve_root_shell_ecosystem(None)
            )
            manifest = load_manifest(eco) if eco else {"repos": []}
            sys.exit(
                cmd_context(
                    manifest,
                    args.service,
                    args.context_types,
                    args.output_format,
                    args.incremental,
                )
            )
        if args.command == "release":
            bump = getattr(args, "bump", None)
            if bump == "status":
                from amof.commands.release import cmd_release_status

                sys.exit(cmd_release_status())
            if bump == "log":
                from amof.commands.release import cmd_release_log

                sys.exit(cmd_release_log())
            pre = getattr(args, "pre", None)
            promote_target = pre if bump == "promote" else None
            if bump == "promote":
                pre = None
            sys.exit(
                cmd_release(
                    bump=bump,
                    pre=pre,
                    promote_target=promote_target,
                    message=getattr(args, "message", None),
                    push=not getattr(args, "no_push", False),
                    dry_run=getattr(args, "dry_run", False),
                    yes=getattr(args, "yes", False),
                    skip_validation=getattr(args, "skip_validation", False),
                    strict=getattr(args, "strict", False),
                )
            )
        if args.command == "server":
            from amof.commands.server_cmd import cmd_serve

            sys.exit(cmd_serve(args))
        if args.command == "mcp":
            from amof.mcp.server import main as mcp_main

            mcp_main()
            sys.exit(0)
        if args.command == "workspace":
            workspace_cmd = getattr(args, "workspace_cmd", None)
            if workspace_cmd == "list":
                if getattr(args, "worktrees", False):
                    sys.exit(cmd_workspace_list())
                sys.exit(
                    cmd_workspace_registry_list(
                        json_output=bool(getattr(args, "json", False))
                    )
                )
            if workspace_cmd == "show":
                sys.exit(cmd_workspace_show(args))
            if workspace_cmd == "register":
                sys.exit(cmd_workspace_register(args))
            if workspace_cmd == "materialize-run":
                sys.exit(cmd_workspace_materialize_run(args))
            if workspace_cmd == "materialize-from-intake":
                sys.exit(cmd_workspace_materialize_from_intake(args))
        if args.command == "eval":
            from amof.commands.eval_cmd import cmd_eval

            sys.exit(
                cmd_eval(
                    {"repos": []},
                    tiers=getattr(args, "tiers", None),
                    tasks_file=getattr(args, "tasks", None),
                    task_filter=getattr(args, "task_filter", None),
                    provider=getattr(args, "provider", None),
                    verbose=getattr(args, "verbose", False),
                    output_dir=getattr(args, "output_dir", None),
                )
            )
        if args.command == "director":
            sys.exit(cmd_director(args))

        if args.command == "promote-main":
            eco = _resolve_root_shell_ecosystem(ecosystem)
            manifest = load_manifest(eco) if eco else {"repos": []}
            sys.exit(cmd_promote_main(manifest, args, eco))

        if args.command == "promote-main-revert":
            eco = _resolve_root_shell_ecosystem(ecosystem)
            manifest = load_manifest(eco) if eco else {"repos": []}
            sys.exit(cmd_promote_main_revert(manifest, args, eco))

        if args.command == "ticket":
            state = get_state()
            eco = ecosystem or state.get("ecosystem")
            manifest = load_manifest(eco) if eco else {"repos": []}
            ticket_cmd = getattr(args, "ticket_cmd", None)
            if ticket_cmd == "preflight":
                sys.exit(cmd_ticket_preflight(manifest, args))
            elif ticket_cmd == "start":
                sys.exit(
                    cmd_ticket_start(
                        manifest,
                        args.ticket_id,
                        getattr(args, "repos", None),
                        eco,
                        getattr(args, "stage", None),
                        getattr(args, "environment", None),
                        getattr(args, "repo_selections", None),
                        getattr(args, "plan_items_json", None),
                        getattr(args, "plan_items_file", None),
                        getattr(args, "planner_profile", None),
                        getattr(args, "planner_model", None),
                    )
                )
            elif ticket_cmd == "list":
                sys.exit(cmd_ticket_list(manifest))
            elif ticket_cmd == "status":
                sys.exit(cmd_ticket_status(manifest, args))
            elif ticket_cmd == "checkpoint":
                sys.exit(cmd_ticket_checkpoint(manifest, args))
            elif ticket_cmd == "switch":
                sys.exit(cmd_ticket_switch(manifest, args.ticket_id, eco))
            elif ticket_cmd == "end":
                sys.exit(
                    cmd_ticket_end(
                        manifest,
                        args.ticket_id,
                        getattr(args, "cleanup", False),
                        getattr(args, "cleanup_local", False),
                    )
                )
            elif ticket_cmd == "env":
                env_cmd = getattr(args, "ticket_env_cmd", None)
                if env_cmd == "upsert":
                    sys.exit(cmd_ticket_env_upsert(args))
                sys.stderr.write("Usage: amof ticket env upsert [options]\n")
                sys.exit(1)
            else:
                sys.stderr.write(
                    "Usage: amof ticket <preflight|start|list|status|checkpoint|switch|end|env>\n"
                )
                sys.exit(1)

    if not ecosystem:
        ecosystem = _resolve_adopted_repo_ecosystem()

    if not ecosystem:
        state = get_state()
        if state and state.get("ecosystem"):
            ecosystem = state["ecosystem"]

        if not ecosystem:
            from amof.utils import get_ecosystem_from_path

            ecosystem = get_ecosystem_from_path()

        if not ecosystem:
            from amof.utils import get_ecosystem_from_branch

            ecosystem = get_ecosystem_from_branch()

        if not ecosystem:
            ecosystem = _resolve_default_ecosystem_from_cwd_config()

        if not ecosystem:
            _write_ecosystem_resolution_failure()
            sys.exit(1)

    manifest = load_manifest(ecosystem)

    if args.command == "sync":
        only = set(args.repos) if getattr(args, "repos", None) else None
        sys.exit(cmd_sync(manifest, only=only))

    if args.command == "status":
        only = set(args.repos) if getattr(args, "repos", None) else None
        sys.exit(cmd_status(manifest, only=only))

    if args.command == "context":
        sys.exit(
            cmd_context(
                manifest,
                args.service,
                args.context_types,
                args.output_format,
                args.incremental,
            )
        )

    if args.command == "profile":
        sys.exit(
            cmd_profile(
                manifest, getattr(args, "repo", None), getattr(args, "all_repos", False)
            )
        )

    if args.command == "eval":
        from amof.commands.eval_cmd import cmd_eval

        sys.exit(
            cmd_eval(
                manifest,
                tiers=getattr(args, "tiers", None),
                tasks_file=getattr(args, "tasks", None),
                task_filter=getattr(args, "task_filter", None),
                provider=getattr(args, "provider", None),
                verbose=getattr(args, "verbose", False),
                output_dir=getattr(args, "output_dir", None),
            )
        )

    if args.command == "agent" and getattr(args, "goal", None) == "install":
        from amof.commands.agent_cmd import cmd_agent_install

        sys.exit(cmd_agent_install())

    if args.command == "agent" and getattr(args, "request_json", None) is not None:
        from amof.commands.agent_cmd import cmd_agent_request_json

        sys.exit(cmd_agent_request_json(manifest, args))

    if args.command == "agent" and getattr(args, "json", False):
        import json as _json

        from amof.commands.agent_cmd import cmd_agent_json

        sys.exit(cmd_agent_json(manifest, _json.load(sys.stdin)))

    if args.command == "agent":
        sys.exit(
            cmd_agent(
                manifest,
                goal=getattr(args, "goal", None),
                plan_mode=getattr(args, "plan", None),
                model=getattr(args, "model", None),
                verbose=getattr(args, "verbose", None),
                max_cost=getattr(args, "max_cost", None),
                budget=getattr(args, "budget", None),
                cost_limit=getattr(args, "cost_limit", None),
                subtask_budget=getattr(args, "subtask_budget", None),
                add_budget=getattr(args, "add_budget", None),
                require_budget_approval=getattr(args, "require_budget_approval", None),
                budget_strict=getattr(args, "budget_strict", None),
                budget_status=getattr(args, "budget_status", None),
                model_ladder=getattr(args, "model_ladder", None),
                fast_model=getattr(args, "fast_model", None),
                strong_model=getattr(args, "strong_model", None),
                plan_execute=getattr(args, "plan_execute", None),
                planner_model=getattr(args, "planner_model", None),
                provider=getattr(args, "provider", None),
                resume_session=getattr(args, "resume", None),
                follow_up=getattr(args, "follow_up", None),
                follow_up_file=getattr(args, "follow_up_file", None),
                plan_file=getattr(args, "plan_file", None),
                no_follow_up=getattr(args, "no_follow_up", None),
                continue_budget=getattr(args, "continue_budget", None),
                approve_plan=getattr(args, "approve_plan", None),
                approve_capabilities=getattr(args, "approve_capabilities", None),
                approve_tool_packs=getattr(args, "approve_tool_packs", None),
                approve_writable_roots=getattr(args, "approve_writable_roots", None),
            )
        )

    if args.command == "director-action":
        sys.exit(cmd_director_action(manifest, args, ecosystem))

    if args.command == "add-repo":
        sys.exit(cmd_add_repo(args, manifest, ecosystem))

    if args.command == "repo":
        repo_cmd = getattr(args, "repo_cmd", None)
        if repo_cmd == "promote":
            sys.exit(cmd_repo_promote(manifest, args.name, ecosystem))
        elif repo_cmd == "cleanup":
            sys.exit(cmd_repo_cleanup(manifest))
        else:
            sys.stderr.write("Usage: amof repo <promote|cleanup>\n")
            sys.exit(1)

    if args.command == "install":
        sys.exit(
            cmd_install(manifest, args.push, getattr(args, "dry_run", False), ecosystem)
        )

    if args.command == "workspace":
        workspace_cmd = getattr(args, "workspace_cmd", None)
        if workspace_cmd == "list":
            if getattr(args, "worktrees", False):
                sys.exit(cmd_workspace_list())
            sys.exit(
                cmd_workspace_registry_list(
                    json_output=bool(getattr(args, "json", False))
                )
            )
        if workspace_cmd == "show":
            sys.exit(cmd_workspace_show(args))
        if workspace_cmd == "register":
            sys.exit(cmd_workspace_register(args))
        else:
            sys.exit(cmd_workspace(manifest, ecosystem))

    if args.command == "push":
        sys.exit(cmd_push(manifest, getattr(args, "message", None), ecosystem))

    if args.command == "discard":
        sys.exit(
            cmd_discard(
                manifest, args.force, getattr(args, "dry_run", False), ecosystem
            )
        )

    if args.command == "archive":
        sys.exit(
            cmd_archive(
                manifest,
                getattr(args, "message", None),
                args.force,
                getattr(args, "dry_run", False),
                ecosystem,
                getattr(args, "delete_workspace", False),
                getattr(args, "cleanup_features", False),
            )
        )

    if args.command == "archive-list":
        sys.exit(cmd_archive_list(manifest))

    if args.command == "actor":
        sys.exit(cmd_actor(args, manifest, ecosystem))

    if args.command == "pr":
        sys.exit(
            cmd_pr(
                manifest,
                getattr(args, "reviewers", None),
                getattr(args, "dry_run", False),
            )
        )

    if args.command == "jira":
        sys.exit(cmd_jira(args, manifest))

    if args.command == "kb":
        sys.exit(cmd_kb(args, manifest, ecosystem))

    if args.command == "spin":
        sys.exit(cmd_spin(manifest, args.action, ecosystem))

    sys.stderr.write(f"Unknown or unimplemented command: {args.command}\n")
    sys.exit(2)


__all__ = ["main"]
