"""Extended help command for public AMOF guidance."""

from __future__ import annotations

from typing import Dict, Optional


_HELP: Dict[str, str] = {
    "init": """
  amof init --adopt . — Adopt the current Git repository

  Public use:
    amof init --adopt .
    amof doctor
    amof agent --plan "Inspect this repo"

  Adoption stores AMOF metadata in app-data by default. It should not write
  .amof, ecosystems, or context directories into the target repo.
""",

    "setup": """
  amof setup provider — Create a provider profile reference

  Public use:
    amof setup provider --list
    amof setup provider openrouter --name openrouter-default --activate
    amof setup provider bedrock --name bedrock-default --activate --yes
    amof setup provider openrouter --name openrouter-default --activate --yes

  Provider setup stores environment variable references only. It does not store
  raw API keys and does not call a provider while writing the profile.
""",

    "chat": """
  amof chat — Read-only planning proposal through remote IAL

  Public use:
    amof chat plan "Inspect this repo" --repo .
    amof chat plan "Plan AMOF-CHAT-001" --ticket-id AMOF-CHAT-001 --file README.md --file scripts/amof/cli.py

  This command reads only a bounded set of repo files, routes inference through
  the active remote-ial provider profile, writes evidence to AMOF app-data under
  chat-plan run paths, and emits a non-executable PlanBundle with
  `execution_allowed: false` that still requires user approval.
""",

    "studio": """
  amof studio — Additive Studio Session ledger core

  Public use:
    amof studio create
    amof studio show studio-20260607-203142
    amof studio checkpoint add studio-20260607-203142 --summary "Planning complete"
    amof studio attach-run studio-20260607-203142 20260607-193107
    amof studio end studio-20260607-203142

  Studio commands create an append-only parent session ledger in AMOF app-data.
  They do not rename existing run `session_id` values and do not require a
  polished client to manage attached runs.
""",

    "agent": """
  amof agent — Run AMOF planning or bounded execution

  Public planning path:
    amof agent --plan "Inspect this repo" --no-follow-up

  No-key validation should reach provider configuration, for example:
    [agent] OPENROUTER_API_KEY not set.

  Bounded execution is manual/advanced:
    amof agent --provider openrouter --plan-execute "Make a bounded change. Do not commit." --no-follow-up

  Execution output must be reviewed as a Git diff. AMOF must not auto-commit,
  auto-push, tag, or promote worker changes.
""",

    "doctor": """
  amof doctor — Report bootstrap readiness

  Public use:
    amof doctor
    amof doctor --json

  Fresh installs may warn when no provider profile is configured. That warning
  is acceptable for install/adoption smoke and does not imply private runtime
  prerequisites.
""",

    "bootstrap": """
  amof bootstrap — Emit bootstrap evidence

  Public use:
    amof bootstrap contract --json
    amof bootstrap bundle --json

  Evidence commands report truthful PASS, WARN, or BLOCKED status. They should
  not require private topology or provider keys for no-key public smoke.
""",

    "paths": """
  amof paths — Show resolved AMOF app-data paths

  Public use:
    amof paths --json

  AMOF runtime state belongs in app-data roots such as ~/.config/amof,
  ~/.local/share/amof, ~/.cache/amof, and ~/.local/state/amof. When AMOF_HOME is
  set, AMOF uses that directory as a flat app-data root.
""",

    "check": """
  amof check — Verify public prerequisites

  Public use:
    amof check

  Checks required local tools and reports optional warnings. It should not
  require kubeconfigs, clusters, private deployment topology, or provider keys.
""",

    "update": """
  amof update — Update a pipx-managed AMOF install

  Public use:
    amof update --check
    amof update
    amof update --version v2.3.0

  The update path targets public release tags from the public AMOF repository.
""",

    "uninstall": """
  amof uninstall — Remove the local AMOF CLI install

  Public use:
    amof uninstall

  For pipx-managed installs, AMOF delegates to pipx uninstall. Repositories and
  AMOF app-data are not deleted by the CLI uninstall command.
""",

    "troubleshoot": """
  amof troubleshoot — Diagnose local AMOF issues

  Public use:
    amof troubleshoot

  Intended for local environment, workspace, config, and recent error hints.
""",

    "shell": """
  amof shell — Emit shell integration helpers

  Public use:
    amof shell init bash

  Convenience command for local shell integration.
""",

    "status": """
  amof status — Advanced workspace/adopted-repo status

  This command remains available for users who know the AMOF workspace model or
  have adopted a repository, but it is not the public first-run path.
""",

    "context": """
  amof context — Advanced context metadata operations

  This command can record local or remote context metadata. It is advanced and
  should not be needed for the public install/adoption happy path.
""",

    "manifest": """
  amof manifest — Advanced ecosystem manifest inspection

  Useful for AMOF workspace manifests. Adopted-repo public users do not need to
  create an ecosystem.yaml by hand.
""",

    "generated-build": """
  amof generated-build — Advanced build-proof lane

  Detects, renders, and proofs generated build artifacts. Keep this manual and
  review-oriented; it is not part of the first-run quickstart.
""",

    "director": """
  amof director — Advanced bounded run planning

  Director commands create and inspect bounded execution envelopes. They are
  discoverable for advanced/manual evidence workflows, not first-run UX.
""",

    "workspace": """
  amof workspace — Advanced workspace registry and materialization

  Workspace commands remain callable for AMOF workspace workflows. They are not
  required for adopting one existing repo with app-data.
""",

    "install": """
  amof install — Workspace-only bootstrap

  This command bootstraps an AMOF ecosystem workspace. It is not the public pipx
  install command and should not be used as the adopted-repo quickstart.
""",

    "sync": """
  amof sync — Workspace-only repository synchronization

  Syncs repositories from an ecosystem manifest. This remains callable for
  workspace users, but it is not part of the public first-run path.
""",

    "ticket": """
  amof ticket — Workspace-only ticket lifecycle

  Ticket commands create and manage branches across AMOF workspace repos. Public
  adopted-repo users should use their normal Git branch and pull request flow.
""",

    "push": """
  amof push — Maintainer/workspace-only push helper

  This command can commit and push workspace branches. It is not a public
  publishing command for arbitrary adopted repos. Use ordinary Git review and PR
  workflows unless you have explicitly opted into AMOF workspace automation.
""",

    "release": """
  amof release — Maintainer-only release automation

  This command can bump versions, commit, tag, and push. It is not part of the
  public quickstart and must not be used for this cleanup task.
""",

    "promote-main": """
  amof promote-main — Maintainer-only promotion workflow

  Promotion requires explicit candidate evidence, expected main SHA, and an
  operator decision. It is not a public user publishing command.
""",

    "promote-main-revert": """
  amof promote-main-revert — Maintainer-only promotion revert workflow

  This is a guarded AMOF mainline recovery command, not a public first-run path.
""",

    "pr": """
  amof pr — Optional integration / maintainer surface

  This command is not part of the clean public baseline until it has public docs,
  tests, and provider-agnostic behavior.
""",

    "jira": """
  amof jira — Optional Atlassian integration

  Requires external credentials and is not part of the public quickstart.
""",

    "kb": """
  amof kb — Optional knowledge-base integration

  Requires external credentials and is not part of the public quickstart.
""",

    "spin": """
  amof spin — Infrastructure provisioner surface

  This is not included in the public baseline. Do not use it unless you have a
  documented public provisioner template and explicit operator intent.
""",

    "mcp": """
  amof mcp — Experimental IDE integration server

  Starts the AMOF MCP server for users who intentionally configure IDE tooling.
  Not part of first-run public smoke.
""",

    "server": """
  amof server — Experimental local API server

  Starts the AMOF API server. Not part of the clean public first-run baseline.
""",
}

_PUBLIC_TOPICS = [
    "check",
    "doctor",
    "paths",
    "setup",
    "init",
    "chat",
    "agent",
    "bootstrap",
    "update",
    "uninstall",
    "troubleshoot",
    "studio",
]
_ADVANCED_TOPICS = ["status", "context", "manifest", "generated-build", "director", "workspace", "mcp", "server"]
_WORKSPACE_TOPICS = ["install", "sync", "ticket"]
_MAINTAINER_TOPICS = ["push", "release", "promote-main", "promote-main-revert", "pr"]
_OPTIONAL_TOPICS = ["jira", "kb", "spin"]


def _topic_list(names: list[str]) -> str:
    return "\n".join(
        f"    {name:<18} {(_HELP[name].strip().splitlines()[0]).split(' — ', 1)[-1]}"
        for name in names
        if name in _HELP
    )


def cmd_help(topic: Optional[str] = None) -> int:
    """Show extended help for a command or general overview."""
    if topic and topic in _HELP:
        print(_HELP[topic])
        return 0

    if topic:
        matches = [name for name in _HELP if name.startswith(topic)]
        if len(matches) == 1:
            print(_HELP[matches[0]])
            return 0
        if matches:
            print(f"\n  Did you mean one of: {', '.join(matches)}?\n")
            return 1
        print(f"\n  No help found for '{topic}'.")
        print(f"  Available topics: {', '.join(sorted(_HELP.keys()))}\n")
        return 1

    print(f"""
  AMOF — Agentic Operations Fabric

  Public quickstart:
    pipx install "git+https://github.com/marekhotshot/amof.git@v2.3.0"
    amof check
    amof doctor
    amof init --adopt .
    amof setup provider --list
    amof agent --plan "Inspect this repo" --no-follow-up
    amof bootstrap bundle --json

  Public first-run commands:
{_topic_list(_PUBLIC_TOPICS)}

  Advanced/manual topics:
{_topic_list(_ADVANCED_TOPICS)}

  Workspace-only topics:
{_topic_list(_WORKSPACE_TOPICS)}

  Maintainer-only topics:
{_topic_list(_MAINTAINER_TOPICS)}

  Optional integration topics:
{_topic_list(_OPTIONAL_TOPICS)}

  Boundaries:
    - Public pipx users should use the `amof` shim, not system `python -m amof`.
    - Provider setup stores env var references only and does not call providers.
    - No-key agent validation should stop at provider configuration, not -e.
    - Bounded worker output must be reviewed and committed manually.
    - AMOF must not auto-commit, auto-push, tag, or promote worker changes.

  More detail:
    amof help <topic>
    docs/runbooks/happy-path-agent-workflow.md
    docs/operations/public-surface-taxonomy.md
""")
    return 0
