"""Bounded long-run loop commands (run, show, logs) with no dispatch/mutation."""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from ..app_paths import get_app_paths, runs_dir
from ..orchestrator.events import EventLog
from .execution import cmd_execution
from .intake import IntakeCliError, _is_read_only_intake, _resolve_context_fail_closed, _validate_packet

TICKET_ID = "AMOF-300-LONG-RUN-BOUNDED-LOOPS-001"
MUTATION_STATUS = "NO_MUTATION_PERFORMED"
DISPATCH_STATUS = "NO_REMOTE_EXECUTION_DISPATCHED"
LOOP_PLANNING_MODE = "bounded_loop_no_execution"
STOP_REASONS = {
    "max_loops_reached",
    "terminal_success_condition_met",
    "fail_closed_gate_triggered",
    "data_contract_invalid",
    "operator_cancelled",
}
FAILURE_CLASSES = {
    "context_resolution_failure",
    "intake_validation_failure",
    "runner_eligibility_failure",
    "execution_scan_failure",
    "artifact_write_failure",
    "policy_violation_blocked",
}


class LoopCliError(RuntimeError):
    """Raised when loop command execution cannot be completed truthfully."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise LoopCliError("payload must be a JSON/YAML object")
    return parsed


def _intake_submissions_dir() -> Path:
    return get_app_paths().data_root / "intake" / "submissions"


def _runner_registry_dir() -> Path:
    return get_app_paths().data_root / "runners" / "registry"


def _loop_runs_root() -> Path:
    return runs_dir() / "loops"


def _loop_session_dir(loop_run_id: str) -> Path:
    return _loop_runs_root() / loop_run_id


def _loop_report_path(loop_run_id: str) -> Path:
    return _loop_session_dir(loop_run_id) / "report.json"


def _resolve_intake_reference(reference: str) -> tuple[dict[str, Any], str]:
    candidate = Path(reference)
    if candidate.exists():
        return _read_payload(candidate), str(candidate)

    submission_path = _intake_submissions_dir() / f"{reference}.json"
    if not submission_path.exists():
        raise LoopCliError(f"intake reference not found: {reference}")
    try:
        submission = json.loads(submission_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoopCliError(f"intake submission record is invalid: {reference}") from exc
    if not isinstance(submission, dict):
        raise LoopCliError(f"intake submission record is invalid: {reference}")
    packet_path = Path(str(submission.get("packet_path") or "").strip())
    if not str(packet_path):
        raise LoopCliError(f"intake submission missing packet path: {reference}")
    return _read_payload(packet_path), str(packet_path)


def _loop_run_id_for(intake_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_intake = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in intake_id).strip("-") or "intake"
    return f"loop-{stamp}-{safe_intake}"


def _load_runner_candidates() -> list[dict[str, str]]:
    root = _runner_registry_dir()
    if not root.exists():
        return []
    candidates: list[dict[str, str]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        candidates.append(
            {
                "runner_id": str(payload.get("runner_id") or ""),
                "context": str(payload.get("context") or ""),
                "status": str(payload.get("status") or ""),
            }
        )
    return candidates


def _run_execution_scan_json(intake_ref: str) -> dict[str, Any]:
    scan_args = SimpleNamespace(
        execution_cmd="scan",
        intake_ref=intake_ref,
        scan_id=None,
        json=True,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_execution(scan_args)
    if code != 0:
        raise LoopCliError(f"execution scan failed: {stderr.getvalue().strip() or 'unknown error'}")
    text = stdout.getvalue().strip()
    if not text:
        raise LoopCliError("execution scan produced empty output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LoopCliError("execution scan produced invalid JSON output") from exc
    if not isinstance(payload, dict):
        raise LoopCliError("execution scan produced invalid payload")
    return payload


def _event_base(
    *,
    intake_id: str,
    context: str,
    iteration: int | None = None,
    scan_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticket_id": TICKET_ID,
        "intake_id": intake_id,
        "context": context,
        "mutation_status": MUTATION_STATUS,
        "dispatch_status": DISPATCH_STATUS,
    }
    if iteration is not None:
        payload["loop_iteration"] = iteration
    if scan_id:
        payload["scan_id"] = scan_id
    return payload


def _write_report(report: dict[str, Any]) -> Path:
    loop_run_id = str(report.get("run_id") or "").strip()
    if not loop_run_id:
        raise LoopCliError("internal error: missing loop run id for report write")
    path = _loop_report_path(loop_run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def _read_report(loop_run_id: str) -> dict[str, Any]:
    path = _loop_report_path(loop_run_id)
    if not path.exists():
        raise LoopCliError(f"loop report not found: {loop_run_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoopCliError(f"loop report is invalid: {loop_run_id}") from exc
    if not isinstance(payload, dict):
        raise LoopCliError(f"loop report is invalid: {loop_run_id}")
    return payload


def _terminal_stop_policy(packet: dict[str, Any]) -> bool:
    stop_policy = packet.get("stop_policy")
    if not isinstance(stop_policy, dict):
        return False
    value = stop_policy.get("terminal_on_ready")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "on"}
    return False


def _failure_class_from_scan(scan_report: dict[str, Any]) -> str:
    reasons = [str(item).lower() for item in (scan_report.get("blocked_reasons") or [])]
    for reason in reasons:
        if "context" in reason:
            return "context_resolution_failure"
        if "eligible runner" in reason:
            return "runner_eligibility_failure"
        if "planning-only" in reason or "read_only" in reason or "remote execution" in reason or "dispatch" in reason:
            return "policy_violation_blocked"
    return "execution_scan_failure"


def _cmd_run(args: argparse.Namespace) -> int:
    intake_ref = str(getattr(args, "intake_ref", "") or "").strip()
    if not intake_ref:
        raise LoopCliError("intake reference is required")
    max_loops = int(getattr(args, "max_loops", 0) or 0)
    if max_loops < 1:
        raise LoopCliError("--max-loops must be >= 1")

    try:
        intake_payload, packet_ref = _resolve_intake_reference(intake_ref)
        validated = _validate_packet(intake_payload)
    except IntakeCliError as exc:
        raise LoopCliError(f"intake validation failed: {exc}") from exc
    if not _is_read_only_intake(validated):
        raise LoopCliError(
            "loop run supports planning-only intake only (mutations.allowed must be empty and read_only gate must stop)"
        )

    try:
        context, _context_source = _resolve_context_fail_closed()
    except IntakeCliError as exc:
        raise LoopCliError(str(exc)) from exc

    loop_run_id = _loop_run_id_for(validated.intake_id)
    session_dir = _loop_session_dir(loop_run_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    events = EventLog(
        session_id=loop_run_id,
        runs_dir=_loop_runs_root(),
        run_id=loop_run_id,
        ticket_id=TICKET_ID,
        planning_mode=LOOP_PLANNING_MODE,
        context=context,
        actor="amof.loop",
    )
    runner_candidates = _load_runner_candidates()
    report: dict[str, Any] = {
        "run_id": loop_run_id,
        "ticket_id": TICKET_ID,
        "intake_id": validated.intake_id,
        "context": context,
        "runner_candidates": runner_candidates,
        "scan_ids": [],
        "loop_count": 0,
        "max_loops": max_loops,
        "status": "running",
        "stop_reason": "-",
        "cost_status": "unknown",
        "events_path": str(events.log_path),
        "reports_path": str(_loop_runs_root()),
        "receipts_path": None,
        "final_verdict": "running",
        "mutation_status": MUTATION_STATUS,
        "dispatch_status": DISPATCH_STATUS,
        "packet_ref": packet_ref,
        "started_at": _now_iso(),
    }
    _write_report(report)

    events.log(
        "loop_run_created",
        **_event_base(intake_id=validated.intake_id, context=context),
        loop_run_id=loop_run_id,
        max_loops=max_loops,
        packet_ref=packet_ref,
    )

    terminal_on_ready = _terminal_stop_policy(intake_payload)
    stop_reason = ""
    failure_class: str | None = None
    status = "completed"
    scan_ids: list[str] = []
    scan_report_paths: list[str] = []
    observed_cost: float | None = None
    any_blocked = False

    for iteration in range(1, max_loops + 1):
        events.log(
            "loop_iteration_started",
            **_event_base(intake_id=validated.intake_id, context=context, iteration=iteration),
        )
        try:
            scan_payload = _run_execution_scan_json(intake_ref)
        except LoopCliError:
            stop_reason = "fail_closed_gate_triggered"
            failure_class = "execution_scan_failure"
            status = "blocked"
            any_blocked = True
            events.log(
                "loop_run_blocked",
                **_event_base(intake_id=validated.intake_id, context=context, iteration=iteration),
                stop_reason=stop_reason,
                failure_class=failure_class,
            )
            break

        scan_id = str(scan_payload.get("scan_id") or "").strip()
        if not scan_id:
            stop_reason = "fail_closed_gate_triggered"
            failure_class = "execution_scan_failure"
            status = "blocked"
            any_blocked = True
            events.log(
                "loop_run_blocked",
                **_event_base(intake_id=validated.intake_id, context=context, iteration=iteration),
                stop_reason=stop_reason,
                failure_class=failure_class,
                detail="scan_id missing from execution scan payload",
            )
            break
        scan_ids.append(scan_id)
        scan_report_path = str(scan_payload.get("report_path") or "")
        if scan_report_path:
            scan_report_paths.append(scan_report_path)

        scan_status = str(scan_payload.get("status") or "").strip().lower()
        events.log(
            "loop_execution_scan_created",
            **_event_base(intake_id=validated.intake_id, context=context, iteration=iteration, scan_id=scan_id),
            scan_status=scan_status or "unknown",
            scan_report_path=scan_report_path or None,
        )
        events.log(
            "loop_iteration_completed",
            **_event_base(intake_id=validated.intake_id, context=context, iteration=iteration, scan_id=scan_id),
            scan_status=scan_status or "unknown",
        )

        report["loop_count"] = iteration

        if scan_status != "ready":
            stop_reason = "fail_closed_gate_triggered"
            failure_class = _failure_class_from_scan(scan_payload)
            if failure_class not in FAILURE_CLASSES:
                failure_class = "execution_scan_failure"
            status = "blocked"
            any_blocked = True
            events.log(
                "loop_run_blocked",
                **_event_base(intake_id=validated.intake_id, context=context, iteration=iteration, scan_id=scan_id),
                stop_reason=stop_reason,
                failure_class=failure_class,
                blocked_reasons=scan_payload.get("blocked_reasons") or [],
            )
            events.log(
                "loop_stop_condition_evaluated",
                **_event_base(intake_id=validated.intake_id, context=context, iteration=iteration, scan_id=scan_id),
                stop_reason=stop_reason,
                status=status,
            )
            break

        if terminal_on_ready:
            stop_reason = "terminal_success_condition_met"
            status = "completed"
            events.log(
                "loop_stop_condition_evaluated",
                **_event_base(intake_id=validated.intake_id, context=context, iteration=iteration, scan_id=scan_id),
                stop_reason=stop_reason,
                status=status,
            )
            break

        if iteration >= max_loops:
            stop_reason = "max_loops_reached"
            status = "completed"
            events.log(
                "loop_stop_condition_evaluated",
                **_event_base(intake_id=validated.intake_id, context=context, iteration=iteration, scan_id=scan_id),
                stop_reason=stop_reason,
                status=status,
            )
            break

    if not stop_reason:
        stop_reason = "data_contract_invalid"
        status = "blocked"
        failure_class = "artifact_write_failure"
        any_blocked = True

    report["scan_ids"] = scan_ids
    report["scan_report_paths"] = scan_report_paths
    report["status"] = status
    report["stop_reason"] = stop_reason
    report["loop_count"] = int(report.get("loop_count") or 0)
    report["finished_at"] = _now_iso()
    report["final_verdict"] = (
        "bounded_loop_completed_no_execution"
        if status == "completed"
        else "bounded_loop_blocked_fail_closed"
    )
    if failure_class:
        report["failure_class"] = failure_class
    report["cost_status"] = "blocked" if any_blocked else "unknown"
    if observed_cost is not None:
        report["cost_status"] = "observed"
        report["estimated_cost"] = observed_cost
    report["mutation_status"] = MUTATION_STATUS
    report["dispatch_status"] = DISPATCH_STATUS

    report_path = _write_report(report)
    events.log(
        "loop_run_finished",
        **_event_base(intake_id=validated.intake_id, context=context),
        loop_count=report["loop_count"],
        max_loops=max_loops,
        stop_reason=stop_reason,
        status=status,
        report_path=str(report_path),
        cost_status=report["cost_status"],
        estimated_cost=report.get("estimated_cost"),
    )
    events.log(
        "run_finished",
        **_event_base(intake_id=validated.intake_id, context=context),
        status=status,
        stop_reason=stop_reason,
        receipt_ref=str(report_path),
        cost_status=report["cost_status"],
        estimated_cost=report.get("estimated_cost"),
    )

    if bool(getattr(args, "json", False)):
        print(json.dumps(report, indent=2))
    else:
        print(
            f"LOOP_RUN run_id={loop_run_id} intake_id={validated.intake_id} loop_count={report['loop_count']} "
            f"max_loops={max_loops} status={status} stop_reason={stop_reason}"
        )
        print(f"report_path={report_path}")
        print(f"events_path={events.log_path}")
        print(MUTATION_STATUS)
        print(DISPATCH_STATUS)

    return 0 if status == "completed" else 1


def _cmd_show(args: argparse.Namespace) -> int:
    loop_run_id = str(getattr(args, "loop_run_id", "") or "").strip()
    if not loop_run_id:
        raise LoopCliError("loop_run_id is required")
    payload = _read_report(loop_run_id)
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2))
        return 0
    for key in (
        "run_id",
        "ticket_id",
        "intake_id",
        "context",
        "loop_count",
        "max_loops",
        "status",
        "stop_reason",
        "cost_status",
        "estimated_cost",
        "events_path",
        "reports_path",
        "receipts_path",
        "final_verdict",
        "mutation_status",
        "dispatch_status",
    ):
        value = payload.get(key)
        if value is None or value == "":
            value = "-"
        print(f"{key}: {value}")
    scan_ids = payload.get("scan_ids")
    if isinstance(scan_ids, list):
        print(f"scan_count: {len(scan_ids)}")
        for scan_id in scan_ids:
            print(f"- scan_id={scan_id}")
    return 0


def _format_event_line(event: dict[str, Any]) -> str:
    ts = str(event.get("timestamp") or event.get("ts") or "-")
    event_type = str(event.get("event_type") or event.get("type") or "unknown")
    chunks = [f"{ts} {event_type}"]
    for key in (
        "event_id",
        "run_id",
        "ticket_id",
        "intake_id",
        "context",
        "loop_iteration",
        "scan_id",
        "status",
        "stop_reason",
        "cost_status",
        "estimated_cost",
        "mutation_status",
        "dispatch_status",
    ):
        value = event.get(key)
        if value is None or str(value).strip() == "":
            continue
        chunks.append(f"{key}={value}")
    return " ".join(chunks)


def _cmd_logs(args: argparse.Namespace) -> int:
    loop_run_id = str(getattr(args, "loop_run_id", "") or "").strip()
    if not loop_run_id:
        raise LoopCliError("loop_run_id is required")
    events_path = _loop_session_dir(loop_run_id) / "events.jsonl"
    if not events_path.exists():
        raise LoopCliError(f"loop events not found: {loop_run_id}")
    events: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    limit = int(getattr(args, "limit", 0) or 0)
    if limit > 0:
        events = events[-limit:]
    if bool(getattr(args, "json", False)):
        print(json.dumps(events, indent=2))
    else:
        for event in events:
            print(_format_event_line(event))
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    action = str(getattr(args, "loop_cmd", "") or "").strip()
    try:
        if action == "run":
            return _cmd_run(args)
        if action == "show":
            return _cmd_show(args)
        if action == "logs":
            return _cmd_logs(args)
        sys.stderr.write("Usage: amof loop {run,show,logs} ...\n")
        return 1
    except LoopCliError as exc:
        sys.stderr.write(f"[loop] {exc}\n")
        return 1


__all__ = ["LoopCliError", "cmd_loop"]
