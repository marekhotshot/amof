"""Read-only AMOF chat planning through remote IAL."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from ..app_paths import evidence_dir, materialized_runs_dir, runs_dir
from ..orchestrator.events import EventLog
from ..orchestrator.llm.base import ProviderError
from ..orchestrator.llm.remote_ial import RemoteIALClient
from ..orchestrator.planning_context import (
    PlanningContextError,
    build_canonical_planning_context,
    write_planning_context_receipt,
)
from ..orchestrator.session import Session
from .agent_cmd import (
    _active_provider_profile,
    _load_agent_config,
    _profile_base_url,
    _profile_credential_env,
    _profile_model,
    _resolve_evidence_policy,
    _resolve_remote_ial_timeout_seconds,
    _sanitize_evidence_value,
    _save_session,
)
from . import director as director_commands

DEFAULT_MAX_FILES = 8
DEFAULT_MAX_CHARS_PER_FILE = 4000
DEFAULT_MAX_SESSION_TURNS = 4
DEFAULT_MAX_CLARIFICATION_QUESTIONS = 3
CHAT_PLAN_SESSION_SUBDIR = "chat-plans"
CHAT_INTAKE_SESSION_SUBDIR = "chat-sessions"
CHAT_APPROVAL_SUBDIR = "chat-approvals"
CHAT_HANDOFF_SUBDIR = "chat-handoffs"
SYSTEM_PROMPT = """You are the AMOF read-only planning chat.

You are producing a proposal only. This output is not executable.

Hard rules:
- Use only the bounded context provided by the caller.
- Do not claim to have executed commands or mutated files.
- Do not propose shell commands from chat.
- Do not add editor integrations, handoff execution, or private gateway policy.
- Return strict JSON only with these keys:
  ticket_id
  proposed_ticket_id
  proposed_steps
  risks
  validation_plan
  execution_prompt_for_director
  execution_allowed

Requirements:
- `ticket_id` may be null only when `proposed_ticket_id` is provided.
- `proposed_steps`, `risks`, and `validation_plan` must be arrays of short strings.
- `execution_prompt_for_director` must state that the packet is proposal-only and requires user approval before execution.
- `execution_allowed` must be false.
"""

SESSION_PROMPT = """You are the AMOF bounded intake session assistant.

You are helping shape a proposal-only PlanPacket.

Hard rules:
- do not execute anything
- do not invoke agents, tools, or shell commands
- do not propose handoff or execution bridges
- ask at most one bounded clarification question per response
- if the context is already sufficient, return ready_to_finalize
- return strict JSON only with these keys:
  state
  assistant_message
  question
  rationale

Requirements:
- `state` must be either `ask_user` or `ready_to_finalize`
- `assistant_message` must be a short operator-facing sentence
- `question` must be null when `state=ready_to_finalize`
- `question` must be one short bounded clarification question when `state=ask_user`
- never include shell commands
- never set execution_allowed or imply execution is authorized
"""


class ChatPlanError(RuntimeError):
    """Raised when a read-only chat plan cannot be produced truthfully."""


@dataclass(frozen=True)
class PlanPacket:
    """Non-executable proposal for AMOF Director."""

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

    def __post_init__(self) -> None:
        if not (self.ticket_id or self.proposed_ticket_id):
            raise ChatPlanError("PlanPacket requires ticket_id or proposed_ticket_id.")
        if not self.objective.strip():
            raise ChatPlanError("PlanPacket objective is required.")
        if not self.repo_scope.strip():
            raise ChatPlanError("PlanPacket repo_scope is required.")
        if not self.files_to_inspect:
            raise ChatPlanError("PlanPacket files_to_inspect must not be empty.")
        if not self.proposed_steps:
            raise ChatPlanError("PlanPacket proposed_steps must not be empty.")
        if not self.validation_plan:
            raise ChatPlanError("PlanPacket validation_plan must not be empty.")
        if self.requires_user_approval is not True:
            raise ChatPlanError("PlanPacket requires_user_approval must be true.")
        if self.execution_allowed is not False:
            raise ChatPlanError("PlanPacket execution_allowed must be false.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InferenceAttribution:
    """Transport and upstream attribution for one remote IAL plan call."""

    transport_provider: str
    resolved_model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    estimated_cost: float
    upstream_provider: str | None = None
    upstream_model: str | None = None
    request_id: str | None = None
    policy_decision: dict[str, Any] | None = None
    input_hash: str | None = None
    output_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChatPlanResult:
    """Structured command result for read-only chat planning."""

    repo_path: str
    session_id: str
    result_kind: str
    non_executable_until_user_approval: bool
    plan_packet: PlanPacket
    inference: InferenceAttribution
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "session_id": self.session_id,
            "result_kind": self.result_kind,
            "non_executable_until_user_approval": self.non_executable_until_user_approval,
            "plan_packet": self.plan_packet.to_dict(),
            "inference": self.inference.to_dict(),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class IntakeSessionState:
    """Persistent bounded intake session state."""

    session_id: str
    repo_path: str
    objective: str
    ticket_id: str | None
    status: str
    created_at: str
    updated_at: str
    max_turns: int
    max_questions: int
    turn_count: int
    questions_asked: int
    pending_question: str | None
    assistant_message: str | None
    files_to_inspect: list[str]
    repo_scope: str
    planning_context_receipt_path: str
    indexed_context_path: str
    session_dir: str
    model: str | None = None
    plan_result_path: str | None = None
    finalized_at: str | None = None
    plan_packet: dict[str, Any] | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IntakeSessionState":
        return cls(
            session_id=str(payload.get("session_id") or ""),
            repo_path=str(payload.get("repo_path") or ""),
            objective=str(payload.get("objective") or ""),
            ticket_id=str(payload.get("ticket_id")).strip() or None if payload.get("ticket_id") is not None else None,
            status=str(payload.get("status") or "active"),
            created_at=str(payload.get("created_at") or _now_iso()),
            updated_at=str(payload.get("updated_at") or _now_iso()),
            max_turns=int(payload.get("max_turns") or DEFAULT_MAX_SESSION_TURNS),
            max_questions=int(payload.get("max_questions") or DEFAULT_MAX_CLARIFICATION_QUESTIONS),
            turn_count=int(payload.get("turn_count") or 0),
            questions_asked=int(payload.get("questions_asked") or 0),
            pending_question=str(payload.get("pending_question")).strip() or None
            if payload.get("pending_question") is not None
            else None,
            assistant_message=str(payload.get("assistant_message")).strip() or None
            if payload.get("assistant_message") is not None
            else None,
            files_to_inspect=[str(item) for item in payload.get("files_to_inspect", []) if str(item).strip()],
            repo_scope=str(payload.get("repo_scope") or ""),
            planning_context_receipt_path=str(payload.get("planning_context_receipt_path") or ""),
            indexed_context_path=str(payload.get("indexed_context_path") or ""),
            session_dir=str(payload.get("session_dir") or ""),
            model=str(payload.get("model")).strip() or None if payload.get("model") is not None else None,
            plan_result_path=str(payload.get("plan_result_path")).strip() or None
            if payload.get("plan_result_path") is not None
            else None,
            finalized_at=str(payload.get("finalized_at")).strip() or None
            if payload.get("finalized_at") is not None
            else None,
            plan_packet=payload.get("plan_packet") if isinstance(payload.get("plan_packet"), dict) else None,
            transcript=list(payload.get("transcript") or []),
        )


@dataclass(frozen=True)
class IntakeSessionResult:
    """Structured command result for bounded intake sessions."""

    result_kind: str
    session_id: str
    status: str
    repo_path: str
    objective: str
    turn_count: int
    max_turns: int
    questions_asked: int
    max_questions: int
    pending_question: str | None
    assistant_message: str | None
    files_to_inspect: list[str]
    non_executable_until_user_approval: bool
    evidence: dict[str, Any]
    plan_packet: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApprovedPlanArtifact:
    """Explicit approval artifact for a finalized proposal-only PlanPacket."""

    approval_id: str
    approval_state: str
    approved_at: str
    approval_artifact_path: str
    source_session: dict[str, Any]
    repo_truth: dict[str, Any]
    context_truth: dict[str, Any]
    plan_packet: dict[str, Any]
    approved_by: str | None = None
    approval_note: str | None = None

    def __post_init__(self) -> None:
        if self.approval_state != "approved":
            raise ChatPlanError("ApprovedPlanArtifact approval_state must be 'approved'.")
        if not self.approval_id.strip():
            raise ChatPlanError("ApprovedPlanArtifact approval_id is required.")
        if not self.approval_artifact_path.strip():
            raise ChatPlanError("ApprovedPlanArtifact approval_artifact_path is required.")
        if not isinstance(self.source_session, dict) or not self.source_session:
            raise ChatPlanError("ApprovedPlanArtifact source_session is required.")
        if not isinstance(self.repo_truth, dict) or not self.repo_truth:
            raise ChatPlanError("ApprovedPlanArtifact repo_truth is required.")
        if not isinstance(self.context_truth, dict) or not self.context_truth:
            raise ChatPlanError("ApprovedPlanArtifact context_truth is required.")
        if not isinstance(self.plan_packet, dict) or not self.plan_packet:
            raise ChatPlanError("ApprovedPlanArtifact plan_packet is required.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ApprovedPlanArtifact":
        return cls(
            approval_id=str(payload.get("approval_id") or ""),
            approval_state=str(payload.get("approval_state") or ""),
            approved_at=str(payload.get("approved_at") or ""),
            approval_artifact_path=str(payload.get("approval_artifact_path") or ""),
            source_session=dict(payload.get("source_session") or {}),
            repo_truth=dict(payload.get("repo_truth") or {}),
            context_truth=dict(payload.get("context_truth") or {}),
            plan_packet=dict(payload.get("plan_packet") or {}),
            approved_by=str(payload.get("approved_by")).strip() or None
            if payload.get("approved_by") is not None
            else None,
            approval_note=str(payload.get("approval_note")).strip() or None
            if payload.get("approval_note") is not None
            else None,
        )


@dataclass(frozen=True)
class ChatApprovalResult:
    """Structured command result for explicit PlanPacket approval."""

    result_kind: str
    approval_id: str
    approval_state: str
    session_id: str
    repo_path: str
    non_executable_until_workspace_handoff: bool
    approval_artifact: ApprovedPlanArtifact
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_kind": self.result_kind,
            "approval_id": self.approval_id,
            "approval_state": self.approval_state,
            "session_id": self.session_id,
            "repo_path": self.repo_path,
            "non_executable_until_workspace_handoff": self.non_executable_until_workspace_handoff,
            "approval_artifact": self.approval_artifact.to_dict(),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ChatHandoffResult:
    """Structured command result for approved chat handoff."""

    result_kind: str
    handoff_id: str
    approval_id: str
    repo_path: str
    intake_path: str
    explicit_workspace_command_required: bool
    materialization_command_hint: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ChatTelemetry:
    usage: InferenceAttribution

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cost": self.usage.estimated_cost,
            "total_calls": 1,
            "provider": self.usage.transport_provider,
            "resolved_model": self.usage.resolved_model,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "latency_ms": self.usage.latency_ms,
            "request_id": self.usage.request_id,
            "upstream_provider": self.usage.upstream_provider,
            "upstream_model": self.usage.upstream_model,
            "input_hash": self.usage.input_hash,
            "output_hash": self.usage.output_hash,
        }


@dataclass
class _SessionTelemetry:
    total_cost: float = 0.0
    total_calls: int = 0
    latest_usage: InferenceAttribution | None = None

    def record(self, usage: InferenceAttribution) -> None:
        self.total_cost += usage.estimated_cost
        self.total_calls += 1
        self.latest_usage = usage

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "total_cost": round(self.total_cost, 6),
            "total_calls": self.total_calls,
        }
        if self.latest_usage is not None:
            payload.update(
                {
                    "provider": self.latest_usage.transport_provider,
                    "resolved_model": self.latest_usage.resolved_model,
                    "prompt_tokens": self.latest_usage.prompt_tokens,
                    "completion_tokens": self.latest_usage.completion_tokens,
                    "latency_ms": self.latest_usage.latency_ms,
                    "request_id": self.latest_usage.request_id,
                    "upstream_provider": self.latest_usage.upstream_provider,
                    "upstream_model": self.latest_usage.upstream_model,
                    "input_hash": self.latest_usage.input_hash,
                    "output_hash": self.latest_usage.output_hash,
                }
            )
        return payload


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ChatPlanError(f"{field_name} is required.")
    return normalized


def _normalize_repo_path(repo: str | Path | None) -> Path:
    target = Path(repo or ".").expanduser().resolve(strict=False)
    if not target.exists():
        raise ChatPlanError(f"repo path does not exist: {target}")
    if not target.is_dir():
        raise ChatPlanError(f"repo path must be a directory: {target}")
    return target


def _planning_provenance(model_override: str | None) -> dict[str, Any]:
    profile = _active_remote_ial_profile()
    return {
        "profile_name": str(profile.get("name") or "").strip() or None,
        "resolved_model": str(model_override or _profile_model(profile) or "").strip() or None,
    }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_focus_files(
    repo_path: Path,
    requested_files: list[str] | None,
    *,
    max_files: int,
) -> list[str]:
    if not requested_files:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in requested_files:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = repo_path / candidate
        candidate = candidate.resolve(strict=False)
        if not _is_relative_to(candidate, repo_path):
            raise ChatPlanError(f"context file must stay under repo path: {raw}")
        if not candidate.exists():
            raise ChatPlanError(f"context file not found: {raw}")
        if not candidate.is_file():
            raise ChatPlanError(f"context path must be a file: {raw}")
        rel = candidate.relative_to(repo_path).as_posix()
        if rel not in seen:
            normalized.append(rel)
            seen.add(rel)
    if len(normalized) > max_files:
        raise ChatPlanError(
            f"bounded context exceeded: requested {len(normalized)} files, max is {max_files}"
        )
    return normalized


def _build_repo_scope(repo_path: Path, planning_context_receipt: dict[str, Any]) -> str:
    listed = ", ".join(planning_context_receipt.get("files_to_inspect") or [])
    return (
        f"Canonical planning context for {repo_path}. "
        f"Planning clone: {planning_context_receipt.get('planning_repo_path')}. "
        f"Merkle root: {planning_context_receipt.get('merkle_root')}. "
        f"Freshness: {planning_context_receipt.get('freshness')}. "
        f"Indexed files to inspect: {listed or '<none>'}. "
        "No shell execution, repo mutation, or editor integration is authorized."
    )


def _build_user_message(
    *,
    objective: str,
    repo_path: Path,
    ticket_id: str | None,
    repo_scope: str,
    planning_context_receipt: dict[str, Any],
    context_prompt: str,
) -> str:
    sections = [
        "## Objective",
        objective,
        "",
        "## Ticket",
        ticket_id or "<none supplied>",
        "",
        "## Repo Scope",
        repo_scope,
        "",
        "## Hard Rules",
        "- proposal only",
        "- no execution",
        "- execution_allowed must be false",
        "- no shell commands from chat",
        "- no repo mutation",
        "- no editor integrations",
        "- requires user approval before Director handoff",
        "",
        "## Planning Context Receipt",
        json.dumps(planning_context_receipt, indent=2),
        "",
        "## Indexed Context",
        context_prompt,
    ]
    return "\n".join(sections).strip() + "\n"


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise ChatPlanError("remote IAL returned an empty planning response.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ChatPlanError("remote IAL did not return valid JSON for the plan proposal.")
        payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ChatPlanError("plan proposal payload must be a JSON object.")
    return payload


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ChatPlanError(f"{key} must be a JSON array of strings.")
    result = [str(item).strip() for item in value if str(item).strip()]
    if not result:
        raise ChatPlanError(f"{key} must not be empty.")
    return result


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _contains_shell_like_text(value: str) -> bool:
    lowered = value.lower()
    suspicious_prefixes = (
        "git ",
        "python ",
        "python3 ",
        "bash ",
        "sh ",
        "npm ",
        "pnpm ",
        "yarn ",
        "pip ",
        "make ",
        "pytest ",
    )
    lines = [line.strip() for line in lowered.splitlines() if line.strip()]
    return any(any(line.startswith(prefix) for prefix in suspicious_prefixes) for line in lines)


def _assert_no_shell_text(values: list[str], *, field_name: str) -> None:
    for value in values:
        if _contains_shell_like_text(value):
            raise ChatPlanError(f"{field_name} must remain proposal-only and must not contain shell commands.")


def _normalize_execution_prompt(value: str) -> str:
    prompt = _require_text(value, "execution_prompt_for_director")
    approval_line = "Proposal only. Do not execute until the user explicitly approves this PlanPacket."
    if _contains_shell_like_text(prompt):
        raise ChatPlanError("execution_prompt_for_director must not contain shell commands.")
    if approval_line.lower() in prompt.lower():
        return prompt
    return f"{approval_line}\n\n{prompt}"


def _assert_execution_allowed_false(payload: dict[str, Any]) -> None:
    value = payload.get("execution_allowed")
    if value is None or value is False:
        return
    raise ChatPlanError("execution_allowed must be false for read-only chat plans.")


def _build_inference_attribution(response: Any) -> InferenceAttribution:
    usage = response.usage
    if usage is None:
        raise ChatPlanError("remote IAL did not return usage metadata for the planning call.")
    return InferenceAttribution(
        transport_provider=str(usage.provider or "remote-ial"),
        resolved_model=str(usage.model or "remote-ial"),
        prompt_tokens=int(usage.prompt_tokens or 0),
        completion_tokens=int(usage.completion_tokens or 0),
        latency_ms=int(usage.latency_ms or 0),
        estimated_cost=float(usage.estimated_cost or 0.0),
        upstream_provider=usage.upstream_provider,
        upstream_model=usage.upstream_model,
        request_id=usage.request_id,
        policy_decision=usage.policy_decision,
        input_hash=usage.input_hash,
        output_hash=usage.output_hash,
    )


def _session_runs_dir() -> Path:
    return runs_dir() / CHAT_INTAKE_SESSION_SUBDIR


def _session_dir(session_id: str) -> Path:
    return _session_runs_dir() / session_id


def _session_state_path(session_id: str) -> Path:
    return _session_dir(session_id) / "intake-session.json"


def _session_indexed_context_path(session_id: str) -> Path:
    return _session_dir(session_id) / "indexed-context.md"


def _session_plan_result_path(session_id: str) -> Path:
    return _session_dir(session_id) / "plan-result.json"


def _artifact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _approval_runs_dir() -> Path:
    return evidence_dir() / CHAT_APPROVAL_SUBDIR


def _approval_dir(approval_id: str) -> Path:
    return _approval_runs_dir() / approval_id


def _approval_artifact_path(approval_id: str) -> Path:
    return _approval_dir(approval_id) / "approved-plan.json"


def _handoff_runs_dir() -> Path:
    return evidence_dir() / CHAT_HANDOFF_SUBDIR


def _handoff_dir(handoff_id: str) -> Path:
    return _handoff_runs_dir() / handoff_id


def _handoff_intake_path(handoff_id: str) -> Path:
    return _handoff_dir(handoff_id) / "director-intake.json"


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_session_state(session_id: str) -> IntakeSessionState:
    path = _session_state_path(session_id)
    if not path.exists():
        raise ChatPlanError(f"chat session not found: {session_id}")
    payload = _load_json(path)
    return IntakeSessionState.from_dict(payload)


def _save_session_state(state: IntakeSessionState) -> Path:
    return _write_json(_session_state_path(state.session_id), state.to_dict())


def _plan_packet_payload_from_state(state: IntakeSessionState) -> dict[str, Any]:
    if state.status != "finalized":
        raise ChatPlanError(
            f"chat session must be finalized before approval or handoff: {state.session_id}"
        )
    if not isinstance(state.plan_packet, dict) or not state.plan_packet:
        raise ChatPlanError(f"finalized chat session is missing plan_packet: {state.session_id}")
    if state.plan_packet.get("requires_user_approval") is not True:
        raise ChatPlanError("finalized plan_packet must require explicit user approval.")
    if state.plan_packet.get("execution_allowed") is not False:
        raise ChatPlanError("finalized plan_packet must remain proposal-only.")
    return dict(state.plan_packet)


def _approval_ref_to_path(approval_ref: str) -> Path:
    normalized = _require_text(approval_ref, "approval_id_or_path")
    candidate = Path(normalized).expanduser()
    if candidate.is_absolute() or candidate.suffix or "/" in normalized:
        return candidate.resolve(strict=False)
    return _approval_artifact_path(normalized)


def _load_approved_plan_artifact(approval_ref: str) -> ApprovedPlanArtifact:
    artifact_path = _approval_ref_to_path(approval_ref)
    if not artifact_path.exists():
        raise ChatPlanError(f"approval artifact not found: {artifact_path}")
    payload = _load_json(artifact_path)
    artifact = ApprovedPlanArtifact.from_dict(payload)
    if artifact.approval_state != "approved":
        raise ChatPlanError("approval artifact must be in approved state before handoff.")
    return artifact


def _transcript_lines(transcript: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for entry in transcript:
        role = str(entry.get("role") or "assistant")
        kind = str(entry.get("kind") or "message")
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"- {role}/{kind}: {content}")
    return lines


def _build_session_user_message(
    *,
    state: IntakeSessionState,
    planning_context_receipt: dict[str, Any],
    context_prompt: str,
) -> str:
    transcript_lines = _transcript_lines(state.transcript)
    sections = [
        "## Objective",
        state.objective,
        "",
        "## Ticket",
        state.ticket_id or "<none supplied>",
        "",
        "## Session Bounds",
        f"- turns used: {state.turn_count}/{state.max_turns}",
        f"- clarification questions used: {state.questions_asked}/{state.max_questions}",
        "",
        "## Repo Scope",
        state.repo_scope,
        "",
        "## Planning Context Receipt",
        json.dumps(planning_context_receipt, indent=2),
        "",
        "## Indexed Context",
        context_prompt,
        "",
        "## Transcript So Far",
    ]
    if transcript_lines:
        sections.extend(transcript_lines)
    else:
        sections.append("- <empty>")
    return "\n".join(sections).strip() + "\n"


def _call_remote_ial_json(
    *,
    client: RemoteIALClient,
    system_prompt: str,
    user_message: str,
    events: EventLog | None = None,
    telemetry: _SessionTelemetry | None = None,
) -> tuple[dict[str, Any], InferenceAttribution]:
    try:
        response = client.chat(
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=4096,
            temperature=0.0,
        )
    except ProviderError as exc:
        if events is not None:
            events.error(
                "provider_error",
                str(exc),
                fatal=True,
                provider=exc.provider,
                status_code=exc.status_code,
                failure_class=exc.failure_class,
                request_id=exc.request_id,
                upstream_provider=exc.upstream_provider,
                upstream_model=exc.upstream_model,
                input_hash=exc.input_hash,
                output_hash=exc.output_hash,
            )
        raise ChatPlanError(str(exc)) from exc
    inference = _build_inference_attribution(response)
    if telemetry is not None:
        telemetry.record(inference)
    if events is not None:
        events.llm_call(
            model=inference.resolved_model,
            prompt_tokens=inference.prompt_tokens,
            completion_tokens=inference.completion_tokens,
            cost=inference.estimated_cost,
            latency_ms=inference.latency_ms,
            provider=inference.transport_provider,
            upstream_provider=inference.upstream_provider,
            upstream_model=inference.upstream_model,
            request_id=inference.request_id,
            policy_decision=inference.policy_decision,
            input_hash=inference.input_hash,
            output_hash=inference.output_hash,
        )
    return _extract_json_object(response.text), inference


def _clarification_response(
    *,
    client: RemoteIALClient,
    state: IntakeSessionState,
    planning_context_receipt: dict[str, Any],
    context_prompt: str,
    events: EventLog,
    telemetry: _SessionTelemetry,
) -> tuple[str, str | None]:
    payload, _ = _call_remote_ial_json(
        client=client,
        system_prompt=SESSION_PROMPT,
        user_message=_build_session_user_message(
            state=state,
            planning_context_receipt=planning_context_receipt,
            context_prompt=context_prompt,
        ),
        events=events,
        telemetry=telemetry,
    )
    next_state = str(payload.get("state") or "").strip()
    assistant_message = _require_text(payload.get("assistant_message"), "assistant_message")
    question = _optional_string(payload, "question")
    if next_state not in {"ask_user", "ready_to_finalize"}:
        raise ChatPlanError("bounded intake session response must set state to ask_user or ready_to_finalize.")
    if next_state == "ask_user":
        if question is None:
            raise ChatPlanError("bounded intake session response must include one question when state=ask_user.")
        if _contains_shell_like_text(question):
            raise ChatPlanError("clarification question must not contain shell commands.")
    if question is not None and _contains_shell_like_text(question):
        raise ChatPlanError("clarification question must not contain shell commands.")
    return assistant_message, question if next_state == "ask_user" else None


def _finalize_user_message(
    *,
    state: IntakeSessionState,
    planning_context_receipt: dict[str, Any],
    context_prompt: str,
) -> str:
    transcript_lines = _transcript_lines(state.transcript)
    sections = [
        "## Objective",
        state.objective,
        "",
        "## Ticket",
        state.ticket_id or "<none supplied>",
        "",
        "## Repo Scope",
        state.repo_scope,
        "",
        "## Hard Rules",
        "- proposal only",
        "- no execution",
        "- execution_allowed must be false",
        "- no shell commands from chat",
        "- no repo mutation",
        "- no editor integrations",
        "- requires user approval before Director handoff",
        "",
        "## Planning Context Receipt",
        json.dumps(planning_context_receipt, indent=2),
        "",
        "## Indexed Context",
        context_prompt,
        "",
        "## Session Transcript",
    ]
    sections.extend(transcript_lines or ["- <empty>"])
    return "\n".join(sections).strip() + "\n"


def _plan_packet_from_payload(
    *,
    payload: dict[str, Any],
    objective_text: str,
    ticket_id: str | None,
    repo_scope: str,
    files_to_inspect: list[str],
) -> PlanPacket:
    _assert_execution_allowed_false(payload)
    normalized_ticket_id = ticket_id or _optional_string(payload, "ticket_id")
    proposed_ticket_id = None if normalized_ticket_id else _optional_string(payload, "proposed_ticket_id")
    proposed_steps = _string_list(payload, "proposed_steps")
    risks = _string_list(payload, "risks")
    validation_plan = _string_list(payload, "validation_plan")
    _assert_no_shell_text(proposed_steps, field_name="proposed_steps")
    _assert_no_shell_text(validation_plan, field_name="validation_plan")
    return PlanPacket(
        ticket_id=normalized_ticket_id,
        proposed_ticket_id=proposed_ticket_id,
        objective=objective_text,
        repo_scope=repo_scope,
        files_to_inspect=files_to_inspect,
        proposed_steps=proposed_steps,
        risks=risks,
        validation_plan=validation_plan,
        execution_prompt_for_director=_normalize_execution_prompt(
            _require_text(payload.get("execution_prompt_for_director"), "execution_prompt_for_director")
        ),
        requires_user_approval=True,
        execution_allowed=False,
    )


def _session_result(state: IntakeSessionState) -> IntakeSessionResult:
    evidence = {
        "session_dir": state.session_dir,
        "session_state_path": str(_session_state_path(state.session_id)),
        "planning_context_receipt_path": state.planning_context_receipt_path,
        "indexed_context_path": state.indexed_context_path,
        "events_path": str(_session_dir(state.session_id) / "events.jsonl"),
        "messages_path": str(_session_dir(state.session_id) / "messages.jsonl"),
        "plan_result_path": state.plan_result_path,
    }
    return IntakeSessionResult(
        result_kind="intake_session",
        session_id=state.session_id,
        status=state.status,
        repo_path=state.repo_path,
        objective=state.objective,
        turn_count=state.turn_count,
        max_turns=state.max_turns,
        questions_asked=state.questions_asked,
        max_questions=state.max_questions,
        pending_question=state.pending_question,
        assistant_message=state.assistant_message,
        files_to_inspect=list(state.files_to_inspect),
        non_executable_until_user_approval=True,
        evidence=evidence,
        plan_packet=state.plan_packet,
    )


def _active_remote_ial_profile() -> dict[str, Any]:
    profile = _active_provider_profile()
    if profile is None:
        raise ChatPlanError(
            "amof chat plan requires one active remote-ial provider profile. "
            "Run: amof setup provider remote-ial --name <profile> --activate"
        )
    provider = str(profile.get("provider") or "").strip()
    if provider != "remote-ial":
        raise ChatPlanError(
            f"amof chat plan requires an active remote-ial profile, found provider {provider or '<missing>'}."
        )
    return profile


def _build_remote_ial_client(*, model_override: str | None) -> RemoteIALClient:
    profile = _active_remote_ial_profile()
    base_url = _require_text(_profile_base_url(profile), "remote-ial base_url")
    model = model_override or _profile_model(profile)
    api_key_env = _profile_credential_env(profile, "api_key_env")
    api_key = str(os.environ.get(api_key_env or "", "")).strip() or None
    timeout_seconds, timeout_error = _resolve_remote_ial_timeout_seconds(profile)
    if timeout_error:
        raise ChatPlanError(timeout_error)
    return RemoteIALClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=timeout_seconds,
    )


def _journal_text_value(value: str, *, journal_mode: str) -> str:
    if journal_mode == "redacted":
        sanitized = _sanitize_evidence_value(value, mode="redacted_local")
        return str(sanitized)
    return value


def _write_chat_journal(
    *,
    session_dir: Path,
    objective: str,
    packet: PlanPacket,
    inference: InferenceAttribution,
    journal_mode: str,
) -> Path | None:
    if journal_mode == "disabled":
        return None
    journal_path = session_dir / "chat-plan-journal.md"
    lines = [
        f"# {_journal_text_value(objective, journal_mode=journal_mode)}",
        "",
        f"**Ticket**: {_journal_text_value(packet.ticket_id or packet.proposed_ticket_id or '', journal_mode=journal_mode)}",
        f"**Requires approval**: true",
        f"**Execution allowed**: false",
        f"**Transport provider**: {inference.transport_provider}",
        f"**Upstream provider**: {inference.upstream_provider or 'unknown'}",
        "",
        "## Proposed Steps",
        "",
    ]
    for step in packet.proposed_steps:
        lines.append(f"- {_journal_text_value(step, journal_mode=journal_mode)}")
    lines.extend(["", "## Validation Plan", ""])
    for step in packet.validation_plan:
        lines.append(f"- {_journal_text_value(step, journal_mode=journal_mode)}")
    lines.extend(
        [
            "",
            "## Director Prompt",
            "",
            _journal_text_value(packet.execution_prompt_for_director, journal_mode=journal_mode),
            "",
        ]
    )
    journal_path.write_text("\n".join(lines), encoding="utf-8")
    return journal_path


def _write_intake_session_artifacts(
    *,
    state: IntakeSessionState,
    session: Session,
    telemetry: _SessionTelemetry,
    events: EventLog,
    cfg: dict[str, Any],
) -> None:
    _save_session(
        session,
        telemetry=telemetry,
        events=events,
        workspace_root=Path(state.repo_path),
        session_subdir=CHAT_INTAKE_SESSION_SUBDIR,
        cfg=cfg,
    )
    _save_session_state(state)


def start_bounded_chat_session(
    *,
    objective: str,
    repo: str | Path | None = None,
    ticket_id: str | None = None,
    files: list[str] | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_turns: int = DEFAULT_MAX_SESSION_TURNS,
    max_questions: int = DEFAULT_MAX_CLARIFICATION_QUESTIONS,
    model: str | None = None,
) -> IntakeSessionResult:
    if max_turns <= 0:
        raise ChatPlanError("max_turns must be greater than zero.")
    if max_questions <= 0:
        raise ChatPlanError("max_questions must be greater than zero.")
    repo_path = _normalize_repo_path(repo)
    objective_text = _require_text(objective, "objective")
    cfg = _load_agent_config(repo_path)
    client = _build_remote_ial_client(model_override=model)
    focus_files = _normalize_focus_files(repo_path, files, max_files=max_files)
    try:
        planning_context = build_canonical_planning_context(
            repo=repo_path,
            objective=objective_text,
            indexer_llm=client,
            planner_provenance=_planning_provenance(model),
            max_files=max_files,
        )
    except PlanningContextError as exc:
        raise ChatPlanError(str(exc)) from exc
    planning_receipt_payload = planning_context.receipt.to_dict()
    if focus_files:
        planning_receipt_payload["files_to_inspect"] = focus_files
    session = Session(mode="chat-intake")
    session.goal = objective_text
    session.ecosystem = repo_path.name
    session.add_user_message(objective_text)
    events = EventLog(session_id=session.id, runs_dir=_session_runs_dir())
    events.session_start(mode="chat-intake", goal=objective_text, ecosystem=repo_path.name)
    events.user_message(objective_text)
    telemetry = _SessionTelemetry()
    repo_scope = _build_repo_scope(repo_path, planning_receipt_payload)
    indexed_context_path = _session_indexed_context_path(session.id)
    indexed_context_path.parent.mkdir(parents=True, exist_ok=True)
    indexed_context_path.write_text(planning_context.context_prompt, encoding="utf-8")
    planning_context_receipt_path = write_planning_context_receipt(
        _session_dir(session.id) / "planning-context-receipt.json",
        planning_context.receipt,
    )
    state = IntakeSessionState(
        session_id=session.id,
        repo_path=str(repo_path),
        objective=objective_text,
        ticket_id=ticket_id,
        status="active",
        created_at=_now_iso(),
        updated_at=_now_iso(),
        max_turns=max_turns,
        max_questions=max_questions,
        turn_count=0,
        questions_asked=0,
        pending_question=None,
        assistant_message=None,
        files_to_inspect=list(planning_receipt_payload.get("files_to_inspect") or []),
        repo_scope=repo_scope,
        planning_context_receipt_path=str(planning_context_receipt_path),
        indexed_context_path=str(indexed_context_path),
        session_dir=str(_session_dir(session.id)),
        model=model,
        transcript=[],
    )
    assistant_message, question = _clarification_response(
        client=client,
        state=state,
        planning_context_receipt=planning_receipt_payload,
        context_prompt=planning_context.context_prompt,
        events=events,
        telemetry=telemetry,
    )
    next_status = "active" if question is not None else "ready_to_finalize"
    updated_transcript = list(state.transcript)
    updated_transcript.append(
        {
            "role": "assistant",
            "kind": "question" if question is not None else "status",
            "content": question or assistant_message,
            "recorded_at": _now_iso(),
        }
    )
    session.add_assistant_message(question or assistant_message)
    events.agent_response(content=question or assistant_message)
    state = IntakeSessionState(
        **{
            **state.to_dict(),
            "status": next_status,
            "updated_at": _now_iso(),
            "questions_asked": 1 if question is not None else 0,
            "pending_question": question,
            "assistant_message": assistant_message,
            "transcript": updated_transcript,
        }
    )
    _write_intake_session_artifacts(state=state, session=session, telemetry=telemetry, events=events, cfg=cfg)
    return _session_result(state)


def ask_bounded_chat_session(
    *,
    session_id: str,
    message: str,
) -> IntakeSessionResult:
    state = _load_session_state(session_id)
    if state.status == "finalized":
        raise ChatPlanError(f"chat session already finalized: {session_id}")
    repo_path = Path(state.repo_path)
    cfg = _load_agent_config(repo_path)
    client = _build_remote_ial_client(model_override=state.model)
    session = Session(session_id=state.session_id, mode="chat-intake")
    session.goal = state.objective
    session.ecosystem = repo_path.name
    for entry in state.transcript:
        content = str(entry.get("content") or "")
        if str(entry.get("role") or "") == "user":
            session.add_user_message(content)
        else:
            session.add_assistant_message(content)
    events = EventLog(session_id=session_id, runs_dir=_session_runs_dir())
    telemetry = _SessionTelemetry()
    user_message = _require_text(message, "message")
    transcript = list(state.transcript)
    transcript.append({"role": "user", "kind": "answer", "content": user_message, "recorded_at": _now_iso()})
    session.add_user_message(user_message)
    events.user_message(user_message)
    turn_count = state.turn_count + 1
    pending_question = None
    assistant_message = "Ready to finalize. Clarification budget reached."
    next_question = None
    next_status = "ready_to_finalize"
    questions_asked = state.questions_asked
    if turn_count < state.max_turns and state.questions_asked < state.max_questions:
        planning_receipt_payload = _load_json(Path(state.planning_context_receipt_path))
        context_prompt = Path(state.indexed_context_path).read_text(encoding="utf-8")
        working_state = IntakeSessionState(
            **{
                **state.to_dict(),
                "turn_count": turn_count,
                "pending_question": None,
                "transcript": transcript,
            }
        )
        assistant_message, next_question = _clarification_response(
            client=client,
            state=working_state,
            planning_context_receipt=planning_receipt_payload,
            context_prompt=context_prompt,
            events=events,
            telemetry=telemetry,
        )
        next_status = "active" if next_question is not None else "ready_to_finalize"
        if next_question is not None:
            questions_asked += 1
    transcript.append(
        {
            "role": "assistant",
            "kind": "question" if next_question is not None else "status",
            "content": next_question or assistant_message,
            "recorded_at": _now_iso(),
        }
    )
    session.add_assistant_message(next_question or assistant_message)
    events.agent_response(content=next_question or assistant_message)
    updated_state = IntakeSessionState(
        **{
            **state.to_dict(),
            "status": next_status,
            "updated_at": _now_iso(),
            "turn_count": turn_count,
            "questions_asked": questions_asked,
            "pending_question": next_question,
            "assistant_message": assistant_message,
            "transcript": transcript,
        }
    )
    _write_intake_session_artifacts(
        state=updated_state,
        session=session,
        telemetry=telemetry,
        events=events,
        cfg=cfg,
    )
    return _session_result(updated_state)


def status_bounded_chat_session(*, session_id: str) -> IntakeSessionResult:
    return _session_result(_load_session_state(session_id))


def finalize_bounded_chat_session(
    *,
    session_id: str,
) -> IntakeSessionResult:
    state = _load_session_state(session_id)
    if state.status == "finalized":
        return _session_result(state)
    repo_path = Path(state.repo_path)
    cfg = _load_agent_config(repo_path)
    client = _build_remote_ial_client(model_override=state.model)
    planning_receipt_payload = _load_json(Path(state.planning_context_receipt_path))
    context_prompt = Path(state.indexed_context_path).read_text(encoding="utf-8")
    session = Session(session_id=state.session_id, mode="chat-intake")
    session.goal = state.objective
    session.ecosystem = repo_path.name
    for entry in state.transcript:
        content = str(entry.get("content") or "")
        if str(entry.get("role") or "") == "user":
            session.add_user_message(content)
        else:
            session.add_assistant_message(content)
    events = EventLog(session_id=session_id, runs_dir=_session_runs_dir())
    telemetry = _SessionTelemetry()
    payload, inference = _call_remote_ial_json(
        client=client,
        system_prompt=SYSTEM_PROMPT,
        user_message=_finalize_user_message(
            state=state,
            planning_context_receipt=planning_receipt_payload,
            context_prompt=context_prompt,
        ),
        events=events,
        telemetry=telemetry,
    )
    packet = _plan_packet_from_payload(
        payload=payload,
        objective_text=state.objective,
        ticket_id=state.ticket_id,
        repo_scope=state.repo_scope,
        files_to_inspect=list(state.files_to_inspect),
    )
    response_json = json.dumps(packet.to_dict(), indent=2)
    session.add_assistant_message(response_json)
    events.agent_response(content=packet.execution_prompt_for_director)
    plan_result_path = _session_plan_result_path(session_id)
    result = IntakeSessionResult(
        result_kind="chat_finalize_proposal",
        session_id=state.session_id,
        status="finalized",
        repo_path=state.repo_path,
        objective=state.objective,
        turn_count=state.turn_count,
        max_turns=state.max_turns,
        questions_asked=state.questions_asked,
        max_questions=state.max_questions,
        pending_question=None,
        assistant_message="PlanPacket finalized.",
        files_to_inspect=list(state.files_to_inspect),
        non_executable_until_user_approval=True,
        evidence={
            "session_dir": state.session_dir,
            "session_state_path": str(_session_state_path(session_id)),
            "planning_context_receipt_path": state.planning_context_receipt_path,
            "indexed_context_path": state.indexed_context_path,
            "events_path": str(_session_dir(session_id) / "events.jsonl"),
            "messages_path": str(_session_dir(session_id) / "messages.jsonl"),
            "plan_result_path": str(plan_result_path),
            "transport_provider": inference.transport_provider,
            "upstream_provider": inference.upstream_provider,
            "upstream_model": inference.upstream_model,
        },
        plan_packet=packet.to_dict(),
    )
    stored_payload = _sanitize_evidence_value(result.to_dict(), mode=_resolve_evidence_policy(cfg)["messages"])
    plan_result_path.write_text(json.dumps(stored_payload, indent=2) + "\n", encoding="utf-8")
    updated_state = IntakeSessionState(
        **{
            **state.to_dict(),
            "status": "finalized",
            "updated_at": _now_iso(),
            "pending_question": None,
            "assistant_message": "PlanPacket finalized.",
            "plan_result_path": str(plan_result_path),
            "finalized_at": _now_iso(),
            "plan_packet": packet.to_dict(),
            "transcript": list(state.transcript)
            + [{"role": "assistant", "kind": "final_plan", "content": response_json, "recorded_at": _now_iso()}],
        }
    )
    _write_intake_session_artifacts(
        state=updated_state,
        session=session,
        telemetry=telemetry,
        events=events,
        cfg=cfg,
    )
    return _session_result(updated_state)


def approve_finalized_chat_session(
    *,
    session_id: str,
    approved_by: str | None = None,
    approval_note: str | None = None,
) -> ChatApprovalResult:
    state = _load_session_state(session_id)
    plan_packet = _plan_packet_payload_from_state(state)
    planning_receipt_payload = _load_json(Path(state.planning_context_receipt_path))
    approval_id = f"{state.session_id}-approved-{_artifact_timestamp()}"
    artifact_path = _approval_artifact_path(approval_id)
    objective = _require_text(plan_packet.get("objective"), "plan_packet.objective")
    events = EventLog(session_id=approval_id, runs_dir=_approval_runs_dir())
    events.session_start(mode="chat-approve", goal=objective, ecosystem=Path(state.repo_path).name)
    events.log(
        "chat_plan_approved",
        source_session_id=state.session_id,
        approval_state="approved",
        plan_result_path=state.plan_result_path,
    )

    artifact = ApprovedPlanArtifact(
        approval_id=approval_id,
        approval_state="approved",
        approved_at=_now_iso(),
        approval_artifact_path=str(artifact_path),
        source_session={
            "session_id": state.session_id,
            "session_dir": state.session_dir,
            "repo_path": state.repo_path,
            "objective": state.objective,
            "ticket_id": state.ticket_id,
            "status": state.status,
            "finalized_at": state.finalized_at,
            "plan_result_path": state.plan_result_path,
            "planning_context_receipt_path": state.planning_context_receipt_path,
            "indexed_context_path": state.indexed_context_path,
        },
        repo_truth={
            "source_repo_path": planning_receipt_payload.get("source_repo_path"),
            "source_git_root": planning_receipt_payload.get("source_git_root"),
            "source_remote_url": planning_receipt_payload.get("source_remote_url"),
            "canonical_remote_url": planning_receipt_payload.get("canonical_remote_url"),
            "planning_workspace_root": planning_receipt_payload.get("planning_workspace_root"),
            "planning_repo_path": planning_receipt_payload.get("planning_repo_path"),
            "planning_branch_ref": planning_receipt_payload.get("planning_branch_ref"),
            "origin_main_sha": planning_receipt_payload.get("origin_main_sha"),
        },
        context_truth={
            "planning_context_receipt_path": state.planning_context_receipt_path,
            "indexed_context_path": state.indexed_context_path,
            "merkle_root": planning_receipt_payload.get("merkle_root"),
            "indexed_at": planning_receipt_payload.get("indexed_at"),
            "freshness": planning_receipt_payload.get("freshness"),
            "refresh_reason": planning_receipt_payload.get("refresh_reason"),
            "index_refreshed": planning_receipt_payload.get("index_refreshed"),
            "repo_scope": planning_receipt_payload.get("repo_scope"),
            "files_to_inspect": planning_receipt_payload.get("files_to_inspect"),
            "planner_provenance": planning_receipt_payload.get("planner_provenance"),
        },
        plan_packet=plan_packet,
        approved_by=str(approved_by).strip() or None if approved_by is not None else None,
        approval_note=str(approval_note).strip() or None if approval_note is not None else None,
    )
    _write_json(artifact_path, artifact.to_dict())
    events.session_end({"total_cost": 0.0, "total_calls": 0})
    return ChatApprovalResult(
        result_kind="chat_approved_plan",
        approval_id=artifact.approval_id,
        approval_state=artifact.approval_state,
        session_id=state.session_id,
        repo_path=state.repo_path,
        non_executable_until_workspace_handoff=True,
        approval_artifact=artifact,
        evidence={
            "approval_artifact_path": str(artifact_path),
            "events_path": str(events.log_path),
            "plan_result_path": state.plan_result_path,
            "planning_context_receipt_path": state.planning_context_receipt_path,
        },
    )


def handoff_approved_chat_plan(
    *,
    approval_id_or_path: str,
    run_id: str | None = None,
    target_base_dir: str | Path | None = None,
) -> ChatHandoffResult:
    artifact = _load_approved_plan_artifact(approval_id_or_path)
    repo_path = _require_text(
        artifact.source_session.get("repo_path"),
        "approval.source_session.repo_path",
    )
    objective = _require_text(
        artifact.plan_packet.get("objective"),
        "approval.plan_packet.objective",
    )
    handoff_id = f"{artifact.approval_id}-handoff-{_artifact_timestamp()}"
    intake_path = _handoff_intake_path(handoff_id)
    resolved_run_id = str(run_id).strip() if run_id is not None else handoff_id
    if not resolved_run_id:
        resolved_run_id = handoff_id
    resolved_target_base_dir = (
        Path(target_base_dir).expanduser().resolve(strict=False)
        if target_base_dir is not None
        else materialized_runs_dir()
    )
    events = EventLog(session_id=handoff_id, runs_dir=_handoff_runs_dir())
    events.session_start(mode="chat-handoff", goal=objective, ecosystem=Path(repo_path).name)
    envelope = director_commands.build_approved_plan_handoff_envelope(
        approval=artifact.to_dict(),
        run_id=resolved_run_id,
        target_base_dir=str(resolved_target_base_dir),
    )
    _write_json(intake_path, envelope)
    events.log(
        "chat_handoff_envelope_written",
        approval_id=artifact.approval_id,
        intake_path=str(intake_path),
        run_id=resolved_run_id,
        target_base_dir=str(resolved_target_base_dir),
    )
    events.session_end({"total_cost": 0.0, "total_calls": 0})
    materialization_hint = (
        f"amof workspace materialize-from-intake --intake {intake_path}"
    )
    return ChatHandoffResult(
        result_kind="chat_approved_handoff",
        handoff_id=handoff_id,
        approval_id=artifact.approval_id,
        repo_path=repo_path,
        intake_path=str(intake_path),
        explicit_workspace_command_required=True,
        materialization_command_hint=materialization_hint,
        evidence={
            "approval_artifact_path": artifact.approval_artifact_path,
            "intake_path": str(intake_path),
            "events_path": str(events.log_path),
            "target_base_dir": str(resolved_target_base_dir),
            "run_id": resolved_run_id,
        },
    )


def plan_read_only_chat(
    *,
    objective: str,
    repo: str | Path | None = None,
    ticket_id: str | None = None,
    files: list[str] | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
    model: str | None = None,
) -> ChatPlanResult:
    repo_path = _normalize_repo_path(repo)
    if max_files <= 0:
        raise ChatPlanError("max_files must be greater than zero.")
    if max_chars_per_file <= 0:
        raise ChatPlanError("max_chars_per_file must be greater than zero.")
    objective_text = _require_text(objective, "objective")

    cfg = _load_agent_config(repo_path)
    evidence_policy = _resolve_evidence_policy(cfg)
    client = _build_remote_ial_client(model_override=model)
    focus_files = _normalize_focus_files(repo_path, files, max_files=max_files)
    try:
        planning_context = build_canonical_planning_context(
            repo=repo_path,
            objective=objective_text,
            indexer_llm=client,
            planner_provenance=_planning_provenance(model),
            max_files=max_files,
        )
    except PlanningContextError as exc:
        raise ChatPlanError(str(exc)) from exc
    planning_receipt_payload = planning_context.receipt.to_dict()
    if focus_files:
        planning_receipt_payload["files_to_inspect"] = focus_files
    repo_scope = _build_repo_scope(repo_path, planning_receipt_payload)
    user_message = _build_user_message(
        objective=objective_text,
        repo_path=repo_path,
        ticket_id=ticket_id,
        repo_scope=repo_scope,
        planning_context_receipt=planning_receipt_payload,
        context_prompt=planning_context.context_prompt,
    )

    session = Session(mode="plan")
    session.goal = objective_text
    session.ecosystem = repo_path.name
    session.add_user_message(objective_text)
    events = EventLog(session_id=session.id, runs_dir=runs_dir() / "chat-plans")
    events.session_start(mode="chat-plan", goal=objective_text, ecosystem=repo_path.name)
    events.user_message(objective_text)

    try:
        response = client.chat(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=4096,
            temperature=0.0,
        )
    except ProviderError as exc:
        events.error(
            "provider_error",
            str(exc),
            fatal=True,
            provider=exc.provider,
            status_code=exc.status_code,
            failure_class=exc.failure_class,
            request_id=exc.request_id,
            upstream_provider=exc.upstream_provider,
            upstream_model=exc.upstream_model,
            input_hash=exc.input_hash,
            output_hash=exc.output_hash,
        )
        raise ChatPlanError(str(exc)) from exc

    inference = _build_inference_attribution(response)
    payload = _extract_json_object(response.text)
    _assert_execution_allowed_false(payload)
    normalized_ticket_id = ticket_id or _optional_string(payload, "ticket_id")
    proposed_ticket_id = None if normalized_ticket_id else _optional_string(payload, "proposed_ticket_id")
    proposed_steps = _string_list(payload, "proposed_steps")
    risks = _string_list(payload, "risks")
    validation_plan = _string_list(payload, "validation_plan")
    _assert_no_shell_text(proposed_steps, field_name="proposed_steps")
    _assert_no_shell_text(validation_plan, field_name="validation_plan")
    packet = PlanPacket(
        ticket_id=normalized_ticket_id,
        proposed_ticket_id=proposed_ticket_id,
        objective=objective_text,
        repo_scope=repo_scope,
        files_to_inspect=list(planning_receipt_payload.get("files_to_inspect") or []),
        proposed_steps=proposed_steps,
        risks=risks,
        validation_plan=validation_plan,
        execution_prompt_for_director=_normalize_execution_prompt(
            _require_text(payload.get("execution_prompt_for_director"), "execution_prompt_for_director")
        ),
        requires_user_approval=True,
        execution_allowed=False,
    )

    response_json = json.dumps(packet.to_dict(), indent=2)
    session.add_assistant_message(content=response_json)
    events.llm_call(
        model=inference.resolved_model,
        prompt_tokens=inference.prompt_tokens,
        completion_tokens=inference.completion_tokens,
        cost=inference.estimated_cost,
        latency_ms=inference.latency_ms,
        provider=inference.transport_provider,
        upstream_provider=inference.upstream_provider,
        upstream_model=inference.upstream_model,
        request_id=inference.request_id,
        policy_decision=inference.policy_decision,
        input_hash=inference.input_hash,
        output_hash=inference.output_hash,
    )
    events.agent_response(content=packet.execution_prompt_for_director)

    telemetry = _ChatTelemetry(usage=inference)
    session_dir = _save_session(
        session,
        telemetry=telemetry,
        events=events,
        workspace_root=repo_path,
        session_subdir="chat-plans",
        cfg=cfg,
    )

    evidence_payload = {
        "recorded_at": _now_iso(),
        "messages_mode": evidence_policy["messages"],
        "journal_mode": evidence_policy["journal"],
        "session_dir": str(session_dir),
        "events_path": str(events.log_path),
        "messages_path": str(session_dir / "messages.jsonl"),
    }

    result = ChatPlanResult(
        repo_path=str(repo_path),
        session_id=session.id,
        result_kind="chat_plan_proposal",
        non_executable_until_user_approval=True,
        plan_packet=packet,
        inference=inference,
        evidence=evidence_payload,
    )

    plan_result_path = session_dir / "plan-result.json"
    planning_context_receipt_path = write_planning_context_receipt(
        session_dir / "planning-context-receipt.json",
        planning_context.receipt,
    )
    stored_payload = _sanitize_evidence_value(result.to_dict(), mode=evidence_policy["messages"])
    plan_result_path.write_text(json.dumps(stored_payload, indent=2) + "\n", encoding="utf-8")
    journal_path = _write_chat_journal(
        session_dir=session_dir,
        objective=objective_text,
        packet=packet,
        inference=inference,
        journal_mode=evidence_policy["journal"],
    )

    evidence_with_artifact = dict(evidence_payload)
    evidence_with_artifact["plan_result_path"] = str(plan_result_path)
    evidence_with_artifact["planning_context_receipt_path"] = str(planning_context_receipt_path)
    evidence_with_artifact["journal_path"] = str(journal_path) if journal_path is not None else None
    result = ChatPlanResult(
        repo_path=result.repo_path,
        session_id=result.session_id,
        result_kind=result.result_kind,
        non_executable_until_user_approval=result.non_executable_until_user_approval,
        plan_packet=result.plan_packet,
        inference=result.inference,
        evidence=evidence_with_artifact,
    )

    events.session_end(telemetry.to_dict())
    return result


def _validate_output_path(repo_path: Path, output_path: str | None) -> Path | None:
    if output_path is None:
        return None
    target = Path(output_path).expanduser().resolve(strict=False)
    if _is_relative_to(target, repo_path):
        raise ChatPlanError("output path must stay outside the target repo.")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def cmd_chat(args: argparse.Namespace) -> int:
    action = getattr(args, "chat_cmd", None)
    try:
        if action == "plan":
            repo_path = _normalize_repo_path(getattr(args, "repo", None))
            output_path = _validate_output_path(repo_path, getattr(args, "output", None))
            result = plan_read_only_chat(
                objective=getattr(args, "objective", None),
                repo=repo_path,
                ticket_id=getattr(args, "ticket_id", None),
                files=getattr(args, "files", None),
                max_files=int(getattr(args, "max_files", DEFAULT_MAX_FILES)),
                model=getattr(args, "model", None),
            )
            rendered = json.dumps(result.to_dict(), indent=2)
            if output_path is not None:
                output_path.write_text(rendered + "\n", encoding="utf-8")
            print(rendered)
            return 0
        if action == "start":
            result = start_bounded_chat_session(
                objective=getattr(args, "objective", None),
                repo=getattr(args, "repo", None),
                ticket_id=getattr(args, "ticket_id", None),
                files=getattr(args, "files", None),
                max_files=int(getattr(args, "max_files", DEFAULT_MAX_FILES)),
                max_turns=int(getattr(args, "max_turns", DEFAULT_MAX_SESSION_TURNS)),
                max_questions=int(getattr(args, "max_questions", DEFAULT_MAX_CLARIFICATION_QUESTIONS)),
                model=getattr(args, "model", None),
            )
            print(json.dumps(result.to_dict(), indent=2))
            return 0
        if action == "ask":
            result = ask_bounded_chat_session(
                session_id=str(getattr(args, "session_id", "") or "").strip(),
                message=getattr(args, "message", None),
            )
            print(json.dumps(result.to_dict(), indent=2))
            return 0
        if action == "status":
            result = status_bounded_chat_session(
                session_id=str(getattr(args, "session_id", "") or "").strip(),
            )
            print(json.dumps(result.to_dict(), indent=2))
            return 0
        if action == "finalize":
            result = finalize_bounded_chat_session(
                session_id=str(getattr(args, "session_id", "") or "").strip(),
            )
            print(json.dumps(result.to_dict(), indent=2))
            return 0
        if action == "approve":
            result = approve_finalized_chat_session(
                session_id=str(getattr(args, "session_id", "") or "").strip(),
                approved_by=getattr(args, "approved_by", None),
                approval_note=getattr(args, "approval_note", None),
            )
            print(json.dumps(result.to_dict(), indent=2))
            return 0
        if action == "handoff":
            result = handoff_approved_chat_plan(
                approval_id_or_path=str(getattr(args, "approval_id_or_path", "") or "").strip(),
                run_id=getattr(args, "run_id", None),
                target_base_dir=getattr(args, "target_base_dir", None),
            )
            print(json.dumps(result.to_dict(), indent=2))
            return 0
        sys.stderr.write("Usage: amof chat {plan,start,ask,status,finalize,approve,handoff} ...\n")
        return 1
    except ChatPlanError as exc:
        sys.stderr.write(f"[chat] {exc}\n")
        return 1


__all__ = [
    "ApprovedPlanArtifact",
    "ChatPlanError",
    "ChatApprovalResult",
    "ChatHandoffResult",
    "ChatPlanResult",
    "InferenceAttribution",
    "IntakeSessionResult",
    "IntakeSessionState",
    "PlanPacket",
    "approve_finalized_chat_session",
    "ask_bounded_chat_session",
    "cmd_chat",
    "finalize_bounded_chat_session",
    "handoff_approved_chat_plan",
    "plan_read_only_chat",
    "start_bounded_chat_session",
    "status_bounded_chat_session",
]
