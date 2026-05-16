"""Troubleshoot command -- diagnose common AMOF issues.

Analyzes recent event logs, checks environment, and suggests fixes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def cmd_troubleshoot(manifest: Optional[Dict[str, Any]] = None) -> int:
    """Run diagnostics and suggest fixes for common issues.

    Checks:
    1. Environment (API keys, tools, .env)
    2. Workspace state (branches, repos)
    3. Recent agent errors (from event logs)
    4. Configuration (agent.yaml, guardrails.yaml)
    """
    workspace_root = Path.cwd()
    issues: List[str] = []
    warnings: List[str] = []
    ok: List[str] = []

    print("\n  AMOF Troubleshooter\n")

    # ── 1. Environment checks ─────────────────────────────────

    print("  [1/4] Checking environment...")

    # .env file
    env_path = workspace_root / ".env"
    if env_path.exists():
        ok.append(".env file exists")
    else:
        issues.append(
            ".env file not found\n"
            "    Fix: cp env .env && edit .env with your credentials"
        )

    # API keys
    if os.environ.get("ANTHROPIC_API_KEY"):
        ok.append("ANTHROPIC_API_KEY is set")
    else:
        warnings.append(
            "ANTHROPIC_API_KEY not set (needed for agent)\n"
            "    Fix: Add to .env: ANTHROPIC_API_KEY=sk-ant-...\n"
            "         Then: source .env"
        )

    if os.environ.get("OPENAI_API_KEY"):
        ok.append("OPENAI_API_KEY is set")

    # Required tools
    for tool, install_hint in [
        ("git", "https://git-scm.com/downloads"),
        ("python3", "https://python.org/downloads"),
    ]:
        try:
            subprocess.run(
                [tool, "--version"], capture_output=True, timeout=5, check=True
            )
            ok.append(f"{tool} is installed")
        except (FileNotFoundError, subprocess.SubprocessError):
            issues.append(
                f"{tool} not found\n"
                f"    Fix: Install from {install_hint}"
            )

    # Optional tools
    for tool, purpose in [
        ("helm", "Helm chart operations"),
        ("kubectl", "Kubernetes debugging"),
        ("aws", "AWS CloudFormation/Lambda logs"),
        ("docker", "Container image operations"),
    ]:
        try:
            subprocess.run(
                [tool, "version"] if tool == "helm" else [tool, "--version"],
                capture_output=True, timeout=5, check=True,
            )
            ok.append(f"{tool} is installed")
        except (FileNotFoundError, subprocess.SubprocessError):
            warnings.append(
                f"{tool} not installed (optional: {purpose})"
            )

    # ── 2. Workspace checks ───────────────────────────────────

    print("  [2/4] Checking workspace...")

    # Git repo
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            ok.append("Inside a git repository")
        else:
            issues.append("Not inside a git repository")
    except Exception:
        issues.append("Cannot determine git status")

    # Current branch
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        branch = result.stdout.strip()
        if branch:
            ok.append(f"On branch: {branch}")
            if branch.startswith("workspace/"):
                ok.append("Branch is a workspace branch")
            elif branch == "main":
                warnings.append(
                    "On main branch (not a workspace)\n"
                    "    Fix: amof -e <ecosystem> install"
                )
        else:
            warnings.append("Cannot determine current branch (detached HEAD?)")
    except Exception:
        pass

    # State file
    state_path = workspace_root / ".amof" / "state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            eco = state.get("ecosystem", "unknown")
            ok.append(f"Workspace state: ecosystem={eco}")
        except Exception:
            warnings.append("state.json exists but is corrupted")
    else:
        warnings.append(
            "No .amof/state.json found\n"
            "    This is normal if you haven't run `amof install` yet"
        )

    # Repos directory
    repos_dir = workspace_root / "repos"
    if repos_dir.exists():
        repo_count = sum(1 for d in repos_dir.iterdir() if d.is_dir())
        ok.append(f"repos/ directory exists ({repo_count} repos)")
    else:
        warnings.append(
            "No repos/ directory\n"
            "    Fix: amof -e <ecosystem> install"
        )

    # ── 3. Recent agent errors ────────────────────────────────

    print("  [3/4] Checking recent agent sessions...")

    runs_dir = workspace_root / ".amof" / "runs"
    recent_errors: List[str] = []

    if runs_dir.exists():
        # Get most recent 3 session dirs
        sessions = sorted(runs_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
        for session_dir in sessions[:3]:
            events_path = session_dir / "events.jsonl"
            if not events_path.exists():
                continue

            try:
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("type") == "tool_error":
                        error_msg = event.get("error", "")[:100]
                        tool_name = event.get("tool", "unknown")
                        recent_errors.append(f"{tool_name}: {error_msg}")
                    elif event.get("type") == "error":
                        recent_errors.append(event.get("message", "")[:100])
            except Exception:
                pass

        if recent_errors:
            # Deduplicate
            seen = set()
            unique_errors = []
            for err in recent_errors:
                if err not in seen:
                    seen.add(err)
                    unique_errors.append(err)

            for err in unique_errors[:5]:
                warnings.append(f"Recent error: {err}")

            if len(unique_errors) > 5:
                warnings.append(
                    f"  ... and {len(unique_errors) - 5} more errors in recent sessions"
                )
        else:
            ok.append("No recent agent errors")
    else:
        ok.append("No agent sessions yet (runs/ not found)")

    # ── 4. Configuration checks ───────────────────────────────

    print("  [4/4] Checking configuration...")

    # agent.yaml
    agent_yaml = workspace_root / ".amof" / "agent.yaml"
    if agent_yaml.exists():
        ok.append("agent.yaml exists")
        try:
            text = agent_yaml.read_text(encoding="utf-8")
            if "default_ecosystem:" in text:
                ok.append("Default ecosystem configured")
            else:
                warnings.append(
                    "No default_ecosystem in agent.yaml\n"
                    "    Fix: Add 'default_ecosystem: my-project' to .amof/agent.yaml"
                )
        except Exception:
            warnings.append("Cannot read agent.yaml")
    else:
        warnings.append(
            "No .amof/agent.yaml\n"
            "    Fix: Create from template or run 'amof install'"
        )

    # guardrails.yaml
    guardrails_yaml = workspace_root / ".amof" / "rules" / "guardrails.yaml"
    if guardrails_yaml.exists():
        ok.append("guardrails.yaml exists")
    else:
        warnings.append(
            "No guardrails.yaml — agent runs without protection\n"
            "    Fix: Create .amof/rules/guardrails.yaml"
        )

    # linters.yaml
    linters_yaml = workspace_root / ".amof" / "rules" / "linters.yaml"
    if linters_yaml.exists():
        ok.append("linters.yaml exists")

    # requirements.txt
    req_path = workspace_root / "requirements.txt"
    if req_path.exists():
        ok.append("requirements.txt exists")
        # Check if anthropic is installed
        try:
            import anthropic  # noqa: F401
            ok.append("anthropic SDK installed")
        except ImportError:
            issues.append(
                "anthropic SDK not installed\n"
                "    Fix: pip install -r requirements.txt"
            )
    else:
        warnings.append("No requirements.txt")

    # ── Summary ───────────────────────────────────────────────

    print()
    if ok:
        print(f"  ✓ {len(ok)} checks passed:")
        for item in ok:
            print(f"    ✓ {item}")

    if warnings:
        print(f"\n  ⚠ {len(warnings)} warnings:")
        for item in warnings:
            first_line = item.split("\n")[0]
            rest = item.split("\n")[1:]
            print(f"    ⚠ {first_line}")
            for line in rest:
                print(f"  {line}")

    if issues:
        print(f"\n  ✗ {len(issues)} issues to fix:")
        for item in issues:
            first_line = item.split("\n")[0]
            rest = item.split("\n")[1:]
            print(f"    ✗ {first_line}")
            for line in rest:
                print(f"  {line}")

    print()
    total = len(ok) + len(warnings) + len(issues)
    if not issues and not warnings:
        print(f"  All {total} checks passed. Everything looks good!")
    elif not issues:
        print(f"  {len(ok)}/{total} passed, {len(warnings)} warnings. System is functional.")
    else:
        print(f"  {len(ok)}/{total} passed, {len(issues)} issues need attention.")
    print()

    return 1 if issues else 0
