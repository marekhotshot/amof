"""Canonical planning and execution contracts shared across AMOF surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ContractError(RuntimeError):
    """Raised when a canonical AMOF contract becomes invalid."""


_INTERPRETATION_ROLES = frozenset({"interpreter", "planner", "critic"})


def _normalize_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("PlanBundle confidence must be a number in [0, 1].") from exc
    if confidence < 0.0 or confidence > 1.0:
        raise ContractError("PlanBundle confidence must be a number in [0, 1].")
    return confidence


def _normalize_interpretations(value: Any) -> list[dict[str, str]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ContractError("PlanBundle interpretations must be an array.")
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ContractError("PlanBundle interpretations entries must be objects.")
        text = str(item.get("text") or "").strip()
        if not text:
            raise ContractError("PlanBundle interpretations[].text is required.")
        entry: dict[str, str] = {"text": text}
        source = str(item.get("source") or "").strip()
        if source:
            entry["source"] = source
        role = str(item.get("role") or "").strip()
        if role:
            if role not in _INTERPRETATION_ROLES:
                raise ContractError(
                    "PlanBundle interpretations[].role must be interpreter, planner, or critic."
                )
            entry["role"] = role
        normalized.append(entry)
    return normalized


def _normalize_dissent(value: Any) -> list[dict[str, str]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ContractError("PlanBundle dissent must be an array.")
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ContractError("PlanBundle dissent entries must be objects.")
        text = str(item.get("text") or "").strip()
        if not text:
            raise ContractError("PlanBundle dissent[].text is required.")
        entry: dict[str, str] = {"text": text}
        severity = str(item.get("severity") or "").strip()
        if severity:
            entry["severity"] = severity
        source = str(item.get("source") or "").strip()
        if source:
            entry["source"] = source
        normalized.append(entry)
    return normalized


def _normalize_suggested_next_actions(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ContractError("PlanBundle suggested_next_actions must be an array.")
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ContractError("PlanBundle suggested_next_actions entries must be objects.")
        label = str(item.get("label") or "").strip()
        if not label:
            raise ContractError("PlanBundle suggested_next_actions[].label is required.")
        prefill = item.get("prefill")
        if not isinstance(prefill, dict):
            raise ContractError("PlanBundle suggested_next_actions[].prefill must be an object.")
        intake_text = str(prefill.get("intake_text") or "").strip()
        if not intake_text:
            raise ContractError(
                "PlanBundle suggested_next_actions[].prefill.intake_text is required."
            )
        # Proposals only: reject command-shaped keys if a model invents them.
        forbidden = {"command", "shell", "execute", "argv", "executable"}
        if forbidden.intersection(prefill.keys()) or forbidden.intersection(item.keys()):
            raise ContractError(
                "PlanBundle suggested_next_actions entries must be intake prefills, "
                "never executable commands."
            )
        normalized.append({"label": label, "prefill": {"intake_text": intake_text}})
    return normalized


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
    interpretations: list[dict[str, str]] | None = None
    confidence: float | None = None
    dissent: list[dict[str, str]] | None = None
    suggested_next_actions: list[dict[str, Any]] | None = None

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
        object.__setattr__(self, "confidence", _normalize_confidence(self.confidence))
        object.__setattr__(
            self, "interpretations", _normalize_interpretations(self.interpretations)
        )
        object.__setattr__(self, "dissent", _normalize_dissent(self.dissent))
        object.__setattr__(
            self,
            "suggested_next_actions",
            _normalize_suggested_next_actions(self.suggested_next_actions),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        # Optional cognition fields: absent when unknown (never fabricate).
        if self.interpretations:
            payload["interpretations"] = [dict(item) for item in self.interpretations]
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.dissent:
            payload["dissent"] = [dict(item) for item in self.dissent]
        if self.suggested_next_actions:
            payload["suggested_next_actions"] = [
                {
                    "label": item["label"],
                    "prefill": {"intake_text": item["prefill"]["intake_text"]},
                }
                for item in self.suggested_next_actions
            ]
        return payload

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
            interpretations=payload.get("interpretations"),
            confidence=payload.get("confidence"),
            dissent=payload.get("dissent"),
            suggested_next_actions=payload.get("suggested_next_actions"),
        )


@dataclass(frozen=True)
class AgentRunResult:
    """Canonical governed agent-run result contract."""

    status: str
    session_id: str
    exit_code: int | str
    stop_reason: str
    final_text: str
    plan_path: str | None
    checkpoint_path: str | None
    event_log_path: str | None
    journal_path: str | None
    budget_summary: dict[str, Any]
    task_findings: str | None = None
    studio_session_id: str | None = None
    runner_id: str | None = None
    backend: str | None = None
    requested_provider: str | None = None
    effective_provider: str | None = None
    requested_model: str | None = None
    effective_model: str | None = None
    transport: str | None = None
    fallback_used: bool | None = None
    runtime_log_path: str | None = None
    result_path: str | None = None
    runtime_log_unavailable_reason: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    failure_classification: str | None = None
    failure: dict[str, Any] | None = None
    changed_paths: list[str] | None = None
    write_scope_binding_id: str | None = None
    write_scope_approval_id: str | None = None
    mutation_receipt: dict[str, Any] | None = None
    validation_summary: dict[str, Any] | None = None
    write_scope_proposal: dict[str, Any] | None = None
    write_scope_proposals: list[dict[str, Any]] | None = None
    proposal_missing_reason: str | None = None
    approved_capabilities: list[str] | None = None
    effective_capabilities: list[str] | None = None
    evidence_refs: dict[str, Any] | None = None
    evidence_previews: list[dict[str, Any]] | None = None
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
            **({"task_findings": self.task_findings} if self.task_findings is not None else {}),
            **(
                {"studio_session_id": self.studio_session_id}
                if self.studio_session_id is not None
                else {}
            ),
            **({"runner_id": self.runner_id} if self.runner_id is not None else {}),
            **({"backend": self.backend} if self.backend is not None else {}),
            **(
                {"requested_provider": self.requested_provider}
                if self.requested_provider is not None
                else {}
            ),
            **(
                {"effective_provider": self.effective_provider}
                if self.effective_provider is not None
                else {}
            ),
            **(
                {"requested_model": self.requested_model}
                if self.requested_model is not None
                else {}
            ),
            **(
                {"effective_model": self.effective_model}
                if self.effective_model is not None
                else {}
            ),
            **({"transport": self.transport} if self.transport is not None else {}),
            **(
                {"fallback_used": self.fallback_used}
                if self.fallback_used is not None
                else {}
            ),
            **({"runtime_log_path": self.runtime_log_path} if self.runtime_log_path is not None else {}),
            **({"result_path": self.result_path} if self.result_path is not None else {}),
            **(
                {"runtime_log_unavailable_reason": self.runtime_log_unavailable_reason}
                if self.runtime_log_unavailable_reason is not None
                else {}
            ),
            **({"started_at": self.started_at} if self.started_at is not None else {}),
            **({"completed_at": self.completed_at} if self.completed_at is not None else {}),
            **(
                {"failure_classification": self.failure_classification}
                if self.failure_classification is not None
                else {}
            ),
            **({"failure": dict(self.failure)} if self.failure is not None else {}),
            **({"changed_paths": list(self.changed_paths)} if self.changed_paths is not None else {}),
            **(
                {"write_scope_binding_id": self.write_scope_binding_id}
                if self.write_scope_binding_id is not None
                else {}
            ),
            **(
                {"write_scope_approval_id": self.write_scope_approval_id}
                if self.write_scope_approval_id is not None
                else {}
            ),
            **(
                {"mutation_receipt": dict(self.mutation_receipt)}
                if self.mutation_receipt is not None
                else {}
            ),
            **({"validation_summary": dict(self.validation_summary)} if self.validation_summary is not None else {}),
            **(
                {"write_scope_proposal": dict(self.write_scope_proposal)}
                if self.write_scope_proposal is not None
                else {}
            ),
            **(
                {
                    "write_scope_proposals": [
                        dict(item) for item in self.write_scope_proposals
                    ]
                }
                if self.write_scope_proposals is not None
                else {}
            ),
            # Schema declares proposal_missing_reason as string|null; always
            # serialize so null round-trips with contract examples.
            "proposal_missing_reason": self.proposal_missing_reason,
            **({"approved_capabilities": list(self.approved_capabilities)} if self.approved_capabilities is not None else {}),
            **({"effective_capabilities": list(self.effective_capabilities)} if self.effective_capabilities is not None else {}),
            **({"evidence_refs": dict(self.evidence_refs)} if self.evidence_refs is not None else {}),
            **(
                {"evidence_previews": [dict(item) for item in self.evidence_previews]}
                if self.evidence_previews is not None
                else {}
            ),
        }
