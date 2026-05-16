"""External GitHub push adapter for the shared build-write decision core.

GitHub is a fallback/external reconciliation source here, not the primary
trigger for AMOF-created commits.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from amof.intake.build_write import (
    BuildWriteDecision,
    BuildWriteDecisionState,
    BuildWriteEvent,
    RUNTIME_DECISION_STATE,
    SOURCE_GITHUB_PUSH,
    decide_build_write,
    infer_ticket_id,
    is_env_only_commit,
    qualifies_ticket_branch,
)

__all__ = [
    "BuildWriteDecision",
    "BuildWriteDecisionState",
    "BuildWriteEvent",
    "decide_github_push_payload",
    "decide_ticket_build_write",
    "infer_ticket_id",
    "is_env_only_commit",
    "load_payload",
    "parse_github_push_event",
    "qualifies_ticket_branch",
]

def _extract_branch(ref: str) -> str:
    prefix = "refs/heads/"
    if ref.startswith(prefix):
        return ref[len(prefix):]
    return ref


def _collect_changed_files(payload: dict[str, Any]) -> list[str]:
    files: set[str] = set()
    for commit in payload.get("commits") or []:
        for key in ("added", "modified", "removed"):
            for path in commit.get(key) or []:
                if path:
                    files.add(path)

    head_commit = payload.get("head_commit") or {}
    for key in ("added", "modified", "removed"):
        for path in head_commit.get(key) or []:
            if path:
                files.add(path)

    return sorted(files)


def parse_github_push_event(payload: dict[str, Any]) -> BuildWriteEvent:
    repo_info = payload.get("repository") or {}
    sender = payload.get("sender") or {}
    pusher = payload.get("pusher") or {}
    head_commit = payload.get("head_commit") or {}

    repo = str(repo_info.get("full_name") or repo_info.get("name") or "").strip()
    branch = _extract_branch(str(payload.get("ref") or "").strip())
    sha = str(payload.get("after") or "").strip()
    actor = str(sender.get("login") or pusher.get("name") or pusher.get("email") or "").strip()

    return BuildWriteEvent(
        repo=repo,
        branch=branch,
        sha=sha,
        actor=actor,
        changed_files=_collect_changed_files(payload),
        commit_message=str(head_commit.get("message") or "").strip(),
        event_source=SOURCE_GITHUB_PUSH,
        amof_created=False,
        deleted=bool(payload.get("deleted", False)),
        created=bool(payload.get("created", False)),
        forced=bool(payload.get("forced", False)),
    )


def decide_ticket_build_write(
    event: BuildWriteEvent,
    *,
    proof_mode: bool = False,
    supported_repos: tuple[str, ...] = ("amof",),
    state: BuildWriteDecisionState | None = None,
) -> BuildWriteDecision:
    return decide_build_write(
        event,
        proof_mode=proof_mode,
        supported_repos=supported_repos,
        state=state,
    )


def decide_github_push_payload(
    payload: dict[str, Any],
    *,
    proof_mode: bool = False,
    supported_repos: tuple[str, ...] = ("amof",),
    state: BuildWriteDecisionState | None = None,
) -> BuildWriteDecision:
    event = parse_github_push_event(payload)
    return decide_ticket_build_write(
        event,
        proof_mode=proof_mode,
        supported_repos=supported_repos,
        state=state if state is not None else RUNTIME_DECISION_STATE,
    )


def load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
