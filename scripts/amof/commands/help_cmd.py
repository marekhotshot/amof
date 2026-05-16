"""Extended help command -- rich examples and workflow guidance.

Provides more context than --help, including real-world examples,
common workflows, and links to documentation.
"""

from __future__ import annotations

import sys
from typing import Dict, Optional


# ── Help content for each command ─────────────────────────────

_HELP: Dict[str, str] = {
    "install": """
  amof install — Bootstrap an ecosystem workspace

  Creates the workspace branch, clones repos, generates profiles,
  and builds the codebase index.

  Examples:
    amof -e demo-dev install              # Full setup
    amof -e demo-dev install --dry-run    # Preview without changes
    amof -e demo-dev install --push       # Push branches to origin

  What it does:
    1. Creates workspace/<ecosystem> branch
    2. Clones all repos from ecosystem.yaml to repos/
    3. Creates feature branches for writable repos
    4. Generates .amof/profile.md in each repo
    5. Builds Merkle tree + codebase index (if API key set)

  Next steps:
    amof ticket start PROJ-123    # Start working on a ticket
    amof agent                    # Launch the AI agent
""",

    "agent": """
  amof agent — Run the AI coding agent

  Supports interactive shell, single-shot tasks, and plan-execute mode.
  Loads defaults from .amof/agent.yaml.

  Setup:
    amof agent install                            # Create venv + install deps
    source .venv/bin/activate                     # Activate the environment

  Examples:
    amof agent                                    # Interactive shell
    amof agent "fix the auth bug"                 # Single-shot task
    amof agent --plan "analyze the codebase"      # Read-only mode
    amof agent --plan-execute "refactor config"   # Plan then execute
    amof agent --max-cost 1.00 "small fix"        # With cost limit
    amof agent --resume <session-id>              # Resume interrupted

  Interactive shell commands:
    /quick <task>    Run without planning
    /status          Show cost and telemetry
    /review          Show git diff --stat
    /release         Tag a release
    /help            Show all commands
    Ctrl+C           Cancel current run
    Ctrl+D           Exit

  Cost optimization:
    --model-ladder      Use tiered models (haiku/sonnet/opus)
    --max-cost 2.00     Set budget ceiling
    --plan-execute      Strong model plans, cheap models execute

""",

    "ticket": """
  amof ticket — Manage tickets within ecosystem workspace

  Commands:
    amof ticket start PROJ-123              # Create feature branches
    amof ticket start PROJ-123 --repos a,b  # Specific repos only
    amof ticket list                        # Show all tickets
    amof ticket switch PROJ-456             # Switch (auto-commits)
    amof ticket end PROJ-123                # Mark done
    amof ticket end PROJ-123 --cleanup      # Delete branches too

  Workflow:
    1. Start ticket -> creates feature/<ticket> in each repo
    2. Work across repos, use agent or manual edits
    3. Push when ready: amof push
    4. End ticket when work is done
""",

    "release": """
  amof release — Automated versioning, tagging, and release management

  Bumps version, updates CHANGELOG + README + __version__ files,
  writes audit record, commits, tags, and pushes.

  Inspect:
    amof release status             # Current version, drift, readiness
    amof release log                # Release history from audit trail

  Bump:
    amof release patch --alpha      # v1.0.3-alpha.1
    amof release patch --alpha      # v1.0.3-alpha.2 (auto-increment)
    amof release minor --alpha      # v1.1.0-alpha.1
    amof release major              # v2.0.0

  Promote:
    amof release promote --beta     # alpha -> beta.1
    amof release promote --rc       # beta -> rc.1
    amof release promote            # pre-release -> stable

  Flags:
    --dry-run                       # Preview without changes
    -y / --yes                      # Skip confirmation
    --no-push                       # Local only (no git push)
    --skip-validation               # Bypass pre-release checks
    --strict                        # Treat warnings as errors

  Versioning flow:
    alpha.1 -> alpha.2 -> beta.1 -> rc.1 -> stable

  Audit trail: releases/<tag>.json written on each release.

  Also available as /release in the interactive shell
  and [t] in the post-run menu.

  Config: auto_tag_on_complete in .amof/agent.yaml
""",

    "status": """
  amof status — Check repository status

  Shows branch, commit, mode (RO/RW), and sync status for all repos.

  Examples:
    amof -e demo-dev status              # All repos
    amof -e demo-dev status --repo iac   # Specific repo

  Output columns:
    REPO     Repository name
    BRANCH   Current branch
    COMMIT   Short hash of HEAD
    MODE     RW (writable) or RO (readonly)
    STATUS   OK, UNPUSHED, DIRTY, WRONG_BRANCH
""",

    "push": """
  amof push — Push all branches to origin

  Pushes workspace branch + all feature branches in one command.

  Examples:
    amof push                        # Push all
    amof push -m "feat: my change"   # Commit first, then push
""",

    "sync": """
  amof sync — Clone/update repositories

  Syncs repos from ecosystem.yaml. Clones new repos, pulls existing ones.
  Auto-generates repo profiles after sync.

  Examples:
    amof -e demo-dev sync              # All repos
    amof -e demo-dev sync --repo iac   # Specific repo
""",

    "profile": """
  amof profile — Generate repo profiles for agent navigation

  Content-aware tech stack detection. Reads Chart.yaml, package.json,
  pom.xml, Dockerfile, etc. Generates .amof/profile.md in each repo.

  Examples:
    amof -e demo-dev profile --all     # All repos
    amof -e demo-dev profile my-repo   # Single repo
""",

    "troubleshoot": """
  amof troubleshoot — Diagnose common issues

  Checks environment, workspace, recent agent errors, and configuration.
  Provides actionable fix suggestions for each issue found.

  Examples:
    amof troubleshoot                  # Run all diagnostics

  Checks:
    • Environment: .env, API keys, required tools (git, python)
    • Workspace: git repo, branch, state.json, repos/
    • Agent: recent errors from event logs
    • Config: agent.yaml, guardrails.yaml, linters.yaml
""",

    "kb": """
  amof kb — Knowledge base operations

  Sync with Confluence or consolidate journals into KB articles.

  Examples:
    amof -e demo-dev kb pull              # Pull from Confluence
    amof -e demo-dev kb push              # Push to Confluence
    amof -e demo-dev kb diff              # Show differences
    amof -e demo-dev kb sync              # Bi-directional
    amof -e demo-dev kb consolidate       # Journals → KB articles
    amof -e demo-dev kb consolidate --dry-run
""",

    "pr": """
  amof pr — Create pull requests for all repos with changes

  Uses Bitbucket REST API. Requires BITBUCKET_USER and BITBUCKET_TOKEN.

  Examples:
    amof -e demo-dev pr                       # Create PRs
    amof -e demo-dev pr --reviewers alice,bob  # With reviewers
    amof -e demo-dev pr --dry-run             # Preview
""",

    "archive": """
  amof archive — Finish workspace (preserve repo branches)

  Pushes all changes, saves state to archives/, deletes workspace branch.
  Keeps repo feature branches for pending PRs.

  Examples:
    amof -e demo-dev archive -m "done"         # Archive with message
    amof -e demo-dev archive --dry-run         # Preview
    amof -e demo-dev archive --cleanup-features # Also delete feature branches
""",

    "discard": """
  amof discard — Delete workspace and all feature branches

  Removes everything: workspace branch, feature branches, repos/.
  Use when you want to start fresh.

  Examples:
    amof -e demo-dev discard           # Interactive confirmation
    amof -e demo-dev discard --force   # Skip confirmation
    amof -e demo-dev discard --dry-run # Preview
""",

    "manifest": """
  amof manifest — Validate and inspect ecosystem.yaml

  Examples:
    amof manifest validate    # Check for errors
    amof manifest show        # Display contents
""",

    "check": """
  amof check — Verify environment prerequisites

  Checks that required tools are installed: git, docker, helm, aws, kubectl.

  Examples:
    amof check
""",
}


def cmd_help(topic: Optional[str] = None) -> int:
    """Show extended help for a command or general overview."""
    if topic and topic in _HELP:
        print(_HELP[topic])
        return 0

    if topic:
        # Try partial match
        matches = [k for k in _HELP if k.startswith(topic)]
        if len(matches) == 1:
            print(_HELP[matches[0]])
            return 0
        elif matches:
            print(f"\n  Did you mean one of: {', '.join(matches)}?\n")
            return 1
        else:
            print(f"\n  No help found for '{topic}'.")
            print(f"  Available topics: {', '.join(sorted(_HELP.keys()))}\n")
            return 1

    # General overview
    print("""
  AMOF — Agentic Multirepo Operating Framework

  Usage: amof [options] <command> [args]

  Getting started:
    amof -e my-project install     Bootstrap workspace
    amof ticket start PROJ-123     Start ticket work
    amof agent                     Launch AI agent (interactive)
    amof status                    Check repo status
    amof push                      Push all changes

  Essential commands:
    install       Bootstrap ecosystem workspace
    agent         Run the AI coding agent
    ticket        Manage tickets (start, list, switch, end)
    status        Show repository status
    push          Push all branches
    release        Version management (status, bump, promote, log)

  Operational:
    sync          Clone/update repos
    profile       Generate repo profiles
    manifest      Validate ecosystem.yaml
    check         Verify prerequisites
    troubleshoot  Diagnose common issues

  Integrations:
    pr            Create pull requests
    jira          Jira ticket operations
    kb            Knowledge base / Confluence sync

  Cleanup:
    archive       Finish workspace (keeps branches)
    discard       Delete everything

  Get detailed help:
    amof help <command>            Extended help with examples
    amof <command> --help          Flag reference

  Documentation:
    docs/getting-started.md        Quick start guide
    docs/cli-reference.md          CLI command reference
    docs/architecture.md           Architecture overview
""")
    return 0
