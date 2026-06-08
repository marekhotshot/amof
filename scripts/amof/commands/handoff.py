from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..app_paths import ensure_app_roots, get_app_paths
from ..commands import agent_cmd
from ..manifest import list_available_ecosystems, load_manifest
from ..state import get_state
from ..utils import get_ecosystem_from_branch, get_ecosystem_from_path, get_git_toplevel

MAX_PAYLOAD_UTF8_BYTES = 40000
HANDOFF_PACKET_SCHEMA_VERSION = 1
HANDOFF_RECEIPT_SCHEMA_VERSION = 1
HANDOFF_TARGET_AMOF_AGENT = "amof-agent"
PAYLOAD_KIND_MAP = {
    "selected-text": "selected_text",
    "last-response": "last_response",
}
ALLOWED_PAYLOAD_KINDS = frozenset(PAYLOAD_KIND_MAP.values())
HANDOFF_EXECUTION_STATUSES = frozenset(
    {"execution_started", "completed", "blocked", "failed"}
)
METADATA_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,63})$")
HANDOFF_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,127})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _payload_kind_from_cli(value: str) -> str:
    normalized = str(value or "").strip().lower()
    payload_kind = PAYLOAD_KIND_MAP.get(normalized)
    if not payload_kind:
        raise ValueError("payload kind is not supported.")
    return payload_kind


def _read_single_stdin_payload() -> PreparedPayload:
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
    return PreparedPayload(
        text=text,
        character_count=len(text),
        utf8_byte_count=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
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
        f"payload_kind: {payload_kind}",
        f"character_count: {payload.character_count}",
        f"utf8_byte_count: {payload.utf8_byte_count}",
        f"sha256: {payload.sha256}",
        "--- BEGIN PAYLOAD ---",
        payload.text,
        "--- END PAYLOAD ---",
    ]
    if studio_session_id is not None:
        lines.insert(3, f"studio_session_id: {studio_session_id}")
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
        "--- BEGIN PAYLOAD ---",
        packet.payload.text,
        "--- END PAYLOAD ---",
    ]
    if packet.studio_session_id is not None:
        lines.insert(18, f"  studio_session_id: {packet.studio_session_id}")
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
    payload: dict[str, Any] = {
        "schema_version": 1,
        "request_id": packet.handoff_id,
        "mode": "plan-execute",
        "goal": packet.payload.text,
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
        "journal_path": _optional_text(result.get("journal_path")),
    }


def _final_status_from_result(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").strip().lower()
    if status in {"completed", "blocked", "failed"}:
        return status
    return "failed"


def _execute_agent_from_handoff(
    args: Any,
) -> HandoffExecutionReceipt:
    handoff_id = _validate_handoff_id(str(getattr(args, "handoff_id", "")))
    packet_path, packet = _load_prepared_packet(handoff_id)
    existing_state = _load_execution_state(handoff_id)
    if existing_state is not None:
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
    started_at = _now_iso()
    _write_execution_state(
        HandoffExecutionState(
            schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
            handoff_id=handoff_id,
            status="execution_started",
            request_id=handoff_id,
            updated_at=started_at,
            started_at=started_at,
        )
    )

    try:
        response = agent_cmd.run_external_agent_plan_execute_envelope(
            manifest,
            request_payload,
            studio_session_id=packet.studio_session_id,
        )
        result_payload = dict(response.result)
    except Exception:
        completed_at = _now_iso()
        receipt_path = _handoff_receipts_dir() / f"{handoff_id}.json"
        receipt = HandoffExecutionReceipt(
            schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
            handoff_id=handoff_id,
            request_id=handoff_id,
            status="failed",
            exit_code=1,
            stop_reason="handoff_dispatch_failed",
            session_id="",
            studio_session_id=None,
            result_path=None,
            result_sha256=None,
            evidence={},
            receipt_path=str(receipt_path),
            started_at=started_at,
            completed_at=completed_at,
        )
        _write_execution_receipt(receipt)
        _write_execution_state(
            HandoffExecutionState(
                schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
                handoff_id=handoff_id,
                status="failed",
                request_id=handoff_id,
                updated_at=completed_at,
                started_at=started_at,
                completed_at=completed_at,
                receipt_path=str(receipt_path),
            )
        )
        return receipt

    result_path = _write_execution_result(handoff_id, result_payload)
    result_sha256 = _file_sha256(result_path)
    completed_at = _now_iso()
    receipt_path = _handoff_receipts_dir() / f"{handoff_id}.json"
    receipt = HandoffExecutionReceipt(
        schema_version=HANDOFF_RECEIPT_SCHEMA_VERSION,
        handoff_id=handoff_id,
        request_id=handoff_id,
        status=_final_status_from_result(result_payload),
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
    _write_execution_state(
        HandoffExecutionState(
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
    )
    return receipt


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
        payload = _read_single_stdin_payload()
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


def cmd_handoff_execute_agent(args: Any) -> int:
    handoff_id = None
    try:
        handoff_id = _validate_handoff_id(str(getattr(args, "handoff_id", "")))
        _load_prepared_packet(handoff_id)
        current_state = _load_execution_state(handoff_id)
        if current_state is not None:
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
    except (FileNotFoundError, ValueError) as exc:
        _stderr(f"[handoff] {exc}")
        return 1

    _emit_json_stdout(receipt.to_dict())
    return int(receipt.exit_code)


def cmd_handoff(args: Any) -> int:
    action = str(getattr(args, "handoff_cmd", "") or "").strip()
    if action == "prepare":
        return cmd_handoff_prepare(args)
    if action == "execute-agent":
        return cmd_handoff_execute_agent(args)
    _stderr("Usage: amof handoff <prepare|execute-agent> [options]")
    return 1
