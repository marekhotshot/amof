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
ALLOWED_MUTATION_MODES = {"read_only"}
REQUIRED_MATCH_CAPABILITIES = {"intake.validate", "intake.plan"}
SUPPORTED_TEMPLATE_KINDS = ("local-planning",)
LOCAL_FORENSIC_TIMEOUT_SECONDS = 15.0

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
            f"allowed_mutation_modes may include planning-only values only ({', '.join(sorted(ALLOWED_MUTATION_MODES))}); found: {', '.join(sorted(set(unknown_modes)))}"
        )
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
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    _emit_event("runner_registered", runner_id=validated.runner_id, context=validated.context, status=validated.status)
    if bool(getattr(args, "json", False)):
        print(json.dumps(record, indent=2))
    else:
        print(
            f"REGISTERED runner_id={validated.runner_id} context={validated.context} status={validated.status} planning_only=yes no_dispatch=yes"
        )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    records = [_runner_summary(item) for item in _load_runners()]
    if bool(getattr(args, "json", False)):
        print(json.dumps(records, indent=2))
        return 0
    if not records:
        print("No registered runners found.")
        return 0
    headers = ("runner_id", "context", "status", "capabilities", "allowed_mutation_modes", "max_concurrency", "updated_at")
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


def _cmd_doctor(args: argparse.Namespace) -> int:
    _resolve_context_fail_closed()
    issues = _doctor_issues()
    ok = not issues
    _emit_event("runner_registry_doctor", ok=ok, issue_count=len(issues))
    payload = {"ok": ok, "issues": issues, "planning_only": True, "dispatch": "none"}
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2))
    else:
        if ok:
            print("RUNNER_REGISTRY_OK planning_only=yes no_dispatch=yes")
        else:
            print("RUNNER_REGISTRY_FAIL planning_only=yes no_dispatch=yes")
            for item in issues:
                print(f"- {item}")
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
    reasons: list[str] = []
    candidates: list[dict[str, Any]] = []
    for record in _load_runners():
        eligible, reason = _eligible_runner(
            record,
            active_context=active_context,
            task_kind=validated_intake.task_kind,
            mutation_mode="read_only",
        )
        reasons.append(reason)
        if eligible:
            candidates.append(
                {
                    "runner_id": str(record.get("runner_id") or ""),
                    "context": str(record.get("context") or ""),
                    "status": str(record.get("status") or ""),
                    "supported_task_kinds": list(record.get("supported_task_kinds") or []),
                    "allowed_mutation_modes": list(record.get("allowed_mutation_modes") or []),
                    "capabilities": list(record.get("capabilities") or []),
                }
            )
    result = {
        "planning_only": True,
        "dispatch": "none",
        "intake_id": validated_intake.intake_id,
        "ticket_id": validated_intake.ticket_id,
        "packet_ref": packet_ref,
        "active_context": active_context,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "reasons": reasons,
    }
    _emit_event(
        "runner_match_planned",
        intake_id=validated_intake.intake_id,
        active_context=active_context,
        candidate_count=len(candidates),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(result, indent=2))
    else:
        print(
            f"MATCH intake_id={validated_intake.intake_id} candidates={len(candidates)} planning_only=yes no_dispatch=yes no_remote_execution=yes"
        )
        for item in candidates:
            print(f"- runner_id={item['runner_id']} context={item['context']} status={item['status']}")
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
