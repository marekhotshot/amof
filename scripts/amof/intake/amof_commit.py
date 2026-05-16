"""Internal AMOF commit-event adapter for the shared build-write decision core."""

from __future__ import annotations

from amof.intake.build_write import (
    BuildWriteDecision,
    BuildWriteDecisionState,
    BuildWriteEvent,
    RUNTIME_DECISION_STATE,
    SOURCE_AMOF_INTERNAL,
    decide_build_write,
)


def build_amof_commit_event(
    *,
    repo: str,
    branch: str,
    sha: str,
    actor: str,
    changed_files: list[str],
    commit_message: str,
    amof_created: bool = True,
) -> BuildWriteEvent:
    return BuildWriteEvent(
        repo=repo,
        branch=branch,
        sha=sha,
        actor=actor,
        changed_files=list(changed_files),
        commit_message=commit_message,
        event_source=SOURCE_AMOF_INTERNAL,
        amof_created=amof_created,
    )


def decide_amof_commit_build_write(
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
        state=state if state is not None else RUNTIME_DECISION_STATE,
    )
