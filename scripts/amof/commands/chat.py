"""Read-only AMOF chat planning through remote IAL."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from ..app_paths import runs_dir
from ..orchestrator.events import EventLog
from ..orchestrator.llm.base import ProviderError
from ..orchestrator.llm.remote_ial import RemoteIALClient
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

DEFAULT_MAX_FILES = 8
DEFAULT_MAX_CHARS_PER_FILE = 4000
TEXT_FILE_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".sh",
}
DEFAULT_CONTEXT_CANDIDATES = (
    "README.md",
    "README",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Makefile",
    ".amof/agent.yaml",
)
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


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_text_candidate(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name.startswith(".") and path.name not in {".env", ".amof", ".gitignore"}:
        return False
    return path.suffix.lower() in TEXT_FILE_SUFFIXES or path.name in {"README", "Makefile"}


def _normalize_context_files(
    repo_path: Path,
    requested_files: list[str] | None,
    *,
    max_files: int,
) -> list[Path]:
    if requested_files:
        normalized: list[Path] = []
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
            normalized.append(candidate)
        deduped: list[Path] = []
        seen: set[Path] = set()
        for item in normalized:
            if item not in seen:
                deduped.append(item)
                seen.add(item)
        if len(deduped) > max_files:
            raise ChatPlanError(
                f"bounded context exceeded: requested {len(deduped)} files, max is {max_files}"
            )
        return deduped

    defaults: list[Path] = []
    for rel in DEFAULT_CONTEXT_CANDIDATES:
        candidate = (repo_path / rel).resolve(strict=False)
        if candidate.exists() and candidate.is_file():
            defaults.append(candidate)
            if len(defaults) >= max_files:
                return defaults

    for candidate in sorted(repo_path.iterdir(), key=lambda item: item.name.lower()):
        if len(defaults) >= max_files:
            break
        if candidate in defaults:
            continue
        if _is_text_candidate(candidate):
            defaults.append(candidate.resolve(strict=False))

    if not defaults:
        raise ChatPlanError(
            "No bounded context files were selected. Pass --file to identify one or more repo files."
        )
    return defaults


def _read_context_excerpt(path: Path, *, max_chars: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n... [truncated]"


def _build_repo_scope(repo_path: Path, files: list[Path]) -> str:
    listed = ", ".join(str(path.relative_to(repo_path)) for path in files)
    return (
        f"Current filesystem view of {repo_path}. "
        f"Bounded to {len(files)} file(s): {listed}. "
        "No shell execution, repo mutation, or editor integration is authorized."
    )


def _build_user_message(
    *,
    objective: str,
    repo_path: Path,
    ticket_id: str | None,
    repo_scope: str,
    files: list[Path],
    max_chars_per_file: int,
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
        "## Bounded Context",
    ]
    for path in files:
        rel = path.relative_to(repo_path)
        sections.extend(
            [
                f"### {rel}",
                _read_context_excerpt(path, max_chars=max_chars_per_file),
                "",
            ]
        )
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

    selected_files = _normalize_context_files(repo_path, files, max_files=max_files)
    repo_scope = _build_repo_scope(repo_path, selected_files)
    objective_text = _require_text(objective, "objective")
    user_message = _build_user_message(
        objective=objective_text,
        repo_path=repo_path,
        ticket_id=ticket_id,
        repo_scope=repo_scope,
        files=selected_files,
        max_chars_per_file=max_chars_per_file,
    )

    cfg = _load_agent_config(repo_path)
    evidence_policy = _resolve_evidence_policy(cfg)
    client = _build_remote_ial_client(model_override=model)

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
        files_to_inspect=[str(path.relative_to(repo_path)) for path in selected_files],
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
    if action != "plan":
        sys.stderr.write("Usage: amof chat plan <objective> [options]\n")
        return 1

    try:
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
    except ChatPlanError as exc:
        sys.stderr.write(f"[chat] {exc}\n")
        return 1


__all__ = [
    "ChatPlanError",
    "ChatPlanResult",
    "InferenceAttribution",
    "PlanPacket",
    "cmd_chat",
    "plan_read_only_chat",
]
