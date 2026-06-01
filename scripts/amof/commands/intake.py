"""Bounded CLI intake MVP commands (validate, submit, list, show)."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..app_config import resolve_active_context_name
from ..app_paths import get_app_paths, runs_dir
from ..orchestrator.events import EventLog

REMOTE_CONTEXT_REQUIRED_ENV = {
    "cloud-dev": ("AMOF_REMOTE_IAL_BASE_URL", "AMOF_REMOTE_IAL_API_KEY"),
    "msg-aws-dev": ("AMOF_REMOTE_IAL_BASE_URL", "AMOF_REMOTE_IAL_API_KEY"),
}

REQUIRED_FIELDS = (
    "id",
    "version",
    "kind",
    "ticket_id",
    "rough_intent",
    "bounded_goal",
    "task_kind",
    "repo_scope",
    "paths_to_inspect",
    "profile_ref",
    "mutations",
    "validation_gates",
    "cost_truth_policy",
)
SUPPORTED_TEMPLATE_KINDS = ("bounded_intake_task",)


class IntakeCliError(RuntimeError):
    """Raised when an intake command cannot be completed truthfully."""


@dataclass(frozen=True)
class ValidatedIntake:
    intake_id: str
    ticket_id: str
    version: str
    kind: str
    task_kind: str
    profile_ref: str
    repo_scope: list[str]
    paths_to_inspect: list[str]
    mutations_allowed: list[str]
    mutations_forbidden: list[str]
    validation_gates: list[dict[str, Any]]
    missing_cost_representation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "intake_id": self.intake_id,
            "ticket_id": self.ticket_id,
            "version": self.version,
            "kind": self.kind,
            "task_kind": self.task_kind,
            "profile_ref": self.profile_ref,
            "repo_scope": list(self.repo_scope),
            "paths_to_inspect": list(self.paths_to_inspect),
            "mutations": {
                "allowed": list(self.mutations_allowed),
                "forbidden": list(self.mutations_forbidden),
            },
            "validation_gates": list(self.validation_gates),
            "cost_truth_policy": {
                "missing_cost_representation": self.missing_cost_representation,
            },
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _intake_store_dir() -> Path:
    return get_app_paths().data_root / "intake" / "submissions"


def _intake_runs_dir() -> Path:
    return runs_dir() / "intake-submissions"


def _read_packet(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise IntakeCliError(f"intake file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = yaml.safe_load(text)
        except Exception as exc:  # pragma: no cover - defensive parser guard
            raise IntakeCliError(f"failed to parse intake file: {exc}") from exc
    if not isinstance(parsed, dict):
        raise IntakeCliError("intake packet must be a JSON/YAML object")
    return parsed


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise IntakeCliError(f"missing required field: {key}")
    return value


def _optional_string(payload: dict[str, Any], key: str) -> str:
    return str(payload.get(key) or "").strip()


def _required_string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise IntakeCliError(f"{key} must be a non-empty list")
    normalized = [str(item).strip() for item in value if str(item).strip()]
    if not normalized:
        raise IntakeCliError(f"{key} must contain at least one non-empty item")
    return normalized


def _validate_mutations(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    mutations = payload.get("mutations")
    if not isinstance(mutations, dict):
        raise IntakeCliError("mutations must be an object")
    allowed = mutations.get("allowed")
    forbidden = mutations.get("forbidden")
    if not isinstance(allowed, list):
        raise IntakeCliError("mutations.allowed must be a list")
    if not isinstance(forbidden, list):
        raise IntakeCliError("mutations.forbidden must be a list")
    allowed_list = [str(item).strip() for item in allowed if str(item).strip()]
    forbidden_list = [str(item).strip() for item in forbidden if str(item).strip()]
    return allowed_list, forbidden_list


def _validate_gates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    gates = payload.get("validation_gates")
    if not isinstance(gates, list) or not gates:
        raise IntakeCliError("validation_gates must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    for gate in gates:
        if not isinstance(gate, dict):
            raise IntakeCliError("each validation_gate must be an object")
        name = str(gate.get("name") or "").strip()
        requirement = str(gate.get("requirement") or "").strip()
        failure_action = str(gate.get("failure_action") or "").strip()
        if not name or not requirement or not failure_action:
            raise IntakeCliError("each validation_gate requires name, requirement, and failure_action")
        normalized.append(
            {
                "name": name,
                "requirement": requirement,
                "failure_action": failure_action,
            }
        )
    return normalized


def _validate_missing_cost_representation(payload: dict[str, Any]) -> str:
    policy = payload.get("cost_truth_policy")
    if not isinstance(policy, dict):
        raise IntakeCliError("cost_truth_policy must be an object")
    value = policy.get("missing_cost_representation")
    normalized = str(value).strip().lower()
    if not normalized:
        raise IntakeCliError("cost_truth_policy.missing_cost_representation is required")
    if normalized in {"0", "0.0", "0.00"}:
        raise IntakeCliError("missing cost representation cannot be 0.0")
    return normalized


def _raise_validation_errors(errors: list[str]) -> None:
    if not errors:
        return
    if len(errors) == 1:
        raise IntakeCliError(errors[0])
    raise IntakeCliError("validation failed:\n- " + "\n- ".join(errors))


def _validate_packet(payload: dict[str, Any]) -> ValidatedIntake:
    errors: list[str] = []
    missing_fields = [key for key in REQUIRED_FIELDS if key not in payload]
    if missing_fields:
        errors.append("missing required fields: " + ", ".join(missing_fields))

    intake_id = _optional_string(payload, "id") if "id" in payload else ""
    if "id" in payload and not intake_id:
        errors.append("missing required field: id")

    version = _optional_string(payload, "version") if "version" in payload else ""
    if "version" in payload and not version:
        errors.append("missing required field: version")

    kind = _optional_string(payload, "kind") if "kind" in payload else ""
    if "kind" in payload:
        if not kind:
            errors.append("missing required field: kind")
        elif kind != "bounded_intake_task":
            errors.append("kind must be bounded_intake_task")

    ticket_id = _optional_string(payload, "ticket_id") if "ticket_id" in payload else ""
    if "ticket_id" in payload and not ticket_id:
        errors.append("missing required field: ticket_id")

    rough_intent = _optional_string(payload, "rough_intent") if "rough_intent" in payload else ""
    if "rough_intent" in payload and not rough_intent:
        errors.append("missing required field: rough_intent")

    bounded_goal = _optional_string(payload, "bounded_goal") if "bounded_goal" in payload else ""
    if "bounded_goal" in payload and not bounded_goal:
        errors.append("missing required field: bounded_goal")

    task_kind = _optional_string(payload, "task_kind") if "task_kind" in payload else ""
    if "task_kind" in payload and not task_kind:
        errors.append("missing required field: task_kind")

    profile_ref = _optional_string(payload, "profile_ref") if "profile_ref" in payload else ""
    if "profile_ref" in payload and not profile_ref:
        errors.append("missing required field: profile_ref")

    repo_scope: list[str] = []
    if "repo_scope" in payload:
        try:
            repo_scope = _required_string_list(payload, "repo_scope")
        except IntakeCliError as exc:
            errors.append(str(exc))

    paths_to_inspect: list[str] = []
    if "paths_to_inspect" in payload:
        try:
            paths_to_inspect = _required_string_list(payload, "paths_to_inspect")
        except IntakeCliError as exc:
            errors.append(str(exc))

    allowed_mutations: list[str] = []
    forbidden_mutations: list[str] = []
    if "mutations" in payload:
        try:
            allowed_mutations, forbidden_mutations = _validate_mutations(payload)
        except IntakeCliError as exc:
            errors.append(str(exc))

    validation_gates: list[dict[str, Any]] = []
    if "validation_gates" in payload:
        try:
            validation_gates = _validate_gates(payload)
        except IntakeCliError as exc:
            errors.append(str(exc))

    missing_cost_representation = ""
    if "cost_truth_policy" in payload:
        try:
            missing_cost_representation = _validate_missing_cost_representation(payload)
        except IntakeCliError as exc:
            errors.append(str(exc))

    _raise_validation_errors(errors)

    return ValidatedIntake(
        intake_id=intake_id,
        ticket_id=ticket_id,
        version=version,
        kind=kind,
        task_kind=task_kind,
        profile_ref=profile_ref,
        repo_scope=repo_scope,
        paths_to_inspect=paths_to_inspect,
        mutations_allowed=allowed_mutations,
        mutations_forbidden=forbidden_mutations,
        validation_gates=validation_gates,
        missing_cost_representation=missing_cost_representation,
    )


def _template_payload(kind: str) -> dict[str, Any]:
    if kind != "bounded_intake_task":
        supported = ", ".join(SUPPORTED_TEMPLATE_KINDS)
        raise IntakeCliError(f"unsupported intake template kind: {kind} (supported: {supported})")
    return {
        "version": "1.0.0",
        "id": "replace-me-intake-id",
        "kind": "bounded_intake_task",
        "ticket_id": "AMOF-XXXX",
        "rough_intent": "Describe the operator's rough intent.",
        "bounded_goal": "State the bounded goal and stop conditions.",
        "task_kind": "other",
        "repo_scope": ["."],
        "paths_to_inspect": ["."],
        "profile_ref": "replace-me-profile-ref",
        "mutations": {
            "allowed": [],
            "forbidden": ["edit", "deploy", "promote", "push"],
        },
        "validation_gates": [
            {
                "name": "read_only",
                "requirement": "Intake remains planning-only.",
                "failure_action": "stop",
            }
        ],
        "cost_truth_policy": {
            "missing_cost_representation": "unknown",
        },
    }


def _resolve_context_fail_closed() -> tuple[str, str]:
    import os

    context, source = resolve_active_context_name()
    required_env = REMOTE_CONTEXT_REQUIRED_ENV.get(context, ())
    missing = [name for name in required_env if not str(os.environ.get(name) or "").strip()]
    if missing:
        raise IntakeCliError(
            f"FAIL_CLOSED: selected context '{context}' is unavailable (missing required env vars: {', '.join(missing)}). No silent fallback."
        )
    return context, source


def _is_read_only_intake(validated: ValidatedIntake) -> bool:
    if validated.mutations_allowed:
        return False
    for gate in validated.validation_gates:
        if gate["name"].lower() == "read_only" and gate["failure_action"].lower() == "stop":
            return True
    return False


def _run_id_for(intake_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"intake-{stamp}-{intake_id}"


def _record_path(intake_id: str) -> Path:
    return _intake_store_dir() / f"{intake_id}.json"


def _load_submission_records() -> list[dict[str, Any]]:
    root = _intake_store_dir()
    if not root.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return records


def _record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "intake_id": str(record.get("intake_id") or ""),
        "ticket_id": str(record.get("ticket_id") or ""),
        "status": str(record.get("status") or ""),
        "context": str(record.get("context") or ""),
        "created_at": str(record.get("created_at") or ""),
        "mutation_mode": str(record.get("mutation_mode") or ""),
        "run_id": str(record.get("run_id") or ""),
        "session_id": str(record.get("session_id") or ""),
    }


def _print_list(records: list[dict[str, Any]], *, as_json: bool) -> None:
    summaries = [_record_summary(record) for record in records]
    if as_json:
        print(json.dumps(summaries, indent=2))
        return
    if not summaries:
        print("No intake submissions found.")
        return
    headers = ("intake_id", "ticket_id", "status", "context", "created_at", "mutation_mode", "run_id")
    print("\t".join(headers))
    for item in summaries:
        print("\t".join(item.get(key) or "-" for key in headers))


def _emit_rejection_event(
    *,
    packet_path: Path,
    packet: dict[str, Any] | None,
    error_message: str,
) -> None:
    intake_id = str((packet or {}).get("id") or "").strip() or "intake-rejected"
    ticket_id = str((packet or {}).get("ticket_id") or "").strip() or None
    context = None
    try:
        context, _source = _resolve_context_fail_closed()
    except IntakeCliError:
        context = None
    run_id = _run_id_for(intake_id)
    events = EventLog(
        session_id=run_id,
        runs_dir=_intake_runs_dir(),
        run_id=run_id,
        ticket_id=ticket_id,
        planning_mode="intake_read_only",
        context=context,
        actor="amof.intake",
    )
    events.log("run_created", mode="intake_submit", packet_ref=str(packet_path))
    events.log(
        "intake_rejected",
        intake_id=intake_id,
        packet_ref=str(packet_path),
        validation_result="fail",
        reason=error_message,
        mutation_mode="rejected",
    )
    events.log("run_finished", status="rejected", cost_status="unknown", cost=None, estimated_cost=None)


def _cmd_validate(args: argparse.Namespace) -> int:
    packet_path = Path(str(getattr(args, "file", "") or "").strip())
    if not str(packet_path):
        raise IntakeCliError("intake file path is required")
    packet = _read_packet(packet_path)
    validated = _validate_packet(packet)
    output = {
        "valid": True,
        "file": str(packet_path),
        **validated.to_dict(),
    }
    if bool(getattr(args, "json", False)):
        print(json.dumps(output, indent=2))
    else:
        print(f"VALID intake_id={validated.intake_id} ticket_id={validated.ticket_id} file={packet_path}")
    return 0


def _cmd_submit(args: argparse.Namespace) -> int:
    packet_path = Path(str(getattr(args, "file", "") or "").strip())
    if not str(packet_path):
        raise IntakeCliError("intake file path is required")

    packet: dict[str, Any] | None = None
    try:
        packet = _read_packet(packet_path)
        validated = _validate_packet(packet)
        context, context_source = _resolve_context_fail_closed()
        if not _is_read_only_intake(validated):
            raise IntakeCliError(
                "MVP submit supports planning-only no-mutation intake only (mutations.allowed must be empty and read_only gate must stop)."
            )
        if _record_path(validated.intake_id).exists():
            raise IntakeCliError(f"intake already submitted: {validated.intake_id}")

        run_id = _run_id_for(validated.intake_id)
        events = EventLog(
            session_id=run_id,
            runs_dir=_intake_runs_dir(),
            run_id=run_id,
            ticket_id=validated.ticket_id,
            planning_mode="intake_read_only",
            context=context,
            actor="amof.intake",
        )
        events.log(
            "run_created",
            mode="intake_submit",
            packet_ref=str(packet_path),
            context_source=context_source,
        )
        events.log(
            "intake_submitted",
            intake_id=validated.intake_id,
            packet_ref=str(packet_path),
            mutation_mode="read_only",
        )
        events.log(
            "intake_validated",
            intake_id=validated.intake_id,
            validation_result="pass",
            allowed_mutations=validated.mutations_allowed,
            forbidden_mutations=validated.mutations_forbidden,
            missing_cost_representation=validated.missing_cost_representation,
        )

        record = {
            "intake_id": validated.intake_id,
            "ticket_id": validated.ticket_id,
            "status": "submitted",
            "context": context,
            "context_source": context_source,
            "created_at": _now_iso(),
            "mutation_mode": "read_only",
            "run_id": run_id,
            "session_id": run_id,
            "events_path": str(events.log_path),
            "session_path": str(events.session_dir),
            "packet_path": str(packet_path),
            "validation_result": "pass",
            "mutations": {
                "allowed": validated.mutations_allowed,
                "forbidden": validated.mutations_forbidden,
            },
        }
        record_path = _record_path(validated.intake_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

        events.log(
            "run_finished",
            status="submitted_read_only",
            receipt_ref=str(record_path),
            cost_status="unknown",
            cost=None,
            estimated_cost=None,
        )

        if bool(getattr(args, "json", False)):
            print(json.dumps(record, indent=2))
        else:
            print(f"SUBMITTED intake_id={validated.intake_id} run_id={run_id} context={context}")
        return 0
    except IntakeCliError as exc:
        _emit_rejection_event(packet_path=packet_path, packet=packet, error_message=str(exc))
        raise


def _cmd_list(args: argparse.Namespace) -> int:
    records = _load_submission_records()
    _print_list(records, as_json=bool(getattr(args, "json", False)))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    intake_id = str(getattr(args, "intake_id", "") or "").strip()
    if not intake_id:
        raise IntakeCliError("intake_id is required")
    records = _load_submission_records()
    matches = [record for record in records if str(record.get("intake_id") or "") == intake_id]
    if not matches:
        raise IntakeCliError(f"intake not found: {intake_id}")
    if len(matches) > 1:
        raise IntakeCliError(f"intake id is ambiguous: {intake_id}")
    record = matches[0]
    output = {
        **_record_summary(record),
        "events_path": str(record.get("events_path") or ""),
        "session_path": str(record.get("session_path") or ""),
        "packet_path": str(record.get("packet_path") or ""),
        "validation_result": str(record.get("validation_result") or ""),
    }
    if bool(getattr(args, "json", False)):
        print(json.dumps(output, indent=2))
    else:
        for key, value in output.items():
            print(f"{key}: {value or '-'}")
    return 0


def _cmd_template(args: argparse.Namespace) -> int:
    kind = str(getattr(args, "kind", "") or "bounded_intake_task").strip() or "bounded_intake_task"
    payload = _template_payload(kind)
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2))
    else:
        print(yaml.safe_dump(payload, sort_keys=False).rstrip())
    return 0


def cmd_intake(args: argparse.Namespace) -> int:
    action = str(getattr(args, "intake_cmd", "") or "").strip()
    try:
        if action == "validate":
            return _cmd_validate(args)
        if action == "submit":
            return _cmd_submit(args)
        if action == "list":
            return _cmd_list(args)
        if action == "show":
            return _cmd_show(args)
        if action == "template":
            return _cmd_template(args)
        sys.stderr.write("Usage: amof intake {validate,submit,list,show,template} ...\n")
        return 1
    except IntakeCliError as exc:
        sys.stderr.write(f"[intake] {exc}\n")
        return 1


__all__ = ["IntakeCliError", "ValidatedIntake", "cmd_intake"]
