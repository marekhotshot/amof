from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..app_paths import ensure_app_roots, get_app_paths
from ..commands import agent_cmd
from ..execution_backends import amof_native, claude_code, cursor_agent, hermes_opensandbox
from ..manifest import list_available_ecosystems, load_manifest
from ..state import get_state
from ..utils import get_ecosystem_from_branch, get_ecosystem_from_path, get_git_toplevel
from ..trust_layer import (
    TrustIntegrityError,
    build_provenance_document,
    evidence_bundle_dir,
    seal_evidence_artifacts,
    verify_evidence_consistency,
    verify_evidence_seal,
    verify_execution_result_integrity,
    write_canonical_evidence_bundle,
)
from ..write_scope_bindings import (
    WriteScopeBindingError,
    finalize_binding,
    prepare_execution_write_scope,
)
from ..write_scope_enforcement import apply_enforcement_to_result

MAX_PAYLOAD_UTF8_BYTES = 40000
HANDOFF_PACKET_SCHEMA_VERSION = 1
HANDOFF_RECEIPT_SCHEMA_VERSION = 1
HANDOFF_TARGET_AMOF_AGENT = "amof-agent"
CANONICAL_MISSION_PACKET_SCHEMA_VERSION = 1
CANONICAL_MISSION_PACKET_CONTRACT_VERSION = "canonical-mission-packet-v1"
CANONICAL_MUTATION_ALLOWED_VALUES = frozenset(
    {"read_only", "bounded_worktree", "runtime_mutation"}
)
PAYLOAD_KIND_MAP = {
    "selected-text": "selected_text",
    "last-response": "last_response",
    "canonical-mission-packet": "canonical_mission_packet",
}
ALLOWED_PAYLOAD_KINDS = frozenset(PAYLOAD_KIND_MAP.values())
HANDOFF_EXECUTION_STATUSES = frozenset(
    {
        "accepted",
        "queued",
        "execution_started",
        "completed",
        "finalized",
        "blocked",
        "failed",
        "timed_out",
        "cancelled",
        "result_missing",
    }
)
METADATA_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,63})$")
HANDOFF_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,127})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CANONICAL_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CANONICAL_REPO_REF_RE = re.compile(
    r"^(?![/.])(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]{1,128}$"
)
CANONICAL_RUNTIME_SCOPE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,119})$")
K8S_NAMESPACE_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,61}[a-z0-9])?$")
CANONICAL_TASK_CLASSES = frozenset(
    {
        "analysis",
        "implementation",
        "validation",
        "operations",
        "research",
        "documentation",
        "migration",
        "incident_response",
        "other",
    }
)
CANONICAL_CLASSIFICATIONS = frozenset({"public", "internal", "restricted"})
SECRET_LIKE_VALUE_RE = re.compile(
    r"(?i)(?:"
    r"authorization\s*:\s*bearer\b|"
    r"bearer\s+[A-Za-z0-9._-]{8,}|"
    r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9._/+:-]{6,}|"
    r"ghp_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9._-]{12,}|"
    r"AKIA[0-9A-Z]{16}"
    r")"
)


@dataclass(frozen=True)
class PreparedPayload:
    text: str
    character_count: int
    utf8_byte_count: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "character_count": self.character_count,
            "utf8_byte_count": self.utf8_byte_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class PreparedHandoffPacket:
    schema_version: int
    handoff_id: str
    source: str
    target: str
    studio_session_id: str | None
    payload_kind: str
    payload: PreparedPayload
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "handoff_id": self.handoff_id,
            "source": self.source,
            "target": self.target,
            **(
                {"studio_session_id": self.studio_session_id}
                if self.studio_session_id is not None
                else {}
            ),
            "payload_kind": self.payload_kind,
            "payload": self.payload.to_dict(),
            "state": self.state,
        }


@dataclass(frozen=True)
class PreparedHandoffReceipt:
    status: str
    handoff_id: str
    packet_path: str
    character_count: int
    utf8_byte_count: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "handoff_id": self.handoff_id,
            "packet_path": self.packet_path,
            "character_count": self.character_count,
            "utf8_byte_count": self.utf8_byte_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class CanonicalMissionPacket:
    schema_version: int
    contract_version: str
    mission_id: str
    ticket_id: str
    task_class: str
    classification: str
    goal: str
    objective: str
    repo_name: str
    repo_owner: str | None
    branch_ref: str
    execution_allowed: bool
    requested_mode: str
    allowed_mutations: tuple[str, ...]
    forbidden_mutations: tuple[str, ...]
    validation_gates: tuple[str, ...]
    studio_session_id: str | None = None
    runtime_environment: str | None = None
    runtime_namespace: str | None = None
    structured_write_scope_proposal_required: bool = False

    def to_payload(self) -> dict[str, Any]:
        mission: dict[str, Any] = {
            "mission_id": self.mission_id,
            "ticket_id": self.ticket_id,
        }
        target_repository: dict[str, Any] = {
            "repo_name": self.repo_name,
            "branch_ref": self.branch_ref,
        }
        if self.repo_owner is not None:
            target_repository["repo_owner"] = self.repo_owner
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "mission": mission,
            "task_class": self.task_class,
            "classification": self.classification,
            "goal": self.goal,
            "objective": self.objective,
            "target_repository": target_repository,
            "execution_allowed": self.execution_allowed,
            "mutations": {
                "requested_mode": self.requested_mode,
                "allowed": list(self.allowed_mutations),
                "forbidden": list(self.forbidden_mutations),
            },
            "validation_gates": list(self.validation_gates),
        }
        if self.structured_write_scope_proposal_required:
            payload["result_requirements"] = {
                "structured_write_scope_proposal": True,
            }
        if self.studio_session_id is not None:
            payload["studio_session_id"] = self.studio_session_id
        if self.runtime_environment is not None or self.runtime_namespace is not None:
            payload["runtime"] = {
                "environment": self.runtime_environment,
                "namespace": self.runtime_namespace,
            }
        return payload

    def safe_preview_lines(self, *, indent: str = "") -> list[str]:
        lines = [
            f"{indent}canonical_mission_packet:",
            f"{indent}  schema_version: {self.schema_version}",
            f"{indent}  contract_version: {self.contract_version}",
            f"{indent}  mission_id: {self.mission_id}",
            f"{indent}  ticket_id: {self.ticket_id}",
            f"{indent}  task_class: {self.task_class}",
            f"{indent}  classification: {self.classification}",
            f"{indent}  target_repository: {self.repo_name}",
            f"{indent}  branch_ref: {self.branch_ref}",
            f"{indent}  execution_allowed: {self.execution_allowed}",
            f"{indent}  requested_mode: {self.requested_mode}",
            f"{indent}  allowed_mutations: {','.join(self.allowed_mutations)}",
            f"{indent}  forbidden_mutation_count: {len(self.forbidden_mutations)}",
            f"{indent}  validation_gate_count: {len(self.validation_gates)}",
        ]
        if self.structured_write_scope_proposal_required:
            lines.append(
                f"{indent}  result_requirement: structured_write_scope_proposal"
            )
        if self.repo_owner is not None:
            lines.insert(8, f"{indent}  target_repository_owner: {self.repo_owner}")
        if self.studio_session_id is not None:
            lines.insert(4, f"{indent}  studio_session_id: {self.studio_session_id}")
        if self.runtime_environment is not None and self.runtime_namespace is not None:
            lines.append(f"{indent}  runtime_environment: {self.runtime_environment}")
            lines.append(f"{indent}  runtime_namespace: {self.runtime_namespace}")
        return lines

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mission_id": self.mission_id,
            "ticket_id": self.ticket_id,
            "task_class": self.task_class,
            "classification": self.classification,
            "target_repository": {
                "repo_name": self.repo_name,
                "branch_ref": self.branch_ref,
            },
            "execution_allowed": self.execution_allowed,
            "requested_mode": self.requested_mode,
            "allowed_mutations": list(self.allowed_mutations),
            "forbidden_mutations": list(self.forbidden_mutations),
            "validation_gates": list(self.validation_gates),
        }
        if self.repo_owner is not None:
            payload["target_repository"]["repo_owner"] = self.repo_owner
        if self.studio_session_id is not None:
            payload["studio_session_id"] = self.studio_session_id
        if self.structured_write_scope_proposal_required:
            payload["result_requirements"] = {
                "structured_write_scope_proposal": True
            }
        if self.runtime_environment is not None and self.runtime_namespace is not None:
            payload["runtime"] = {
                "environment": self.runtime_environment,
                "namespace": self.runtime_namespace,
            }
        return payload

    def derived_goal(self) -> str:
        repo_identity = (
            f"{self.repo_owner}/{self.repo_name}"
            if self.repo_owner is not None
            else self.repo_name
        )
        parts = [
            "Execute the bounded canonical AMOF mission packet.",
            f"Mission: {self.mission_id}.",
            f"Ticket: {self.ticket_id}.",
            f"Task class: {self.task_class}.",
            f"Classification: {self.classification}.",
            f"Goal: {self.goal}.",
            f"Target repository: {repo_identity} @ {self.branch_ref}.",
            f"Objective: {self.objective}.",
            f"Execution allowed: {str(self.execution_allowed).lower()}.",
            f"Requested mutation mode: {self.requested_mode}.",
            f"Allowed mutations: {', '.join(self.allowed_mutations)}.",
            f"Forbidden mutations: {', '.join(self.forbidden_mutations)}.",
            f"Validation gates: {', '.join(self.validation_gates)}.",
        ]
        if self.structured_write_scope_proposal_required:
            parts.append(
                "Result requirements: emit a structured write_scope_proposal when the inspected evidence justifies a bounded follow-up; otherwise omit the proposal object and explain why so AMOF can mark proposal_missing."
            )
        if self.runtime_environment is not None and self.runtime_namespace is not None:
            parts.append(
                f"Runtime scope: environment {self.runtime_environment}, namespace {self.runtime_namespace}."
            )
        return " ".join(parts)


@dataclass(frozen=True)
class HandoffExecutionState:
    schema_version: int
    handoff_id: str
    status: str
    request_id: str
    updated_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    receipt_path: Optional[str] = None
    result_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "handoff_id": self.handoff_id,
            "status": self.status,
            "request_id": self.request_id,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "receipt_path": self.receipt_path,
            "result_path": self.result_path,
        }


@dataclass(frozen=True)
class HandoffExecutionReceipt:
    schema_version: int
    handoff_id: str
    request_id: str
    status: str
    exit_code: int
    stop_reason: str
    session_id: str
    studio_session_id: str | None
    result_path: Optional[str]
    result_sha256: Optional[str]
    evidence: dict[str, Optional[str]]
    receipt_path: Optional[str]
    started_at: str
    completed_at: str
    finalized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "handoff_id": self.handoff_id,
            "request_id": self.request_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "stop_reason": self.stop_reason,
            "session_id": self.session_id,
            **(
                {"studio_session_id": self.studio_session_id}
                if self.studio_session_id is not None
                else {}
            ),
            "result_path": self.result_path,
            "result_sha256": self.result_sha256,
            "evidence": dict(self.evidence),
            "receipt_path": self.receipt_path,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "finalized": bool(self.finalized),
        }


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (_canonical_json(payload) + "\n").encode("utf-8")


def _emit_json_stdout(payload: dict[str, Any]) -> None:
    sys.stdout.write(_canonical_json(payload))
    sys.stdout.write("\n")


def _stderr(message: str) -> None:
    sys.stderr.write(message)
    if not message.endswith("\n"):
        sys.stderr.write("\n")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_metadata_label(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError(f"{field_name} is required.")
    if not METADATA_LABEL_RE.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must match {METADATA_LABEL_RE.pattern!r} and remain metadata only."
        )
    return normalized


def _validate_handoff_id(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("handoff id is required.")
    if not HANDOFF_ID_RE.fullmatch(normalized):
        raise ValueError("handoff id must be a bounded identifier, not a path.")
    return normalized


def _validate_required_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} is required.")
    if len(text) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters.")
    if pattern is not None and not pattern.fullmatch(text):
        raise ValueError(f"{field_name} has an invalid format.")
    return text


def _validate_optional_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
) -> str | None:
    if value is None:
        return None
    return _validate_required_text(
        value, field_name=field_name, max_length=max_length, pattern=pattern
    )


def _require_strict_object(
    value: Any,
    *,
    field_name: str,
    allowed: set[str],
    required: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    extras = sorted(set(value) - allowed)
    if extras:
        raise ValueError(f"{field_name} has unknown fields: {extras}")
    missing = sorted(key for key in required if key not in value)
    if missing:
        raise ValueError(f"{field_name} is missing required fields: {missing}")
    return value


def _validate_string_array(
    value: Any,
    *,
    field_name: str,
    min_items: int,
    max_items: int,
    max_item_length: int,
    pattern: re.Pattern[str] | None = None,
    allowed_values: set[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array.")
    if len(value) < min_items:
        raise ValueError(f"{field_name} must contain at least {min_items} item(s).")
    if len(value) > max_items:
        raise ValueError(f"{field_name} must contain at most {max_items} item(s).")
    normalized: list[str] = []
    for index, item in enumerate(value):
        text = _validate_required_text(
            item,
            field_name=f"{field_name}[{index}]",
            max_length=max_item_length,
            pattern=pattern,
        )
        if allowed_values is not None and text not in allowed_values:
            raise ValueError(f"{field_name}[{index}] is not an allowed value.")
        normalized.append(text)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates.")
    return tuple(normalized)


def _reject_secret_like_values(value: Any, *, field_name: str) -> None:
    if isinstance(value, str):
        if SECRET_LIKE_VALUE_RE.search(value):
            raise ValueError(f"{field_name} contains secret-like material.")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_secret_like_values(item, field_name=f"{field_name}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_like_values(item, field_name=f"{field_name}[{index}]")


def _payload_kind_from_cli(value: str) -> str:
    normalized = str(value or "").strip().lower()
    payload_kind = PAYLOAD_KIND_MAP.get(normalized)
    if not payload_kind:
        raise ValueError("payload kind is not supported.")
    return payload_kind


def _canonical_mission_packet_schema_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "contracts"
        / "canonical-mission-packet.schema.json"
    )


def _validate_schema_if_available(payload: dict[str, Any], schema_path: Path) -> None:
    if importlib.util.find_spec("jsonschema") is None or not schema_path.exists():
        return
    import jsonschema

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(instance=payload, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(exc.message) from exc


def _parse_canonical_mission_packet_object(payload: Any) -> CanonicalMissionPacket:
    root = _require_strict_object(
        payload,
        field_name="canonical mission packet",
        allowed={
            "schema_version",
            "contract_version",
            "mission",
            "task_class",
            "classification",
            "goal",
            "objective",
            "target_repository",
            "execution_allowed",
            "mutations",
            "validation_gates",
            "runtime",
            "studio_session_id",
            "result_requirements",
        },
        required={
            "schema_version",
            "contract_version",
            "mission",
            "task_class",
            "classification",
            "goal",
            "objective",
            "target_repository",
            "execution_allowed",
            "mutations",
            "validation_gates",
        },
    )
    if root.get("schema_version") != CANONICAL_MISSION_PACKET_SCHEMA_VERSION:
        raise ValueError("canonical mission packet schema_version must equal 1.")
    contract_version = str(root.get("contract_version") or "").strip()
    if contract_version != CANONICAL_MISSION_PACKET_CONTRACT_VERSION:
        raise ValueError(
            "canonical mission packet contract_version is not supported."
        )
    mission = _require_strict_object(
        root.get("mission"),
        field_name="canonical mission packet mission",
        allowed={"mission_id", "ticket_id"},
        required={"mission_id", "ticket_id"},
    )
    target_repository = _require_strict_object(
        root.get("target_repository"),
        field_name="canonical mission packet target_repository",
        allowed={"repo_name", "repo_owner", "branch_ref"},
        required={"repo_name", "branch_ref"},
    )
    result_requirements = _require_strict_object(
        root.get("result_requirements"),
        field_name="canonical mission packet result_requirements",
        allowed={"structured_write_scope_proposal"},
        required=set(),
    ) if root.get("result_requirements") is not None else {}
    mutations = _require_strict_object(
        root.get("mutations"),
        field_name="canonical mission packet mutations",
        allowed={"requested_mode", "allowed", "forbidden"},
        required={"requested_mode", "allowed", "forbidden"},
    )
    execution_allowed = root.get("execution_allowed")
    if not isinstance(execution_allowed, bool):
        raise ValueError("canonical mission packet execution_allowed must be boolean.")
    task_class = _validate_required_text(
        root.get("task_class"), field_name="task_class", max_length=64
    )
    if task_class not in CANONICAL_TASK_CLASSES:
        raise ValueError("task_class is not supported for canonical mission packets.")
    classification = _validate_required_text(
        root.get("classification"), field_name="classification", max_length=64
    )
    if classification not in CANONICAL_CLASSIFICATIONS:
        raise ValueError(
            "classification is not supported for canonical mission packets."
        )
    goal = _validate_required_text(root.get("goal"), field_name="goal", max_length=280)
    objective = _validate_required_text(
        root.get("objective"), field_name="objective", max_length=800
    )
    mission_id = _validate_required_text(
        mission.get("mission_id"),
        field_name="mission.mission_id",
        max_length=128,
        pattern=CANONICAL_ID_RE,
    )
    ticket_id = _validate_required_text(
        mission.get("ticket_id"),
        field_name="mission.ticket_id",
        max_length=128,
        pattern=CANONICAL_ID_RE,
    )
    repo_name = _validate_required_text(
        target_repository.get("repo_name"),
        field_name="target_repository.repo_name",
        max_length=128,
        pattern=CANONICAL_REPO_ID_RE,
    )
    repo_owner = _validate_optional_text(
        target_repository.get("repo_owner"),
        field_name="target_repository.repo_owner",
        max_length=128,
        pattern=CANONICAL_REPO_ID_RE,
    )
    branch_ref = _validate_required_text(
        target_repository.get("branch_ref"),
        field_name="target_repository.branch_ref",
        max_length=128,
        pattern=CANONICAL_REPO_REF_RE,
    )
    requested_mode = _validate_required_text(
        mutations.get("requested_mode"),
        field_name="mutations.requested_mode",
        max_length=64,
    )
    if requested_mode not in CANONICAL_MUTATION_ALLOWED_VALUES:
        raise ValueError(
            "mutations.requested_mode is not supported for canonical mission packets."
        )
    allowed_mutations = _validate_string_array(
        mutations.get("allowed"),
        field_name="mutations.allowed",
        min_items=1,
        max_items=4,
        max_item_length=64,
        allowed_values=set(CANONICAL_MUTATION_ALLOWED_VALUES),
    )
    if requested_mode not in allowed_mutations:
        raise ValueError("mutations.requested_mode must be included in mutations.allowed.")
    forbidden_mutations = _validate_string_array(
        mutations.get("forbidden"),
        field_name="mutations.forbidden",
        min_items=1,
        max_items=12,
        max_item_length=80,
        pattern=METADATA_LABEL_RE,
    )
    validation_gates = _validate_string_array(
        root.get("validation_gates"),
        field_name="validation_gates",
        min_items=1,
        max_items=12,
        max_item_length=120,
        pattern=METADATA_LABEL_RE,
    )
    if set(allowed_mutations) & set(forbidden_mutations):
        raise ValueError(
            "mutations.allowed and mutations.forbidden must not overlap."
        )
    studio_session_id = _validate_optional_text(
        root.get("studio_session_id"),
        field_name="studio_session_id",
        max_length=128,
        pattern=CANONICAL_ID_RE,
    )
    runtime_environment: str | None = None
    runtime_namespace: str | None = None
    structured_write_scope_proposal_required = result_requirements.get(
        "structured_write_scope_proposal"
    )
    if not isinstance(structured_write_scope_proposal_required, bool):
        structured_write_scope_proposal_required = False
    runtime = root.get("runtime")
    if runtime is not None:
        runtime_obj = _require_strict_object(
            runtime,
            field_name="canonical mission packet runtime",
            allowed={"environment", "namespace"},
            required={"environment", "namespace"},
        )
        runtime_environment = _validate_required_text(
            runtime_obj.get("environment"),
            field_name="runtime.environment",
            max_length=64,
            pattern=CANONICAL_RUNTIME_SCOPE_RE,
        )
        runtime_namespace = _validate_required_text(
            runtime_obj.get("namespace"),
            field_name="runtime.namespace",
            max_length=63,
            pattern=K8S_NAMESPACE_RE,
        )
    if requested_mode == "runtime_mutation":
        if runtime_environment is None or runtime_namespace is None:
            raise ValueError(
                "runtime.environment and runtime.namespace are required when runtime_mutation is requested."
            )
    elif runtime is not None:
        raise ValueError(
            "runtime scope is only allowed when mutations.requested_mode is runtime_mutation."
        )
    _reject_secret_like_values(root, field_name="canonical mission packet")
    packet = CanonicalMissionPacket(
        schema_version=CANONICAL_MISSION_PACKET_SCHEMA_VERSION,
        contract_version=CANONICAL_MISSION_PACKET_CONTRACT_VERSION,
        mission_id=mission_id,
        ticket_id=ticket_id,
        task_class=task_class,
        classification=classification,
        goal=goal,
        objective=objective,
        repo_name=repo_name,
        repo_owner=repo_owner,
        branch_ref=branch_ref,
        execution_allowed=execution_allowed,
        requested_mode=requested_mode,
        allowed_mutations=allowed_mutations,
        forbidden_mutations=forbidden_mutations,
        validation_gates=validation_gates,
        studio_session_id=studio_session_id,
        runtime_environment=runtime_environment,
        runtime_namespace=runtime_namespace,
        structured_write_scope_proposal_required=structured_write_scope_proposal_required,
    )
    _validate_schema_if_available(
        packet.to_payload(), _canonical_mission_packet_schema_path()
    )
    return packet


def _parse_canonical_mission_packet_text(
    text: str,
    *,
    field_name: str,
    require_canonical_text: bool,
    studio_session_id: str | None = None,
) -> tuple[CanonicalMissionPacket, str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    payload_studio_session_id = payload.get("studio_session_id")
    if payload_studio_session_id is not None and studio_session_id is None:
        raise ValueError(
            f"{field_name} studio_session_id must be supplied by the handoff envelope."
        )
    if payload_studio_session_id is not None and studio_session_id is not None:
        if str(payload_studio_session_id).strip() != studio_session_id:
            raise ValueError(f"{field_name} studio_session_id must match the envelope.")
    if studio_session_id is not None and payload_studio_session_id is None:
        payload = dict(payload)
        payload["studio_session_id"] = studio_session_id
    packet = _parse_canonical_mission_packet_object(payload)
    canonical_text = _canonical_json(packet.to_payload())
    if require_canonical_text and text != canonical_text:
        raise ValueError(f"{field_name} must use canonical JSON formatting.")
    return packet, canonical_text


def _read_single_stdin_text() -> str:
    raw = sys.stdin.buffer.read()
    if not raw:
        raise ValueError("payload stdin is empty.")
    if b"\x00" in raw:
        raise ValueError("payload stdin must not contain NUL bytes.")
    if len(raw) > MAX_PAYLOAD_UTF8_BYTES:
        raise ValueError(
            f"payload exceeds {MAX_PAYLOAD_UTF8_BYTES} UTF-8 bytes; received {len(raw)} bytes."
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("payload stdin must be valid UTF-8.") from exc
    return text


def _read_single_stdin_payload(
    payload_kind: str, *, studio_session_id: str | None = None
) -> PreparedPayload:
    text = _read_single_stdin_text()
    if payload_kind == "canonical_mission_packet":
        _packet, canonical_text = _parse_canonical_mission_packet_text(
            text,
            field_name="canonical mission packet",
            require_canonical_text=False,
            studio_session_id=studio_session_id,
        )
        return _validated_payload_from_text(
            canonical_text, field_name="canonical mission packet"
        )
    return PreparedPayload(
        text=text,
        character_count=len(text),
        utf8_byte_count=len(text.encode("utf-8")),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _validated_payload_from_text(text: Any, *, field_name: str) -> PreparedPayload:
    if not isinstance(text, str) or not text:
        raise ValueError(f"{field_name} must be a non-empty string.")
    if "\x00" in text:
        raise ValueError(f"{field_name} must not contain NUL bytes.")
    raw = text.encode("utf-8")
    if len(raw) > MAX_PAYLOAD_UTF8_BYTES:
        raise ValueError(
            f"{field_name} exceeds {MAX_PAYLOAD_UTF8_BYTES} UTF-8 bytes; received {len(raw)} bytes."
        )
    return PreparedPayload(
        text=text,
        character_count=len(text),
        utf8_byte_count=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _render_preview(
    *,
    source: str,
    target: str,
    studio_session_id: str | None,
    payload_kind: str,
    payload: PreparedPayload,
) -> str:
    lines = [
        "[handoff] Preview",
        f"source: {source}",
        f"target: {target}",
    ]
    if studio_session_id is not None:
        lines.append(f"studio_session_id: {studio_session_id}")
    lines.extend(
        [
            f"payload_kind: {payload_kind}",
            f"character_count: {payload.character_count}",
            f"utf8_byte_count: {payload.utf8_byte_count}",
            f"sha256: {payload.sha256}",
        ]
    )
    if payload_kind == "canonical_mission_packet":
        canonical_packet, _ = _parse_canonical_mission_packet_text(
            payload.text,
            field_name="canonical mission packet",
            require_canonical_text=True,
            studio_session_id=studio_session_id,
        )
        lines.extend(canonical_packet.safe_preview_lines())
    else:
        lines.extend(["--- BEGIN PAYLOAD ---", payload.text, "--- END PAYLOAD ---"])
    return "\n".join(lines) + "\n"


def _render_execution_preview(
    *,
    packet: PreparedHandoffPacket,
    request_payload: dict[str, Any],
) -> str:
    lines = [
        "[handoff] Execute-agent preview",
        f"handoff_id: {packet.handoff_id}",
        f"source: {packet.source}",
        f"target: {packet.target}",
    ]
    if packet.studio_session_id is not None:
        lines.append(f"studio_session_id: {packet.studio_session_id}")
    lines.extend(
        [
            f"payload_kind: {packet.payload_kind}",
            f"character_count: {packet.payload.character_count}",
            f"utf8_byte_count: {packet.payload.utf8_byte_count}",
            f"sha256: {packet.payload.sha256}",
            "selected_execution_configuration:",
            f"  request_id: {request_payload['request_id']}",
            f"  mode: {request_payload['mode']}",
            f"  no_follow_up: {request_payload['no_follow_up']}",
            f"  provider: {request_payload.get('provider')}",
            f"  model: {request_payload.get('model')}",
            f"  planner_model: {request_payload.get('planner_model')}",
            f"  budget: {request_payload.get('budget')}",
            f"  budget_strict: {request_payload.get('budget_strict', False)}",
            f"  subtask_budget: {request_payload.get('subtask_budget')}",
            f"  approve_capabilities: {request_payload.get('approve_capabilities', [])}",
            f"  approve_tool_packs: {request_payload.get('approve_tool_packs', [])}",
            f"  approve_writable_roots: {request_payload.get('approve_writable_roots', [])}",
        ]
    )
    if packet.payload_kind == "canonical_mission_packet":
        canonical_packet, _ = _parse_canonical_mission_packet_text(
            packet.payload.text,
            field_name="handoff packet canonical mission payload",
            require_canonical_text=True,
            studio_session_id=packet.studio_session_id,
        )
        lines.append("goal_source: canonical mission packet fields")
        lines.extend(canonical_packet.safe_preview_lines(indent="  "))
    else:
        lines.extend(["--- BEGIN PAYLOAD ---", packet.payload.text, "--- END PAYLOAD ---"])
    return "\n".join(lines) + "\n"


def _handoff_root_dir() -> Path:
    return get_app_paths().data_root / "handoff"


def _handoff_outbox_dir() -> Path:
    return _handoff_root_dir() / "outbox"


def _handoff_results_dir() -> Path:
    return _handoff_root_dir() / "results"


def _handoff_receipts_dir() -> Path:
    return _handoff_root_dir() / "receipts"


def _handoff_state_dir() -> Path:
    return _handoff_root_dir() / "state"


def _handoff_seals_dir() -> Path:
    return _handoff_root_dir() / "seals"


def _handoff_seal_dir(handoff_id: str) -> Path:
    return _handoff_seals_dir() / handoff_id


def _ensure_operator_only_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_operator_only_json(path: Path, payload: dict[str, Any]) -> Path:
    ensure_app_roots()
    _ensure_operator_only_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(_canonical_json_bytes(payload))
    os.chmod(path, 0o600)
    return path


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _generate_handoff_id(payload: PreparedPayload) -> str:
    return f"handoff-{time.time_ns():x}-{payload.sha256[:12]}"


def _write_packet(packet: PreparedHandoffPacket) -> Path:
    outbox = _ensure_operator_only_dir(_handoff_outbox_dir())
    packet_path = outbox / f"{packet.handoff_id}.json"
    payload = _canonical_json(packet.to_dict()) + "\n"
    fd = os.open(packet_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(packet_path, 0o600)
    return packet_path


def _build_packet(
    *,
    source: str,
    target: str,
    studio_session_id: str | None,
    payload_kind: str,
    payload: PreparedPayload,
) -> PreparedHandoffPacket:
    return PreparedHandoffPacket(
        schema_version=HANDOFF_PACKET_SCHEMA_VERSION,
        handoff_id=_generate_handoff_id(payload),
        source=source,
        target=target,
        studio_session_id=studio_session_id,
        payload_kind=payload_kind,
        payload=payload,
        state="prepared",
    )


def _load_packet_payload(handoff_id: str) -> tuple[Path, dict[str, Any]]:
    packet_path = _handoff_outbox_dir() / f"{handoff_id}.json"
    if not packet_path.is_file():
        raise FileNotFoundError(f"handoff packet not found: {handoff_id}")
    try:
        payload = json.loads(packet_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"handoff packet is not valid JSON: {handoff_id}") from exc
    if not isinstance(payload, dict):
        raise ValueError("handoff packet must be a JSON object.")
    return packet_path, payload


def _parse_prepared_handoff_packet(payload: dict[str, Any]) -> PreparedHandoffPacket:
    allowed = {
        "schema_version",
        "handoff_id",
        "source",
        "target",
        "studio_session_id",
        "payload_kind",
        "payload",
        "state",
    }
    extras = sorted(set(payload) - allowed)
    if extras:
        raise ValueError(f"handoff packet has unknown fields: {extras}")
    if payload.get("schema_version") != HANDOFF_PACKET_SCHEMA_VERSION:
        raise ValueError("handoff packet schema_version must equal 1.")
    handoff_id = _validate_handoff_id(str(payload.get("handoff_id") or ""))
    source = _validate_metadata_label(
        str(payload.get("source") or ""), field_name="source"
    )
    target = _validate_metadata_label(
        str(payload.get("target") or ""), field_name="target"
    )
    studio_session_id = _optional_studio_session_id(payload.get("studio_session_id"))
    payload_kind = str(payload.get("payload_kind") or "").strip().lower()
    if payload_kind not in ALLOWED_PAYLOAD_KINDS:
        raise ValueError("handoff packet payload_kind is not supported.")
    state = str(payload.get("state") or "").strip().lower()
    if state != "prepared":
        raise ValueError("handoff packet state must be prepared.")
    payload_obj = payload.get("payload")
    if not isinstance(payload_obj, dict):
        raise ValueError("handoff packet payload must be an object.")
    payload_allowed = {"text", "character_count", "utf8_byte_count", "sha256"}
    payload_extras = sorted(set(payload_obj) - payload_allowed)
    if payload_extras:
        raise ValueError(f"handoff packet payload has unknown fields: {payload_extras}")
    prepared_payload = _validated_payload_from_text(
        payload_obj.get("text"), field_name="payload.text"
    )
    character_count = payload_obj.get("character_count")
    utf8_byte_count = payload_obj.get("utf8_byte_count")
    sha256 = str(payload_obj.get("sha256") or "").strip().lower()
    if character_count != prepared_payload.character_count:
        raise ValueError("handoff packet character_count does not match payload text.")
    if utf8_byte_count != prepared_payload.utf8_byte_count:
        raise ValueError("handoff packet utf8_byte_count does not match payload text.")
    if not SHA256_RE.fullmatch(sha256):
        raise ValueError(
            "handoff packet sha256 must be a 64-character lowercase hex string."
        )
    if sha256 != prepared_payload.sha256:
        raise ValueError("handoff packet sha256 does not match payload text.")
    if payload_kind == "canonical_mission_packet":
        canonical_packet, canonical_text = _parse_canonical_mission_packet_text(
            prepared_payload.text,
            field_name="handoff packet canonical mission payload",
            require_canonical_text=True,
            studio_session_id=studio_session_id,
        )
        prepared_payload = _validated_payload_from_text(
            canonical_text, field_name="payload.text"
        )
    return PreparedHandoffPacket(
        schema_version=HANDOFF_PACKET_SCHEMA_VERSION,
        handoff_id=handoff_id,
        source=source,
        target=target,
        studio_session_id=studio_session_id,
        payload_kind=payload_kind,
        payload=prepared_payload,
        state="prepared",
    )


def _load_prepared_packet(handoff_id: str) -> tuple[Path, PreparedHandoffPacket]:
    packet_path, payload = _load_packet_payload(handoff_id)
    packet = _parse_prepared_handoff_packet(payload)
    if packet.handoff_id != handoff_id:
        raise ValueError("handoff packet id does not match requested handoff id.")
    return packet_path, packet


def _load_execution_state(handoff_id: str) -> Optional[HandoffExecutionState]:
    path = _handoff_state_dir() / f"{handoff_id}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"handoff state sidecar is not valid JSON: {handoff_id}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("handoff state sidecar must be a JSON object.")
    status = str(payload.get("status") or "").strip().lower()
    if status not in HANDOFF_EXECUTION_STATUSES:
        raise ValueError("handoff state sidecar has unsupported status.")
    return HandoffExecutionState(
        schema_version=int(
            payload.get("schema_version") or HANDOFF_RECEIPT_SCHEMA_VERSION
        ),
        handoff_id=_validate_handoff_id(str(payload.get("handoff_id") or "")),
        status=status,
        request_id=str(payload.get("request_id") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        started_at=str(payload.get("started_at") or "") or None,
        completed_at=str(payload.get("completed_at") or "") or None,
        receipt_path=str(payload.get("receipt_path") or "") or None,
        result_path=str(payload.get("result_path") or "") or None,
    )


def _write_execution_state(state: HandoffExecutionState) -> Path:
    return _write_operator_only_json(
        _handoff_state_dir() / f"{state.handoff_id}.json",
        state.to_dict(),
    )


def _write_execution_result(handoff_id: str, result: dict[str, Any]) -> Path:
    return _write_operator_only_json(
        _handoff_results_dir() / f"{handoff_id}.json",
        result,
    )


def _write_execution_receipt(receipt: HandoffExecutionReceipt) -> Path:
    return _write_operator_only_json(
        _handoff_receipts_dir() / f"{receipt.handoff_id}.json",
        receipt.to_dict(),
    )


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _load_execution_result_payload(handoff_id: str) -> dict[str, Any] | None:
    return _read_optional_json(_handoff_results_dir() / f"{handoff_id}.json")


def _load_execution_receipt_payload(handoff_id: str) -> dict[str, Any] | None:
    return _read_optional_json(_handoff_receipts_dir() / f"{handoff_id}.json")


def _receipt_is_finalized(receipt: dict[str, Any] | None) -> bool:
    if receipt is None:
        return False
    if bool(receipt.get("finalized")):
        return True
    evidence = receipt.get("evidence")
    if isinstance(evidence, dict) and str(evidence.get("evidence_seal_path") or "").strip():
        return True
    return False


def _verify_consumed_execution_result(
    handoff_id: str,
    *,
    result: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    state: HandoffExecutionState | None = None,
) -> None:
    """Fail-closed verify-on-consume for result_sha256 (+ seal when finalized)."""
    if result is None:
        return
    result_path = _handoff_results_dir() / f"{handoff_id}.json"
    verify_execution_result_integrity(result_path=result_path, receipt=receipt)
    state_finalized = bool(
        state is not None and str(state.status or "").strip().lower() == "finalized"
    )
    if state_finalized or _receipt_is_finalized(receipt):
        verify_evidence_seal(_handoff_seal_dir(handoff_id))
        bundle_path = None
        if isinstance(receipt, dict):
            evidence = receipt.get("evidence")
            if isinstance(evidence, dict):
                bundle_path = str(evidence.get("bundle_path") or "").strip() or None
        if bundle_path:
            verify_evidence_consistency(bundle_path)
        else:
            # Wave 002 finalizations always emit a bundle; require it when state is finalized.
            if state_finalized:
                verify_evidence_consistency(
                    evidence_bundle_dir(get_app_paths().data_root, handoff_id)
                )


def _preserve_durable_receipt_after_finalize_failure(
    *,
    receipt: HandoffExecutionReceipt,
    exc: TrustIntegrityError,
) -> HandoffExecutionReceipt:
    """Keep semantic exit_code after durable result when post-result finalize fails.

    Execution Jobs run without trust signing keys (emptyDir). Finalize/signing is
    post-result evidence work and must not rewrite a durable semantic exit into a
    non-zero process exit (which becomes Job BackoffLimitExceeded).
    """
    code = getattr(exc, "code", None)
    _stderr(
        "[handoff] finalize failed after durable result; "
        f"preserving semantic exit_code={receipt.exit_code}"
        + (f" code={code}" if code else "")
        + f": {exc}"
    )
    evidence = {
        **dict(receipt.evidence),
        "finalization": "FINALIZE_FAILED_AFTER_DURABLE_RESULT",
        "finalize_error": str(exc)[:500],
        "finalize_error_code": str(code or "trust_integrity"),
    }
    preserved = HandoffExecutionReceipt(
        schema_version=receipt.schema_version,
        handoff_id=receipt.handoff_id,
        request_id=receipt.request_id,
        status=receipt.status,
        exit_code=receipt.exit_code,
        stop_reason=receipt.stop_reason,
        session_id=receipt.session_id,
        studio_session_id=receipt.studio_session_id,
        result_path=receipt.result_path,
        result_sha256=receipt.result_sha256,
        evidence=evidence,
        receipt_path=receipt.receipt_path,
        started_at=receipt.started_at,
        completed_at=receipt.completed_at,
        finalized=False,
    )
    _write_execution_receipt(preserved)
    return preserved


def _cleanup_incomplete_finalize_bundle(
    bundle_dir: Path,
    *,
    created_by_this_invocation: bool,
) -> None:
    """Remove incomplete finalize artifacts only when this invocation created them.

    FINALIZED (and any pre-existing) evidence bundles are immutable: never
    ``rmtree`` on ``bundle_exists`` / re-finalize. Signing failure after a
    successful write by *this* invocation may clean up only that incomplete tree.
    """
    if not created_by_this_invocation:
        return
    import shutil

    shutil.rmtree(bundle_dir, ignore_errors=True)


def _seal_or_preserve_durable_receipt(
    *,
    handoff_id: str,
    receipt: HandoffExecutionReceipt,
    result_path: Path,
    receipt_path: Path,
    state: HandoffExecutionState,
    packet: PreparedHandoffPacket | None = None,
    binding: dict[str, Any] | None = None,
) -> HandoffExecutionReceipt:
    """Finalize when possible; never override durable semantic exit on finalize failure."""
    try:
        return _seal_and_finalize_execution(
            handoff_id=handoff_id,
            receipt=receipt,
            result_path=result_path,
            receipt_path=receipt_path,
            state=state,
            packet=packet,
            binding=binding,
        )
    except TrustIntegrityError as exc:
        # Re-finalize against an existing immutable bundle must refuse explicitly —
        # do not rewrite into a silent FINALIZE_FAILED receipt that obscures
        # pre-existing FINALIZED evidence.
        if getattr(exc, "code", None) == "bundle_exists":
            raise
        return _preserve_durable_receipt_after_finalize_failure(receipt=receipt, exc=exc)


def _seal_and_finalize_execution(
    *,
    handoff_id: str,
    receipt: HandoffExecutionReceipt,
    result_path: Path,
    receipt_path: Path,
    state: HandoffExecutionState,
    packet: PreparedHandoffPacket | None = None,
    binding: dict[str, Any] | None = None,
) -> HandoffExecutionReceipt:
    """Seal evidence after COMPLETE; emit canonical bundle; FINALIZED only when verified.

    Preserves receipt.status (completed/failed/...) for backward compatibility.
    Finalization is recorded via finalized=true, evidence_seal_path, bundle_path, and state.
    """
    seal_dir = _ensure_operator_only_dir(_handoff_seal_dir(handoff_id))
    seal_receipt = seal_evidence_artifacts(
        seal_dir=seal_dir,
        receipt_id=f"handoff-seal-{handoff_id}",
        claim_summary=(
            f"Handoff {handoff_id} execution evidence "
            f"(status={receipt.status}, exit_code={receipt.exit_code})"
        ),
        artifacts=(
            ("result.json", result_path),
            ("execution-receipt.json", receipt_path),
        ),
        producer={
            "role": "runtime",
            "model": "amof-trust-layer",
            "session_ref": handoff_id,
        },
    )
    verify_evidence_seal(seal_dir)

    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result_payload, dict):
        raise TrustIntegrityError("execution result must be a JSON object", code="invalid_result")

    workspace_root = None
    base_sha = None
    if isinstance(binding, dict):
        workspace_root = str(binding.get("workspace_root") or "").strip() or None
        base_sha = str(binding.get("base_sha") or "").strip().lower() or None

    bundle_receipt = {
        **receipt.to_dict(),
        "finalized": True,
        "workspace_root": workspace_root,
        "base_sha": base_sha,
        "evidence": {
            **dict(receipt.evidence),
            "evidence_seal_path": str(seal_dir / "receipt.json"),
            "finalization": "FINALIZED",
        },
    }
    provenance = build_provenance_document(
        run_id=handoff_id,
        receipt=bundle_receipt,
        result=result_payload,
        seal_receipt=seal_receipt,
        mission_id=handoff_id,
        payload_sha256=packet.payload.sha256 if packet is not None else None,
        workspace_root=workspace_root,
        git_sha=base_sha,
        base_sha=base_sha,
        why=str(result_payload.get("stop_reason") or receipt.stop_reason or "") or None,
    )
    bundle_dir = evidence_bundle_dir(get_app_paths().data_root, handoff_id)
    # FINALIZED evidence is immutable: refuse re-finalize before any write/sign
    # work so a pre-existing signed bundle can never be destroyed by cleanup.
    if bundle_dir.exists():
        raise TrustIntegrityError(
            f"refuse re-finalize: evidence bundle already exists (immutable): {bundle_dir}",
            code="bundle_exists",
        )
    # Do not leave a durable bundle that claims FINALIZED if signing fails.
    # Cleanup may remove only incomplete artifacts created by THIS invocation —
    # never a pre-existing tree (including code=bundle_exists).
    created_by_this_invocation = False
    try:
        write_canonical_evidence_bundle(
            bundle_dir,
            run_id=handoff_id,
            receipt=bundle_receipt,
            result=result_payload,
            provenance=provenance,
            result_source=result_path,
        )
        created_by_this_invocation = True
        # Wave 003: sign manifest + evidence digests (hashes remain canonical).
        # Key creation is an explicit authority action (`amof trust keygen`) — never
        # auto-created as a side effect of finalization.
        from ..trust_crypto import (
            FilesystemKeyProvider,
            load_trust_policy,
            sign_evidence_bundle,
        )
        from ..trust_crypto.bundle_sign import verify_bundle_signature

        key_provider = FilesystemKeyProvider()
        policy = load_trust_policy()
        preferred = str(policy.preferred_key_id or "").strip().lower()
        if not preferred:
            raise TrustIntegrityError(
                "no preferred signing key; run `amof trust keygen` before finalize",
                code="missing_signing_authority",
            )
        policy.assert_key_usable(preferred)
        # Prove private key material is present and loadable before claiming FINALIZED.
        key_provider.get_private_key(preferred)
        sign_evidence_bundle(bundle_dir, key_provider=key_provider, policy=policy)
        verify_evidence_consistency(bundle_dir)
        verify_bundle_signature(bundle_dir, key_provider=key_provider, policy=policy)
    except Exception:
        _cleanup_incomplete_finalize_bundle(
            bundle_dir,
            created_by_this_invocation=created_by_this_invocation,
        )
        raise

    finalized = HandoffExecutionReceipt(
        schema_version=receipt.schema_version,
        handoff_id=receipt.handoff_id,
        request_id=receipt.request_id,
        status=receipt.status,
        exit_code=receipt.exit_code,
        stop_reason=receipt.stop_reason,
        session_id=receipt.session_id,
        studio_session_id=receipt.studio_session_id,
        result_path=receipt.result_path,
        result_sha256=receipt.result_sha256,
        evidence={
            **dict(receipt.evidence),
            "evidence_seal_path": str(seal_dir / "receipt.json"),
            "bundle_path": str(bundle_dir),
            "finalization": "FINALIZED",
        },
        receipt_path=receipt.receipt_path,
        started_at=receipt.started_at,
        completed_at=receipt.completed_at,
        finalized=True,
    )
    _write_execution_receipt(finalized)
    _write_execution_state(
        HandoffExecutionState(
            schema_version=state.schema_version,
            handoff_id=state.handoff_id,
            status="finalized",
            request_id=state.request_id,
            updated_at=_now_iso(),
            started_at=state.started_at,
            completed_at=state.completed_at,
            receipt_path=state.receipt_path,
            result_path=state.result_path,
        )
    )
    return finalized


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hermes_runs_dir() -> Path:
    return get_app_paths().data_root / "runs" / "hermes-opensandbox"


def _find_hermes_run_dir(handoff_id: str, result: dict[str, Any] | None) -> Path | None:
    expected_run_id = str((result or {}).get("session_id") or "").strip()
    runs_root = _hermes_runs_dir()
    if expected_run_id:
        candidate = runs_root / expected_run_id
        if candidate.is_dir():
            return candidate
    if not runs_root.is_dir():
        return None
    for candidate in sorted(runs_root.iterdir(), reverse=True):
        if not candidate.is_dir():
            continue
        if handoff_id in candidate.name:
            return candidate
        request_payload = _read_optional_json(candidate / "request.json") or {}
        if str(request_payload.get("request_id") or "").strip() == handoff_id:
            return candidate
    return None


def _load_latest_event(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    latest: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            latest = payload
    return latest


def _load_run_info(handoff_id: str, result: dict[str, Any] | None) -> dict[str, Any]:
    run_dir = _find_hermes_run_dir(handoff_id, result)
    if run_dir is None:
        return {"available": False}
    event_log_path = run_dir / "events.jsonl"
    runtime_log_path = run_dir / "runtime.log"
    runtime_log_has_content = runtime_log_path.is_file() and bool(
        runtime_log_path.read_text(encoding="utf-8").strip()
    )
    return {
        "available": True,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "event_log_path": str(event_log_path) if event_log_path.is_file() else None,
        "runtime_log_path": str(runtime_log_path) if runtime_log_path.exists() else None,
        "runtime_log_has_content": runtime_log_has_content,
        "latest_event": _load_latest_event(event_log_path),
    }


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_studio_session_id(value: Any) -> Optional[str]:
    return _optional_text(value)


def _string_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        return [str(values)]
    return [str(item) for item in values]


def _build_external_request_payload(
    args: Any, packet: PreparedHandoffPacket
) -> dict[str, Any]:
    goal = packet.payload.text
    if packet.payload_kind == "canonical_mission_packet":
        canonical_packet, _canonical_text = _parse_canonical_mission_packet_text(
            packet.payload.text,
            field_name="handoff packet canonical mission payload",
            require_canonical_text=True,
            studio_session_id=packet.studio_session_id,
        )
        if not canonical_packet.execution_allowed:
            raise ValueError(
                "canonical mission packet execution_allowed must be true before execute-agent."
            )
        goal = canonical_packet.derived_goal()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "request_id": packet.handoff_id,
        "mode": "plan-execute",
        "goal": goal,
        "no_follow_up": True,
    }
    if _optional_text(getattr(args, "provider", None)) is not None:
        payload["provider"] = _optional_text(getattr(args, "provider", None))
    if _optional_text(getattr(args, "model", None)) is not None:
        payload["model"] = _optional_text(getattr(args, "model", None))
    if _optional_text(getattr(args, "planner_model", None)) is not None:
        payload["planner_model"] = _optional_text(getattr(args, "planner_model", None))
    if getattr(args, "budget", None) is not None:
        payload["budget"] = getattr(args, "budget")
    if bool(getattr(args, "budget_strict", False)):
        payload["budget_strict"] = True
    if getattr(args, "subtask_budget", None) is not None:
        payload["subtask_budget"] = getattr(args, "subtask_budget")
    approve_capabilities = [
        item.strip()
        for item in _string_list(getattr(args, "approve_capabilities", None))
        if item.strip()
    ]
    approve_tool_packs = [
        item.strip()
        for item in _string_list(getattr(args, "approve_tool_packs", None))
        if item.strip()
    ]
    approve_writable_roots = [
        item.strip()
        for item in _string_list(getattr(args, "approve_writable_roots", None))
        if item.strip()
    ]
    if approve_capabilities:
        payload["approve_capabilities"] = approve_capabilities
    if approve_tool_packs:
        payload["approve_tool_packs"] = approve_tool_packs
    if approve_writable_roots:
        payload["approve_writable_roots"] = approve_writable_roots
    agent_cmd.parse_external_agent_plan_execute_request(payload)
    return payload


def _current_git_root() -> Path | None:
    try:
        root = get_git_toplevel()
    except Exception:
        return None
    if root is None:
        return None
    return Path(root).resolve(strict=False)


def _resolve_adopted_repo_ecosystem() -> str | None:
    git_root = _current_git_root()
    if git_root is None:
        return None
    try:
        from ..app_config import get_repo_binding_for_git_root

        binding = get_repo_binding_for_git_root(git_root)
    except Exception:
        return None
    if not binding:
        return None
    ecosystem = str(binding.get("ecosystem") or "").strip()
    return ecosystem or None


def _resolve_default_ecosystem_from_cwd_config() -> str | None:
    agent_config = Path(".amof/agent.yaml")
    if not agent_config.exists():
        return None
    for line in agent_config.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("default_ecosystem:"):
            value = line.split(":", 1)[1].strip()
            if " #" in value:
                value = value[: value.index(" #")].rstrip()
            if value:
                return value
    return None


def _resolve_execution_ecosystem(explicit_ecosystem: Any) -> str | None:
    if explicit_ecosystem:
        return str(explicit_ecosystem)
    ecosystem = _resolve_adopted_repo_ecosystem()
    if ecosystem:
        return ecosystem
    state = get_state()
    if state and state.get("ecosystem"):
        return str(state.get("ecosystem"))
    ecosystem = get_ecosystem_from_path()
    if ecosystem:
        return ecosystem
    ecosystem = get_ecosystem_from_branch()
    if ecosystem:
        return ecosystem
    ecosystem = _resolve_default_ecosystem_from_cwd_config()
    if ecosystem:
        return ecosystem
    ecosystems = list_available_ecosystems()
    if len(ecosystems) == 1:
        return ecosystems[0]
    return None


def _load_execution_manifest(args: Any) -> dict[str, Any]:
    ecosystem = _resolve_execution_ecosystem(getattr(args, "ecosystem", None))
    if not ecosystem:
        raise ValueError(
            "no ecosystem resolved for handoff execution; use -e/--ecosystem or run from an adopted repo context."
        )
    try:
        return load_manifest(ecosystem)
    except SystemExit as exc:
        raise ValueError(
            f"failed to load manifest for ecosystem '{ecosystem}'."
        ) from exc


def _build_safe_evidence_refs(result: dict[str, Any]) -> dict[str, Optional[str]]:
    return {
        "plan_path": _optional_text(result.get("plan_path")),
        "checkpoint_path": _optional_text(result.get("checkpoint_path")),
        "event_log_path": _optional_text(result.get("event_log_path")),
        "runtime_log_path": _optional_text(result.get("runtime_log_path")),
        "journal_path": _optional_text(result.get("journal_path")),
    }


def _lifecycle_status_from_result(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").strip().lower()
    stop_reason = str(result.get("stop_reason") or "").strip().lower()
    exit_code = result.get("exit_code")
    if stop_reason in {"cancelled", "canceled"}:
        return "cancelled"
    if stop_reason == "timeout" or exit_code == 124:
        return "timed_out"
    if status in {"completed", "blocked", "failed", "timed_out", "cancelled", "result_missing"}:
        return status
    return "failed"


def _project_inflight_lifecycle(
    state: HandoffExecutionState | None, run_info: dict[str, Any]
) -> str:
    raw_status = str(state.status if state is not None else "").strip().lower()
    if raw_status in {"accepted", "queued"}:
        return raw_status
    if raw_status != "execution_started":
        return raw_status or "prepared"
    if not bool(run_info.get("available")):
        return "queued"
    if bool(run_info.get("runtime_log_has_content")):
        return "executing"
    last_seen = _parse_timestamp(
        ((run_info.get("latest_event") or {}) if isinstance(run_info, dict) else {}).get("timestamp")
        or (state.updated_at if state is not None else None)
    )
    if last_seen is not None and (datetime.now(timezone.utc) - last_seen).total_seconds() > 5:
        return "waiting"
    return "planning"


def _handoff_status_payload(
    handoff_id: str,
    *,
    packet: PreparedHandoffPacket | None = None,
    state: HandoffExecutionState | None = None,
) -> dict[str, Any]:
    loaded_packet = packet
    if loaded_packet is None:
        _packet_path, loaded_packet = _load_prepared_packet(handoff_id)
    loaded_state = state if state is not None else _load_execution_state(handoff_id)
    result = _load_execution_result_payload(handoff_id)
    receipt = _load_execution_receipt_payload(handoff_id)
    _verify_consumed_execution_result(
        handoff_id, result=result, receipt=receipt, state=loaded_state
    )
    run_info = _load_run_info(handoff_id, result)
    raw_status = (
        str(loaded_state.status or "").strip().lower()
        if loaded_state is not None
        else str(loaded_packet.state or "prepared").strip().lower()
    )
    if raw_status == "finalized" or _receipt_is_finalized(receipt):
        lifecycle_state = "finalized"
    elif result is not None:
        lifecycle_state = _lifecycle_status_from_result(result)
    elif raw_status in {
        "completed",
        "finalized",
        "blocked",
        "failed",
        "timed_out",
        "cancelled",
        "result_missing",
    }:
        lifecycle_state = "result_missing"
    else:
        lifecycle_state = _project_inflight_lifecycle(loaded_state, run_info)
    payload = {
        "handoff_id": handoff_id,
        "tracking_ref": handoff_id,
        "status": lifecycle_state,
        "state_status": raw_status,
        "accepted": lifecycle_state != "prepared",
        "source": loaded_packet.source,
        "target": loaded_packet.target,
        "payload_kind": loaded_packet.payload_kind,
        "studio_session_id": loaded_packet.studio_session_id,
        "character_count": loaded_packet.payload.character_count,
        "utf8_byte_count": loaded_packet.payload.utf8_byte_count,
        "sha256": loaded_packet.payload.sha256,
        "request_id": handoff_id,
        "run_id": str((result or {}).get("session_id") or run_info.get("run_id") or "").strip() or None,
        "runner_id": _optional_text((result or {}).get("runner_id")),
        "backend": _optional_text((result or {}).get("backend")),
        "stop_reason": _optional_text((result or {}).get("stop_reason")),
        "failure_classification": _optional_text((result or {}).get("failure_classification"))
        or ("result_missing" if lifecycle_state == "result_missing" else None),
        "final_text": _optional_text((result or {}).get("final_text")),
        "task_findings": _optional_text((result or {}).get("task_findings")),
        "exit_code": (result or {}).get("exit_code", "unknown"),
        "started_at": _optional_text((result or {}).get("started_at"))
        or (loaded_state.started_at if loaded_state is not None else None),
        "completed_at": _optional_text((result or {}).get("completed_at"))
        or (loaded_state.completed_at if loaded_state is not None else None),
        "result_path": _optional_text((result or {}).get("result_path"))
        or (loaded_state.result_path if loaded_state is not None else None),
        "receipt_path": _optional_text((receipt or {}).get("receipt_path"))
        or (loaded_state.receipt_path if loaded_state is not None else None),
        "event_log_path": _optional_text((result or {}).get("event_log_path"))
        or _optional_text(run_info.get("event_log_path")),
        "runtime_log_path": _optional_text((result or {}).get("runtime_log_path"))
        or _optional_text(run_info.get("runtime_log_path")),
        "requested_provider": _optional_text((result or {}).get("requested_provider")),
        "effective_provider": _optional_text((result or {}).get("effective_provider")),
        "requested_model": _optional_text((result or {}).get("requested_model")),
        "effective_model": _optional_text((result or {}).get("effective_model")),
        "transport": _optional_text((result or {}).get("transport")),
        "fallback_used": (result or {}).get("fallback_used"),
        "approved_capabilities": list((result or {}).get("approved_capabilities") or []),
        "effective_capabilities": list((result or {}).get("effective_capabilities") or []),
        "changed_paths": list((result or {}).get("changed_paths") or []),
        "latest_event": run_info.get("latest_event"),
        "recovery_boundary": (
            "result_missing"
            if lifecycle_state == "result_missing"
            else "poll"
            if lifecycle_state in {"accepted", "queued", "planning", "executing", "waiting"}
            else "finalized"
            if lifecycle_state == "finalized"
            else "complete"
        ),
    }
    receipt_evidence = (receipt or {}).get("evidence")
    if isinstance(receipt_evidence, dict):
        payload["evidence_seal_path"] = _optional_text(
            receipt_evidence.get("evidence_seal_path")
        )
        payload["bundle_path"] = _optional_text(receipt_evidence.get("bundle_path"))
        payload["execution_status"] = _optional_text(
            receipt_evidence.get("execution_status")
        )
    else:
        payload["evidence_seal_path"] = None
        payload["bundle_path"] = None
        payload["execution_status"] = None
    payload["finalized"] = lifecycle_state == "finalized" or _receipt_is_finalized(receipt)
    if loaded_packet.payload_kind == "canonical_mission_packet":
        canonical_packet, _canonical_text = _parse_canonical_mission_packet_text(
            loaded_packet.payload.text,
            field_name="handoff packet canonical mission payload",
            require_canonical_text=True,
            studio_session_id=loaded_packet.studio_session_id,
        )
        payload["canonical_packet_identity"] = canonical_packet.identity_payload()
    return payload


def _runner_registry_dir() -> Path:
    return get_app_paths().data_root / "runners" / "registry"


def _load_runner_record(runner_id: str) -> dict[str, Any]:
    record_path = _runner_registry_dir() / f"{runner_id}.json"
    if not record_path.is_file():
        raise ValueError(f"selected runner not found: {runner_id}")
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"selected runner record is invalid JSON: {runner_id}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"selected runner record is invalid: {runner_id}")
    return payload


def _selected_runner_id(args: Any) -> str | None:
    value = _optional_text(getattr(args, "runner_id", None))
    return value


def _explicit_builtin_runner_selected(runner_id: str | None) -> bool:
    return runner_id in {"code", "built-in", "builtin", "amof-built-in-code"}


def _builtin_read_only_structured_proposal_discovery(
    packet: PreparedHandoffPacket,
    args: Any,
) -> bool:
    """Read-only discovery that must emit structured write-scope proposals."""
    if _writable_root_approvals(args):
        return False
    if packet.payload_kind != "canonical_mission_packet":
        return False
    try:
        canonical, _canonical_text = _parse_canonical_mission_packet_text(
            packet.payload.text,
            field_name="handoff packet canonical mission payload",
            require_canonical_text=False,
            studio_session_id=packet.studio_session_id,
        )
    except ValueError:
        return False
    return canonical.structured_write_scope_proposal_required


def _synthetic_builtin_runner_record(runner_id: str) -> dict[str, Any]:
    normalized = runner_id.strip() or "amof-built-in-code"
    return {
        "runner_id": normalized,
        "backend": hermes_opensandbox.BACKEND_TYPE,
        "execution": {"max_runtime_seconds": 900},
    }


def _capability_approvals(args: Any) -> list[str]:
    return [
        item.strip()
        for item in _string_list(getattr(args, "approve_capabilities", None))
        if item.strip()
    ]


def _writable_root_approvals(args: Any) -> list[str]:
    return [
        item.strip()
        for item in _string_list(getattr(args, "approve_writable_roots", None))
        if item.strip()
    ]


def _primary_manifest_workspace(manifest: dict[str, Any]) -> Path | None:
    repos = manifest.get("repos")
    if not isinstance(repos, list):
        return None
    paths = [
        Path(str(repo.get("path") or "").strip()).expanduser().resolve(strict=False)
        for repo in repos
        if isinstance(repo, dict) and str(repo.get("path") or "").strip()
    ]
    if len(paths) == 1:
        return paths[0]
    return None


def _apply_write_scope_bind_gate(
    *,
    args: Any,
    request_payload: dict[str, Any],
    manifest: dict[str, Any],
    run_id: str,
) -> dict[str, Any] | None:
    """Bind Approval before mutation authority; inject resolved absolute roots.

    Returns the Binding record when ``--write-scope-approval`` is used, else None.
    Fail closed before worker dispatch when the Approval is invalid.
    """
    approval_ref = _optional_text(getattr(args, "write_scope_approval", None))
    naked_roots = _writable_root_approvals(args)
    caps = _capability_approvals(args)
    workspace = _primary_manifest_workspace(manifest) or _current_git_root()
    try:
        resolved_roots, binding = prepare_execution_write_scope(
            write_scope_approval=approval_ref,
            approve_writable_roots=naked_roots or None,
            run_id=run_id,
            workspace_root=workspace,
            manifest=manifest,
            requested_capabilities=caps,
            runner_id=_selected_runner_id(args),
            legacy_path_elevation=bool(getattr(args, "legacy_path_elevation", False)),
            require_bounded_write=bool(approval_ref),
            warn_stream=sys.stderr,
        )
    except WriteScopeBindingError as exc:
        raise ValueError(f"write-scope bind gate failed: {exc}") from exc

    if binding is not None:
        # Inject Runtime-resolved absolute roots for backend selection.
        # Do not add write_scope_* keys to the external request schema surface.
        args.approve_writable_roots = list(resolved_roots)
        request_payload["approve_writable_roots"] = list(resolved_roots)
        # Internal handoff-only attribute for Hermes/Claude terminal enforcement.
        args.write_scope_binding_id = binding["binding_id"]
        _stderr(
            "[handoff] WriteScopeBinding created: "
            f"{binding['binding_id']} (approval={binding['approval_id']})"
        )
    elif resolved_roots:
        args.approve_writable_roots = list(resolved_roots)
        request_payload["approve_writable_roots"] = list(resolved_roots)
    return binding


def _finalize_write_scope_binding(
    binding: dict[str, Any] | None,
    *,
    success: bool,
    reason: str | None = None,
    result_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Wave 4: enforce mutations + MutationReceipt when Binding is active.

    Falls back to Wave 3 finalize_binding when no result payload is available
    (e.g. pre-dispatch bind failure with empty diagnostic).
    """
    if binding is None:
        return result_payload
    if isinstance(result_payload, dict):
        try:
            workspace = Path(str(binding.get("workspace_root") or "")).expanduser()
            return apply_enforcement_to_result(
                result_payload,
                binding=binding,
                workspace_root=workspace if workspace.exists() else None,
                runner_failed=not success,
            )
        except Exception as exc:
            _stderr(f"[handoff] write-scope enforcement warning: {exc}")
            try:
                finalize_binding(
                    binding["binding_id"],
                    success=False,
                    reason=f"enforcement_error: {exc}",
                )
            except WriteScopeBindingError as bind_exc:
                _stderr(f"[handoff] write-scope binding finalize warning: {bind_exc}")
            failed = dict(result_payload)
            failed["status"] = "failed"
            failed["exit_code"] = 1
            failed["stop_reason"] = "write_scope_enforcement_error"
            failed["write_scope_binding_id"] = binding["binding_id"]
            failed["write_scope_approval_id"] = binding["approval_id"]
            return failed
    try:
        finalize_binding(
            binding["binding_id"],
            success=success,
            reason=reason,
        )
    except WriteScopeBindingError as exc:
        _stderr(f"[handoff] write-scope binding finalize warning: {exc}")
    return result_payload


def _dispatch_backend_handoff(
    *,
    args: Any,
    packet: PreparedHandoffPacket,
    manifest: dict[str, Any],
    runner_record: dict[str, Any],
    request_payload: dict[str, Any],
    backend_module: Any = hermes_opensandbox,
) -> dict[str, Any]:
    runner_id = str(runner_record.get("runner_id") or "").strip()
    if not runner_id:
        raise ValueError("selected runner record is missing runner_id")
    execution = runner_record.get("execution") if isinstance(runner_record.get("execution"), dict) else {}
    timeout_seconds = int(
        getattr(args, "runner_timeout_seconds", None)
        or execution.get("max_runtime_seconds")
        or 900
    )
    manifest_repos = manifest.get("repos")
    readable_root = None
    if isinstance(manifest_repos, list) and manifest_repos:
        repo_paths = [
            Path(str(repo.get("path") or "").strip())
            for repo in manifest_repos
            if isinstance(repo, dict) and str(repo.get("path") or "").strip()
        ]
        if len(repo_paths) == 1:
            readable_root = str(repo_paths[0])
        elif len(repo_paths) > 1:
            # Multi-target dispatch: every target is materialized as a direct
            # child of one workspace directory; that directory is the readable
            # boundary so the agent can inspect every target, not just the
            # first. Fail closed to the first repo if the layout ever differs.
            parents = {path.parent for path in repo_paths}
            readable_root = (
                str(parents.pop()) if len(parents) == 1 else str(repo_paths[0])
            )
    binding_id = _optional_text(getattr(args, "write_scope_binding_id", None))
    selection_kwargs: dict[str, Any] = {
        "runner_id": runner_id,
        "requested_capabilities": _capability_approvals(args),
        "approve_writable_roots": _writable_root_approvals(args),
        "timeout_seconds": timeout_seconds,
        "readable_root": readable_root,
    }
    # Wave 4: pass Binding id when the backend selection contract accepts it.
    try:
        selection = backend_module.build_selection(
            **selection_kwargs,
            write_scope_binding_id=binding_id,
        )
    except TypeError:
        selection = backend_module.build_selection(**selection_kwargs)
    return backend_module.run(
        manifest=manifest,
        goal=str(request_payload.get("goal") or packet.payload.text),
        request_id=str(request_payload.get("request_id") or packet.handoff_id),
        studio_session_id=packet.studio_session_id,
        selection=selection,
        provider=_optional_text(request_payload.get("provider")),
        model=_optional_text(request_payload.get("model")),
    )


# Backwards-compatible alias (Hermes was the only dispatch backend before
# the Claude Code runner landed).
def _dispatch_hermes_handoff(**kwargs: Any) -> dict[str, Any]:
    return _dispatch_backend_handoff(**kwargs, backend_module=hermes_opensandbox)


def _execute_agent_from_handoff(
    args: Any,
) -> HandoffExecutionReceipt:
    handoff_id = _validate_handoff_id(str(getattr(args, "handoff_id", "")))
    packet_path, packet = _load_prepared_packet(handoff_id)
    existing_state = _load_execution_state(handoff_id)
    if existing_state is not None and existing_state.status not in {"accepted", "queued"}:
        raise ValueError(
            f"handoff packet is not eligible for re-execution; current state is {existing_state.status}."
        )
    if packet.target != HANDOFF_TARGET_AMOF_AGENT:
        raise ValueError(
            f"handoff packet target must be {HANDOFF_TARGET_AMOF_AGENT!r}; received {packet.target!r}."
        )
    request_payload = _build_external_request_payload(args, packet)
    _stderr(_render_execution_preview(packet=packet, request_payload=request_payload))
    if not bool(getattr(args, "confirm", False)):
        raise RuntimeError("preview-only")

    manifest = _load_execution_manifest(args)
    binding: dict[str, Any] | None = None
    started_at = _now_iso()
    _write_execution_state(
        HandoffExecutionState(
            schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
            handoff_id=handoff_id,
            status="queued",
            request_id=handoff_id,
            updated_at=started_at,
            started_at=started_at,
        )
    )

    try:
        # Fail closed before worker mutation authority when Approval is invalid.
        binding = _apply_write_scope_bind_gate(
            args=args,
            request_payload=request_payload,
            manifest=manifest,
            run_id=handoff_id,
        )
        runner_id = _selected_runner_id(args)
        execution_started_at = _now_iso()
        _write_execution_state(
            HandoffExecutionState(
                schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
                handoff_id=handoff_id,
                status="execution_started",
                request_id=handoff_id,
                updated_at=execution_started_at,
                started_at=started_at,
            )
        )
        if runner_id is None or _explicit_builtin_runner_selected(runner_id):
            effective_runner = (runner_id or "amof-built-in-code").strip()
            if _builtin_read_only_structured_proposal_discovery(packet, args):
                result_payload = _dispatch_backend_handoff(
                    args=args,
                    packet=packet,
                    manifest=manifest,
                    runner_record=_synthetic_builtin_runner_record(effective_runner),
                    request_payload=request_payload,
                    backend_module=hermes_opensandbox,
                )
                result_payload.setdefault("runner_id", effective_runner)
                result_payload["backend"] = "amof_builtin_code"
            else:
                response = agent_cmd.run_external_agent_plan_execute_envelope(
                    manifest,
                    request_payload,
                    studio_session_id=packet.studio_session_id,
                )
                result_payload = dict(response.result)
                if runner_id is not None:
                    result_payload.setdefault("runner_id", runner_id)
                    result_payload.setdefault("backend", "amof_builtin_code")
        else:
            runner_record = _load_runner_record(runner_id)
            backend = hermes_opensandbox.runner_backend_type(runner_record)
            dispatch_backends = {
                hermes_opensandbox.BACKEND_TYPE: hermes_opensandbox,
                claude_code.BACKEND_TYPE: claude_code,
                amof_native.BACKEND_TYPE: amof_native,
                cursor_agent.BACKEND_TYPE: cursor_agent,
            }
            backend_module = dispatch_backends.get(backend)
            if backend_module is None:
                raise ValueError(
                    f"selected runner {runner_id!r} does not provide a supported dispatch backend "
                    f"({', '.join(sorted(dispatch_backends))}); found {backend!r}"
                )
            result_payload = _dispatch_backend_handoff(
                args=args,
                packet=packet,
                manifest=manifest,
                runner_record=runner_record,
                request_payload=request_payload,
                backend_module=backend_module,
            )
    except Exception as exc:
        completed_at = _now_iso()
        receipt_path = _handoff_receipts_dir() / f"{handoff_id}.json"
        runner_id = _selected_runner_id(args)
        stop_reason = (
            "write_scope_bind_failed"
            if "write-scope bind gate failed" in str(exc)
            else (
                "handoff_dispatch_failed"
                if runner_id is None
                else "selected_runner_dispatch_failed"
            )
        )
        diagnostic_result = {
            "result_kind": "agent_run_result",
            "contract_version": "agent-run-v1",
            "schema_version": 1,
            "status": "failed",
            "session_id": "",
            "exit_code": 1,
            "stop_reason": stop_reason,
            "final_text": str(exc),
            "runner_id": runner_id,
            "backend": None,
            "studio_session_id": packet.studio_session_id,
            "plan_path": None,
            "checkpoint_path": None,
            "event_log_path": None,
            "runtime_log_path": None,
            "started_at": started_at,
            "completed_at": completed_at,
            "failure_classification": stop_reason,
            "journal_path": None,
            "changed_paths": [],
            "validation_summary": {"status": "not_run", "reason": str(exc)},
            "approved_capabilities": _capability_approvals(args),
            "effective_capabilities": [],
            "evidence_refs": {},
            "budget_summary": {"limit": None, "spent": 0.0, "remaining": None},
        }
        if binding is not None:
            diagnostic_result["write_scope_binding_id"] = binding["binding_id"]
            diagnostic_result["write_scope_approval_id"] = binding["approval_id"]
            # Bind-gate failures leave no Binding; dispatch failures enforce.
            if stop_reason != "write_scope_bind_failed":
                diagnostic_result = (
                    _finalize_write_scope_binding(
                        binding,
                        success=False,
                        reason=str(exc),
                        result_payload=diagnostic_result,
                    )
                    or diagnostic_result
                )
            else:
                _finalize_write_scope_binding(
                    binding,
                    success=False,
                    reason=str(exc),
                )
        result_path = _write_execution_result(handoff_id, diagnostic_result)
        result_sha256 = _file_sha256(result_path)
        receipt = HandoffExecutionReceipt(
            schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
            handoff_id=handoff_id,
            request_id=handoff_id,
            status=_lifecycle_status_from_result(diagnostic_result),
            exit_code=1,
            stop_reason=stop_reason,
            session_id="",
            studio_session_id=packet.studio_session_id,
            result_path=str(result_path),
            result_sha256=result_sha256,
            evidence=_build_safe_evidence_refs(diagnostic_result),
            receipt_path=str(receipt_path),
            started_at=started_at,
            completed_at=completed_at,
        )
        _write_execution_receipt(receipt)
        state = HandoffExecutionState(
            schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
            handoff_id=handoff_id,
            status=receipt.status,
            request_id=handoff_id,
            updated_at=completed_at,
            started_at=started_at,
            completed_at=completed_at,
            receipt_path=str(receipt_path),
            result_path=str(result_path),
        )
        _write_execution_state(state)
        return _seal_or_preserve_durable_receipt(
            handoff_id=handoff_id,
            receipt=receipt,
            result_path=result_path,
            receipt_path=receipt_path,
            state=state,
            packet=packet,
            binding=binding,
        )

    success = int(result_payload.get("exit_code") or 0) == 0 and str(
        result_payload.get("status") or ""
    ) in {"completed", "ok", "success"}
    result_payload = (
        _finalize_write_scope_binding(
            binding,
            success=success,
            reason=str(result_payload.get("stop_reason") or "") or None,
            result_payload=result_payload,
        )
        or result_payload
    )
    if binding is not None:
        result_payload.setdefault("write_scope_binding_id", binding["binding_id"])
        result_payload.setdefault("write_scope_approval_id", binding["approval_id"])

    result_path = _write_execution_result(handoff_id, result_payload)
    result_sha256 = _file_sha256(result_path)
    completed_at = _now_iso()
    receipt_path = _handoff_receipts_dir() / f"{handoff_id}.json"
    receipt = HandoffExecutionReceipt(
        schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
        handoff_id=handoff_id,
        request_id=handoff_id,
        status=_lifecycle_status_from_result(result_payload),
        exit_code=int(result_payload.get("exit_code") or 0),
        stop_reason=str(result_payload.get("stop_reason") or ""),
        session_id=str(result_payload.get("session_id") or ""),
        studio_session_id=str(result_payload.get("studio_session_id") or "").strip() or None,
        result_path=str(result_path),
        result_sha256=result_sha256,
        evidence=_build_safe_evidence_refs(result_payload),
        receipt_path=str(receipt_path),
        started_at=started_at,
        completed_at=completed_at,
    )
    _write_execution_receipt(receipt)
    state = HandoffExecutionState(
        schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
        handoff_id=handoff_id,
        status=receipt.status,
        request_id=handoff_id,
        updated_at=completed_at,
        started_at=started_at,
        completed_at=completed_at,
        receipt_path=str(receipt_path),
        result_path=str(result_path),
    )
    _write_execution_state(state)
    return _seal_or_preserve_durable_receipt(
        handoff_id=handoff_id,
        receipt=receipt,
        result_path=result_path,
        receipt_path=receipt_path,
        state=state,
        packet=packet,
        binding=binding,
    )


def cmd_handoff_accept_agent(args: Any) -> int:
    try:
        handoff_id = _validate_handoff_id(str(getattr(args, "handoff_id", "")))
        _packet_path, packet = _load_prepared_packet(handoff_id)
        if packet.target != HANDOFF_TARGET_AMOF_AGENT:
            raise ValueError(
                f"handoff packet target must be {HANDOFF_TARGET_AMOF_AGENT!r}; received {packet.target!r}."
            )
        current_state = _load_execution_state(handoff_id)
        if not bool(getattr(args, "confirm", False)):
            _stderr(
                "[handoff] Preview only; no acceptance occurred. Re-run with --confirm to persist accepted state."
            )
            return 0
        if current_state is None:
            current_state = HandoffExecutionState(
                schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
                handoff_id=handoff_id,
                status="accepted",
                request_id=handoff_id,
                updated_at=_now_iso(),
            )
            _write_execution_state(current_state)
        payload = _handoff_status_payload(handoff_id, packet=packet, state=current_state)
        payload["operation"] = "handoff.accept"
    except (FileNotFoundError, ValueError) as exc:
        _stderr(f"[handoff] {exc}")
        return 1
    _emit_json_stdout(payload)
    return 0


def cmd_handoff_prepare(args: Any) -> int:
    try:
        source = _validate_metadata_label(
            str(getattr(args, "source", "")), field_name="source"
        )
        target = _validate_metadata_label(
            str(getattr(args, "target", "")), field_name="target"
        )
        studio_session_id = _optional_studio_session_id(
            getattr(args, "studio_session", None)
        )
        payload_kind = _payload_kind_from_cli(str(getattr(args, "payload_kind", "")))
        payload = _read_single_stdin_payload(
            payload_kind, studio_session_id=studio_session_id
        )
    except ValueError as exc:
        _stderr(f"[handoff] {exc}")
        return 1

    _stderr(
        _render_preview(
            source=source,
            target=target,
            studio_session_id=studio_session_id,
            payload_kind=payload_kind,
            payload=payload,
        )
    )
    if not bool(getattr(args, "confirm", False)):
        _stderr(
            "[handoff] Preview only; no packet written. Re-run with --confirm to write one local outbox packet."
        )
        return 0

    packet = _build_packet(
        source=source,
        target=target,
        studio_session_id=studio_session_id,
        payload_kind=payload_kind,
        payload=payload,
    )
    packet_path = _write_packet(packet)
    receipt = PreparedHandoffReceipt(
        status="prepared",
        handoff_id=packet.handoff_id,
        packet_path=str(packet_path),
        character_count=payload.character_count,
        utf8_byte_count=payload.utf8_byte_count,
        sha256=payload.sha256,
    )
    _emit_json_stdout(receipt.to_dict())
    return 0


def cmd_handoff_status(args: Any) -> int:
    try:
        handoff_id = _validate_handoff_id(str(getattr(args, "handoff_id", "")))
        payload = _handoff_status_payload(handoff_id)
    except TrustIntegrityError as exc:
        _stderr(f"[handoff] FAIL_CLOSED integrity: {exc}")
        return 1
    except (FileNotFoundError, ValueError) as exc:
        _stderr(f"[handoff] {exc}")
        return 1
    _emit_json_stdout(payload)
    return 0


def cmd_handoff_execute_agent(args: Any) -> int:
    handoff_id = None
    try:
        handoff_id = _validate_handoff_id(str(getattr(args, "handoff_id", "")))
        _load_prepared_packet(handoff_id)
        current_state = _load_execution_state(handoff_id)
        if current_state is not None and current_state.status not in {"accepted", "queued"}:
            raise ValueError(
                f"handoff packet is not eligible for re-execution; current state is {current_state.status}."
            )
        receipt = _execute_agent_from_handoff(args)
    except RuntimeError as exc:
        if str(exc) == "preview-only":
            _stderr(
                "[handoff] Preview only; no agent execution occurred. Re-run with --confirm to execute this handoff through governed AMOF Agent."
            )
            return 0
        _stderr(f"[handoff] {exc}")
        return 1
    except TrustIntegrityError as exc:
        _stderr(f"[handoff] FAIL_CLOSED integrity: {exc}")
        return 1
    except (FileNotFoundError, ValueError) as exc:
        _stderr(f"[handoff] {exc}")
        return 1

    _emit_json_stdout(receipt.to_dict())
    return int(receipt.exit_code) if isinstance(receipt.exit_code, int) else 1


def cmd_handoff(args: Any) -> int:
    action = str(getattr(args, "handoff_cmd", "") or "").strip()
    if action == "prepare":
        return cmd_handoff_prepare(args)
    if action == "accept-agent":
        return cmd_handoff_accept_agent(args)
    if action == "execute-agent":
        return cmd_handoff_execute_agent(args)
    if action == "status":
        return cmd_handoff_status(args)
    _stderr("Usage: amof handoff <prepare|accept-agent|execute-agent|status> [options]")
    return 1
