"""Shared ticket build-write intake decision core.

This module separates event-source adapters from the ticket build-write decision
engine so AMOF can stay the primary authority for its own commits.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any

TICKET_ID_RE = re.compile(r"([A-Za-z][A-Za-z0-9]+-\d+[A-Za-z]*)\b")
QUALIFYING_BRANCH_RE = re.compile(r"^(feat|fix|chore|refactor|hotfix|bugfix)/.+", re.IGNORECASE)
ENV_ONLY_COMMIT_MESSAGE_RE = re.compile(r"^fix\(gitops\): update .+ env from head$", re.IGNORECASE)
ENV_PATH_PREFIX = "envs/tickets/"

SOURCE_AMOF_INTERNAL = "amof_internal"
SOURCE_GITHUB_PUSH = "github_push"

PUBLIC_BUILD_WRITE_REMOVED_REASON = (
    "ticket build-write was removed from public AMOF canonical main; "
    "runtime build-write flows moved out of the public repo"
)


@dataclass(frozen=True)
class BuildWriteEvent:
    repo: str
    branch: str
    sha: str
    actor: str
    changed_files: list[str]
    commit_message: str
    event_source: str
    amof_created: bool
    deleted: bool = False
    created: bool = False
    forced: bool = False


@dataclass
class BuildWriteDecisionState:
    processed_shas: set[str] = field(default_factory=set)
    amof_origin_shas: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class BuildWriteDecision:
    action: str
    reason: str
    repo: str
    normalized_repo: str
    branch: str
    ticket_id: str | None
    source_sha: str
    actor: str
    changed_files: list[str]
    env_only_commit: bool
    command: list[str]
    event_source: str
    amof_created: bool
    dedupe_key: str
    already_processed: bool
    amof_origin_replay: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RUNTIME_DECISION_STATE = BuildWriteDecisionState()


def normalize_repo(repo: str) -> str:
    text = str(repo or "").strip()
    if not text:
        return ""
    return text.rsplit("/", 1)[-1]


def infer_ticket_id(branch: str) -> str | None:
    match = TICKET_ID_RE.search(branch)
    if not match:
        return None
    return match.group(1).upper()


def qualifies_ticket_branch(branch: str) -> bool:
    return bool(QUALIFYING_BRANCH_RE.match(branch) and infer_ticket_id(branch))


def is_env_only_commit(event: BuildWriteEvent) -> bool:
    if event.changed_files:
        return all(path.startswith(ENV_PATH_PREFIX) for path in event.changed_files)
    return bool(ENV_ONLY_COMMIT_MESSAGE_RE.match(event.commit_message))


def dedupe_key_for_event(event: BuildWriteEvent) -> str:
    repo = normalize_repo(event.repo)
    sha = str(event.sha or "").strip()
    if not repo or not sha:
        return ""
    return f"{repo}:{sha}"


def build_write_command(event: BuildWriteEvent, *, ticket_id: str | None, proof_mode: bool) -> list[str]:
    return []


def _base_decision(
    event: BuildWriteEvent,
    *,
    ticket_id: str | None,
    env_only_commit: bool,
    dedupe_key: str,
    already_processed: bool,
    amof_origin_replay: bool,
    reason: str = "unsupported event",
) -> BuildWriteDecision:
    return BuildWriteDecision(
        action="ignore",
        reason=reason,
        repo=event.repo,
        normalized_repo=normalize_repo(event.repo),
        branch=event.branch,
        ticket_id=ticket_id,
        source_sha=event.sha,
        actor=event.actor,
        changed_files=event.changed_files,
        env_only_commit=env_only_commit,
        command=[],
        event_source=event.event_source,
        amof_created=event.amof_created,
        dedupe_key=dedupe_key,
        already_processed=already_processed,
        amof_origin_replay=amof_origin_replay,
    )


def _remember_event(event: BuildWriteEvent, state: BuildWriteDecisionState | None) -> None:
    if state is None:
        return
    dedupe_key = dedupe_key_for_event(event)
    if not dedupe_key:
        return
    state.processed_shas.add(dedupe_key)
    if event.amof_created:
        state.amof_origin_shas.add(dedupe_key)


def decide_build_write(
    event: BuildWriteEvent,
    *,
    proof_mode: bool = False,
    supported_repos: tuple[str, ...] = ("amof",),
    state: BuildWriteDecisionState | None = None,
) -> BuildWriteDecision:
    ticket_id = infer_ticket_id(event.branch)
    env_only_commit = is_env_only_commit(event)
    dedupe_key = dedupe_key_for_event(event)
    already_processed = bool(state and dedupe_key and dedupe_key in state.processed_shas)
    known_amof_origin = event.amof_created or bool(state and dedupe_key and dedupe_key in state.amof_origin_shas)
    amof_origin_replay = event.event_source == SOURCE_GITHUB_PUSH and known_amof_origin

    decision = _base_decision(
        event,
        ticket_id=ticket_id,
        env_only_commit=env_only_commit,
        dedupe_key=dedupe_key,
        already_processed=already_processed,
        amof_origin_replay=amof_origin_replay,
    )

    if event.deleted:
        _remember_event(event, state)
        return BuildWriteDecision(**{**decision.to_dict(), "reason": "branch deletion push is ignored"})
    if not event.branch or not event.sha:
        return BuildWriteDecision(**{**decision.to_dict(), "reason": "event is missing branch or sha"})
    if decision.normalized_repo not in supported_repos:
        _remember_event(event, state)
        return BuildWriteDecision(
            **{
                **decision.to_dict(),
                "reason": f"repo {decision.normalized_repo or '<unknown>'} is not enabled for ticket build-write",
            }
        )
    if not qualifies_ticket_branch(event.branch):
        _remember_event(event, state)
        return BuildWriteDecision(
            **{
                **decision.to_dict(),
                "reason": "branch does not match a qualifying ticket branch pattern",
            }
        )
    if amof_origin_replay:
        _remember_event(event, state)
        return BuildWriteDecision(
            **{
                **decision.to_dict(),
                "reason": "github replay of AMOF-origin commit is ignored; AMOF is the first-party source",
            }
        )
    if env_only_commit:
        _remember_event(event, state)
        return BuildWriteDecision(
            **{
                **decision.to_dict(),
                "reason": "env-only commit is ignored to prevent recursive retrigger",
            }
        )
    if already_processed:
        return BuildWriteDecision(
            **{
                **decision.to_dict(),
                "reason": "commit sha was already processed and is ignored as a replay",
            }
        )
    _remember_event(event, state)
    return BuildWriteDecision(
        **{
            **decision.to_dict(),
            "reason": PUBLIC_BUILD_WRITE_REMOVED_REASON,
        }
    )
