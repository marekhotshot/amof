#!/usr/bin/env python3
"""Update one ticket env file from the current branch state.

This is a thin operator-facing writer flow over:
  python3 scripts/amof.py ticket env upsert

It derives current branch and HEAD SHA, updates the target env file,
stages only that file, commits only when it changed, and optionally pushes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.intake.amof_commit import build_amof_commit_event, decide_amof_commit_build_write
from amof.intake.build_write import RUNTIME_DECISION_STATE
from amof.intake.github_push import decide_github_push_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update one ticket env file from current branch state."
    )
    parser.add_argument("--ticket-id", help="Override ticket id. Otherwise infer from current branch.")
    parser.add_argument("--branch", help="Override branch stored in the env file.")
    parser.add_argument(
        "--commit-sha",
        help="Override commit SHA stored in the env file. Defaults to the current HEAD short SHA.",
    )
    parser.add_argument(
        "--host-mode",
        choices=("local", "cloud"),
        default="local",
        help="Hostname mode passed to the canonical wrapper (default: local).",
    )
    parser.add_argument(
        "--owner-id",
        default="operator@amof.dev",
        help="Owner identity stored in the env file (default: operator@amof.dev).",
    )
    parser.add_argument(
        "--owner-slug",
        default="operator-amof-dev",
        help="Label-safe owner slug stored in the env file (default: operator-amof-dev).",
    )
    parser.add_argument(
        "--owner-type",
        default="team",
        help="Owner type stored in the env file (default: team).",
    )
    parser.add_argument(
        "--target-revision",
        help="Override target revision stored in the env file. Defaults to the resolved branch.",
    )
    parser.add_argument(
        "--registry-base",
        help="Override image registry base stored in the env file.",
    )
    parser.add_argument(
        "--output",
        help="Optional explicit output path under envs/tickets/.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and report the env update without writing, committing, or pushing.",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push the current checked-out branch after committing the env file.",
    )
    parser.add_argument(
        "--summary-json",
        action="store_true",
        help="Print a machine-readable summary instead of human output.",
    )
    return parser.parse_args()


def run(
    args: list[str],
    *,
    cwd: Path,
    capture_output: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=capture_output,
        text=True,
        check=check,
    )


def git_output(repo_root: Path, *git_args: str) -> str:
    return run(["git", *git_args], cwd=repo_root).stdout.strip()


def load_workspace_env(workspace_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env_path = workspace_root / ".env"
    if not env_path.exists():
        return env
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if value.startswith("~"):
            value = str(Path(value).expanduser())
        if key and value and key not in env:
            env[key] = value
    return env


def infer_ticket_id(branch: str) -> str | None:
    match = re.search(r"([A-Za-z][A-Za-z0-9]+-\d+)", branch)
    if not match:
        return None
    return match.group(1).upper()


def build_push_url(origin_url: str, token: str) -> str:
    if origin_url.startswith("git@github.com:"):
        path = origin_url.removeprefix("git@github.com:")
        return f"https://{token}@github.com/{path}"
    if origin_url.startswith("https://github.com/"):
        return origin_url.replace("https://github.com/", f"https://{token}@github.com/", 1)
    raise RuntimeError(f"Unsupported push remote for token auth: {origin_url}")


def build_github_replay_payload(*, repo: str, branch: str, sha: str, actor: str, message: str, changed_files: list[str]) -> dict[str, object]:
    return {
        "ref": f"refs/heads/{branch}",
        "after": sha,
        "deleted": False,
        "created": False,
        "forced": False,
        "repository": {"full_name": repo, "name": repo.rsplit("/", 1)[-1]},
        "sender": {"login": actor},
        "head_commit": {
            "message": message,
            "modified": changed_files,
            "added": [],
            "removed": [],
        },
        "commits": [
            {
                "modified": changed_files,
                "added": [],
                "removed": [],
            }
        ],
    }


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    workspace_root = repo_root.parents[1]
    command_env = load_workspace_env(workspace_root)

    current_branch = git_output(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if current_branch == "HEAD" and not args.branch:
        sys.stderr.write("[ticket writer] Detached HEAD. Pass --branch explicitly.\n")
        return 1

    ticket_branch = args.branch or current_branch
    ticket_id = args.ticket_id or infer_ticket_id(ticket_branch)
    if not ticket_id:
        sys.stderr.write(
            "[ticket writer] Could not infer ticket id from branch. Pass --ticket-id explicitly.\n"
        )
        return 1

    head_sha = args.commit_sha or git_output(repo_root, "rev-parse", "--short=16", "HEAD")
    target_revision = args.target_revision or ticket_branch

    staged = git_output(repo_root, "diff", "--cached", "--name-only")
    if staged:
        sys.stderr.write(
            "[ticket writer] Refusing to commit with pre-staged changes present. Clear the index first.\n"
        )
        return 1

    wrapper_cmd = [
        sys.executable,
        "scripts/amof.py",
        "ticket",
        "env",
        "upsert",
        "--ticket-id",
        ticket_id,
        "--branch",
        ticket_branch,
        "--commit-sha",
        head_sha,
        "--host-mode",
        args.host_mode,
        "--owner-id",
        args.owner_id,
        "--owner-slug",
        args.owner_slug,
        "--owner-type",
        args.owner_type,
        "--target-revision",
        target_revision,
        "--summary-json",
    ]
    if args.registry_base:
        wrapper_cmd.extend(["--registry-base", args.registry_base])
    if args.output:
        wrapper_cmd.extend(["--output", args.output])
    if args.dry_run:
        wrapper_cmd.append("--dry-run")

    wrapper_result = run(wrapper_cmd, cwd=repo_root, capture_output=True)
    try:
        summary = json.loads(wrapper_result.stdout.strip())
    except json.JSONDecodeError:
        if wrapper_result.stdout:
            sys.stdout.write(wrapper_result.stdout)
        if wrapper_result.stderr:
            sys.stderr.write(wrapper_result.stderr)
        sys.stderr.write("[ticket writer] Env upsert did not return valid summary JSON.\n")
        return 1
    env_path = str(summary["output_path"])
    changed = bool(summary["changed"])

    commit_happened = False
    commit_sha = ""
    commit_message = ""
    internal_event_summary: dict[str, object] | None = None
    github_replay_summary: dict[str, object] | None = None
    if changed and not args.dry_run:
        run(["git", "add", "--", env_path], cwd=repo_root, capture_output=True)
        staged_diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", env_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if staged_diff.returncode == 1:
            commit_message = f"fix(gitops): update {ticket_id.lower()} env from head"
            run(["git", "commit", "-m", commit_message], cwd=repo_root, capture_output=True)
            commit_happened = True
            commit_sha = git_output(repo_root, "rev-parse", "HEAD")
            internal_event = build_amof_commit_event(
                repo=repo_root.name,
                branch=ticket_branch,
                sha=commit_sha,
                actor="amof",
                changed_files=[env_path],
                commit_message=commit_message,
            )
            internal_decision = decide_amof_commit_build_write(
                internal_event,
                proof_mode=False,
                state=RUNTIME_DECISION_STATE,
            )
            internal_event_summary = {
                "repo": internal_decision.repo,
                "branch": internal_decision.branch,
                "commit_sha": internal_decision.source_sha,
                "decision": internal_decision.action,
                "reason": internal_decision.reason,
                "dedupe_key": internal_decision.dedupe_key,
                "amof_origin": internal_decision.amof_created,
                "event_source": internal_decision.event_source,
            }
            github_replay_payload = build_github_replay_payload(
                repo=repo_root.name,
                branch=ticket_branch,
                sha=commit_sha,
                actor="github",
                message=commit_message,
                changed_files=[env_path],
            )
            github_replay_decision = decide_github_push_payload(
                github_replay_payload,
                proof_mode=False,
                state=RUNTIME_DECISION_STATE,
            )
            github_replay_summary = {
                "repo": github_replay_decision.repo,
                "branch": github_replay_decision.branch,
                "commit_sha": github_replay_decision.source_sha,
                "decision": github_replay_decision.action,
                "reason": github_replay_decision.reason,
                "dedupe_key": github_replay_decision.dedupe_key,
                "already_processed": github_replay_decision.already_processed,
                "amof_origin_replay": github_replay_decision.amof_origin_replay,
                "event_source": github_replay_decision.event_source,
            }
        elif staged_diff.returncode != 0:
            sys.stderr.write("[ticket writer] Failed to determine staged diff state.\n")
            return 1

    push_happened = False
    if args.push and not args.dry_run:
        if current_branch == "HEAD":
            sys.stderr.write("[ticket writer] Cannot push detached HEAD.\n")
            return 1
        token = command_env.get("GITHUB_TOKEN") or command_env.get("GIT_TOKEN")
        if not token:
            sys.stderr.write("[ticket writer] GITHUB_TOKEN or GIT_TOKEN is required for push.\n")
            return 1
        origin_url = git_output(repo_root, "config", "--get", "remote.origin.url")
        push_url = build_push_url(origin_url, token)
        run(["git", "push", push_url, current_branch], cwd=repo_root, capture_output=True)
        push_happened = True

    result = {
        "file": env_path,
        "ticket_id": ticket_id,
        "branch": ticket_branch,
        "source_commit_sha": head_sha,
        "namespace": str(summary["namespace"]),
        "hostname": str(summary["hostname"]),
        "file_changed": changed,
        "commit_happened": commit_happened,
        "gitops_commit_sha": commit_sha,
        "push_happened": push_happened,
        "dry_run": args.dry_run,
        "internal_commit_event": internal_event_summary,
        "github_replay_check": github_replay_summary,
    }
    if args.summary_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("[ticket writer] Summary")
        print(f"  Dry run: {'yes' if args.dry_run else 'no'}")
        print(f"  File: {env_path}")
        print(f"  Ticket: {ticket_id}")
        print(f"  Branch: {ticket_branch}")
        print(f"  Source code SHA: {head_sha}")
        print(f"  Namespace: {summary['namespace']}")
        print(f"  Hostname: {summary['hostname']}")
        print(f"  File changed: {'yes' if changed else 'no'}")
        print(f"  GitOps commit happened: {'yes' if commit_happened else 'no'}")
        if commit_happened:
            print(f"  GitOps env commit SHA: {commit_sha}")
        if internal_event_summary:
            print("  Internal commit decision:")
            print(f"    Repo: {internal_event_summary['repo']}")
            print(f"    Branch: {internal_event_summary['branch']}")
            print(f"    Commit SHA: {internal_event_summary['commit_sha']}")
            print(f"    Decision: {internal_event_summary['decision']}")
            print(f"    Dedupe key: {internal_event_summary['dedupe_key']}")
            print(f"    AMOF origin: {'yes' if internal_event_summary['amof_origin'] else 'no'}")
        if github_replay_summary:
            print("  Simulated GitHub replay:")
            print(f"    Decision: {github_replay_summary['decision']}")
            print(f"    Dedupe key: {github_replay_summary['dedupe_key']}")
            print(f"    Already processed: {'yes' if github_replay_summary['already_processed'] else 'no'}")
            print(f"    AMOF-origin replay: {'yes' if github_replay_summary['amof_origin_replay'] else 'no'}")
        print(f"  Push happened: {'yes' if push_happened else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
