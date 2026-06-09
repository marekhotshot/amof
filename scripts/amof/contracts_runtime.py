"""Canonical planning and execution contracts shared across AMOF surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ContractError(RuntimeError):
    """Raised when a canonical AMOF contract becomes invalid."""


@dataclass(frozen=True)
class PlanBundle:
    """Canonical proposal-only planning contract."""

    objective: str
    repo_scope: str
    files_to_inspect: list[str]
    proposed_steps: list[str]
    risks: list[str]
    validation_plan: list[str]
    execution_prompt_for_director: str
    requires_user_approval: bool = True
    execution_allowed: bool = False
    ticket_id: str | None = None
    proposed_ticket_id: str | None = None
    result_kind: str = "plan_bundle"
    contract_version: str = "plan-bundle-v1"

    def __post_init__(self) -> None:
        if self.result_kind != "plan_bundle":
            raise ContractError("PlanBundle result_kind must be 'plan_bundle'.")
        if not self.contract_version.strip():
            raise ContractError("PlanBundle contract_version is required.")
        if not (self.ticket_id or self.proposed_ticket_id):
            raise ContractError("PlanBundle requires ticket_id or proposed_ticket_id.")
        if not self.objective.strip():
            raise ContractError("PlanBundle objective is required.")
        if not self.repo_scope.strip():
            raise ContractError("PlanBundle repo_scope is required.")
        if not self.files_to_inspect:
            raise ContractError("PlanBundle files_to_inspect must not be empty.")
        if not self.proposed_steps:
            raise ContractError("PlanBundle proposed_steps must not be empty.")
        if not self.validation_plan:
            raise ContractError("PlanBundle validation_plan must not be empty.")
        if self.requires_user_approval is not True:
            raise ContractError("PlanBundle requires_user_approval must be true.")
        if self.execution_allowed is not False:
            raise ContractError("PlanBundle execution_allowed must be false.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_kind": self.result_kind,
            "contract_version": self.contract_version,
            "objective": self.objective,
            "repo_scope": self.repo_scope,
            "files_to_inspect": list(self.files_to_inspect),
            "proposed_steps": list(self.proposed_steps),
            "risks": list(self.risks),
            "validation_plan": list(self.validation_plan),
            "execution_prompt_for_director": self.execution_prompt_for_director,
            "requires_user_approval": self.requires_user_approval,
            "execution_allowed": self.execution_allowed,
            "ticket_id": self.ticket_id,
            "proposed_ticket_id": self.proposed_ticket_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanBundle":
        return cls(
            objective=str(payload.get("objective") or ""),
            repo_scope=str(payload.get("repo_scope") or ""),
            files_to_inspect=[
                str(item) for item in payload.get("files_to_inspect", []) if str(item).strip()
            ],
            proposed_steps=[
                str(item) for item in payload.get("proposed_steps", []) if str(item).strip()
            ],
            risks=[str(item) for item in payload.get("risks", []) if str(item).strip()],
            validation_plan=[
                str(item) for item in payload.get("validation_plan", []) if str(item).strip()
            ],
            execution_prompt_for_director=str(payload.get("execution_prompt_for_director") or ""),
            requires_user_approval=bool(payload.get("requires_user_approval", True)),
            execution_allowed=bool(payload.get("execution_allowed", False)),
            ticket_id=str(payload.get("ticket_id")).strip() or None
            if payload.get("ticket_id") is not None
            else None,
            proposed_ticket_id=str(payload.get("proposed_ticket_id")).strip() or None
            if payload.get("proposed_ticket_id") is not None
            else None,
            result_kind=str(payload.get("result_kind") or "plan_bundle"),
            contract_version=str(payload.get("contract_version") or "plan-bundle-v1"),
        )


@dataclass(frozen=True)
class AgentRunResult:
    """Canonical governed agent-run result contract."""

    status: str
    session_id: str
    exit_code: int
    stop_reason: str
    final_text: str
    plan_path: str | None
    checkpoint_path: str | None
    event_log_path: str | None
    journal_path: str | None
    budget_summary: dict[str, Any]
    studio_session_id: str | None = None
    runner_id: str | None = None
    backend: str | None = None
    runtime_log_path: str | None = None
    changed_paths: list[str] | None = None
    validation_summary: dict[str, Any] | None = None
    approved_capabilities: list[str] | None = None
    effective_capabilities: list[str] | None = None
    evidence_refs: dict[str, Any] | None = None
    schema_version: int = 1
    result_kind: str = "agent_run_result"
    contract_version: str = "agent-run-v1"

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("AgentRunResult schema_version must be 1.")
        if self.result_kind != "agent_run_result":
            raise ContractError("AgentRunResult result_kind must be 'agent_run_result'.")
        if not self.contract_version.strip():
            raise ContractError("AgentRunResult contract_version is required.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_kind": self.result_kind,
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
            "status": self.status,
            "session_id": self.session_id,
            "exit_code": self.exit_code,
            "stop_reason": self.stop_reason,
            "final_text": self.final_text,
            "plan_path": self.plan_path,
            "checkpoint_path": self.checkpoint_path,
            "event_log_path": self.event_log_path,
            "journal_path": self.journal_path,
            "budget_summary": dict(self.budget_summary),
            **(
                {"studio_session_id": self.studio_session_id}
                if self.studio_session_id is not None
                else {}
            ),
            **({"runner_id": self.runner_id} if self.runner_id is not None else {}),
            **({"backend": self.backend} if self.backend is not None else {}),
            **({"runtime_log_path": self.runtime_log_path} if self.runtime_log_path is not None else {}),
            **({"changed_paths": list(self.changed_paths)} if self.changed_paths is not None else {}),
            **({"validation_summary": dict(self.validation_summary)} if self.validation_summary is not None else {}),
            **({"approved_capabilities": list(self.approved_capabilities)} if self.approved_capabilities is not None else {}),
            **({"effective_capabilities": list(self.effective_capabilities)} if self.effective_capabilities is not None else {}),
            **({"evidence_refs": dict(self.evidence_refs)} if self.evidence_refs is not None else {}),
        }
