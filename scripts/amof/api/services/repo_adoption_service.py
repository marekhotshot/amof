"""Bounded repo-adoption analysis service for the control API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from amof.api.command_builder import get_workspace_root
from amof.app_paths import runs_dir
from amof.manifest import ManifestError, find_repo, load_manifest
from amof.orchestrator.events import EventLog
from amof.orchestrator.llm.base import ProviderError
from amof.orchestrator.llm.remote_ial import DEFAULT_REMOTE_IAL_TIMEOUT_SECONDS, RemoteIALClient
from amof.orchestrator.planning_context import (
    PlanningContextError,
    PlanningContextResult,
    build_canonical_planning_context,
    write_planning_context_receipt,
)
from amof.utils import get_git_toplevel


TICKET_ID = "AMOF-REPO-ADOPTION-OPERATOR-ANALYSIS-CONTRACT-001"
RUNS_SUBDIR = "repo-adoption-analyses"
ALLOWED_STATUSES = {"inferred", "validated", "blocked", "unknown"}
SHELL_PREFIXES = (
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


class RepoAdoptionError(RuntimeError):
    """Raised when repo-adoption analysis cannot be completed truthfully."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class _BaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GovernedRepositoryRef(_BaseModel):
    kind: Literal["ecosystem_repo"]
    ecosystem: str
    repo_name: str

    @field_validator("ecosystem", "repo_name")
    @classmethod
    def _require_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class RepoAdoptionRequest(_BaseModel):
    repository: GovernedRepositoryRef
    max_recommended_tickets: int = Field(default=5, ge=0, le=5)


class EvidenceRef(_BaseModel):
    kind: Literal["planning_context", "analysis_run", "remote_request"]
    id: str

    @field_validator("id")
    @classmethod
    def _require_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("id is required")
        return normalized


class RepositorySummary(_BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _require_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("repository summary text is required")
        return normalized


class RuntimeFact(_BaseModel):
    fact: str
    value: Any
    status: Literal["inferred", "validated", "blocked", "unknown"]
    source: Literal["repo_adoption_inference", "planning_context", "canonical_checkout"]
    evidence_ref: EvidenceRef | None = None

    @field_validator("fact")
    @classmethod
    def _require_fact(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("fact is required")
        return normalized


class Blocker(_BaseModel):
    message: str
    status: Literal["blocked", "unknown"]
    source: Literal["repo_adoption_inference", "planning_context", "canonical_checkout"]
    evidence_ref: EvidenceRef | None = None

    @field_validator("message")
    @classmethod
    def _require_message(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("blocker message is required")
        return normalized


class RecommendedTicket(_BaseModel):
    title: str
    severity: Literal["low", "medium", "high"]
    lane: Literal["replay-now", "replay-later", "defer", "kill"]
    expected_impact: str

    @field_validator("title", "expected_impact")
    @classmethod
    def _require_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class RepoAdoptionPayload(_BaseModel):
    overall_status: Literal["inferred", "validated", "blocked", "unknown"]
    repository_summary: RepositorySummary
    runtime_facts: list[RuntimeFact]
    blockers: list[Blocker]
    recommended_tickets: list[RecommendedTicket]
    recommended_next_action: str

    @field_validator("recommended_next_action")
    @classmethod
    def _require_next_action(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("recommended_next_action is required")
        return normalized


class RepositoryIdentity(_BaseModel):
    kind: Literal["ecosystem_repo"]
    ecosystem: str
    repo_name: str
    canonical_remote_url: str | None = None


class AnalysisReferences(_BaseModel):
    run_id: str
    request_id: str | None = None
    planning_context: EvidenceRef


class RepoAdoptionResponse(_BaseModel):
    analysis_id: str
    repository: RepositoryIdentity
    overall_status: Literal["inferred", "validated", "blocked", "unknown"]
    repository_summary: RepositorySummary
    runtime_facts: list[RuntimeFact]
    blockers: list[Blocker]
    recommended_tickets: list[RecommendedTicket]
    recommended_next_action: str
    references: AnalysisReferences


@dataclass(frozen=True)
class _ResolvedRepository:
    identity: RepositoryIdentity
    manifest: dict[str, Any]
    repo_entry: dict[str, Any]
    repo_path: Path
    workspace_root: Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _analysis_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"repo-adoption-{stamp}"


def _analysis_runs_dir() -> Path:
    return runs_dir() / RUNS_SUBDIR


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _normalize_repo_path(workspace_root: Path, raw_path: str) -> Path:
    candidate = Path(str(raw_path or "").strip())
    if not str(candidate):
        raise RepoAdoptionError(
            "repo_adoption_repo_path_missing",
            "Manifest repository path is missing.",
            status_code=409,
        )
    resolved = candidate if candidate.is_absolute() else (workspace_root / candidate)
    return resolved.resolve(strict=False)


def _safe_remote_url(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if normalized.startswith("https://") or normalized.startswith("http://"):
        return normalized
    return None


def _resolve_governed_repository(repository: GovernedRepositoryRef) -> _ResolvedRepository:
    workspace_root = get_workspace_root().resolve(strict=False)
    try:
        manifest = load_manifest(repository.ecosystem)
    except (FileNotFoundError, ManifestError, ValueError) as exc:
        raise RepoAdoptionError(
            "repo_adoption_ecosystem_not_found",
            f"Ecosystem manifest was not found for {repository.ecosystem}.",
            status_code=404,
        ) from exc
    repo_entry = find_repo(manifest, repository.repo_name)
    if repo_entry is None:
        raise RepoAdoptionError(
            "repo_adoption_repo_not_found",
            f"Repository {repository.repo_name} is not governed under ecosystem {repository.ecosystem}.",
            status_code=404,
        )
    repo_path = _normalize_repo_path(workspace_root, str(repo_entry.get("path") or ""))
    if not _is_relative_to(repo_path, workspace_root):
        raise RepoAdoptionError(
            "repo_adoption_repo_path_unsafe",
            "Governed repository path must remain inside the configured workspace root.",
            status_code=400,
        )
    if not repo_path.exists() or not repo_path.is_dir():
        raise RepoAdoptionError(
            "repo_adoption_repo_checkout_missing",
            f"Governed repository checkout is missing for {repository.repo_name}.",
            status_code=404,
        )
    git_root = get_git_toplevel(repo_path)
    if git_root is None:
        raise RepoAdoptionError(
            "repo_adoption_repo_not_git",
            f"Governed repository checkout is not a git repository: {repository.repo_name}.",
            status_code=409,
        )
    resolved_git_root = Path(git_root).resolve(strict=False)
    if resolved_git_root != repo_path and not _is_relative_to(repo_path, resolved_git_root):
        raise RepoAdoptionError(
            "repo_adoption_repo_checkout_invalid",
            "Governed repository path must resolve within one git checkout.",
            status_code=409,
        )
    return _ResolvedRepository(
        identity=RepositoryIdentity(
            kind="ecosystem_repo",
            ecosystem=repository.ecosystem,
            repo_name=repository.repo_name,
            canonical_remote_url=_safe_remote_url(str(repo_entry.get("url") or "").strip() or None),
        ),
        manifest=manifest,
        repo_entry=repo_entry,
        repo_path=repo_path,
        workspace_root=workspace_root,
    )


def _resolve_timeout_seconds() -> float:
    raw = str(os.environ.get("AMOF_REMOTE_IAL_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_REMOTE_IAL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_REMOTE_IAL_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_REMOTE_IAL_TIMEOUT_SECONDS


def _build_remote_ial_client() -> RemoteIALClient:
    base_url = str(os.environ.get("AMOF_REMOTE_IAL_BASE_URL") or "").strip()
    api_key = str(os.environ.get("AMOF_REMOTE_IAL_API_KEY") or "").strip()
    model = str(os.environ.get("AMOF_REMOTE_IAL_MODEL") or "").strip() or None
    if not base_url or not api_key:
        raise RepoAdoptionError(
            "repo_adoption_remote_ial_unconfigured",
            "Set AMOF_REMOTE_IAL_BASE_URL and AMOF_REMOTE_IAL_API_KEY for repo-adoption analysis.",
            status_code=503,
        )
    try:
        return RemoteIALClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=_resolve_timeout_seconds(),
        )
    except ValueError as exc:
        raise RepoAdoptionError(
            "repo_adoption_remote_ial_invalid",
            str(exc),
            status_code=503,
        ) from exc


def _analysis_system_prompt(max_recommended_tickets: int) -> str:
    return (
        "You are the AMOF repo-adoption analysis formatter.\n\n"
        "Use only the governed planning context and indexed code context provided by the caller.\n"
        "Do not claim command execution, runtime validation, deployment, mutation, or filesystem access beyond the given context.\n"
        "Do not include shell commands, raw prompts, raw completions, credentials, provider IDs, receipt bodies, or host filesystem paths.\n"
        "If the context is insufficient for a fact, mark it unknown.\n"
        "If a blocker is only inferred from the indexed code context, keep its status blocked and source repo_adoption_inference.\n"
        "recommended_tickets must contain at most "
        f"{max_recommended_tickets} items.\n"
        "recommended_next_action must be exactly one short operator-facing sentence.\n"
        "Use only these status values: inferred, validated, blocked, unknown.\n"
        "Use only these runtime fact sources: repo_adoption_inference, planning_context, canonical_checkout.\n"
        "Use only these blocker sources: repo_adoption_inference, planning_context, canonical_checkout.\n"
    )


def _analysis_user_message(
    *,
    repository: RepositoryIdentity,
    planning_context: PlanningContextResult,
) -> str:
    receipt = planning_context.receipt.to_dict()
    sections = [
        "## Objective",
        "Produce a bounded repository adoption analysis.",
        "",
        "## Governed Repository",
        json.dumps(repository.model_dump(mode="json"), indent=2),
        "",
        "## Hard Rules",
        "- no execution",
        "- no runner dispatch",
        "- no mutation",
        "- no deployment",
        "- no UI advice",
        "- do not emit host filesystem paths",
        "",
        "## Planning Context Receipt",
        json.dumps(receipt, indent=2),
        "",
        "## Indexed Context",
        planning_context.context_prompt,
    ]
    return "\n".join(sections).strip() + "\n"


def _default_evidence_ref(
    *,
    source: str,
    planning_ref: EvidenceRef,
    request_ref: EvidenceRef | None,
    run_ref: EvidenceRef,
) -> EvidenceRef:
    if source == "canonical_checkout":
        return planning_ref
    if source == "planning_context":
        return planning_ref
    return request_ref or run_ref


def _contains_shell_like_text(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return False
    return any(lowered.startswith(prefix) for prefix in SHELL_PREFIXES)


def _iter_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(_iter_strings(item))
        return values
    if isinstance(value, dict):
        values = []
        for item in value.values():
            values.extend(_iter_strings(item))
        return values
    return []


def _validate_safe_response_strings(payload: RepoAdoptionPayload, *, forbidden_fragments: list[str]) -> None:
    for text in _iter_strings(payload.model_dump(mode="json")):
        normalized = text.strip()
        if not normalized:
            continue
        if _contains_shell_like_text(normalized):
            raise RepoAdoptionError(
                "repo_adoption_shell_text_forbidden",
                "Repo-adoption analysis must not contain shell commands.",
                status_code=502,
            )
        for fragment in forbidden_fragments:
            if fragment and fragment in normalized:
                raise RepoAdoptionError(
                    "repo_adoption_host_path_leak",
                    "Repo-adoption analysis must not leak host filesystem paths.",
                    status_code=502,
                )


def _strict_payload(
    raw_payload: RepoAdoptionPayload,
    *,
    request: RepoAdoptionRequest,
    planning_ref: EvidenceRef,
    request_ref: EvidenceRef | None,
    run_ref: EvidenceRef,
    forbidden_fragments: list[str],
) -> RepoAdoptionPayload:
    if len(raw_payload.recommended_tickets) > request.max_recommended_tickets:
        raise RepoAdoptionError(
            "repo_adoption_ticket_limit_exceeded",
            f"recommended_tickets must not exceed {request.max_recommended_tickets}.",
            status_code=502,
        )
    runtime_facts = [
        RuntimeFact(
            **{
                **fact.model_dump(mode="json"),
                "evidence_ref": fact.evidence_ref or _default_evidence_ref(
                    source=fact.source,
                    planning_ref=planning_ref,
                    request_ref=request_ref,
                    run_ref=run_ref,
                ),
            }
        )
        for fact in raw_payload.runtime_facts
    ]
    blockers = [
        Blocker(
            **{
                **blocker.model_dump(mode="json"),
                "evidence_ref": blocker.evidence_ref or _default_evidence_ref(
                    source=blocker.source,
                    planning_ref=planning_ref,
                    request_ref=request_ref,
                    run_ref=run_ref,
                ),
            }
        )
        for blocker in raw_payload.blockers
    ]
    payload = RepoAdoptionPayload(
        overall_status=raw_payload.overall_status,
        repository_summary=raw_payload.repository_summary,
        runtime_facts=runtime_facts,
        blockers=blockers,
        recommended_tickets=raw_payload.recommended_tickets[: request.max_recommended_tickets],
        recommended_next_action=raw_payload.recommended_next_action,
    )
    _validate_safe_response_strings(payload, forbidden_fragments=forbidden_fragments)
    return payload


def analyze_repo_adoption(request: RepoAdoptionRequest) -> RepoAdoptionResponse:
    resolved_repo = _resolve_governed_repository(request.repository)
    analysis_id = _analysis_id()
    events = EventLog(
        session_id=analysis_id,
        runs_dir=_analysis_runs_dir(),
        run_id=analysis_id,
        ticket_id=TICKET_ID,
        planning_mode="repo_adoption_control",
        actor="amof.repo_adoption",
    )
    events.log(
        "run_created",
        analysis_id=analysis_id,
        repository=resolved_repo.identity.model_dump(mode="json"),
    )
    events.log(
        "governed_repository_resolved",
        ecosystem=request.repository.ecosystem,
        repo_name=request.repository.repo_name,
    )

    client = _build_remote_ial_client()
    try:
        planning_context = build_canonical_planning_context(
            repo=resolved_repo.repo_path,
            objective="Produce a bounded repository adoption analysis.",
            indexer_llm=client,
            planner_provenance={
                "feature": "repo_adoption_control",
                "ticket_id": TICKET_ID,
                "remote_ial_model": client.model_name(),
            },
            max_files=8,
        )
    except PlanningContextError as exc:
        events.error("planning_context_error", str(exc), fatal=True)
        raise RepoAdoptionError(
            "repo_adoption_planning_context_failed",
            str(exc),
            status_code=503,
        ) from exc

    planning_context_receipt_path = write_planning_context_receipt(
        events.session_dir / "planning-context-receipt.json",
        planning_context.receipt,
    )
    planning_ref = EvidenceRef(kind="planning_context", id=analysis_id)
    run_ref = EvidenceRef(kind="analysis_run", id=analysis_id)
    events.log(
        "planning_context_ready",
        receipt_ref=str(planning_context_receipt_path),
        origin_main_sha=planning_context.receipt.origin_main_sha,
        merkle_root=planning_context.receipt.merkle_root,
    )

    user_message = _analysis_user_message(
        repository=resolved_repo.identity,
        planning_context=planning_context,
    )
    events.user_message("Produce a bounded repository adoption analysis.")

    try:
        structured = client.chat_structured(
            system=_analysis_system_prompt(request.max_recommended_tickets),
            messages=[{"role": "user", "content": user_message}],
            response_model=RepoAdoptionPayload,
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
            input_hash=exc.input_hash,
            output_hash=exc.output_hash,
        )
        status_code = 504 if exc.failure_class == "network" else 502
        raise RepoAdoptionError(
            "repo_adoption_remote_ial_failed",
            str(exc),
            status_code=status_code,
        ) from exc
    except ValidationError as exc:
        events.error("schema_validation_error", str(exc), fatal=True)
        raise RepoAdoptionError(
            "repo_adoption_response_invalid",
            f"Remote repo-adoption response failed schema validation: {exc}",
            status_code=502,
        ) from exc

    usage = structured.usage
    request_id = str(getattr(usage, "request_id", "") or "").strip() or None
    request_ref = EvidenceRef(kind="remote_request", id=request_id) if request_id else None
    events.llm_call(
        model=str(getattr(usage, "model", "") or client.model_name()),
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        cost=(float(getattr(usage, "estimated_cost", 0.0) or 0.0) if getattr(usage, "cost_status", "observed") == "observed" else None),
        latency_ms=int(getattr(usage, "latency_ms", 0) or 0),
        provider="remote-ial",
        upstream_provider=getattr(usage, "upstream_provider", None),
        upstream_model=getattr(usage, "upstream_model", None),
        request_id=request_id,
        policy_decision=getattr(usage, "policy_decision", None),
        input_hash=getattr(usage, "input_hash", None),
        output_hash=getattr(usage, "output_hash", None),
        cost_status=str(getattr(usage, "cost_status", "unknown") or "unknown"),
        provider_generation_ref=getattr(usage, "provider_generation_ref", None),
    )

    forbidden_fragments = [
        str(resolved_repo.workspace_root),
        str(resolved_repo.repo_path),
        str(planning_context.receipt.source_repo_path),
        str(planning_context.receipt.source_git_root),
        str(planning_context.receipt.planning_workspace_root),
        str(planning_context.receipt.planning_repo_path),
        str(events.session_dir),
        str(planning_context_receipt_path),
    ]
    payload = _strict_payload(
        structured.parsed,
        request=request,
        planning_ref=planning_ref,
        request_ref=request_ref,
        run_ref=run_ref,
        forbidden_fragments=forbidden_fragments,
    )
    response = RepoAdoptionResponse(
        analysis_id=analysis_id,
        repository=resolved_repo.identity,
        overall_status=payload.overall_status,
        repository_summary=payload.repository_summary,
        runtime_facts=payload.runtime_facts,
        blockers=payload.blockers,
        recommended_tickets=payload.recommended_tickets,
        recommended_next_action=payload.recommended_next_action,
        references=AnalysisReferences(
            run_id=analysis_id,
            request_id=request_id,
            planning_context=planning_ref,
        ),
    )
    result_path = _write_json(
        events.session_dir / "analysis-result.json",
        response.model_dump(mode="json"),
    )
    events.agent_response(content=response.recommended_next_action)
    events.log(
        "repo_adoption_analysis_written",
        result_ref=str(result_path),
        request_id=request_id,
        overall_status=response.overall_status,
    )
    events.session_end(
        {
            "request_id": request_id,
            "overall_status": response.overall_status,
            "recommended_ticket_count": len(response.recommended_tickets),
        }
    )
    events.log(
        "run_finished",
        status=response.overall_status,
        receipt_ref=analysis_id,
        request_id=request_id,
        cost_status=str(getattr(usage, "cost_status", "unknown") or "unknown"),
        estimated_cost=(
            float(getattr(usage, "estimated_cost", 0.0) or 0.0)
            if getattr(usage, "cost_status", "unknown") == "observed"
            else None
        ),
    )
    return response


def http_analyze_repo_adoption(request: RepoAdoptionRequest) -> RepoAdoptionResponse:
    try:
        return analyze_repo_adoption(request)
    except RepoAdoptionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": exc.message,
            },
        ) from exc
