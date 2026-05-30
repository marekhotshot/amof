"""Bounded execution scan/report commands (scan, report) with no execution."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..app_config import resolve_active_context_name
from ..app_paths import get_app_paths
from ..orchestrator.events import EventLog
from .intake import IntakeCliError, _is_read_only_intake, _validate_packet

TICKET_ID = "AMOF-REMOTE-EXECUTION-SCAN-REPORT-001"
NO_EXECUTION_OUTCOME = "NO_EXECUTION_PERFORMED"

REMOTE_CONTEXT_REQUIRED_ENV = {
    "cloud-dev": ("AMOF_REMOTE_IAL_BASE_URL", "AMOF_REMOTE_IAL_API_KEY"),
    "msg-aws-dev": ("AMOF_REMOTE_IAL_BASE_URL", "AMOF_REMOTE_IAL_API_KEY"),
}

RUNNER_ELIGIBLE_STATUSES = {"available", "registered", "ready"}
REQUIRED_SCAN_CAPABILITIES = {"intake.validate", "intake.plan", "execution.scan_report"}

SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|password|api[_-]?key|access[_-]?key|private[_-]?key|bearer|credential)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(sk-or-|Bearer\s+[A-Za-z0-9_\-]+|OPENROUTER_API_KEY=|access_token=|token=)",
    re.IGNORECASE,
)


class ExecutionScanError(RuntimeError):
    """Raised when execution scan/report cannot be completed truthfully."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execution_scans_root() -> Path:
    return get_app_paths().data_root / "execution-scans"


def _scan_dir(scan_id: str) -> Path:
    return _execution_scans_root() / scan_id


def _report_path(scan_id: str) -> Path:
    return _scan_dir(scan_id) / "report.json"


def _runner_registry_dir() -> Path:
    return get_app_paths().data_root / "runners" / "registry"


def _intake_submissions_dir() -> Path:
    return get_app_paths().data_root / "intake" / "submissions"


def _resolve_context_fail_closed() -> str:
    import os

    context, _source = resolve_active_context_name()
    required_env = REMOTE_CONTEXT_REQUIRED_ENV.get(context, ())
    missing = [name for name in required_env if not str(os.environ.get(name) or "").strip()]
    if missing:
        raise ExecutionScanError(
            f"FAIL_CLOSED: selected context '{context}' is unavailable (missing required env vars: {', '.join(missing)}). No silent fallback."
        )
    return context


def _scan_id_for(intake_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", intake_id).strip("-") or "intake"
    return f"execution-scan-{stamp}-{normalized}"


def _read_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ExecutionScanError("payload must be a JSON/YAML object")
    return parsed


def _resolve_intake_reference(reference: str) -> tuple[dict[str, Any], str]:
    candidate = Path(reference)
    if candidate.exists():
        return _read_payload(candidate), str(candidate)

    submission_path = _intake_submissions_dir() / f"{reference}.json"
    if not submission_path.exists():
        raise ExecutionScanError(f"intake reference not found: {reference}")
    try:
        submission = json.loads(submission_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExecutionScanError(f"intake submission record is invalid: {reference}") from exc
    if not isinstance(submission, dict):
        raise ExecutionScanError(f"intake submission record is invalid: {reference}")
    packet_path = Path(str(submission.get("packet_path") or "").strip())
    if not str(packet_path):
        raise ExecutionScanError(f"intake submission missing packet path: {reference}")
    return _read_payload(packet_path), str(packet_path)


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
    if isinstance(value, str) and SENSITIVE_VALUE_PATTERN.search(value):
        findings.append(prefix or "<value>")
    return findings


def _load_runner_records() -> list[dict[str, Any]]:
    registry_dir = _runner_registry_dir()
    if not registry_dir.exists():
        raise ExecutionScanError("runner registry not found; register at least one runner first")
    records: list[dict[str, Any]] = []
    for path in sorted(registry_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    if not records:
        raise ExecutionScanError("no runner records found in registry")
    return records


def _intake_requests_remote_execution(payload: dict[str, Any]) -> bool:
    for key in ("remote_execution", "execution_mode", "dispatch", "runner_dispatch", "execute"):
        value = payload.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, str) and value.strip().lower() in {"true", "yes", "remote", "dispatch"}:
            return True
    return False


def _evaluate_runner(
    record: dict[str, Any],
    *,
    active_context: str,
    task_kind: str,
) -> tuple[bool, str]:
    runner_id = str(record.get("runner_id") or "").strip() or "<unknown>"
    sensitive_paths = _iter_sensitive_paths(record)
    if sensitive_paths:
        return False, f"{runner_id}: secret-like fields detected ({', '.join(sorted(set(sensitive_paths)))})"
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
    if "read_only" not in mutation_modes:
        return False, f"{runner_id}: missing read_only mutation mode"
    capabilities = {str(item) for item in (record.get("capabilities") or [])}
    missing_caps = sorted(REQUIRED_SCAN_CAPABILITIES - capabilities)
    if missing_caps:
        return False, f"{runner_id}: missing capabilities {', '.join(missing_caps)}"
    return True, f"{runner_id}: eligible"


def _emit_scan_events(
    *,
    scan_id: str,
    intake_id: str,
    context: str,
    packet_ref: str,
    candidate_count: int,
    eligible_count: int,
    blocked_reasons: list[str],
    report_path: Path,
) -> Path:
    events = EventLog(
        session_id=scan_id,
        runs_dir=_execution_scans_root(),
        run_id=scan_id,
        ticket_id=TICKET_ID,
        planning_mode="execution_scan_report_only",
        context=context,
        actor="amof.execution",
    )
    events.log(
        "execution_scan_started",
        intake_id=intake_id,
        packet_ref=packet_ref,
        outcome=NO_EXECUTION_OUTCOME,
    )
    events.log(
        "runner_candidates_evaluated",
        intake_id=intake_id,
        candidate_count=candidate_count,
        eligible_count=eligible_count,
        outcome=NO_EXECUTION_OUTCOME,
    )
    if blocked_reasons:
        events.log(
            "execution_scan_blocked",
            intake_id=intake_id,
            blocked_reasons=blocked_reasons,
            outcome=NO_EXECUTION_OUTCOME,
        )
    events.log(
        "execution_scan_completed",
        intake_id=intake_id,
        status="blocked" if blocked_reasons else "ready",
        outcome=NO_EXECUTION_OUTCOME,
    )
    events.log(
        "execution_report_written",
        intake_id=intake_id,
        report_path=str(report_path),
        outcome=NO_EXECUTION_OUTCOME,
    )
    events.log(
        "run_finished",
        status="blocked" if blocked_reasons else "ready",
        outcome=NO_EXECUTION_OUTCOME,
        cost_status="unknown",
        cost=None,
        estimated_cost=None,
    )
    return events.log_path


def _build_report(
    *,
    scan_id: str,
    intake_payload: dict[str, Any],
    intake_id: str,
    intake_ticket_id: str,
    packet_ref: str,
    context: str,
    candidate_records: list[dict[str, Any]],
    eligibility_reasons: list[str],
    eligible_runners: list[dict[str, str]],
    blocked_reasons: list[str],
) -> dict[str, Any]:
    safety_gates = [
        {"name": "no_execution", "status": "pass"},
        {"name": "no_mutation", "status": "pass" if not blocked_reasons or "intake is not planning-only read_only" not in blocked_reasons else "fail"},
        {"name": "context_compatible", "status": "fail" if any("context" in reason for reason in blocked_reasons) else "pass"},
        {"name": "runner_available", "status": "pass" if eligible_runners else "fail"},
    ]
    report = {
        "scan_id": scan_id,
        "ticket_id": TICKET_ID,
        "intake_id": intake_id,
        "intake_ticket_id": intake_ticket_id,
        "intake_packet_ref": packet_ref,
        "context": context,
        "status": "blocked" if blocked_reasons else "ready",
        "outcome": NO_EXECUTION_OUTCOME,
        "candidate_runner_count": len(candidate_records),
        "eligible_runners": eligible_runners,
        "blocked_reasons": blocked_reasons,
        "eligibility_reasons": eligibility_reasons,
        "required_capabilities": sorted(REQUIRED_SCAN_CAPABILITIES),
        "safety_gates": safety_gates,
        "mutation_mode": "read_only",
        "proposed_execution_plan": [
            "Validate intake and runner metadata compatibility.",
            "If all gates pass, execution would be eligible in a future ticket.",
            "Stop at scan/report boundary and perform no execution.",
        ],
        "reason_no_execution": "scan/report-only contract boundary for this ticket",
        "report_path": str(_report_path(scan_id)),
        "created_at": _now_iso(),
    }
    if intake_payload.get("context") is not None:
        report["intake_requested_context"] = str(intake_payload.get("context") or "")
    return report


def _write_report(scan_id: str, payload: dict[str, Any]) -> Path:
    path = _report_path(scan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _scan(args: argparse.Namespace) -> int:
    intake_ref = str(getattr(args, "intake_ref", "") or "").strip()
    if not intake_ref:
        raise ExecutionScanError("intake reference is required")
    intake_payload, packet_ref = _resolve_intake_reference(intake_ref)
    try:
        validated_intake = _validate_packet(intake_payload)
    except IntakeCliError as exc:
        raise ExecutionScanError(f"intake validation failed: {exc}") from exc

    context = _resolve_context_fail_closed()
    runner_records = _load_runner_records()
    scan_id = _scan_id_for(validated_intake.intake_id)

    blocked_reasons: list[str] = []
    if not _is_read_only_intake(validated_intake):
        blocked_reasons.append("intake is not planning-only read_only")
    requested_context = str(intake_payload.get("context") or "").strip()
    if requested_context and requested_context != context:
        blocked_reasons.append(f"intake requested context '{requested_context}' but active context is '{context}'")
    if _intake_requests_remote_execution(intake_payload):
        blocked_reasons.append("intake requests remote execution/dispatch, but scan/report is no-execution only")

    eligibility_reasons: list[str] = []
    eligible_runners: list[dict[str, str]] = []
    for record in runner_records:
        eligible, reason = _evaluate_runner(
            record,
            active_context=context,
            task_kind=validated_intake.task_kind,
        )
        eligibility_reasons.append(reason)
        if eligible:
            eligible_runners.append(
                {
                    "runner_id": str(record.get("runner_id") or ""),
                    "context": str(record.get("context") or ""),
                    "reason": reason,
                }
            )

    if not eligible_runners:
        blocked_reasons.append("no eligible runner candidates")

    report = _build_report(
        scan_id=scan_id,
        intake_payload=intake_payload,
        intake_id=validated_intake.intake_id,
        intake_ticket_id=validated_intake.ticket_id,
        packet_ref=packet_ref,
        context=context,
        candidate_records=runner_records,
        eligibility_reasons=eligibility_reasons,
        eligible_runners=eligible_runners,
        blocked_reasons=blocked_reasons,
    )
    report_file = _write_report(scan_id, report)
    events_path = _emit_scan_events(
        scan_id=scan_id,
        intake_id=validated_intake.intake_id,
        context=context,
        packet_ref=packet_ref,
        candidate_count=len(runner_records),
        eligible_count=len(eligible_runners),
        blocked_reasons=blocked_reasons,
        report_path=report_file,
    )
    report["events_path"] = str(events_path)
    _write_report(scan_id, report)

    if bool(getattr(args, "json", False)):
        print(json.dumps(report, indent=2))
    else:
        print(
            f"SCAN scan_id={scan_id} intake_id={validated_intake.intake_id} status={report['status']} outcome={NO_EXECUTION_OUTCOME}"
        )
        print(f"report_path={report_file}")
        print(f"events_path={events_path}")
        print(f"eligible_runners={len(eligible_runners)}")
    return 0


def _report(args: argparse.Namespace) -> int:
    scan_id = str(getattr(args, "scan_id", "") or "").strip()
    if not scan_id:
        raise ExecutionScanError("scan_id is required")
    path = _report_path(scan_id)
    if not path.exists():
        raise ExecutionScanError(f"scan report not found: {scan_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExecutionScanError(f"scan report is invalid: {scan_id}") from exc
    if not isinstance(payload, dict):
        raise ExecutionScanError(f"scan report is invalid: {scan_id}")
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2))
    else:
        for key in (
            "scan_id",
            "ticket_id",
            "intake_id",
            "context",
            "status",
            "outcome",
            "mutation_mode",
            "report_path",
            "created_at",
        ):
            print(f"{key}: {payload.get(key) or '-'}")
        eligible = payload.get("eligible_runners")
        if isinstance(eligible, list):
            print(f"eligible_runner_count: {len(eligible)}")
            for item in eligible:
                if isinstance(item, dict):
                    print(f"- runner_id={item.get('runner_id', '-')}, reason={item.get('reason', '-')}")
    return 0


def cmd_execution(args: argparse.Namespace) -> int:
    action = str(getattr(args, "execution_cmd", "") or "").strip()
    try:
        if action == "scan":
            return _scan(args)
        if action == "report":
            return _report(args)
        sys.stderr.write("Usage: amof execution {scan,report} ...\n")
        return 1
    except ExecutionScanError as exc:
        sys.stderr.write(f"[execution] {exc}\n")
        return 1


__all__ = ["ExecutionScanError", "cmd_execution"]
