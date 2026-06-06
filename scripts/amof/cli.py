"""CLI argument parsing for AMOF."""

from __future__ import annotations

import argparse

from . import __version__
from .app_paths import (
    director_prepare_runs_dir,
    director_run_local_dir,
    materialized_runs_dir,
)
from .manifest import list_available_ecosystems

PUBLIC_HELP_COMMANDS = (
    "check",
    "doctor",
    "paths",
    "setup",
    "init",
    "chat",
    "intake",
    "runner",
    "execution",
    "loop",
    "runs",
    "agent",
    "bootstrap",
    "update",
    "uninstall",
    "help",
    "troubleshoot",
    "shell",
)

PUBLIC_HELP_COMMANDS_METAVAR = "{" + ",".join(PUBLIC_HELP_COMMANDS) + "}"


def _first_run_help(name: str, help_text: str | None) -> str | None:
    """Keep first-run help focused while preserving hidden command compatibility."""
    if name in PUBLIC_HELP_COMMANDS:
        return help_text
    return argparse.SUPPRESS


def get_available_ecosystems() -> list[str]:
    """Get list of available ecosystem names."""
    return list_available_ecosystems()


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="amof",
        description="AMOF - Agentic Operations Fabric",
        epilog="Examples:\n"
        "  amof check                         Verify local prerequisites\n"
        "  amof doctor                        Report bootstrap readiness\n"
        "  amof setup provider --list         Show public provider templates\n"
        "  amof init --adopt .                Adopt the current Git repo\n"
        '  amof chat plan "Inspect this repo"  Route read-only planning through remote IAL\n'
        '  amof agent --plan "Inspect this repo"  Run a read-only plan\n'
        "  amof bootstrap bundle --json       Emit bootstrap evidence\n"
        "\n"
        "Advanced, workspace, and maintainer commands remain callable when known.\n"
        "Use 'amof help' for categorized guidance.\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"AMOF v{__version__}")
    parser.add_argument(
        "-e",
        "--ecosystem",
        help="Ecosystem to use (required for most commands)",
        metavar="NAME",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        title="public first-run commands",
        metavar=PUBLIC_HELP_COMMANDS_METAVAR,
    )
    add_top_level_parser = subparsers.add_parser

    def add_public_surface_parser(name: str, *args, **kwargs):
        help_text = _first_run_help(name, kwargs.get("help"))
        kwargs["help"] = help_text
        before = len(subparsers._choices_actions)
        command_parser = add_top_level_parser(name, *args, **kwargs)
        if help_text == argparse.SUPPRESS and len(subparsers._choices_actions) > before:
            subparsers._choices_actions.pop()
        return command_parser

    subparsers.add_parser = add_public_surface_parser

    # Sync command
    sync_parser = subparsers.add_parser(
        "sync", help="Synchronize repositories defined in ecosystem.yaml"
    )
    sync_parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Sync only the specified repo(s); can be repeated",
    )

    # Status command
    status_parser = subparsers.add_parser("status", help="Show repository status")
    status_parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Show status only for the specified repo(s); can be repeated",
    )

    # Add-repo command
    add_repo_parser = subparsers.add_parser(
        "add-repo",
        help="Append a repository to ecosystem.yaml and optionally sync it",
    )
    add_repo_parser.add_argument("name", help="Repository name (manifest key)")
    add_repo_parser.add_argument("url", help="Git URL")
    add_repo_parser.add_argument(
        "--branch", default="main", help="Branch to track (default: main)"
    )
    add_repo_parser.add_argument(
        "--path",
        help="Local path; defaults to repos/<name>",
    )
    add_repo_parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Include glob (can repeat). Default is all files",
    )
    add_repo_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude glob (can repeat)",
    )
    add_repo_parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing repo with the same name",
    )
    add_repo_parser.add_argument(
        "--sync",
        action="store_true",
        help="Run sync for the new repo after updating the manifest",
    )
    add_repo_parser.add_argument(
        "--readonly",
        action="store_true",
        help="Add as readonly (no feature branch created)",
    )

    init_parser = subparsers.add_parser(
        "init",
        help="Initialize AMOF metadata for an existing repository",
    )
    init_parser.add_argument(
        "--adopt",
        metavar="PATH",
        help="Adopt an existing git repository into AMOF app-data",
    )
    init_parser.add_argument(
        "--name",
        help="Ecosystem name to store for the adopted repository",
    )
    init_parser.add_argument(
        "--write-local",
        action="store_true",
        help="Reserved for future local repo metadata writes; app-data is used by default",
    )

    # Repo management command
    repo_parser = subparsers.add_parser(
        "repo",
        help="Manage repositories in workspace",
    )
    repo_sub = repo_parser.add_subparsers(dest="repo_cmd")

    repo_promote = repo_sub.add_parser(
        "promote",
        help="Promote readonly repo to writable (create feature branch)",
    )
    repo_promote.add_argument("name", help="Repository name to promote")

    repo_sub.add_parser(
        "cleanup",
        help="Delete feature branches with no commits",
    )

    # Context command
    context_parser = subparsers.add_parser(
        "context", help="Manage runtime context or generate service context"
    )
    context_parser.add_argument(
        "service",
        nargs="?",
        help="Service name from manifest/adopted ecosystem, or one of: current, list, show, use, doctor, add",
    )
    context_parser.add_argument(
        "context_target",
        nargs="?",
        help="Target AMOF context name for show/use/add",
    )
    context_parser.add_argument(
        "--type",
        dest="context_types",
        default="all",
        help="Context types to generate: all,api,config,structure,impact,chunks (comma-separated)",
    )
    context_parser.add_argument(
        "--format",
        dest="output_format",
        choices=["json", "markdown"],
        default="json",
        help="Output format (default: json)",
    )
    context_parser.add_argument(
        "--incremental",
        action="store_true",
        help="Only process files changed since last context generation",
    )
    context_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON for AMOF operational context commands",
    )
    context_parser.add_argument(
        "--controlplane-mode",
        choices=["local-cli", "remote-api"],
        help="Override controlplane mode when using 'amof context add'",
    )
    context_parser.add_argument(
        "--controlplane-url",
        help="Override controlplane URL when using 'amof context add'",
    )
    context_parser.add_argument(
        "--execution-backend",
        choices=["local", "remote-worker", "kubernetes-worker"],
        help="Override execution backend when using 'amof context add'",
    )
    context_parser.add_argument(
        "--workspace-backend",
        choices=["local-appdata", "remote-worker-pvc", "object-store"],
        help="Override workspace backend when using 'amof context add'",
    )
    context_parser.add_argument(
        "--evidence-backend",
        choices=["local-appdata", "remote-controlplane", "mirrored"],
        help="Override evidence backend when using 'amof context add'",
    )
    context_parser.add_argument(
        "--browser-backend",
        choices=["local-http", "local-playwright", "cloudflare-browser-run"],
        help="Override browser backend metadata when using 'amof context add'",
    )
    context_parser.add_argument(
        "--browser-recordings",
        choices=["true", "false"],
        help="Store whether browser session recordings are desired when using 'amof context add'",
    )
    context_parser.add_argument(
        "--browser-human-in-loop",
        choices=["true", "false"],
        help="Store whether browser human-in-the-loop is desired when using 'amof context add'",
    )
    context_parser.add_argument(
        "--browser-allowed-host",
        action="append",
        dest="browser_allowed_hosts",
        help="Store one allowed browser target host when using 'amof context add'; can be repeated",
    )
    context_parser.add_argument(
        "--kubeconfig-ref",
        help="Store a kubeconfig path/reference when using 'amof context add'",
    )
    context_parser.add_argument(
        "--namespace",
        help="Store a Kubernetes namespace hint when using 'amof context add'",
    )
    context_parser.add_argument(
        "--plain",
        action="store_true",
        help="Emit plain prompt text for 'amof context prompt'",
    )

    shell_parser = subparsers.add_parser(
        "shell",
        help="Emit shell integration helpers",
    )
    shell_sub = shell_parser.add_subparsers(dest="shell_cmd", required=True)
    shell_init = shell_sub.add_parser(
        "init",
        help="Print a sourceable shell integration snippet",
    )
    shell_init.add_argument(
        "shell_name",
        choices=["bash"],
        help="Shell to target",
    )

    preview_parser = subparsers.add_parser(
        "preview",
        help="Run bounded preview evidence checks",
    )
    preview_sub = preview_parser.add_subparsers(dest="preview_cmd", required=True)
    preview_check_url = preview_sub.add_parser(
        "check-url",
        help="Fetch one preview URL with the local-http backend and emit a preview evidence receipt",
    )
    preview_check_url.add_argument(
        "--url", required=True, help="Explicit preview URL to validate"
    )
    preview_check_url.add_argument(
        "--run-id", required=True, help="Opaque run identifier for the preview check"
    )
    preview_check_url.add_argument(
        "--require-text",
        action="append",
        dest="required_text",
        default=[],
        help="Text that must appear in the response body; can be repeated",
    )
    preview_check_url.add_argument(
        "--forbid-text",
        action="append",
        dest="forbidden_text",
        default=[],
        help="Text that must not appear in the response body; can be repeated",
    )
    preview_check_url.add_argument(
        "--expect-link",
        action="append",
        dest="expected_links",
        default=[],
        help="Href or href substring that must appear in a link; can be repeated",
    )
    preview_check_url.add_argument(
        "--output", help="Optional path for preview-check-result.json"
    )
    preview_check_url.add_argument(
        "--context",
        help="Optional AMOF context name to read instead of the active context",
    )
    preview_check_url.add_argument(
        "--browser-backend",
        choices=["local-http"],
        default=None,
        help="Preview backend to use for this MVP (default: local-http)",
    )
    preview_check_url.add_argument(
        "--timeout-seconds",
        type=int,
        default=10,
        help="HTTP fetch timeout in seconds (default: 10)",
    )

    # Manifest command
    manifest_parser = subparsers.add_parser(
        "manifest", help="Validate and inspect ecosystem.yaml manifest"
    )
    manifest_sub = manifest_parser.add_subparsers(dest="manifest_command")

    manifest_validate = manifest_sub.add_parser(
        "validate",
        help="Validate manifest schema and show detailed errors",
    )
    manifest_validate.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors (fail on warnings)",
    )

    manifest_sub.add_parser(
        "show",
        help="Display manifest contents in readable format",
    )

    # Doctor command
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Report AMOF bootstrap guardrails for topology, app-data, contracts, and toolchain",
    )
    doctor_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Emit governed workstation bootstrap evidence",
    )
    bootstrap_sub = bootstrap_parser.add_subparsers(dest="bootstrap_cmd", required=True)
    bootstrap_contract = bootstrap_sub.add_parser(
        "contract",
        help="Write one governed workstation bootstrap contract artifact",
    )
    bootstrap_contract.add_argument(
        "--json", action="store_true", help="Print the emitted JSON contract to stdout"
    )
    bootstrap_contract.add_argument(
        "--output", help="Optional path for the emitted bootstrap contract artifact"
    )
    bootstrap_bundle = bootstrap_sub.add_parser(
        "bundle",
        help="Write the governed workstation bootstrap evidence bundle",
    )
    bootstrap_bundle.add_argument(
        "--json",
        action="store_true",
        help="Print the emitted UP10 summary JSON to stdout",
    )
    bootstrap_bundle.add_argument(
        "--output-dir",
        help="Optional directory for the emitted bootstrap evidence bundle",
    )

    paths_parser = subparsers.add_parser(
        "paths",
        help="Show resolved AMOF app-data paths",
    )
    paths_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    setup_parser = subparsers.add_parser(
        "setup",
        help="Guided setup for public AMOF profiles",
    )
    setup_sub = setup_parser.add_subparsers(dest="setup_cmd")
    setup_provider = setup_sub.add_parser(
        "provider",
        help="Create an AMOF provider profile in app-data",
    )
    setup_provider.add_argument(
        "provider_template",
        nargs="?",
        choices=[
            "openrouter",
            "local-qwen",
            "openai",
            "anthropic",
            "bedrock",
            "remote-ial",
            "xai",
            "runpod",
        ],
        help="Provider template to use",
    )
    setup_provider.add_argument(
        "--list",
        dest="list_templates",
        action="store_true",
        help="List provider templates",
    )
    setup_provider.add_argument(
        "--name", dest="profile_name", help="Provider profile name to write"
    )
    setup_provider.add_argument("--lane", help="Override profile lane")
    setup_provider.add_argument("--model", help="Concrete model id to record")
    setup_provider.add_argument(
        "--model-env", help="Environment variable name containing the model id"
    )
    setup_provider.add_argument(
        "--api-key-env", help="Environment variable name containing the API key"
    )
    setup_provider.add_argument(
        "--base-url", help="Concrete OpenAI-compatible base URL to record"
    )
    setup_provider.add_argument(
        "--base-url-env", help="Environment variable name containing the base URL"
    )
    setup_provider.add_argument(
        "--timeout-seconds",
        type=float,
        help="Local provider request timeout in seconds",
    )
    setup_provider.add_argument(
        "--activate",
        action="store_true",
        help="Add this profile name to the current context",
    )
    setup_provider.add_argument(
        "--dry-run",
        action="store_true",
        help="Print target path and YAML without writing",
    )
    setup_provider.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    setup_provider.add_argument(
        "--print-template",
        action="store_true",
        help="Print the resolved YAML template without writing",
    )

    update_parser = subparsers.add_parser(
        "update",
        help="Update AMOF from the public release tags",
    )
    update_parser.add_argument(
        "--check",
        action="store_true",
        help="Check the latest available stable tag only",
    )
    update_parser.add_argument(
        "--version",
        dest="target_version",
        help="Explicit release tag to install, for example v2.1.0",
    )
    update_parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    update_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the update command without running it",
    )
    update_parser.add_argument(
        "--verbose", action="store_true", help="Print successful installer output"
    )
    update_parser.add_argument(
        "--source-url",
        default="https://github.com/marekhotshot/amof.git",
        help="Git repository URL to install from (default: public AMOF repository)",
    )

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Uninstall the locally installed AMOF CLI and remove local install artifacts",
    )
    uninstall_parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )

    # Generated-build command (read/proof lane; never deploys)
    generated_build_parser = subparsers.add_parser(
        "generated-build",
        help="Generated-build lane: detect, render, and build-proof codebases without existing build contracts",
    )
    generated_build_sub = generated_build_parser.add_subparsers(
        dest="generated_build_cmd", required=True
    )

    gb_detect = generated_build_sub.add_parser(
        "detect",
        help="Detect runtime family and print the generated-build artifact JSON",
    )
    gb_detect.add_argument("repo_path", help="Repository root to inspect")
    gb_detect.add_argument("--output", help="Optional path to write artifact JSON")

    gb_render = generated_build_sub.add_parser(
        "render",
        help="Render the generated Dockerfile template and print the artifact JSON",
    )
    gb_render.add_argument("repo_path", help="Repository root to inspect")
    gb_render.add_argument("--output", help="Optional path to write artifact JSON")
    gb_render.add_argument(
        "--service", help="Optional service name for the deterministic output path"
    )

    gb_build_proof = generated_build_sub.add_parser(
        "build-proof",
        help="Render and run docker build proof; does not attempt runtime proof",
    )
    gb_build_proof.add_argument("repo_path", help="Repository root to inspect")
    gb_build_proof.add_argument(
        "--image", required=True, help="Image reference to tag with docker build"
    )
    gb_build_proof.add_argument("--output", help="Optional path to write artifact JSON")
    gb_build_proof.add_argument(
        "--service", help="Optional service name for the deterministic output path"
    )

    gb_runtime_proof = generated_build_sub.add_parser(
        "runtime-proof",
        help="Render, build-proof, then run local Docker liveness proof; does not deploy",
    )
    gb_runtime_proof.add_argument("repo_path", help="Repository root to inspect")
    gb_runtime_proof.add_argument(
        "--image", required=True, help="Image reference that was already build-proven"
    )
    gb_runtime_proof.add_argument(
        "--output", help="Optional path to write artifact JSON"
    )
    gb_runtime_proof.add_argument(
        "--service", help="Optional service name for the deterministic output path"
    )
    gb_runtime_proof.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="Seconds to wait for liveness (default: 20)",
    )

    gb_list = generated_build_sub.add_parser(
        "list",
        help="List locally persisted generated-build artifacts",
    )
    gb_list.add_argument("--output", help="Optional path to write index JSON")

    gb_show = generated_build_sub.add_parser(
        "show",
        help="Show one locally persisted generated-build artifact",
    )
    gb_show.add_argument(
        "repo_path", help="Repository root path used when the artifact was persisted"
    )
    gb_show.add_argument("--service", help="Optional service name (defaults to root)")
    gb_show.add_argument("--output", help="Optional path to write artifact JSON")

    gb_admission = generated_build_sub.add_parser(
        "admission-preview",
        help="Return the public generated-build admission contract",
    )
    gb_admission.add_argument(
        "repo_path", help="Repository root path used when the artifact was persisted"
    )
    gb_admission.add_argument(
        "--service", help="Optional service name (defaults to root)"
    )
    gb_admission.add_argument(
        "--output", help="Optional path to write contract result JSON"
    )

    # Profile command
    profile_parser = subparsers.add_parser(
        "profile", help="Generate repo profile for agent navigation"
    )
    profile_parser.add_argument(
        "repo",
        nargs="?",
        help="Repo name (omit for all repos)",
    )
    profile_parser.add_argument(
        "--all",
        action="store_true",
        dest="all_repos",
        help="Profile all repos in workspace",
    )

    chat_parser = subparsers.add_parser(
        "chat",
        help="Create a read-only planning proposal through remote IAL",
    )
    chat_sub = chat_parser.add_subparsers(dest="chat_cmd", required=True)
    chat_plan = chat_sub.add_parser(
        "plan",
        help="Build one non-executable PlanPacket proposal for AMOF Director",
    )
    chat_plan.add_argument(
        "objective",
        help="Planning objective or operator request to analyze",
    )
    chat_plan.add_argument(
        "--repo",
        default=".",
        help="Repository/workspace path to inspect (default: current directory)",
    )
    chat_plan.add_argument(
        "--ticket-id",
        help="Optional ticket identifier to pin into the PlanPacket",
    )
    chat_plan.add_argument(
        "--file",
        action="append",
        dest="files",
        help="Bound planning context to one repo-relative file path; can be repeated",
    )
    chat_plan.add_argument(
        "--max-files",
        type=int,
        default=8,
        help="Maximum files to inspect when --file is omitted (default: 8)",
    )
    chat_plan.add_argument(
        "--model",
        help="Optional remote-IAL model override; defaults to the active provider profile",
    )
    chat_plan.add_argument(
        "--minimal-context",
        action="store_true",
        help="Bypass canonical planning context/indexer and use only objective plus explicit --file context",
    )
    chat_plan.add_argument(
        "--output",
        help="Optional path outside the target repo for the emitted proposal JSON",
    )
    chat_start = chat_sub.add_parser(
        "start",
        help="Start one bounded proposal-only intake session",
    )
    chat_start.add_argument(
        "objective",
        help="Planning objective or operator request to clarify",
    )
    chat_start.add_argument(
        "--repo",
        default=".",
        help="Repository/workspace path to inspect (default: current directory)",
    )
    chat_start.add_argument(
        "--ticket-id",
        help="Optional ticket identifier to pin into the eventual PlanPacket",
    )
    chat_start.add_argument(
        "--file",
        action="append",
        dest="files",
        help="Focus the bounded intake session on one repo-relative file path; can be repeated",
    )
    chat_start.add_argument(
        "--max-files",
        type=int,
        default=8,
        help="Maximum indexed files to inspect when --file is omitted (default: 8)",
    )
    chat_start.add_argument(
        "--max-turns",
        type=int,
        default=4,
        help="Maximum user-answer turns allowed before finalize is forced ready (default: 4)",
    )
    chat_start.add_argument(
        "--max-questions",
        type=int,
        default=3,
        help="Maximum clarification questions the session may ask (default: 3)",
    )
    chat_start.add_argument(
        "--model",
        help="Optional remote-IAL model override; defaults to the active provider profile",
    )
    chat_ask = chat_sub.add_parser(
        "ask",
        help="Answer the current bounded intake question and advance the session",
    )
    chat_ask.add_argument("session_id", help="Existing bounded intake session id")
    chat_ask.add_argument("message", help="Operator answer or clarification input")
    chat_status = chat_sub.add_parser(
        "status",
        help="Show the current bounded intake session state",
    )
    chat_status.add_argument("session_id", help="Existing bounded intake session id")
    chat_finalize = chat_sub.add_parser(
        "finalize",
        help="Finalize one bounded intake session into a proposal-only PlanPacket",
    )
    chat_finalize.add_argument("session_id", help="Existing bounded intake session id")
    chat_approve = chat_sub.add_parser(
        "approve",
        help="Write one explicit approval artifact for a finalized proposal-only PlanPacket",
    )
    chat_approve.add_argument(
        "session_id", help="Existing finalized bounded intake session id"
    )
    chat_approve.add_argument(
        "--approved-by",
        help="Optional operator identifier to record in the approval artifact",
    )
    chat_approve.add_argument(
        "--approval-note",
        help="Optional short note explaining the approval context",
    )
    chat_handoff = chat_sub.add_parser(
        "handoff",
        help="Convert one approved PlanPacket artifact into a Director Intake execution envelope",
    )
    chat_handoff.add_argument(
        "approval_id_or_path",
        help="Approval id emitted by 'amof chat approve' or a direct path to approved-plan.json",
    )
    chat_handoff.add_argument(
        "--run-id",
        help="Optional run id to record in execution_handoff.workspace_materialization",
    )
    chat_handoff.add_argument(
        "--target-base-dir",
        help="Optional workspace materialization base directory to record in the handoff envelope",
    )

    runs_parser = subparsers.add_parser(
        "runs",
        help="Inspect runtime run sessions from AMOF_HOME",
    )
    runs_sub = runs_parser.add_subparsers(dest="runs_cmd", required=True)
    runs_list = runs_sub.add_parser(
        "list",
        help="List discovered run sessions from app-data",
    )
    runs_list.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    runs_show = runs_sub.add_parser(
        "show",
        help="Show one run summary by run_id",
    )
    runs_show.add_argument("run_id", help="Run id or session id to inspect")
    runs_show.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    runs_logs = runs_sub.add_parser(
        "logs",
        help="Print events.jsonl lines for one run",
    )
    runs_logs.add_argument("run_id", help="Run id or session id to inspect")
    runs_logs.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional limit of most recent events to print (default: all)",
    )
    runs_logs.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    runs_tail = runs_sub.add_parser(
        "tail",
        help="Print latest event lines and optionally follow for new events",
    )
    runs_tail.add_argument("run_id", help="Run id or session id to inspect")
    runs_tail.add_argument(
        "--lines",
        type=int,
        default=20,
        help="Number of recent lines to print before follow mode (default: 20)",
    )
    runs_tail.add_argument(
        "--follow",
        action="store_true",
        help="Poll and print appended events for a bounded number of polls",
    )
    runs_tail.add_argument(
        "--poll-seconds",
        type=float,
        default=1.0,
        help="Polling interval in seconds for --follow mode (default: 1.0)",
    )
    runs_tail.add_argument(
        "--max-polls",
        type=int,
        default=20,
        help="Maximum follow polls before exit (default: 20)",
    )
    runs_tail.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    intake_parser = subparsers.add_parser(
        "intake",
        help="Validate and submit bounded intake packets from AMOF_HOME",
    )
    intake_sub = intake_parser.add_subparsers(dest="intake_cmd", required=True)
    intake_validate = intake_sub.add_parser(
        "validate",
        help="Validate one intake YAML/JSON packet against bounded MVP rules",
    )
    intake_validate.add_argument("file", help="Path to intake YAML/JSON")
    intake_validate.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    intake_validate.add_argument(
        "--authority-json",
        action="store_true",
        help="Emit only the machine-readable authority decision artifact",
    )

    intake_submit = intake_sub.add_parser(
        "submit",
        help="Validate then submit one planning-only intake packet locally",
    )
    intake_submit.add_argument("file", help="Path to intake YAML/JSON")
    intake_submit.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    intake_submit.add_argument(
        "--authority-artifact",
        help="Optional path to persist the authority decision artifact",
    )

    intake_list = intake_sub.add_parser(
        "list",
        help="List local intake submissions from AMOF_HOME",
    )
    intake_list.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    intake_show = intake_sub.add_parser(
        "show",
        help="Show one local intake submission by intake_id",
    )
    intake_show.add_argument("intake_id", help="Intake identifier")
    intake_show.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    intake_template = intake_sub.add_parser(
        "template",
        help="Print a minimal valid intake YAML/JSON skeleton",
    )
    intake_template.add_argument(
        "--kind",
        default="bounded_intake_task",
        choices=["bounded_intake_task"],
        help="Template kind to print (default: bounded_intake_task)",
    )
    intake_template.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    intake_draft = intake_sub.add_parser(
        "draft",
        help="Compile raw intake text into a canonical draft packet",
    )
    intake_draft.add_argument(
        "--raw-text",
        required=True,
        help="Raw operator capture text to compile",
    )
    intake_draft.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    runner_parser = subparsers.add_parser(
        "runner",
        help="Register and match planning-only runner metadata from AMOF_HOME",
    )
    runner_sub = runner_parser.add_subparsers(dest="runner_cmd", required=True)

    runner_template = runner_sub.add_parser(
        "template",
        help="Print a safe local planning runner YAML template",
    )
    runner_template.add_argument(
        "--kind",
        default="local-planning",
        choices=["local-planning"],
        help="Template kind to print (default: local-planning)",
    )

    runner_register = runner_sub.add_parser(
        "register",
        help="Register one runner metadata YAML/JSON file locally",
    )
    runner_register.add_argument("file", help="Path to runner metadata YAML/JSON")
    runner_register.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    runner_list = runner_sub.add_parser(
        "list",
        help="List locally registered runner metadata",
    )
    runner_list.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    runner_show = runner_sub.add_parser(
        "show",
        help="Show one runner metadata record by runner_id",
    )
    runner_show.add_argument("runner_id", help="Runner identifier")
    runner_show.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    runner_doctor = runner_sub.add_parser(
        "doctor",
        help="Validate local runner registry readiness without dispatch",
    )
    runner_doctor.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    runner_match = runner_sub.add_parser(
        "match",
        help="Planning-only intake-to-runner compatibility check (no dispatch)",
    )
    runner_match.add_argument("intake_ref", help="Intake file path or known intake id")
    runner_match.add_argument(
        "--authority-artifact",
        help="Optional authority decision artifact to gate runner eligibility",
    )
    runner_match.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    runner_local_forensic = runner_sub.add_parser(
        "run-local-forensic",
        help="Run the bounded local read-only forensic command pack for one intake",
    )
    runner_local_forensic.add_argument(
        "intake_ref", help="Intake file path or known intake id"
    )
    runner_local_forensic.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    execution_parser = subparsers.add_parser(
        "execution",
        help="Produce no-execution remote execution readiness scan/report artifacts",
    )
    execution_sub = execution_parser.add_subparsers(dest="execution_cmd", required=True)

    execution_scan = execution_sub.add_parser(
        "scan",
        help="Scan intake plus runner metadata and write a no-execution readiness report",
    )
    execution_scan.add_argument(
        "intake_ref", help="Intake file path or known intake id"
    )
    execution_scan.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    execution_report = execution_sub.add_parser(
        "report",
        help="Show one previously written execution scan report by scan_id",
    )
    execution_report.add_argument("scan_id", help="Execution scan identifier")
    execution_report.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    loop_parser = subparsers.add_parser(
        "loop",
        help="Run bounded no-mutation/no-dispatch loop proofs from AMOF_HOME",
    )
    loop_sub = loop_parser.add_subparsers(dest="loop_cmd", required=True)

    loop_run = loop_sub.add_parser(
        "run",
        help="Run one bounded loop over intake + runner + execution scan/report surfaces",
    )
    loop_run.add_argument("intake_ref", help="Intake file path or known intake id")
    loop_run.add_argument(
        "--max-loops",
        type=int,
        required=True,
        help="Maximum loop iterations before forced stop",
    )
    loop_run.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    loop_show = loop_sub.add_parser(
        "show",
        help="Show one bounded loop report by loop_run_id",
    )
    loop_show.add_argument("loop_run_id", help="Loop run identifier")
    loop_show.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    loop_logs = loop_sub.add_parser(
        "logs",
        help="Print events for one bounded loop run",
    )
    loop_logs.add_argument("loop_run_id", help="Loop run identifier")
    loop_logs.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional limit of most recent events to print (default: all)",
    )
    loop_logs.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    # Agent command
    agent_parser = subparsers.add_parser(
        "agent",
        help="Run the AMOF coding agent (loads defaults from .amof/agent.yaml)",
        epilog=(
            "Resume examples:\n"
            "  amof agent --resume 20260521-115444\n"
            '  amof agent --resume 20260521-115444 --follow-up "Retry only S003."\n'
            "  amof agent --resume 20260521-115444 --follow-up-file /tmp/amof-followup.md\n"
            "  amof agent --resume 20260521-115444 --add-budget 1.00 "
            '--approve-capabilities secret --follow-up "Do not rerun completed subtasks."\n'
            '  amof agent --plan-execute "goal" --budget 2.00 --budget-strict\n'
            '  amof agent --plan-execute "goal" --budget 10.00 --budget-strict '
            "--approve-capabilities secret --approve-tool-pack ops-jenkins\n"
            "  amof agent --resume 20260521-115444 --budget-status\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    agent_parser.add_argument(
        "goal",
        nargs="?",
        help="Goal for the agent, or 'install' to set up the environment (omit for interactive mode)",
    )
    agent_parser.add_argument(
        "--provider",
        choices=["anthropic", "openai", "openrouter", "bedrock", "remote-ial"],
        default=None,
        help="LLM provider (default: from config or anthropic)",
    )
    agent_parser.add_argument(
        "--plan",
        action="store_true",
        default=None,
        help="Run in PLAN mode (read-only, no code changes)",
    )
    agent_parser.add_argument(
        "--model",
        default=None,
        help="LLM model to use (default: from env or claude-sonnet-4)",
    )
    agent_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=None,
        help="Show detailed tool call output (default: from config)",
    )
    agent_parser.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="Maximum cost in USD for the session (alias for --budget; default: from config)",
    )
    agent_parser.add_argument(
        "--budget",
        type=float,
        default=None,
        metavar="USD",
        help="Hard total run budget in USD (must be > 0)",
    )
    agent_parser.add_argument(
        "--cost-limit",
        type=float,
        default=None,
        metavar="USD",
        help="Alias for --budget (must match if both are set)",
    )
    agent_parser.add_argument(
        "--subtask-budget",
        type=float,
        default=None,
        metavar="USD",
        help="Maximum budget per subtask/worker run (default: runner cap)",
    )
    agent_parser.add_argument(
        "--add-budget",
        type=float,
        default=None,
        metavar="USD",
        help="Add budget when resuming a session/checkpoint (records approval; must be > 0)",
    )
    agent_parser.add_argument(
        "--require-budget-approval",
        action="store_true",
        default=None,
        help="Ask before execution when estimated plan cost exceeds remaining budget",
    )
    agent_parser.add_argument(
        "--budget-strict",
        action="store_true",
        default=None,
        help="Fail before provider calls if estimate exceeds available budget",
    )
    agent_parser.add_argument(
        "--budget-status",
        action="store_true",
        default=None,
        help="Print budget state for --resume session/checkpoint and exit",
    )
    agent_parser.add_argument(
        "--model-ladder",
        action="store_true",
        default=None,
        help="Enable multi-model cost optimization (default: from config)",
    )
    agent_parser.add_argument(
        "--fast-model",
        default=None,
        help="Model for fast tier (default: claude-haiku-4-5)",
    )
    agent_parser.add_argument(
        "--strong-model",
        default=None,
        help="Model for strong tier (default: claude-opus-4-6)",
    )
    agent_parser.add_argument(
        "--plan-execute",
        action="store_true",
        default=None,
        help="Use planner-executor mode: strong model plans, cheap models execute",
    )
    agent_parser.add_argument(
        "--planner-model",
        default=None,
        help="Model for planning (default: strong tier model or claude-opus-4-6)",
    )
    agent_parser.add_argument(
        "--index",
        action="store_true",
        help="Generate/refresh codebase index before running (uses planner-model)",
    )
    agent_parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION_ID",
        help="Resume a previous session (loads messages + telemetry; plan checkpoints restore subtask state)",
    )
    agent_parser.add_argument(
        "--follow-up",
        default=None,
        metavar="TEXT",
        help="Operator follow-up appended on resume (does not replace goal/plan/checkpoint)",
    )
    agent_parser.add_argument(
        "--follow-up-file",
        default=None,
        metavar="PATH",
        help="Read operator follow-up from file on resume (must be under readable roots)",
    )
    agent_parser.add_argument(
        "--plan-file",
        default=None,
        metavar="PATH",
        help="Resume from a plan markdown file (continues from first unchecked task)",
    )
    agent_parser.add_argument(
        "--no-follow-up",
        action="store_true",
        default=None,
        help="Skip the post-run interactive menu; does not skip --plan-execute approval",
    )
    agent_parser.add_argument(
        "--approve-plan",
        "--yes",
        dest="approve_plan",
        action="store_true",
        default=None,
        help="Approve a generated --plan-execute plan non-interactively",
    )
    agent_parser.add_argument(
        "--continue-budget",
        type=float,
        default=None,
        metavar="USD",
        help="Default additional budget for 'continue' in follow-up menu (default: $1.00)",
    )
    agent_parser.add_argument(
        "--approve-capabilities",
        "--allow-capability",
        dest="approve_capabilities",
        action="append",
        default=None,
        metavar="CAP",
        help=(
            "Explicitly approve extra trusted capabilities for this plan-execute run only "
            "(repeatable; e.g. --approve-capabilities secret). Does not persist globally."
        ),
    )
    agent_parser.add_argument(
        "--approve-tool-pack",
        dest="approve_tool_packs",
        action="append",
        default=None,
        metavar="PACK",
        help=(
            "Approve a plan-scoped tool pack (repeatable; e.g. "
            "--approve-tool-pack ops-jenkins). Does not persist globally."
        ),
    )
    agent_parser.add_argument(
        "--approve-writable-root",
        dest="approve_writable_roots",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "Approve an additional writable root for this plan-execute run only "
            "(repeatable; e.g. --approve-writable-root /tmp/delivery-3663-matrix-reports)."
        ),
    )
    agent_parser.add_argument(
        "--request-json",
        default=None,
        metavar="PATH",
        help="Read one canonical external handoff request packet (use '-' for stdin; bounded local adapter mode).",
    )
    agent_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Read one machine JSON request from stdin and emit one result envelope JSON.",
    )

    # Director first-class contract actions
    director_parser = subparsers.add_parser(
        "director",
        help="Plan bounded Director dry-run envelopes",
    )
    director_sub = director_parser.add_subparsers(dest="director_cmd")
    director_plan_materialization = director_sub.add_parser(
        "plan-materialization",
        help="Write a Director Intake execution envelope for workspace materialization",
    )
    director_plan_materialization.add_argument(
        "--repo",
        required=True,
        help="Git URL or local repository path to include in the envelope",
    )
    director_plan_materialization.add_argument(
        "--expected-sha",
        required=True,
        help="Exact commit SHA that the execution envelope should request",
    )
    director_plan_materialization.add_argument(
        "--run-id",
        required=True,
        help="Opaque run identifier to record in execution_handoff.workspace_materialization",
    )
    director_plan_materialization.add_argument(
        "--output",
        required=True,
        help="Path to write the Director Intake execution envelope JSON",
    )
    director_plan_materialization.add_argument(
        "--target-base-dir",
        default=str(materialized_runs_dir()),
        help="Base directory recorded in the execution handoff (default: AMOF app-data materialized runs root)",
    )
    director_prepare_run = director_sub.add_parser(
        "prepare-run",
        help="Plan and materialize one isolated per-run workspace in a single local flow",
    )
    director_prepare_run.add_argument(
        "--repo",
        required=True,
        help="Git URL or local repository path to materialize",
    )
    director_prepare_run.add_argument(
        "--expected-sha",
        required=True,
        help="Exact commit SHA that the prepared run must materialize",
    )
    director_prepare_run.add_argument(
        "--run-id",
        required=True,
        help="Opaque run identifier used for all emitted artifacts",
    )
    director_prepare_run.add_argument(
        "--output-dir",
        default=str(director_prepare_runs_dir()),
        help="Base directory for emitted intake, plan result, and run summary artifacts (default: AMOF app-data evidence root)",
    )
    director_prepare_run.add_argument(
        "--execute-noop",
        action="store_true",
        help="Run one bounded local no-op command inside the prepared workspace and emit execution-step-receipt.json",
    )
    director_prepare_run.add_argument(
        "--execute-profile",
        help="Execute one named bounded validation profile such as 'validation.git-status'",
    )
    director_prepare_run.add_argument(
        "--execute-command",
        help='Execute one allowlisted bounded command expressed as argv JSON, for example \'["git", "rev-parse", "HEAD"]\'',
    )
    director_prepare_run.add_argument(
        "--allow-raw-execute-command",
        action="store_true",
        help="Developer-only opt-in required before --execute-command argv JSON can be used",
    )
    director_run_local = director_sub.add_parser(
        "run-local",
        help="Resolve one repo/ref to an exact SHA, then prepare and execute one bounded local run",
    )
    director_run_local.add_argument(
        "--repo",
        required=True,
        help="Git URL or local repository path to resolve and materialize",
    )
    director_run_local.add_argument(
        "--ref",
        help="Branch, tag, HEAD, or exact SHA to resolve before materialization; optional when --repo includes @ref",
    )
    director_run_local.add_argument(
        "--run-id",
        required=True,
        help="Opaque run identifier used for all emitted artifacts",
    )
    director_run_local.add_argument(
        "--output-dir",
        default=str(director_run_local_dir()),
        help="Base directory for emitted run-local artifacts (default: AMOF app-data evidence root)",
    )
    director_run_local.add_argument(
        "--execute-profile",
        required=True,
        help="Named bounded validation profile to execute, such as 'validation.git-status'",
    )
    director_classify_promotion_readiness = director_sub.add_parser(
        "classify-promotion-readiness",
        help="Classify one run-summary.json as ready, blocked, failed, or needs review",
    )
    director_classify_promotion_readiness.add_argument(
        "--run-summary",
        required=True,
        help="Path to the run-summary.json artifact to classify",
    )
    director_classify_promotion_readiness.add_argument(
        "--output",
        help="Optional path to write the promotion-readiness result JSON",
    )
    director_readiness_report = director_sub.add_parser(
        "readiness-report",
        help="Write promotion readiness output and print an operator-friendly promote-main hint",
    )
    director_readiness_report.add_argument(
        "--run-summary",
        required=True,
        help="Path to the run-summary.json artifact to classify and summarize",
    )
    director_readiness_report.add_argument(
        "--promotion-readiness-output",
        help="Optional path to write the promotion-readiness result JSON (default: next to run-summary.json)",
    )
    director_readiness_report.add_argument(
        "--repo-name",
        default="amof",
        help="Promotion target repo name for the command hint (default: amof)",
    )
    director_readiness_report.add_argument(
        "--ticket-id",
        help="Optional ticket id for a complete promote-main command hint",
    )
    director_readiness_report.add_argument(
        "--candidate-branch",
        help="Optional candidate branch for a complete promote-main command hint",
    )
    director_readiness_report.add_argument(
        "--expected-main-sha",
        help="Optional current origin/main SHA for a complete promote-main command hint",
    )
    director_readiness_report.add_argument(
        "--promotion-reason",
        help="Optional promotion reason for a complete promote-main command hint",
    )
    director_readiness_report.add_argument(
        "--promote-mode",
        choices=("dry-run", "push"),
        default="dry-run",
        help="Whether the command hint should target promote-main --dry-run or --push (default: dry-run)",
    )

    # Director first-class contract actions
    director_action_parser = subparsers.add_parser(
        "director-action",
        help="Run bounded Director contract actions",
    )
    director_action_sub = director_action_parser.add_subparsers(
        dest="director_action_cmd"
    )
    gmd_dev_proof = director_action_sub.add_parser(
        "gmd-dev-local-proof",
        help="Run director.gmd_dev_local_proof.v1 against local gmd-dev",
    )
    gmd_dev_proof.add_argument(
        "--input",
        required=True,
        help="Path to the JSON input contract",
    )
    gmd_dev_proof.add_argument(
        "--output",
        help="Path for the JSON result artifact (default: contract artifacts.result_path)",
    )
    gmd_dev_proof.add_argument(
        "--local-port",
        type=int,
        help="Override the contract readback.local_port",
    )

    # Eval command
    eval_parser = subparsers.add_parser(
        "eval",
        help="Run a private-backed maintainer command",
        description=(
            "Public OSS does not include the eval harness implementation.\n"
            "This command remains as a fail-closed maintainer entrypoint."
        ),
    )
    eval_parser.add_argument(
        "--tiers",
        nargs="+",
        choices=["fast", "standard", "strong"],
        default=None,
        help=argparse.SUPPRESS,
    )
    eval_parser.add_argument(
        "--tasks",
        default=None,
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    eval_parser.add_argument(
        "--filter",
        nargs="+",
        dest="task_filter",
        default=None,
        metavar="TASK_ID",
        help=argparse.SUPPRESS,
    )
    eval_parser.add_argument(
        "--provider",
        choices=["anthropic", "openai", "openrouter", "bedrock", "remote-ial"],
        default=None,
        help=argparse.SUPPRESS,
    )
    eval_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    eval_parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help=argparse.SUPPRESS,
    )

    # Install command
    install_parser = subparsers.add_parser(
        "install",
        help="Bootstrap ecosystem workspace (one per ecosystem)",
    )
    install_parser.add_argument(
        "--push",
        action="store_true",
        help="Push branches to origin (default: work locally)",
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )

    # Workspace command
    workspace_parser = subparsers.add_parser(
        "workspace",
        help="Workspace operations (generate file, list worktrees)",
    )
    workspace_sub = workspace_parser.add_subparsers(dest="workspace_cmd")
    workspace_sub.add_parser(
        "generate",
        help="Generate/update VSCode workspace file for multi-repo git tracking",
    )
    workspace_list = workspace_sub.add_parser(
        "list",
        help="List registered workspaces by default, or active worktrees with --worktrees",
    )
    workspace_list.add_argument(
        "--worktrees",
        action="store_true",
        help="List active git worktrees instead of the AMOF app-data registry",
    )
    workspace_list.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    workspace_show = workspace_sub.add_parser(
        "show",
        help="Show one registered workspace entry",
    )
    workspace_show.add_argument("name", help="Registered workspace name")
    workspace_show.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    workspace_register = workspace_sub.add_parser(
        "register",
        help="Register one workspace alias in AMOF app config",
    )
    workspace_register.add_argument("name", nargs="?", help="Workspace alias")
    workspace_register.add_argument(
        "path", nargs="?", help="Local path for the workspace"
    )
    workspace_register.add_argument(
        "--name",
        dest="workspace_name",
        help="Workspace alias (flag form)",
    )
    workspace_register.add_argument(
        "--repo",
        dest="workspace_repo",
        help="Local repository path to register",
    )
    workspace_register.add_argument(
        "--default-ref",
        default="main",
        help="Default branch or ref to associate with the workspace (default: main)",
    )
    workspace_register.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    workspace_materialize = workspace_sub.add_parser(
        "materialize-run",
        help="Materialize an isolated per-run workspace at an exact SHA",
    )
    workspace_materialize.add_argument(
        "--repo",
        required=True,
        help="Git URL or local repository path to materialize",
    )
    workspace_materialize.add_argument(
        "--expected-sha",
        required=True,
        help="Exact commit SHA that must be checked out in the run workspace",
    )
    workspace_materialize.add_argument(
        "--run-id",
        required=True,
        help="Opaque run identifier used for the isolated run root",
    )
    workspace_materialize.add_argument(
        "--target-base-dir",
        required=True,
        help="Base directory under which the isolated run workspace will be created",
    )
    workspace_materialize.add_argument(
        "--branch-or-ref",
        help="Optional branch or ref label recorded in the receipt",
    )
    workspace_materialize.add_argument(
        "--candidate-sha",
        help="Optional candidate SHA recorded in the receipt",
    )
    workspace_materialize_from_intake = workspace_sub.add_parser(
        "materialize-from-intake",
        help="Materialize an isolated per-run workspace from a Director Intake envelope",
    )
    workspace_materialize_from_intake.add_argument(
        "--intake",
        required=True,
        help="Path to the Director Intake execution envelope JSON",
    )
    workspace_materialize_from_intake.add_argument(
        "--target-base-dir",
        help="Optional override for execution_handoff.workspace_materialization.target_base_dir",
    )

    # Open command
    open_parser = subparsers.add_parser(
        "open",
        help="Open existing workspace in Cursor IDE",
    )
    open_parser.add_argument(
        "open_ecosystem",
        nargs="?",
        help="Ecosystem name (e.g., demo-dev). Also accepts -e flag.",
    )

    # Ticket commands
    ticket_parser = subparsers.add_parser(
        "ticket",
        help="Manage tickets within workspace (start, list, switch, end)",
    )
    ticket_sub = ticket_parser.add_subparsers(dest="ticket_cmd")

    ticket_start = ticket_sub.add_parser(
        "start",
        help="Start ticket work - create feature branches in repos",
    )
    ticket_start.add_argument("ticket_id", help="Ticket ID (e.g., PROJ-123)")
    ticket_start.add_argument(
        "--repos", help="Comma-separated repo names (default: all writable)"
    )
    ticket_start.add_argument(
        "--stage", help="Persist the linked lifecycle stage id for this ticket"
    )
    ticket_start.add_argument(
        "--environment",
        help="Persist the linked lifecycle environment id for this ticket",
    )
    ticket_start.add_argument(
        "--repo-selections", help="JSON array describing the per-repo intake contract"
    )
    ticket_start.add_argument(
        "--plan-items-json", help="JSON list/object describing the TicketPlan PlanItems"
    )
    ticket_start.add_argument(
        "--plan-items-file",
        help="Path to a JSON file describing the TicketPlan PlanItems",
    )
    ticket_start.add_argument(
        "--planner-profile",
        help="Optional planner provider profile name for observational provenance",
    )
    ticket_start.add_argument(
        "--planner-model", help="Optional planner model id for observational provenance"
    )

    ticket_preflight = ticket_sub.add_parser(
        "preflight",
        help="Verify canonical repo truth and clean start conditions for a ticket",
    )
    ticket_preflight.add_argument("ticket_id", help="Ticket ID (e.g., AMOF-280)")
    ticket_preflight.add_argument(
        "--repos", help="Comma-separated repo names (default: all writable)"
    )
    ticket_preflight.add_argument(
        "--repo-selections", help="JSON array describing the per-repo intake contract"
    )
    ticket_preflight.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    ticket_sub.add_parser("list", help="List active tickets and their repo branches")

    ticket_switch = ticket_sub.add_parser("switch", help="Switch active ticket")
    ticket_switch.add_argument("ticket_id", help="Ticket to switch to")

    ticket_status = ticket_sub.add_parser(
        "status", help="Show TicketPlan status and promote readiness"
    )
    ticket_status.add_argument(
        "ticket_id", nargs="?", help="Ticket to inspect (defaults to the active ticket)"
    )
    ticket_status.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    ticket_checkpoint = ticket_sub.add_parser(
        "checkpoint",
        help="Create one PlanItem-bound checkpoint commit after validation passes",
    )
    ticket_checkpoint.add_argument(
        "ticket_id",
        nargs="?",
        help="Ticket to checkpoint (defaults to the active ticket)",
    )
    ticket_checkpoint.add_argument(
        "--repo", required=True, help="Repo name inside the ticket worktree set"
    )
    ticket_checkpoint.add_argument(
        "--plan-item",
        action="append",
        dest="plan_item_ids",
        required=True,
        help="Referenced PlanItem id; can be repeated",
    )
    ticket_checkpoint.add_argument(
        "--file",
        action="append",
        dest="files",
        required=True,
        help="Explicit file path to stage; can be repeated",
    )
    ticket_checkpoint.add_argument(
        "--message",
        required=True,
        help="Checkpoint summary appended to the ticket commit prefix",
    )

    ticket_end = ticket_sub.add_parser("end", help="End ticket work")
    ticket_end.add_argument("ticket_id", help="Ticket to end")
    ticket_end.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete feature branches (local + remote)",
    )
    ticket_end.add_argument(
        "--cleanup-local", action="store_true", help="Delete local branches only"
    )

    ticket_env = ticket_sub.add_parser(
        "env",
        help="Create or update ticket GitOps environment files",
    )
    ticket_env_sub = ticket_env.add_subparsers(dest="ticket_env_cmd")

    ticket_env_upsert = ticket_env_sub.add_parser(
        "upsert",
        help="Create or update one TicketEnvironment file deterministically",
    )
    ticket_env_upsert.add_argument(
        "--ticket-id", required=True, help="Ticket identifier (e.g. AMOF-123)"
    )
    ticket_env_upsert.add_argument(
        "--branch", required=True, help="Git branch for the ticket environment"
    )
    ticket_env_upsert.add_argument(
        "--commit-sha", required=True, help="Commit SHA used for image tags"
    )
    ticket_env_upsert.add_argument(
        "--host-mode", choices=["local", "cloud"], required=True, help="Hostname mode"
    )
    ticket_env_upsert.add_argument(
        "--owner-id", help="Raw owner identity (default: operator@amof.dev)"
    )
    ticket_env_upsert.add_argument(
        "--owner-slug", help="Label-safe owner slug (default: operator-amof-dev)"
    )
    ticket_env_upsert.add_argument(
        "--owner-type", default="team", help="Owner type (default: team)"
    )
    ticket_env_upsert.add_argument(
        "--base-domain", help="Override base domain for hostname generation"
    )
    ticket_env_upsert.add_argument(
        "--registry-base",
        help="Image registry base for generated repositories (defaults by host mode)",
    )
    ticket_env_upsert.add_argument(
        "--target-revision",
        default="main",
        help="GitOps target revision stored in the env file (default: main)",
    )
    ticket_env_upsert.add_argument("--output", help="Output path under envs/tickets/")
    ticket_env_upsert.add_argument(
        "--dry-run",
        action="store_true",
        help="Render and compare the target env file without writing it",
    )
    ticket_env_upsert.add_argument(
        "--summary-json",
        action="store_true",
        help="Print machine-readable summary JSON",
    )

    # Push command
    push_parser = subparsers.add_parser(
        "push",
        help="Push all branches (workspace + feature branches) to origin",
    )
    push_parser.add_argument(
        "--message",
        "-m",
        help="Commit message for uncommitted changes (default: auto-generated)",
    )

    promote_main_parser = subparsers.add_parser(
        "promote-main",
        help="Dry-run a coherent candidate-bundle promotion plan into main",
    )
    promote_main_parser.add_argument(
        "--repo", required=True, help="Repository name, e.g. amof"
    )
    promote_main_parser.add_argument(
        "--ticket-id", required=True, help="Ticket identifier, e.g. AMOF-123"
    )
    promote_main_parser.add_argument(
        "--candidate-branch",
        required=True,
        help="Candidate branch containing the validated source and env commits",
    )
    promote_main_parser.add_argument(
        "--source-sha", required=True, help="Validated source/code SHA to promote"
    )
    promote_main_parser.add_argument(
        "--gitops-commit-sha",
        help="Optional AMOF-origin env commit SHA linked to the source SHA; omit for code-only promotions",
    )
    promote_main_parser.add_argument(
        "--expected-main-sha",
        required=True,
        help="Expected current origin/main commit SHA",
    )
    promote_main_parser.add_argument(
        "--promotion-reason",
        required=True,
        help="Short operator reason for the promotion attempt",
    )
    promote_main_evidence = promote_main_parser.add_mutually_exclusive_group(
        required=False
    )
    promote_main_evidence.add_argument(
        "--require-run-summary",
        help="Optional auditable run-summary.json evidence path that must validate before promotion proceeds",
    )
    promote_main_evidence.add_argument(
        "--require-promotion-readiness-result",
        help="Optional auditable promotion-readiness-result.json evidence path that must validate before promotion proceeds",
    )
    promote_main_mode = promote_main_parser.add_mutually_exclusive_group(required=True)
    promote_main_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and plan only; do not write to main",
    )
    promote_main_mode.add_argument(
        "--push",
        action="store_true",
        help="Validate, synthesize one promotion commit, and push it fast-forward to origin/main; requires non-interactive GitHub auth (GITHUB_TOKEN repo/Contents write or credential.helper)",
    )

    promote_main_revert_parser = subparsers.add_parser(
        "promote-main-revert",
        help="Revert one AMOF synthetic promotion commit on main with a fast-forward push",
    )
    promote_main_revert_parser.add_argument(
        "--repo", required=True, help="Repository name, e.g. amof"
    )
    promote_main_revert_parser.add_argument(
        "--synthetic-commit-sha",
        required=True,
        help="Synthetic promotion commit SHA to revert from main",
    )

    # Discard command
    discard_parser = subparsers.add_parser(
        "discard",
        help="Delete workspace and all feature branches (clean slate)",
    )
    discard_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    discard_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without making changes",
    )

    # Archive command
    archive_parser = subparsers.add_parser(
        "archive",
        help="Finish workspace: push, save state (keeps workspace branch by default)",
    )
    archive_parser.add_argument("--message", "-m", help="Optional description")
    archive_parser.add_argument(
        "--force", action="store_true", help="Skip confirmation"
    )
    archive_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done"
    )
    archive_parser.add_argument(
        "--delete-workspace", action="store_true", help="Delete workspace branch"
    )
    archive_parser.add_argument(
        "--cleanup-features", action="store_true", help="Delete all feature branches"
    )

    # Archive list subcommand
    subparsers.add_parser(
        "archive-list",
        help="List archived workspaces for current ecosystem",
    )

    # Ecosystem commands
    ecosystem_parser = subparsers.add_parser(
        "ecosystem",
        help="Manage ecosystems (persistent branch templates)",
    )
    ecosystem_sub = ecosystem_parser.add_subparsers(dest="ecosystem_cmd")

    eco_create = ecosystem_sub.add_parser(
        "create", help="Create a new ecosystem branch"
    )
    eco_create.add_argument("name", help="Ecosystem name (e.g., demo-migration)")
    eco_create.add_argument(
        "--from", dest="from_branch", default="main", help="Base branch"
    )

    ecosystem_sub.add_parser("list", help="List available ecosystems")

    # Actor commands
    actor_parser = subparsers.add_parser(
        "actor",
        help="Manage actors/customers in ecosystem manifest",
    )
    actor_sub = actor_parser.add_subparsers(dest="actor_cmd")

    actor_add = actor_sub.add_parser("add", help="Add an actor to manifest")
    actor_add.add_argument("id", help="Actor ID (e.g., dev, ops)")
    actor_add.add_argument("--name", help="Display name")
    actor_add.add_argument(
        "--role", choices=["reference", "template", "customer"], default="customer"
    )
    actor_add.add_argument(
        "--status", choices=["complete", "in_progress", "planned"], default="planned"
    )

    actor_sub.add_parser("list", help="List actors in ecosystem")

    actor_update = actor_sub.add_parser("update", help="Update actor status")
    actor_update.add_argument("id", help="Actor ID")
    actor_update.add_argument(
        "--status", choices=["complete", "in_progress", "planned"]
    )

    # PR command
    pr_parser = subparsers.add_parser(
        "pr",
        help="Create pull requests for all changed repos",
    )
    pr_parser.add_argument(
        "--reviewers",
        "-r",
        action="append",
        help="Add reviewer (username); can be repeated",
    )
    pr_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without making requests",
    )

    # Jira commands
    jira_parser = subparsers.add_parser(
        "jira",
        help="Jira ticket operations",
    )
    jira_sub = jira_parser.add_subparsers(dest="jira_cmd")

    jira_info = jira_sub.add_parser("info", help="Show ticket details")
    jira_info.add_argument("ticket", help="Ticket ID (e.g., Issue-123)")

    jira_context = jira_sub.add_parser(
        "context", help="Generate AI context from ticket"
    )
    jira_context.add_argument("ticket", help="Ticket ID")
    jira_context.add_argument("--output", "-o", help="Output file path")

    # KB (Confluence) commands
    kb_parser = subparsers.add_parser(
        "kb",
        help="Knowledge base sync with Confluence",
    )
    kb_sub = kb_parser.add_subparsers(dest="kb_cmd")

    kb_pull = kb_sub.add_parser("pull", help="Pull KB articles from Confluence")
    kb_pull.add_argument("--space", help="Confluence space key")

    kb_push = kb_sub.add_parser("push", help="Push local KB to Confluence")
    kb_push.add_argument("--space", help="Confluence space key")

    kb_diff = kb_sub.add_parser(
        "diff", help="Show differences between local and Confluence"
    )
    kb_diff.add_argument("--space", help="Confluence space key")

    kb_sync = kb_sub.add_parser("sync", help="Bi-directional sync")
    kb_sync.add_argument("--space", help="Confluence space key")

    kb_consolidate = kb_sub.add_parser(
        "consolidate",
        help="Consolidate journal entries into KB articles by topic",
    )
    kb_consolidate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be consolidated without modifying files",
    )

    # Check command
    subparsers.add_parser(
        "check",
        help="Check environment prerequisites (git, docker, helm, etc.)",
    )

    # Help command (extended help with examples)
    help_parser = subparsers.add_parser(
        "help",
        help="Extended help with examples and workflow guidance",
    )
    help_parser.add_argument(
        "topic",
        nargs="?",
        help="Command name to get help for (e.g., agent, release, ticket)",
    )

    # Troubleshoot command
    subparsers.add_parser(
        "troubleshoot",
        help="Diagnose common issues (env, workspace, config, recent errors)",
    )

    # Spin command (provisioner deploy/destroy)
    spin_parser = subparsers.add_parser(
        "spin",
        help="Deploy or destroy infrastructure via ecosystem provisioner",
        description=(
            "Run the provisioner (e.g. aws-spin) for the target ecosystem.\n\n"
            "  amof -e aws-boilerplate spin deploy\n"
            "  amof -e aws-boilerplate spin destroy\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    spin_parser.add_argument(
        "action",
        choices=["deploy", "destroy"],
        help="Deploy or destroy infrastructure",
    )

    # Release command (AMOF version management)
    release_parser = subparsers.add_parser(
        "release",
        help="Bump AMOF version, update docs, commit, tag, push",
        description=(
            "Automate AMOF releases with SemVer + pre-release suffixes.\n\n"
            "  amof release status            Show current version info\n"
            "  amof release log               Show release history\n"
            "  amof release patch --alpha     v1.0.3-alpha.1\n"
            "  amof release patch --alpha     v1.0.3-alpha.2 (auto-increment)\n"
            "  amof release promote --beta    alpha -> beta.1\n"
            "  amof release promote           pre-release -> stable\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    release_parser.add_argument(
        "bump",
        choices=["major", "minor", "patch", "promote", "status", "log"],
        help="Version part to bump, 'promote' to advance stage, 'status' to inspect, 'log' for history",
    )
    release_pre = release_parser.add_mutually_exclusive_group()
    release_pre.add_argument(
        "--alpha",
        action="store_const",
        const="alpha",
        dest="pre",
        help="Tag as alpha pre-release",
    )
    release_pre.add_argument(
        "--beta",
        action="store_const",
        const="beta",
        dest="pre",
        help="Tag as beta pre-release (or promote target)",
    )
    release_pre.add_argument(
        "--rc",
        action="store_const",
        const="rc",
        dest="pre",
        help="Tag as release candidate (or promote target)",
    )
    release_parser.add_argument(
        "--message",
        "-m",
        help="Tag annotation message (default: version string)",
    )
    release_parser.add_argument(
        "--no-push",
        action="store_true",
        help="Don't push after tagging (local only)",
    )
    release_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )
    release_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    release_parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Bypass all pre-release validation checks",
    )
    release_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat validation warnings as errors",
    )

    # MCP command (Model Context Protocol server)
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Start the AMOF MCP server (stdio transport for IDE integration)",
        description=(
            "Run the MCP (Model Context Protocol) server for IDE tool integration.\n\n"
            "  amof mcp start    Launch MCP server on stdio\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mcp_parser.add_argument(
        "mcp_action",
        nargs="?",
        choices=["start"],
        default="start",
        help="Action (default: start)",
    )

    # Server command (FastAPI API Layer)
    server_parser = subparsers.add_parser("server", help="Start the AMOF API server")
    server_parser.add_argument(
        "action",
        nargs="?",
        choices=["start", "stop", "status", "restart"],
        default="start",
        help="Action to perform (default: start)",
    )
    server_parser.add_argument(
        "--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)"
    )
    server_parser.add_argument(
        "--port", type=int, default=8000, help="Port to bind to (default: 8000)"
    )
    server_parser.add_argument(
        "--reload", action="store_true", help="Reload on code changes (development)"
    )

    return parser.parse_args()
