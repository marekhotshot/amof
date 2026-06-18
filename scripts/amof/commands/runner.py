"""Bounded runner registration MVP commands (register, list, show, doctor, match)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..app_config import load_contexts, resolve_active_context_name
from ..app_paths import get_app_paths, runs_dir
from ..execution_backends import hermes_opensandbox
from ..orchestrator.events import EventLog
from .intake import IntakeCliError, _is_read_only_intake, _validate_packet

REMOTE_CONTEXT_REQUIRED_ENV = {
    "cloud-dev": ("AMOF_REMOTE_IAL_BASE_URL", "AMOF_REMOTE_IAL_API_KEY"),
    "msg-aws-dev": ("AMOF_REMOTE_IAL_BASE_URL", "AMOF_REMOTE_IAL_API_KEY"),
}

RUNNER_REQUIRED_FIELDS = (
    "runner_id",
    "name",
    "context",
    "status",
    "capabilities",
    "supported_task_kinds",
    "allowed_mutation_modes",
    "max_concurrency",
    "trust_level",
    "registration_source",
)

RUNNER_STATUS_ALLOWED = {
    "available",
    "registered",
    "ready",
    "degraded",
    "unreachable",
    "disabled",
    "retired",
}

RUNNER_ELIGIBLE_STATUSES = {"available", "registered", "ready"}
ALLOWED_MUTATION_MODES = {"read_only", "bounded_worktree"}
REQUIRED_MATCH_CAPABILITIES = {"intake.validate", "intake.plan"}
SUPPORTED_TEMPLATE_KINDS = ("local-planning", "hermes-opensandbox")
LOCAL_FORENSIC_TIMEOUT_SECONDS = 15.0
SUPPORTED_BACKENDS = {"planning_only", hermes_opensandbox.BACKEND_TYPE}
HERMES_ALLOWED_EXECUTION_CAPABILITIES = {"read", "bounded_write", "shell_limited", "focused_tests"}
HERMES_DENIED_CAPABILITIES = {
    "kubernetes",
    "kubernetes_mutation",
    "deploy",
    "deployment",
    "secrets",
    "secret_access",
    "unrestricted_network",
    "network_unrestricted",
    "push",
    "promote",
    "promotion",
    "tag",
    "tags",
    "release",
    "releases",
}

SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|password|api[_-]?key|access[_-]?key|private[_-]?key|bearer|credential)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(sk-or-|Bearer\s+[A-Za-z0-9_\-]+|OPENROUTER_API_KEY=|access_token=|token=)",
    re.IGNORECASE,
)


class RunnerCliError(RuntimeError):
    """Raised when a runner command cannot be completed truthfully."""


@dataclass(frozen=True)
class ValidatedRunner:
    runner_id: str
    name: str
    context: str
    status: str
    capabilities: list[str]
    supported_task_kinds: list[str]
    allowed_mutation_modes: list[str]
    max_concurrency: int
    labels: list[str]
    trust_level: str
    registration_source: str
    endpoint_ref: str

    def to_record(self, *, registered_at: str, updated_at: str, source_path: str) -> dict[str, Any]:
        return {
            "runner_id": self.runner_id,
            "name": self.name,
            "context": self.context,
            "status": self.status,
            "capabilities": list(self.capabilities),
            "supported_task_kinds": list(self.supported_task_kinds),
            "allowed_mutation_modes": list(self.allowed_mutation_modes),
            "max_concurrency": self.max_concurrency,
            "labels": list(self.labels),
            "trust_level": self.trust_level,
            "registration_source": self.registration_source,
            "endpoint_ref": self.endpoint_ref,
            "registered_at": registered_at,
            "updated_at": updated_at,
            "source_path": source_path,
        }


@dataclass(frozen=True)
class ForensicCommand:
    label: str
    command: str


LOCAL_FORENSIC_COMMAND_PACK: tuple[ForensicCommand, ...] = (
    ForensicCommand("pwd", "pwd"),
    ForensicCommand("git-status-short", "git status --short"),
    ForensicCommand("git-rev-parse-head", "git rev-parse HEAD"),
    ForensicCommand("file-inventory", "find . -maxdepth 3 -type f | sort | sed 's#^\\./##' | head -300"),
    ForensicCommand(
        "image-static-grep",
        'grep -RIn "primaryImage\\|/uploads\\|products/\\|express.static\\|multer\\|DATA_ROOT" . --exclude-dir=node_modules --exclude-dir=.git | head -300',
    ),
)
LOCAL_FORENSIC_ALLOWED_COMMANDS = {item.command for item in LOCAL_FORENSIC_COMMAND_PACK}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runner_registry_dir() -> Path:
    return get_app_paths().data_root / "runners" / "registry"


def _runner_events_dir() -> Path:
    return runs_dir() / "runner-registry"


def _local_forensic_runs_dir() -> Path:
    return runs_dir() / "local-forensic"


def _runner_record_path(runner_id: str) -> Path:
    return _runner_registry_dir() / f"{runner_id}.json"


def _load_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RunnerCliError(f"runner file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = yaml.safe_load(text)
        except Exception as exc:  # pragma: no cover - defensive parser guard
            raise RunnerCliError(f"failed to parse runner file: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RunnerCliError("runner metadata must be a JSON/YAML object")
    return parsed


def _read_intake_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RunnerCliError(f"intake file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = yaml.safe_load(text)
        except Exception as exc:  # pragma: no cover - defensive parser guard
            raise RunnerCliError(f"failed to parse intake file: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RunnerCliError("intake payload must be a JSON/YAML object")
    return parsed


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise RunnerCliError(f"missing required field: {key}")
    return value


def _required_string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise RunnerCliError(f"{key} must be a non-empty list")
    normalized = [str(item).strip() for item in value if str(item).strip()]
    if not normalized:
        raise RunnerCliError(f"{key} must contain at least one non-empty item")
    return normalized


def _iter_sensitive_paths(value: Any, *, prefix: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            nested_prefix = f"{prefix}.{key_text}" if prefix else key_text
            if SENSITIVE_KEY_PATTERN.search(key_text):
                findings.append(nested_prefix)
            findings.extend(_iter_sensitive_paths(nested, prefix=nested_prefix))
        return findings
    if isinstance(value, list):
        for idx, item in enumerate(value):
            nested_prefix = f"{prefix}[{idx}]"
            findings.extend(_iter_sensitive_paths(item, prefix=nested_prefix))
        return findings
    if isinstance(value, str):
        if SENSITIVE_VALUE_PATTERN.search(value):
            findings.append(prefix or "<value>")
        if "://" in value and "@" in value and not value.endswith("@"):
            findings.append(prefix or "<value>")
    return findings


def _validate_endpoint_ref(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    if "://" in normalized or "/" in normalized:
        raise RunnerCliError("endpoint_ref must be opaque/public-safe (no URL or path syntax)")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", normalized):
        raise RunnerCliError("endpoint_ref contains unsupported characters")
    return normalized


def _backend_type_for_payload(payload: dict[str, Any]) -> str:
    backend = str(payload.get("backend") or payload.get("backend_type") or "").strip()
    if backend:
        return backend
    if str(payload.get("driver") or "").strip().lower() == "hermes":
        return hermes_opensandbox.BACKEND_TYPE
    return "planning_only"


def _validate_backend_payload(payload: dict[str, Any], *, mutation_modes: list[str], capabilities: list[str]) -> None:
    backend = _backend_type_for_payload(payload)
    if backend not in SUPPORTED_BACKENDS:
        raise RunnerCliError(f"unsupported runner backend: {backend}")
    if backend == "planning_only":
        unsupported_modes = [item for item in mutation_modes if item != "read_only"]
        if unsupported_modes:
            raise RunnerCliError(
                f"planning-only runners may include read_only mutation mode only; found: {', '.join(sorted(set(unsupported_modes)))}"
            )
        return
    if backend == hermes_opensandbox.BACKEND_TYPE:
        dangerous = sorted({item for item in capabilities if item in HERMES_DENIED_CAPABILITIES})
        if dangerous:
            raise RunnerCliError(f"Hermes backend does not support dangerous capabilities: {', '.join(dangerous)}")
        declared_execution_caps = [
            item.removeprefix("capability.")
            for item in capabilities
            if item in HERMES_ALLOWED_EXECUTION_CAPABILITIES or item.startswith("capability.")
        ]
        unknown_execution_caps = sorted(
            {item for item in declared_execution_caps if item not in HERMES_ALLOWED_EXECUTION_CAPABILITIES}
        )
        if unknown_execution_caps:
            raise RunnerCliError(
                f"Hermes backend supports only: {', '.join(sorted(HERMES_ALLOWED_EXECUTION_CAPABILITIES))}; found: {', '.join(unknown_execution_caps)}"
            )
        if "bounded_worktree" in mutation_modes:
            authority = payload.get("authority")
            if not isinstance(authority, dict) or authority.get("writable_roots_required") is not True:
                raise RunnerCliError("Hermes bounded_worktree runners must set authority.writable_roots_required: true")


def _validate_context(name: str) -> str:
    contexts = load_contexts().get("contexts", {})
    if name not in contexts:
        raise RunnerCliError(f"unknown context: {name}")
    return name


def _validate_runner(payload: dict[str, Any]) -> ValidatedRunner:
    for key in RUNNER_REQUIRED_FIELDS:
        if key not in payload:
            raise RunnerCliError(f"missing required field: {key}")

    sensitive_paths = _iter_sensitive_paths(payload)
    if sensitive_paths:
        raise RunnerCliError(f"secret-like content is not allowed in runner metadata: {', '.join(sorted(set(sensitive_paths)))}")

    runner_id = _required_string(payload, "runner_id")
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,100}", runner_id):
        raise RunnerCliError("runner_id must match [A-Za-z0-9._-]{3,100}")
    context = _validate_context(_required_string(payload, "context"))
    status = _required_string(payload, "status").lower()
    if status not in RUNNER_STATUS_ALLOWED:
        raise RunnerCliError(f"status must be one of: {', '.join(sorted(RUNNER_STATUS_ALLOWED))}")
    capabilities = _required_string_list(payload, "capabilities")
    task_kinds = _required_string_list(payload, "supported_task_kinds")
    mutation_modes = [item.lower() for item in _required_string_list(payload, "allowed_mutation_modes")]
    unknown_modes = [item for item in mutation_modes if item not in ALLOWED_MUTATION_MODES]
    if unknown_modes:
        raise RunnerCliError(
            f"allowed_mutation_modes may include only ({', '.join(sorted(ALLOWED_MUTATION_MODES))}); found: {', '.join(sorted(set(unknown_modes)))}"
        )
    _validate_backend_payload(payload, mutation_modes=mutation_modes, capabilities=capabilities)
    max_concurrency_raw = payload.get("max_concurrency")
    if not isinstance(max_concurrency_raw, int) or max_concurrency_raw < 1:
        raise RunnerCliError("max_concurrency must be an integer >= 1")
    labels = payload.get("labels")
    if labels is None:
        normalized_labels: list[str] = []
    else:
        if not isinstance(labels, list):
            raise RunnerCliError("labels must be a list when provided")
        normalized_labels = [str(item).strip() for item in labels if str(item).strip()]
    trust_level = _required_string(payload, "trust_level")
    registration_source = _required_string(payload, "registration_source")
    endpoint_ref = _validate_endpoint_ref(str(payload.get("endpoint_ref") or ""))
    return ValidatedRunner(
        runner_id=runner_id,
        name=_required_string(payload, "name"),
        context=context,
        status=status,
        capabilities=capabilities,
        supported_task_kinds=task_kinds,
        allowed_mutation_modes=mutation_modes,
        max_concurrency=max_concurrency_raw,
        labels=normalized_labels,
        trust_level=trust_level,
        registration_source=registration_source,
        endpoint_ref=endpoint_ref,
    )


def _template_payload(kind: str) -> dict[str, Any]:
    if kind == "hermes-opensandbox":
        return {
            "version": "1.0.0",
            "runner_id": "hermes-local-ticket-write",
            "name": "Hermes CLI Remote IAL Ticket Write",
            "context": "local",
            "status": "available",
            "backend": hermes_opensandbox.BACKEND_TYPE,
            "backend_contract_version": hermes_opensandbox.BACKEND_CONTRACT_VERSION,
            "runtime_contract": hermes_opensandbox.RUNTIME_CONTRACT,
            "isolation_model": hermes_opensandbox.ISOLATION_MODEL,
            "capabilities": [
                "intake.validate",
                "intake.plan",
                "execution.scan_report",
                "read",
                "bounded_write",
                "shell_limited",
                "focused_tests",
            ],
            "supported_task_kinds": [
                "other",
                "documentation",
            ],
            "allowed_mutation_modes": [
                "read_only",
                "bounded_worktree",
            ],
            "max_concurrency": 1,
            "labels": [
                "local",
                "hermes",
                "hermes-cli",
                "remote-ial",
            ],
            "trust_level": "local",
            "registration_source": "amof.runner.template.hermes-opensandbox",
            "endpoint_ref": "hermes-local",
            "execution": {
                "mode": "ticket_write",
                "max_runtime_seconds": 2700,
            },
            "authority": {
                "mutation": "bounded_worktree",
                "writable_roots_required": True,
                "commit": "denied",
                "push": "denied",
                "promote": "denied",
                "deploy": "denied",
            },
            "evidence": {
                "canonical_result_required": True,
                "event_log_required": True,
            },
        }
    if kind != "local-planning":
        supported = ", ".join(SUPPORTED_TEMPLATE_KINDS)
        raise RunnerCliError(f"unsupported runner template kind: {kind} (supported: {supported})")
    return {
        "version": "1.0.0",
        "runner_id": "local-planning",
        "name": "Local Planning Runner",
        "context": "local",
        "status": "available",
        "capabilities": [
            "intake.validate",
            "intake.plan",
            "execution.scan_report",
        ],
        "supported_task_kinds": [
            "other",
            "documentation",
        ],
        "allowed_mutation_modes": [
            "read_only",
        ],
        "max_concurrency": 1,
        "labels": [
            "local",
            "planning-only",
            "no-dispatch",
        ],
        "trust_level": "local",
        "registration_source": "amof.runner.template.local-planning",
    }


def _load_runners() -> list[dict[str, Any]]:
    root = _runner_registry_dir()
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
    records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return records


def _runner_summary(record: dict[str, Any]) -> dict[str, str]:
    capabilities = record.get("capabilities")
    capability_summary = ",".join(str(item) for item in capabilities[:3]) if isinstance(capabilities, list) else "-"
    mutation_modes = record.get("allowed_mutation_modes")
    mutation_summary = ",".join(str(item) for item in mutation_modes) if isinstance(mutation_modes, list) else "-"
    return {
        "runner_id": str(record.get("runner_id") or ""),
        "context": str(record.get("context") or ""),
        "status": str(record.get("status") or ""),
        "backend": hermes_opensandbox.runner_backend_type(record),
        "capabilities": capability_summary or "-",
        "allowed_mutation_modes": mutation_summary or "-",
        "max_concurrency": str(record.get("max_concurrency") or ""),
        "updated_at": str(record.get("updated_at") or ""),
    }


def _emit_event(event_type: str, **payload: Any) -> None:
    run_id = "runner-registry-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    context, source = resolve_active_context_name()
    events = EventLog(
        session_id=run_id,
        runs_dir=_runner_events_dir(),
        run_id=run_id,
        ticket_id="AMOF-RUNNER-REGISTRATION-001",
        planning_mode="runner_registry_read_only",
        context=context,
        actor="amof.runner",
    )
    events.log("run_created", mode="runner_registry", context_source=source)
    events.log(event_type, **payload)
    events.log("run_finished", status="ok", cost_status="unknown", cost=None, estimated_cost=None)


def _resolve_context_fail_closed() -> str:
    import os

    context, _source = resolve_active_context_name()
    required_env = REMOTE_CONTEXT_REQUIRED_ENV.get(context, ())
    missing = [name for name in required_env if not str(os.environ.get(name) or "").strip()]
    if missing:
        raise RunnerCliError(
            f"FAIL_CLOSED: selected context '{context}' is unavailable (missing required env vars: {', '.join(missing)}). No silent fallback."
        )
    return context


def _cmd_register(args: argparse.Namespace) -> int:
    file_path = Path(str(getattr(args, "file", "") or "").strip())
    if not str(file_path):
        raise RunnerCliError("runner file path is required")
    payload = _load_payload(file_path)
    validated = _validate_runner(payload)
    existing_path = _runner_record_path(validated.runner_id)
    now = _now_iso()
    registered_at = now
    if existing_path.exists():
        try:
            previous = json.loads(existing_path.read_text(encoding="utf-8"))
            if isinstance(previous, dict):
                registered_at = str(previous.get("registered_at") or now)
        except json.JSONDecodeError:
            registered_at = now
    record = validated.to_record(registered_at=registered_at, updated_at=now, source_path=str(file_path))
    for key in ("backend", "backend_type", "kind", "driver", "execution_profile", "execution", "authority", "tools", "evidence"):
        if key in payload:
            record[key] = payload[key]
    record.setdefault("backend", _backend_type_for_payload(payload))
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    _emit_event("runner_registered", runner_id=validated.runner_id, context=validated.context, status=validated.status)
    if bool(getattr(args, "json", False)):
        print(json.dumps(record, indent=2))
    else:
        backend = str(record.get("backend") or "planning_only")
        dispatch = "yes" if backend == hermes_opensandbox.BACKEND_TYPE and hermes_opensandbox.runtime_health()["dispatch_available"] else "no"
        suffix = (
            "planning_only=yes no_dispatch=yes"
            if backend == "planning_only"
            else (
                f"backend={backend} "
                f"backend_contract_version={hermes_opensandbox.BACKEND_CONTRACT_VERSION} "
                f"runtime_contract={hermes_opensandbox.RUNTIME_CONTRACT} "
                f"dispatch_available={dispatch}"
            )
        )
        print(f"REGISTERED runner_id={validated.runner_id} context={validated.context} status={validated.status} {suffix}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    records = [_runner_summary(item) for item in _load_runners()]
    if bool(getattr(args, "json", False)):
        print(json.dumps(records, indent=2))
        return 0
    if not records:
        print("No registered runners found.")
        return 0
    headers = ("runner_id", "context", "status", "backend", "capabilities", "allowed_mutation_modes", "max_concurrency", "updated_at")
    print("\t".join(headers))
    for item in records:
        print("\t".join(item.get(key) or "-" for key in headers))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    runner_id = str(getattr(args, "runner_id", "") or "").strip()
    if not runner_id:
        raise RunnerCliError("runner_id is required")
    path = _runner_record_path(runner_id)
    if not path.exists():
        raise RunnerCliError(f"runner not found: {runner_id}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunnerCliError(f"runner record is invalid: {runner_id}") from exc
    if not isinstance(record, dict):
        raise RunnerCliError(f"runner record is invalid: {runner_id}")
    if bool(getattr(args, "json", False)):
        print(json.dumps(record, indent=2))
    else:
        for key, value in record.items():
            if isinstance(value, list):
                rendered = ", ".join(str(item) for item in value) if value else "-"
                print(f"{key}: {rendered}")
            else:
                print(f"{key}: {value if value not in ('', None) else '-'}")
    return 0


def _doctor_issues() -> list[str]:
    issues: list[str] = []
    records = _load_runners()
    contexts = set(load_contexts().get("contexts", {}).keys())
    if not records:
        issues.append("no runners registered")
        return issues
    for record in records:
        runner_id = str(record.get("runner_id") or "").strip() or "<unknown>"
        try:
            _validate_runner(record)
        except RunnerCliError as exc:
            issues.append(f"{runner_id}: invalid record ({exc})")
            continue
        context = str(record.get("context") or "")
        if context not in contexts:
            issues.append(f"{runner_id}: unknown context '{context}'")
        status = str(record.get("status") or "").lower()
        if status not in RUNNER_STATUS_ALLOWED:
            issues.append(f"{runner_id}: invalid status '{status}'")
    return issues


def _doctor_backend_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in _load_runners():
        backend = hermes_opensandbox.runner_backend_type(record)
        if backend == hermes_opensandbox.BACKEND_TYPE:
            records.append(hermes_opensandbox.doctor_record(record))
        else:
            records.append(
                {
                    "runner_id": str(record.get("runner_id") or ""),
                    "backend_type": "planning_only",
                    "dispatch_available": False,
                    "runtime_health": "not_applicable",
                    "execution_endpoint": None,
                    "process_identity": None,
                    "supported_capabilities": [str(item) for item in record.get("capabilities", [])],
                    "writable_root_required": False,
                    "cancellation_support": "not_applicable",
                    "log_event_support": "registry_events_only",
                }
            )
    return records


def _cmd_doctor(args: argparse.Namespace) -> int:
    _resolve_context_fail_closed()
    issues = _doctor_issues()
    ok = not issues
    _emit_event("runner_registry_doctor", ok=ok, issue_count=len(issues))
    backend_records = _doctor_backend_records()
    dispatch_available = any(bool(item.get("dispatch_available")) for item in backend_records)
    payload = {
        "ok": ok,
        "issues": issues,
        "planning_only": not dispatch_available,
        "dispatch": "available" if dispatch_available else "none",
        "runners": backend_records,
    }
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2))
    else:
        if ok:
            print(f"RUNNER_REGISTRY_OK dispatch={'available' if dispatch_available else 'none'}")
        else:
            print(f"RUNNER_REGISTRY_FAIL dispatch={'available' if dispatch_available else 'none'}")
            for item in issues:
                print(f"- {item}")
        for item in backend_records:
            print(
                "runner "
                f"{item.get('runner_id') or '-'} "
                f"backend={item.get('backend_type') or '-'} "
                f"backend_contract_version={item.get('backend_contract_version') or '-'} "
                f"runtime_contract={item.get('runtime_contract') or '-'} "
                f"dispatch_available={'yes' if item.get('dispatch_available') else 'no'} "
                f"runtime_health={item.get('runtime_health') or '-'} "
                f"endpoint={item.get('execution_endpoint') or '-'} "
                f"writable_root_required={'yes' if item.get('writable_root_required') else 'no'} "
                f"cancellation={item.get('cancellation_support') or '-'} "
                f"logs={item.get('log_event_support') or '-'}"
            )
    return 0 if ok else 1


def _resolve_intake_reference(reference: str) -> tuple[dict[str, Any], str]:
    candidate = Path(reference)
    if candidate.exists():
        return _read_intake_payload(candidate), str(candidate)
    submission_path = get_app_paths().data_root / "intake" / "submissions" / f"{reference}.json"
    if not submission_path.exists():
        raise RunnerCliError(f"intake reference not found: {reference}")
    try:
        submission = json.loads(submission_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunnerCliError(f"intake submission record is invalid: {reference}") from exc
    if not isinstance(submission, dict):
        raise RunnerCliError(f"intake submission record is invalid: {reference}")
    packet_path = Path(str(submission.get("packet_path") or "").strip())
    if not str(packet_path):
        raise RunnerCliError(f"intake submission missing packet path: {reference}")
    return _read_intake_payload(packet_path), str(packet_path)


def _authority_artifact_path_for_reference(reference: str) -> str:
    submission_path = get_app_paths().data_root / "intake" / "submissions" / f"{reference}.json"
    if not submission_path.exists():
        return ""
    try:
        submission = json.loads(submission_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(submission, dict):
        return ""
    return str(submission.get("authority_decision_path") or "").strip()


def _load_authority_artifact(path: str) -> dict[str, Any]:
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise RunnerCliError(f"authority artifact not found: {path}")
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunnerCliError(f"authority artifact is invalid JSON: {path}") from exc
    if not isinstance(artifact, dict):
        raise RunnerCliError(f"authority artifact must be a JSON object: {path}")
    return artifact


def _tool_names(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("tool_name") or "").strip()
        if name:
            names.append(name)
    return names


def _authority_gate(artifact: dict[str, Any], *, artifact_ref: str) -> dict[str, Any]:
    decision_class = str(artifact.get("decision_class") or "").strip()
    blockers = [str(item) for item in artifact.get("blockers", []) if str(item)] if isinstance(artifact.get("blockers"), list) else []
    eligible_tools = _tool_names(artifact.get("eligible_tools"))
    ineligible_tools = _tool_names(artifact.get("ineligible_tools"))
    allowed = decision_class in {"bounded_action", "answer_only"} and bool(eligible_tools or decision_class == "answer_only")
    if decision_class in {"refuse", "escalate", "privileged_action"}:
        allowed = False
    if decision_class == "bounded_action" and not eligible_tools:
        blockers = blockers or ["authority decision has no eligible tools"]
        allowed = False
    if decision_class == "answer_only":
        blockers = blockers or ["answer_only authority does not permit runner execution selection"]
        allowed = False
    return {
        "artifact_ref": artifact_ref,
        "decision_class": decision_class,
        "allowed": allowed,
        "blockers": blockers,
        "eligible_tools": eligible_tools,
        "ineligible_tools": ineligible_tools,
        "rationale": str(artifact.get("rationale") or ""),
    }


def _authority_candidate_evidence(
    record: dict[str, Any],
    *,
    authority_gate: dict[str, Any] | None,
    runner_reason: str,
) -> dict[str, Any]:
    return {
        "runner_id": str(record.get("runner_id") or ""),
        "runner_reason": runner_reason,
        "authority_decision_class": str((authority_gate or {}).get("decision_class") or ""),
        "authority_allowed": bool((authority_gate or {}).get("allowed", False)) if authority_gate else None,
        "authority_eligible_tools": list((authority_gate or {}).get("eligible_tools") or []),
        "authority_ineligible_tools": list((authority_gate or {}).get("ineligible_tools") or []),
        "authority_blockers": list((authority_gate or {}).get("blockers") or []),
    }


def _has_read_only_stop_gate(validated: Any) -> bool:
    for gate in validated.validation_gates:
        if str(gate.get("name") or "").lower() == "read_only" and str(gate.get("failure_action") or "").lower() == "stop":
            return True
    return False


def _validate_local_forensic_intake(validated: Any) -> list[dict[str, str]]:
    gates: list[dict[str, str]] = []
    if validated.mutations_allowed:
        raise RunnerCliError("local forensic runner requires mutations.allowed == []")
    gates.append({"name": "mutations.allowed", "status": "pass", "requirement": "must be empty"})
    if not _has_read_only_stop_gate(validated):
        raise RunnerCliError("local forensic runner requires validation gate named read_only with failure_action=stop")
    gates.append({"name": "read_only", "status": "pass", "requirement": "read-only gate must stop on failure"})
    return gates


def _resolve_local_forensic_paths(paths_to_inspect: list[str]) -> list[Path]:
    resolved: list[Path] = []
    for raw_path in paths_to_inspect:
        path = Path(raw_path).expanduser().resolve(strict=False)
        if not path.exists():
            raise RunnerCliError(f"inspection path not found: {raw_path}")
        if not path.is_dir():
            raise RunnerCliError(f"inspection path is not a directory: {raw_path}")
        resolved.append(path)
    if not resolved:
        raise RunnerCliError("local forensic runner requires at least one path to inspect")
    return resolved


def _local_forensic_run_id(intake_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_intake_id = re.sub(r"[^A-Za-z0-9._-]+", "-", intake_id).strip("-") or "intake"
    return f"local-forensic-{stamp}-{safe_intake_id}"


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _execute_local_forensic_command(command: str, *, cwd: Path, timeout_seconds: float = LOCAL_FORENSIC_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    if command not in LOCAL_FORENSIC_ALLOWED_COMMANDS:
        raise RunnerCliError(f"command is not in local forensic allowlist: {command}")
    try:
        return subprocess.run(
            ["bash", "-lc", command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunnerCliError(f"local forensic command timed out after {timeout_seconds:g}s: {command}") from exc


def _report_section(title: str, body: str) -> str:
    return f"## {title}\n{body.rstrip() or '-'}\n"


def _build_local_forensic_report(summary: dict[str, Any], command_records: list[dict[str, Any]]) -> str:
    lines: list[str] = ["# AMOF Local Forensic Executor Report\n"]
    lines.append(_report_section("Verdict", f"status: {summary['status']}\nmutation_mode: read_only"))
    lines.append(
        _report_section(
            "Intake",
            f"intake_id: {summary['intake_id']}\nticket_id: {summary['ticket_id']}\npacket_ref: {summary['packet_ref']}",
        )
    )
    lines.append(_report_section("Paths Inspected", "\n".join(f"- {path}" for path in summary["paths_inspected"])))
    lines.append(
        _report_section(
            "Safety Gates",
            "\n".join(f"- {gate['name']}: {gate['status']} ({gate['requirement']})" for gate in summary["safety_gates"]),
        )
    )
    lines.append(_report_section("Blocked Reasons", "\n".join(f"- {item}" for item in summary["blocked_reasons"])))
    lines.append("## Commands\n")
    for record in command_records:
        lines.append(f"### command-{record['sequence']:03d} {record['label']}\n")
        lines.append(f"- cwd: {record['cwd']}\n")
        lines.append(f"- command: `{record['command']}`\n")
        lines.append(f"- exit_code: {record['exit_code']}\n")
        stdout = Path(record["stdout_path"]).read_text(encoding="utf-8")
        stderr = Path(record["stderr_path"]).read_text(encoding="utf-8")
        lines.append("\nstdout:\n```text\n")
        lines.append(stdout[:12000])
        if len(stdout) > 12000:
            lines.append("\n... truncated ...\n")
        lines.append("\n```\n")
        if stderr:
            lines.append("\nstderr:\n```text\n")
            lines.append(stderr[:4000])
            if len(stderr) > 4000:
                lines.append("\n... truncated ...\n")
            lines.append("\n```\n")
    lines.append("\n## Stop Boundary\nNo mutation, cloud execution, kubectl, curl, DB, restart, deploy, migration, push, or secret dump was performed.\n")
    return "\n".join(lines)


def _cmd_run_local_forensic(args: argparse.Namespace) -> int:
    reference = str(getattr(args, "intake_ref", "") or "").strip()
    if not reference:
        raise RunnerCliError("intake reference is required")
    payload, packet_ref = _resolve_intake_reference(reference)
    try:
        validated_intake = _validate_packet(payload)
    except IntakeCliError as exc:
        raise RunnerCliError(f"intake validation failed: {exc}") from exc

    safety_gates = _validate_local_forensic_intake(validated_intake)
    inspection_paths = _resolve_local_forensic_paths(validated_intake.paths_to_inspect)
    run_id = _local_forensic_run_id(validated_intake.intake_id)
    events = EventLog(
        session_id=run_id,
        runs_dir=_local_forensic_runs_dir(),
        run_id=run_id,
        ticket_id=validated_intake.ticket_id,
        planning_mode="local_forensic_read_only",
        context="local",
        actor="amof.runner.local_forensic",
    )
    events.log(
        "run_created",
        mode="local_forensic",
        intake_id=validated_intake.intake_id,
        packet_ref=packet_ref,
        mutation_mode="read_only",
    )

    command_records: list[dict[str, Any]] = []
    sequence = 0
    status = "completed"
    blocked_reasons: list[str] = []
    try:
        for repo_path in inspection_paths:
            for spec in LOCAL_FORENSIC_COMMAND_PACK:
                sequence += 1
                stdout_path = events.session_dir / f"command-{sequence:03d}.stdout"
                stderr_path = events.session_dir / f"command-{sequence:03d}.stderr"
                events.log("command_started", sequence=sequence, cwd=str(repo_path), command=spec.command, label=spec.label)
                completed = _execute_local_forensic_command(spec.command, cwd=repo_path)
                _write_text(stdout_path, completed.stdout or "")
                _write_text(stderr_path, completed.stderr or "")
                record = {
                    "sequence": sequence,
                    "label": spec.label,
                    "cwd": str(repo_path),
                    "command": spec.command,
                    "exit_code": completed.returncode,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                }
                command_records.append(record)
                events.log("command_finished", **record)
    except RunnerCliError as exc:
        status = "blocked"
        blocked_reasons.append(str(exc))
        events.log("run_blocked", reason=str(exc), severity="error")

    summary = {
        "run_id": run_id,
        "intake_id": validated_intake.intake_id,
        "ticket_id": validated_intake.ticket_id,
        "status": status,
        "mutation_mode": "read_only",
        "paths_inspected": [str(path) for path in inspection_paths],
        "commands_run": command_records,
        "packet_ref": packet_ref,
        "report_path": str(events.session_dir / "report.md"),
        "events_path": str(events.log_path),
        "run_path": str(events.session_dir / "run.json"),
        "safety_gates": safety_gates,
        "blocked_reasons": blocked_reasons,
    }
    _write_text(events.session_dir / "report.md", _build_local_forensic_report(summary, command_records))
    _write_text(events.session_dir / "run.json", json.dumps(summary, indent=2) + "\n")
    events.log(
        "run_finished",
        status=status,
        receipt_ref=summary["run_path"],
        report_path=summary["report_path"],
        cost_status="unknown",
        cost=None,
        estimated_cost=None,
    )
    if blocked_reasons:
        raise RunnerCliError("; ".join(blocked_reasons))
    if bool(getattr(args, "json", False)):
        print(json.dumps(summary, indent=2))
    else:
        print(f"LOCAL_FORENSIC_RUN run_id={run_id} status={status} report={summary['report_path']}")
    return 0


def _eligible_runner(record: dict[str, Any], *, active_context: str, task_kind: str, mutation_mode: str) -> tuple[bool, str]:
    runner_id = str(record.get("runner_id") or "").strip() or "<unknown>"
    status = str(record.get("status") or "").lower()
    if status not in RUNNER_ELIGIBLE_STATUSES:
        return False, f"{runner_id}: status '{status}' is not eligible"
    if str(record.get("context") or "") != active_context:
        return False, f"{runner_id}: context mismatch"
    task_kinds = record.get("supported_task_kinds")
    task_kind_values = {str(item) for item in task_kinds} if isinstance(task_kinds, list) else set()
    if task_kind not in task_kind_values and "*" not in task_kind_values:
        return False, f"{runner_id}: unsupported task_kind '{task_kind}'"
    mutation_modes = {str(item).lower() for item in (record.get("allowed_mutation_modes") or [])}
    if mutation_mode.lower() not in mutation_modes:
        return False, f"{runner_id}: mutation mode '{mutation_mode}' not allowed"
    capabilities = {str(item) for item in (record.get("capabilities") or [])}
    missing_caps = sorted(REQUIRED_MATCH_CAPABILITIES - capabilities)
    if missing_caps:
        return False, f"{runner_id}: missing capabilities {', '.join(missing_caps)}"
    return True, f"{runner_id}: eligible"


def _cmd_match(args: argparse.Namespace) -> int:
    reference = str(getattr(args, "intake_ref", "") or "").strip()
    if not reference:
        raise RunnerCliError("intake reference is required")
    payload, packet_ref = _resolve_intake_reference(reference)
    try:
        validated_intake = _validate_packet(payload)
    except IntakeCliError as exc:
        raise RunnerCliError(f"intake validation failed: {exc}") from exc
    if not _is_read_only_intake(validated_intake):
        raise RunnerCliError("runner match supports planning-only intake only (read_only/no dispatch)")
    active_context = _resolve_context_fail_closed()
    authority_artifact_ref = str(getattr(args, "authority_artifact", "") or "").strip() or _authority_artifact_path_for_reference(reference)
    authority_gate: dict[str, Any] | None = None
    if authority_artifact_ref:
        authority_gate = _authority_gate(_load_authority_artifact(authority_artifact_ref), artifact_ref=authority_artifact_ref)
    reasons: list[str] = []
    candidates: list[dict[str, Any]] = []
    ineligible_candidates: list[dict[str, Any]] = []
    for record in _load_runners():
        eligible, reason = _eligible_runner(
            record,
            active_context=active_context,
            task_kind=validated_intake.task_kind,
            mutation_mode="read_only",
        )
        evidence = _authority_candidate_evidence(record, authority_gate=authority_gate, runner_reason=reason)
        reasons.append(reason)
        if authority_gate and not authority_gate["allowed"]:
            eligible = False
            reason = f"{str(record.get('runner_id') or '<unknown>')}: authority blocked ({authority_gate['decision_class']})"
            reasons[-1] = reason
            evidence = _authority_candidate_evidence(record, authority_gate=authority_gate, runner_reason=reason)
        if eligible:
            backend = hermes_opensandbox.runner_backend_type(record)
            dispatch_available = (
                bool(hermes_opensandbox.runtime_health()["dispatch_available"])
                if backend == hermes_opensandbox.BACKEND_TYPE
                else False
            )
            candidate = {
                "runner_id": str(record.get("runner_id") or ""),
                "context": str(record.get("context") or ""),
                "status": str(record.get("status") or ""),
                "backend": backend,
                "dispatch_available": dispatch_available,
                "supported_task_kinds": list(record.get("supported_task_kinds") or []),
                "allowed_mutation_modes": list(record.get("allowed_mutation_modes") or []),
                "capabilities": list(record.get("capabilities") or []),
            }
            if authority_gate:
                candidate["authority_evidence"] = evidence
            candidates.append(
                candidate
            )
        else:
            ineligible_candidates.append(evidence)
    result = {
        "planning_only": True,
        "dispatch": "available" if any(bool(item.get("dispatch_available")) for item in candidates) else "none",
        "intake_id": validated_intake.intake_id,
        "ticket_id": validated_intake.ticket_id,
        "packet_ref": packet_ref,
        "active_context": active_context,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "reasons": reasons,
    }
    if authority_gate:
        result["authority_gate"] = authority_gate
        result["ineligible_candidates"] = ineligible_candidates
    _emit_event(
        "runner_match_planned",
        intake_id=validated_intake.intake_id,
        active_context=active_context,
        candidate_count=len(candidates),
    )
    if authority_gate and not authority_gate["allowed"]:
        if bool(getattr(args, "json", False)):
            print(json.dumps(result, indent=2))
            return 1
        blockers = "; ".join(authority_gate["blockers"]) or authority_gate["rationale"] or "authority gate denied runner match"
        raise RunnerCliError(f"authority gate denied runner match: {authority_gate['decision_class']}; {blockers}")
    if bool(getattr(args, "json", False)):
        print(json.dumps(result, indent=2))
    else:
        print(
            f"MATCH intake_id={validated_intake.intake_id} candidates={len(candidates)} planning_only=yes no_dispatch=yes no_remote_execution=yes"
        )
        for item in candidates:
            print(
                f"- runner_id={item['runner_id']} context={item['context']} status={item['status']} "
                f"backend={item['backend']} dispatch_available={'yes' if item['dispatch_available'] else 'no'}"
            )
    return 0


def _cmd_template(args: argparse.Namespace) -> int:
    kind = str(getattr(args, "kind", "") or "local-planning").strip() or "local-planning"
    payload = _template_payload(kind)
    print(yaml.safe_dump(payload, sort_keys=False).rstrip())
    return 0


def cmd_runner(args: argparse.Namespace) -> int:
    action = str(getattr(args, "runner_cmd", "") or "").strip()
    try:
        if action == "template":
            return _cmd_template(args)
        if action == "register":
            return _cmd_register(args)
        if action == "list":
            return _cmd_list(args)
        if action == "show":
            return _cmd_show(args)
        if action == "doctor":
            return _cmd_doctor(args)
        if action == "match":
            return _cmd_match(args)
        if action == "run-local-forensic":
            return _cmd_run_local_forensic(args)
        sys.stderr.write("Usage: amof runner {template,register,list,show,doctor,match,run-local-forensic} ...\n")
        return 1
    except RunnerCliError as exc:
        sys.stderr.write(f"[runner] {exc}\n")
        return 1


__all__ = [
    "LOCAL_FORENSIC_ALLOWED_COMMANDS",
    "LOCAL_FORENSIC_COMMAND_PACK",
    "RunnerCliError",
    "ValidatedRunner",
    "cmd_runner",
]
