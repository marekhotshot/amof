"""Read-only CLI surfaces for inspecting AMOF runtime runs."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..app_paths import runs_dir


class RunsCliError(RuntimeError):
    """Raised when a runs command cannot be completed truthfully."""


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    session_id: str
    studio_session_id: str | None
    ticket_id: str | None
    status: str
    planning_mode: str | None
    started_at: str | None
    finished_at: str | None
    cost_status: str | None
    estimated_cost: float | None
    tokens_in: int | None
    tokens_out: int | None
    receipt_ref: str | None
    session_path: str
    events_path: str
    output_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "studio_session_id": self.studio_session_id,
            "ticket_id": self.ticket_id,
            "status": self.status,
            "planning_mode": self.planning_mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cost_status": self.cost_status,
            "estimated_cost": self.estimated_cost,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "receipt_ref": self.receipt_ref,
            "session_path": self.session_path,
            "events_path": self.events_path,
            "output_path": self.output_path,
        }


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("event_type") or event.get("type") or "").strip()


def _event_timestamp(event: dict[str, Any]) -> str | None:
    value = event.get("timestamp")
    if value is None:
        value = event.get("ts")
    text = str(value or "").strip()
    return text or None


def _normalized_cost_status(value: Any) -> str | None:
    lowered = str(value or "").strip().lower()
    if lowered in {"observed", "unknown"}:
        return lowered
    return None


def _event_sort_key(event: dict[str, Any], index: int) -> tuple[str, int]:
    ts = _event_timestamp(event) or "9999-99-99T99:99:99+00:00"
    return (ts, index)


def _sanitize_event_for_output(event: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(event)
    sanitized.pop("provider_generation_id", None)
    return sanitized


def _read_events(events_path: Path) -> list[dict[str, Any]]:
    if not events_path.exists():
        raise RunsCliError(f"events file not found: {events_path}")
    events: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(_sanitize_event_for_output(payload))
    return events


def _first_nonempty(events: Iterable[dict[str, Any]], key: str) -> str | None:
    for event in events:
        value = str(event.get(key) or "").strip()
        if value:
            return value
    return None


def _last_event_with_cost(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    preferred = {"run_finished", "ial_request_finished", "llm_call"}
    for event in reversed(events):
        event_type = _event_type(event)
        if event_type not in preferred:
            continue
        cost_status = _normalized_cost_status(event.get("cost_status"))
        if cost_status is None:
            continue
        return event
    return None


def _compute_run_summary(events_path: Path) -> RunSummary:
    events = _read_events(events_path)
    session_dir = events_path.parent
    session_id = _first_nonempty(events, "session_id") or session_dir.name
    run_id = _first_nonempty(events, "run_id") or session_dir.name
    studio_session_id = _first_nonempty(events, "studio_session_id")
    ticket_id = _first_nonempty(events, "ticket_id")
    planning_mode = _first_nonempty(events, "planning_mode")
    started_at = None
    finished_at = None
    for event in events:
        ts = _event_timestamp(event)
        if ts and started_at is None:
            started_at = ts
        if ts and _event_type(event) in {"run_finished", "session_end"}:
            finished_at = ts
    status = "finished" if finished_at else "running"

    cost_status = None
    estimated_cost = None
    tokens_in = None
    tokens_out = None
    cost_event = _last_event_with_cost(events)
    if cost_event is not None:
        cost_status = _normalized_cost_status(cost_event.get("cost_status"))
        if cost_status == "observed":
            value = cost_event.get("estimated_cost", cost_event.get("cost"))
            if isinstance(value, (int, float)):
                estimated_cost = float(value)
            tokens_in_value = cost_event.get("tokens_in")
            if tokens_in_value is None:
                tokens_in_value = (cost_event.get("tokens") or {}).get("in")
            tokens_out_value = cost_event.get("tokens_out")
            if tokens_out_value is None:
                tokens_out_value = (cost_event.get("tokens") or {}).get("out")
            if isinstance(tokens_in_value, (int, float)):
                tokens_in = int(tokens_in_value)
            if isinstance(tokens_out_value, (int, float)):
                tokens_out = int(tokens_out_value)
        elif cost_status == "unknown":
            estimated_cost = None

    receipt_ref = None
    for event in reversed(events):
        if _event_type(event) in {"planning_context_receipt_written", "run_finished"}:
            value = str(event.get("receipt_ref") or "").strip()
            if value:
                receipt_ref = value
                break

    output_path = None
    candidate_output = session_dir / "plan-result.json"
    if candidate_output.exists():
        output_path = str(candidate_output)

    return RunSummary(
        run_id=run_id,
        session_id=session_id,
        studio_session_id=studio_session_id,
        ticket_id=ticket_id,
        status=status,
        planning_mode=planning_mode,
        started_at=started_at,
        finished_at=finished_at,
        cost_status=cost_status,
        estimated_cost=estimated_cost,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        receipt_ref=receipt_ref,
        session_path=str(session_dir),
        events_path=str(events_path),
        output_path=output_path,
    )


def _discover_run_summaries() -> list[RunSummary]:
    root = runs_dir()
    if not root.exists():
        return []
    summaries: list[RunSummary] = []
    for events_path in sorted(root.rglob("events.jsonl")):
        if not events_path.is_file():
            continue
        try:
            summary = _compute_run_summary(events_path)
        except RunsCliError:
            continue
        summaries.append(summary)
    summaries.sort(key=lambda item: (item.started_at or "", item.run_id), reverse=True)
    return summaries


def _resolve_run(run_id: str, runs: list[RunSummary]) -> RunSummary:
    matches = [
        run
        for run in runs
        if run.run_id == run_id or run.session_id == run_id or Path(run.session_path).name == run_id
    ]
    if not matches:
        raise RunsCliError(f"run not found: {run_id}")
    if len(matches) > 1:
        raise RunsCliError(f"run id is ambiguous: {run_id}")
    return matches[0]


def _format_cost(run: RunSummary) -> str:
    if run.cost_status == "observed":
        if run.estimated_cost is None:
            return "observed/-"
        return f"observed/{run.estimated_cost:.6f}"
    if run.cost_status == "unknown":
        return "unknown/-"
    return "-"


def _print_runs_table(runs: list[RunSummary]) -> None:
    if not runs:
        print("No runs found.")
        return
    headers = (
        "run_id",
        "session_id",
        "studio_session_id",
        "ticket_id",
        "status",
        "planning_mode",
        "started_at",
        "finished_at",
        "cost",
        "events_path",
    )
    print("\t".join(headers))
    for run in runs:
        print(
            "\t".join(
                (
                    run.run_id,
                    run.session_id,
                    run.studio_session_id or "-",
                    run.ticket_id or "-",
                    run.status,
                    run.planning_mode or "-",
                    run.started_at or "-",
                    run.finished_at or "-",
                    _format_cost(run),
                    run.events_path,
                )
            )
        )


def _print_show(run: RunSummary) -> None:
    pairs = [
        ("run_id", run.run_id),
        ("session_id", run.session_id),
        ("studio_session_id", run.studio_session_id or "-"),
        ("ticket_id", run.ticket_id or "-"),
        ("status", run.status),
        ("planning_mode", run.planning_mode or "-"),
        ("started_at", run.started_at or "-"),
        ("finished_at", run.finished_at or "-"),
        ("cost_status", run.cost_status or "-"),
        (
            "estimated_cost",
            f"{run.estimated_cost:.6f}" if run.cost_status == "observed" and run.estimated_cost is not None else "-",
        ),
        ("tokens_in", str(run.tokens_in) if run.tokens_in is not None else "-"),
        ("tokens_out", str(run.tokens_out) if run.tokens_out is not None else "-"),
        ("receipt_ref", run.receipt_ref or "-"),
        ("session_path", run.session_path),
        ("events_path", run.events_path),
        ("output_path", run.output_path or "-"),
    ]
    for key, value in pairs:
        print(f"{key}: {value}")


def _read_run_events(run: RunSummary) -> list[dict[str, Any]]:
    events = _read_events(Path(run.events_path))
    indexed = list(enumerate(events))
    indexed.sort(key=lambda item: _event_sort_key(item[1], item[0]))
    return [event for _, event in indexed]


def _event_cost_fields(event: dict[str, Any]) -> tuple[str | None, str | None]:
    status = _normalized_cost_status(event.get("cost_status"))
    if status == "observed":
        value = event.get("estimated_cost", event.get("cost"))
        if isinstance(value, (int, float)):
            return status, f"{float(value):.6f}"
        return status, "-"
    if status == "unknown":
        return status, "-"
    return None, None


def _format_event_line(event: dict[str, Any]) -> str:
    ts = _event_timestamp(event) or "-"
    event_type = _event_type(event) or "unknown"
    chunks = [f"{ts} {event_type}"]
    for key in ("event_id", "run_id", "session_id", "ticket_id", "planning_mode"):
        value = event.get(key)
        if value is not None and str(value).strip():
            chunks.append(f"{key}={value}")
    cost_status, cost_value = _event_cost_fields(event)
    if cost_status is not None:
        chunks.append(f"cost_status={cost_status}")
        chunks.append(f"estimated_cost={cost_value}")
    for key in ("tokens_in", "tokens_out", "receipt_ref", "context_file"):
        value = event.get(key)
        if value is not None and str(value).strip():
            chunks.append(f"{key}={value}")
    return " ".join(chunks)


def _print_logs(events: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(events, indent=2))
        return
    for event in events:
        print(_format_event_line(event))


def _tail_events(
    run: RunSummary,
    *,
    lines: int,
    follow: bool,
    poll_seconds: float,
    max_polls: int,
    as_json: bool,
) -> int:
    events = _read_run_events(run)
    if not follow:
        _print_logs(events[-lines:] if lines > 0 else events, as_json=as_json)
        return 0
    if max_polls <= 0:
        raise RunsCliError("tail --max-polls must be greater than zero when --follow is set.")
    if poll_seconds <= 0:
        raise RunsCliError("tail --poll-seconds must be greater than zero when --follow is set.")

    printed = 0
    batch = events[-lines:] if lines > 0 else events
    _print_logs(batch, as_json=as_json)
    printed = len(events)
    polls = 0
    while polls < max_polls:
        polls += 1
        time.sleep(poll_seconds)
        next_events = _read_run_events(run)
        if len(next_events) <= printed:
            continue
        delta = next_events[printed:]
        _print_logs(delta, as_json=as_json)
        printed = len(next_events)
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    action = str(getattr(args, "runs_cmd", "") or "").strip()
    try:
        runs = _discover_run_summaries()
        if action == "list":
            if bool(getattr(args, "json", False)):
                print(json.dumps([run.to_dict() for run in runs], indent=2))
            else:
                _print_runs_table(runs)
            return 0

        if action in {"show", "logs", "tail"}:
            run_ref = str(getattr(args, "run_id", "") or "").strip()
            if not run_ref:
                raise RunsCliError("run_id is required.")
            run = _resolve_run(run_ref, runs)

            if action == "show":
                if bool(getattr(args, "json", False)):
                    print(json.dumps(run.to_dict(), indent=2))
                else:
                    _print_show(run)
                return 0

            if action == "logs":
                events = _read_run_events(run)
                limit = int(getattr(args, "limit", 0) or 0)
                if limit > 0:
                    events = events[-limit:]
                _print_logs(events, as_json=bool(getattr(args, "json", False)))
                return 0

            if action == "tail":
                return _tail_events(
                    run,
                    lines=int(getattr(args, "lines", 20) or 20),
                    follow=bool(getattr(args, "follow", False)),
                    poll_seconds=float(getattr(args, "poll_seconds", 1.0) or 1.0),
                    max_polls=int(getattr(args, "max_polls", 20) or 20),
                    as_json=bool(getattr(args, "json", False)),
                )

        sys.stderr.write("Usage: amof runs {list,show,logs,tail} ...\n")
        return 1
    except RunsCliError as exc:
        sys.stderr.write(f"[runs] {exc}\n")
        return 1


__all__ = ["RunsCliError", "RunSummary", "cmd_runs"]
