"""Run manager: lifecycle and event storage for control plane runs."""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

from amof.orchestrator.merkle import build_compact_merkle_root

if TYPE_CHECKING:
    from amof.api.run_store import RunStore
    from amof.logs import StructuredLogStore
    from amof.queue import QueueStore


logger = logging.getLogger(__name__)

# Status values aligned with roadmap
RUN_STATUS_QUEUED = "queued"
RUN_STATUS_PAUSED = "paused"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCESS = "success"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_VISIBILITY_VISIBLE = "visible"
RUN_VISIBILITY_ARCHIVED = "archived"
RUN_TERMINAL_STATUSES = {
    RUN_STATUS_SUCCESS,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
    "stopped",
    "error",
}


def _clone_session_snapshot(snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return None
    return deepcopy(snapshot)


def _coerce_event_payload(event: Any) -> Dict[str, Any]:
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        return payload
    if isinstance(event, dict):
        maybe_payload = event.get("payload")
        if isinstance(maybe_payload, dict):
            return maybe_payload
    return {}


def _coerce_event_type(event: Any) -> str:
    event_type = getattr(event, "type", None)
    if isinstance(event_type, str):
        return event_type
    if isinstance(event, dict):
        raw = event.get("type")
        if isinstance(raw, str):
            return raw
    return ""


def _coerce_event_message(event: Any) -> str:
    message = getattr(event, "message", None)
    if isinstance(message, str):
        return message
    if isinstance(event, dict):
        raw = event.get("message")
        if isinstance(raw, str):
            return raw
    return ""


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _latest_arena_metadata(events: Optional[Iterable[Any]]) -> Dict[str, Any]:
    for event in reversed(list(events or [])):
        if _coerce_event_type(event) != "arena_context":
            continue
        payload = _coerce_event_payload(event)
        return payload if isinstance(payload, dict) else {}
    return {}


def normalize_run_retention(loop_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    retention = dict((loop_state or {}).get("retention") or {})
    visibility = str(retention.get("visibility") or RUN_VISIBILITY_VISIBLE).strip().lower()
    if visibility != RUN_VISIBILITY_ARCHIVED:
        visibility = RUN_VISIBILITY_VISIBLE
    return {
        "visibility": visibility,
        "archived": visibility == RUN_VISIBILITY_ARCHIVED,
        "archived_at": retention.get("archived_at"),
        "archive_reason": retention.get("archive_reason"),
        "seeded_example": bool(retention.get("seeded_example")),
        "canonical_history": bool(retention.get("canonical_history")),
        "label": retention.get("label"),
    }


def run_is_visible(run: "RunRecord") -> bool:
    return not normalize_run_retention(run.loop_state).get("archived", False)


def summarize_stop_boundary(events: Optional[Iterable[Any]]) -> Dict[str, Any]:
    latest_request: Optional[Dict[str, Any]] = None
    latest_request_index = -1
    latest_honored: Optional[Dict[str, Any]] = None

    for index, event in enumerate(events or []):
        event_type = _coerce_event_type(event)
        payload = _coerce_event_payload(event)
        message = _coerce_event_message(event)

        if event_type == "loop_phase" and (
            payload.get("summary") == "stop_requested"
            or message == "Stop requested between bounded loop steps."
        ):
            latest_request = {
                "phase": payload.get("phase") if isinstance(payload.get("phase"), str) else None,
                "step": _coerce_int(payload.get("step")),
                "summary": "stop_requested",
            }
            latest_request_index = index
            latest_honored = None
            continue

        if latest_request is None or index <= latest_request_index or event_type != "loop_checkpoint":
            continue

        checkpoint_stop_reason = payload.get("stop_reason")
        checkpoint_decision = payload.get("decision")
        checkpoint_summary = payload.get("summary")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        cancel_requested = bool(result.get("cancel_requested"))

        if checkpoint_stop_reason == "cancelled" and cancel_requested:
            latest_honored = {
                "step": _coerce_int(payload.get("step")),
                "decision": checkpoint_decision if isinstance(checkpoint_decision, str) else None,
                "summary": checkpoint_summary if isinstance(checkpoint_summary, str) and checkpoint_summary else None,
                "stop_reason": "cancelled",
            }

    return {
        "stop_boundary_requested": bool(latest_request),
        "stop_boundary_phase": latest_request.get("phase") if latest_request else None,
        "stop_boundary_step": latest_request.get("step") if latest_request else None,
        "stop_boundary_honored": True if latest_honored else (False if latest_request else None),
        "stop_boundary_decision": latest_honored.get("decision") if latest_honored else None,
        "stop_boundary_summary": latest_honored.get("summary") if latest_honored else None,
        "stop_boundary_stop_reason": latest_honored.get("stop_reason") if latest_honored else None,
    }


def _summarize_merkle_roots(
    *,
    run: Optional["RunRecord"],
    state: Dict[str, Any],
    events: Optional[Iterable[Any]],
    latest_evidence: Dict[str, Any],
    latest_decision: Optional[str],
    latest_summary: Optional[str],
    terminal_condition: Optional[str],
    blocker: Optional[str],
) -> Dict[str, Any]:
    arena_metadata = _latest_arena_metadata(events)
    updated_at = (
        run.finished_at
        if run and run.finished_at
        else (run.started_at if run and run.started_at else (run.created_at if run else None))
    )
    effective_loop_state = state.get("loop_state") or ("idle" if run is None else None)
    effective_queue_state = state.get("queue_state") or ("idle" if run is None else None)
    scratch_artifact_paths = list(
        latest_evidence.get("scratch_artifact_paths") or state.get("scratch_artifact_paths") or []
    )
    canonical_write_targets = list(latest_evidence.get("canonical_write_targets") or [])
    promotion_evidence = list(
        latest_evidence.get("promotion_evidence") or state.get("promotion_evidence") or []
    )
    materialization_mode = latest_evidence.get("materialization_mode")
    agent_id = arena_metadata.get("agent_id")
    thread_id = arena_metadata.get("thread_id")
    runtime_entries = {
        "loop_state": effective_loop_state,
        "loop_step": state.get("loop_step"),
        "decision": latest_decision,
        "summary": latest_summary,
        "terminal_condition": terminal_condition,
        "blocker": blocker,
        "stop_reason": state.get("stop_reason"),
        "cancel_requested": state.get("cancel_requested"),
        "queue_state": effective_queue_state,
        "queue_depth": state.get("queue_depth"),
        "run_counts": state.get("run_counts"),
        "run_present": bool(run),
    }
    runtime_root = build_compact_merkle_root(
        "runtime",
        runtime_entries,
        updated_at=updated_at,
    )
    artifact_root = build_compact_merkle_root(
        "artifact",
        {
            "materialization_mode": materialization_mode,
            "promotion_applied": latest_evidence.get("promotion_applied"),
            "scratch_artifact_paths": scratch_artifact_paths,
            "canonical_write_targets": canonical_write_targets,
            "promotion_evidence": promotion_evidence,
            "artifacts_present": bool(
                materialization_mode or scratch_artifact_paths or canonical_write_targets or promotion_evidence
            ),
        },
        updated_at=updated_at,
    )
    agent_root = build_compact_merkle_root(
        "agent",
        {
            "action": run.action if run else None,
            "session_id": run.session_id if run else None,
            "agent_id": agent_id,
            "thread_id": thread_id,
            "arena_mode": arena_metadata.get("arena_mode"),
            "team_id": arena_metadata.get("team_id"),
            "agent_present": bool(run or agent_id or thread_id),
        },
        updated_at=updated_at,
    )
    pipeline_root = build_compact_merkle_root(
        "pipeline",
        {
            "ecosystem": run.ecosystem if run else None,
            "status": run.status if run else None,
            "queue_state": effective_queue_state,
            "exit_code": run.exit_code if run else None,
            "started_at": run.started_at if run else None,
            "finished_at": run.finished_at if run else None,
            "runtime_profile": state.get("runtime_profile"),
            "task_id": state.get("task_id"),
            "run_present": bool(run),
        },
        updated_at=updated_at,
    )
    return {
        "runtime_merkle_root": runtime_root,
        "artifact_merkle_root": artifact_root,
        "agent_merkle_root": agent_root,
        "pipeline_merkle_root": pipeline_root,
    }


def summarize_loop_state(
    loop_state: Optional[Dict[str, Any]],
    events: Optional[Iterable[Any]] = None,
    run: Optional["RunRecord"] = None,
) -> Dict[str, Any]:
    state = dict(loop_state or {})
    latest_evidence = dict(state.get("latest_evidence") or {})
    latest_decision = (
        latest_evidence.get("decision")
        or state.get("decision")
    )
    latest_summary = (
        latest_evidence.get("summary")
        or state.get("last_result_summary")
    )
    terminal_condition = latest_evidence.get("terminal_condition")
    blocker = latest_evidence.get("blocker")
    blocker_summary = (
        latest_evidence.get("blocker_summary")
        or (latest_summary if blocker else None)
    )
    materialization_mode = latest_evidence.get("materialization_mode")
    promotion_applied = latest_evidence.get("promotion_applied")
    scratch_artifact_paths = list(latest_evidence.get("scratch_artifact_paths") or state.get("scratch_artifact_paths") or [])
    canonical_write_targets = list(latest_evidence.get("canonical_write_targets") or [])
    promotion_evidence = list(latest_evidence.get("promotion_evidence") or state.get("promotion_evidence") or [])
    pause_mode = latest_evidence.get("pause_mode") or state.get("pause_mode")
    required_choice = latest_evidence.get("required_choice") or state.get("required_choice")
    forced_choices = list(latest_evidence.get("forced_choices") or state.get("forced_choices") or [])
    handoff = latest_evidence.get("handoff") or state.get("handoff")
    runtime_mode = state.get("runtime_mode")
    batch_id = state.get("batch_id")
    batch_state = state.get("batch_state")
    batch_cursor = state.get("batch_cursor")
    current_item_id = state.get("current_item_id")
    current_child_run_id = state.get("current_child_run_id")
    child_run_ids = list(state.get("child_run_ids") or [])
    completed_items = list(state.get("completed_items") or [])
    persistence_store_status = state.get("persistence_store_status")
    persistence_warning_active = state.get("persistence_warning_active")
    persistence_warning = state.get("persistence_warning")
    last_persistence_error = state.get("last_persistence_error")
    last_persistence_error_at = state.get("last_persistence_error_at")
    last_persisted_at = state.get("last_persisted_at")
    persistence_error_count = state.get("persistence_error_count")
    summary = {
        "latest_evidence": latest_evidence,
        "latest_decision": latest_decision,
        "latest_summary": latest_summary,
        "terminal_condition": terminal_condition,
        "blocker": blocker,
        "blocker_summary": blocker_summary,
        "materialization_mode": materialization_mode,
        "promotion_applied": promotion_applied,
        "scratch_artifact_paths": scratch_artifact_paths,
        "canonical_write_targets": canonical_write_targets,
        "promotion_evidence": promotion_evidence,
        "pause_mode": pause_mode,
        "required_choice": required_choice,
        "forced_choices": forced_choices,
        "handoff": dict(handoff) if isinstance(handoff, dict) else {},
        "runtime_mode": runtime_mode,
        "batch_id": batch_id,
        "batch_state": batch_state,
        "batch_cursor": batch_cursor,
        "current_item_id": current_item_id,
        "current_child_run_id": current_child_run_id,
        "child_run_ids": child_run_ids,
        "completed_items": completed_items,
        "persistence_store_status": persistence_store_status,
        "persistence_warning_active": persistence_warning_active,
        "persistence_warning": persistence_warning,
        "last_persistence_error": last_persistence_error,
        "last_persistence_error_at": last_persistence_error_at,
        "last_persisted_at": last_persisted_at,
        "persistence_error_count": persistence_error_count,
    }
    summary.update(
        _summarize_merkle_roots(
            run=run,
            state=state,
            events=events,
            latest_evidence=latest_evidence,
            latest_decision=latest_decision if isinstance(latest_decision, str) else None,
            latest_summary=latest_summary if isinstance(latest_summary, str) else None,
            terminal_condition=terminal_condition if isinstance(terminal_condition, str) else None,
            blocker=blocker if isinstance(blocker, str) else None,
        )
    )
    summary.update(summarize_stop_boundary(events))
    return summary


@dataclass
class RunEvent:
    """Single event in a run (log line or state change)."""
    timestamp: str  # ISO format
    level: str  # e.g. info, error
    type: str  # e.g. log, state, exit
    message: str
    run_id: str
    payload: Optional[Dict[str, Any]] = None


@dataclass
class RunRecord:
    """One run: metadata and event log. Optional session_id links run to a conversation session."""
    run_id: str
    ecosystem: str
    action: str
    command: List[str]
    status: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    events: List[RunEvent] = field(default_factory=list)
    session_id: Optional[str] = None
    session_snapshot: Optional[Dict[str, Any]] = None
    loop_state: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        summary = summarize_loop_state(self.loop_state, self.events, self)
        retention = normalize_run_retention(self.loop_state)
        session_snapshot = _clone_session_snapshot(self.session_snapshot)
        session_telemetry = session_snapshot.get("telemetry") if isinstance(session_snapshot, dict) else None
        session_messages = session_snapshot.get("messages") if isinstance(session_snapshot, dict) else None
        return {
            "run_id": self.run_id,
            "ecosystem": self.ecosystem,
            "action": self.action,
            "command": self.command,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "session_id": self.session_id,
            "session_state": session_snapshot.get("session_state") if isinstance(session_snapshot, dict) else None,
            "session_message": session_snapshot.get("message") if isinstance(session_snapshot, dict) else None,
            "session_has_telemetry": bool(isinstance(session_telemetry, dict) and session_telemetry),
            "session_has_messages": bool(isinstance(session_messages, list) and session_messages),
            "loop_state": dict(self.loop_state or {}),
            "retention": retention,
            **summary,
            "logs": [e.message for e in self.events if e.type == "log"],
            "events": [
                {
                    "timestamp": e.timestamp,
                    "level": e.level,
                    "type": e.type,
                    "message": e.message,
                    "run_id": e.run_id,
                    "payload": e.payload,
                }
                for e in self.events
            ],
        }


class RunManager:
    """Central store for runs; single source of truth for API. Optional persistence via RunStore."""

    def __init__(
        self,
        store: Optional["RunStore"] = None,
        queue_store: Optional["QueueStore"] = None,
        log_store: Optional["StructuredLogStore"] = None,
    ) -> None:
        self._runs: Dict[str, RunRecord] = {}
        self._store = store
        self._queue_store = queue_store
        self._log_store = log_store

    def _persist(self, run: RunRecord) -> None:
        if not self._store:
            return
        now = datetime.utcnow().isoformat() + "Z"
        state = dict(run.loop_state or {})
        state["last_persist_attempt_at"] = now
        state.setdefault("persistence_error_count", 0)
        state["persistence_store_status"] = "healthy"
        state["persistence_warning_active"] = False
        state["persistence_warning"] = None
        state["last_persisted_at"] = now
        run.loop_state = state
        try:
            self._store.save(run)
        except Exception as exc:
            degraded_state = dict(run.loop_state or {})
            degraded_state["persistence_store_status"] = "degraded"
            degraded_state["persistence_warning_active"] = True
            degraded_state["persistence_warning"] = f"Run persistence failed: {exc}"
            degraded_state["last_persistence_error"] = str(exc)
            degraded_state["last_persistence_error_at"] = now
            degraded_state["persistence_error_count"] = int(degraded_state.get("persistence_error_count") or 0) + 1
            run.loop_state = degraded_state
            logger.exception("Run persistence failed for %s", run.run_id)

    def create_run(
        self,
        ecosystem: str,
        action: str,
        command: List[str],
        session_id: Optional[str] = None,
        queue_payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat() + "Z"
        run = RunRecord(
            run_id=run_id,
            ecosystem=ecosystem,
            action=action,
            command=command,
            status=RUN_STATUS_QUEUED,
            created_at=now,
            session_id=session_id,
        )
        self._runs[run_id] = run
        self._persist(run)
        if self._queue_store:
            from amof.queue import QueueTaskRecord

            self._queue_store.save(
                QueueTaskRecord(
                    run_id=run_id,
                    ecosystem=ecosystem,
                    action=action,
                    status=self._queue_status_for_run(RUN_STATUS_QUEUED),
                    created_at=now,
                    updated_at=now,
                    session_id=session_id,
                    payload=dict(queue_payload or {}),
                )
            )
        return run_id

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        cached = self._runs.get(run_id)
        if self._store:
            persisted = self._store.load(run_id)
            if persisted and self._should_refresh_cached_run(cached, persisted):
                self._runs[run_id] = persisted
                return persisted
            if cached:
                return cached
            if persisted:
                self._runs[run_id] = persisted
                return persisted
            return None
        if cached:
            return cached
        return None

    def list_runs(
        self,
        ecosystem: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> List[RunRecord]:
        run_ids = set(self._runs)
        if self._store:
            run_ids |= set(self._store.list_run_ids())
        runs = []
        for rid in run_ids:
            r = self.get_run(rid)
            if r:
                runs.append(r)
        if ecosystem is not None:
            runs = [r for r in runs if r.ecosystem == ecosystem]
        if status is not None:
            runs = [r for r in runs if r.status == status]
        if not include_archived:
            runs = [r for r in runs if run_is_visible(r)]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    def list_runs_summary(
        self,
        ecosystem: Optional[str] = None,
        status: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> List[RunRecord]:
        runs_by_id: Dict[str, RunRecord] = {}
        for run in self._runs.values():
            if ecosystem is not None and run.ecosystem != ecosystem:
                continue
            if status is not None and run.status != status:
                continue
            if action is not None and run.action != action:
                continue
            runs_by_id[run.run_id] = run
        if self._store and hasattr(self._store, "list_runs_summary"):
            for run in self._store.list_runs_summary(ecosystem=ecosystem, action=action, status=status, limit=limit * 2):
                cached = runs_by_id.get(run.run_id)
                if self._should_refresh_cached_run(cached, run):
                    runs_by_id[run.run_id] = run
                else:
                    runs_by_id.setdefault(run.run_id, run)
        runs = list(runs_by_id.values())
        if not include_archived:
            runs = [run for run in runs if run_is_visible(run)]
        runs.sort(key=lambda run: run.created_at, reverse=True)
        return runs[:limit]

    @staticmethod
    def _status_is_terminal(status: Optional[str]) -> bool:
        return str(status or "").strip().lower() in RUN_TERMINAL_STATUSES

    @classmethod
    def _should_refresh_cached_run(
        cls,
        cached: Optional[RunRecord],
        persisted: Optional[RunRecord],
    ) -> bool:
        if persisted is None:
            return False
        if cached is None:
            return True

        cached_terminal = cls._status_is_terminal(cached.status)
        persisted_terminal = cls._status_is_terminal(persisted.status)

        # Terminal persisted truth must beat a stale in-memory active record,
        # especially after a restart or when another writer finalized the run.
        if persisted_terminal and not cached_terminal:
            return True
        if persisted.finished_at and not cached.finished_at:
            return True
        if persisted.status != cached.status and persisted_terminal:
            return True

        # If both records are otherwise active, prefer the persisted copy only
        # when it is strictly richer than the cache.
        if len(persisted.events) > len(cached.events):
            return True
        return False

    def update_retention(
        self,
        run_id: str,
        *,
        visibility: Optional[str] = None,
        seeded_example: Optional[bool] = None,
        canonical_history: Optional[bool] = None,
        label: Optional[str] = None,
        archive_reason: Optional[str] = None,
        archived_at: Optional[str] = None,
    ) -> Optional[RunRecord]:
        run = self._runs.get(run_id) or self.get_run(run_id)
        if not run:
            return None
        next_loop_state = dict(run.loop_state or {})
        retention = dict(next_loop_state.get("retention") or {})
        if visibility is not None:
            normalized_visibility = str(visibility or "").strip().lower()
            retention["visibility"] = RUN_VISIBILITY_ARCHIVED if normalized_visibility == RUN_VISIBILITY_ARCHIVED else RUN_VISIBILITY_VISIBLE
        if seeded_example is not None:
            retention["seeded_example"] = bool(seeded_example)
        if canonical_history is not None:
            retention["canonical_history"] = bool(canonical_history)
        if label is not None:
            retention["label"] = label
        if archive_reason is not None:
            retention["archive_reason"] = archive_reason
        if archived_at is not None:
            retention["archived_at"] = archived_at
        elif retention.get("visibility") == RUN_VISIBILITY_ARCHIVED and not retention.get("archived_at"):
            retention["archived_at"] = datetime.utcnow().isoformat() + "Z"
        elif retention.get("visibility") != RUN_VISIBILITY_ARCHIVED:
            retention["archived_at"] = None
        next_loop_state["retention"] = retention
        run.loop_state = next_loop_state
        self._persist(run)
        return run

    def update_status(
        self,
        run_id: str,
        status: str,
        exit_code: Optional[int] = None,
    ) -> None:
        run = self._runs.get(run_id) or self.get_run(run_id)
        if not run:
            return
        run.status = status
        now = datetime.utcnow().isoformat() + "Z"
        if status == RUN_STATUS_RUNNING and run.started_at is None:
            run.started_at = now
        if status in (RUN_STATUS_SUCCESS, RUN_STATUS_FAILED, RUN_STATUS_CANCELLED):
            run.finished_at = now
            run.exit_code = exit_code
            run.loop_state = self._finalize_terminal_loop_state(run, status)
        self._persist(run)
        if self._queue_store:
            from amof.queue import QueueTaskRecord

            existing = self._queue_store.load(run_id)
            created_at = existing.created_at if existing else run.created_at
            control = dict(existing.control) if existing else {}
            control["loop_state"] = dict(run.loop_state or {})
            control.update(summarize_loop_state(run.loop_state, run.events, run))
            self._queue_store.save(
                QueueTaskRecord(
                    run_id=run.run_id,
                    ecosystem=run.ecosystem,
                    action=run.action,
                    status=self._queue_status_for_run(status),
                    created_at=created_at,
                    updated_at=now,
                    session_id=run.session_id,
                    payload=dict(existing.payload) if existing else {},
                    control=control,
                )
            )

    def append_event(
        self,
        run_id: str,
        level: str,
        type: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        run = self._runs.get(run_id)
        if not run:
            return
        now = datetime.utcnow().isoformat() + "Z"
        run.events.append(
            RunEvent(timestamp=now, level=level, type=type, message=message, run_id=run_id, payload=payload)
        )
        self._persist(run)
        if self._log_store:
            from amof.logs import StructuredLogRecord

            step = len(run.events)
            model = None
            tokens = None
            hash_value = None
            phase = None
            cost = None
            elapsed_ms = None
            decision = None
            summary = None
            error = None
            stop_reason = None
            evidence = None
            if isinstance(payload, dict):
                model = payload.get("model")
                tokens = payload.get("tokens")
                hash_value = payload.get("hash")
                phase = payload.get("phase")
                cost = payload.get("cost")
                elapsed_ms = payload.get("elapsed_ms")
                decision = payload.get("decision")
                summary = payload.get("summary")
                error = payload.get("error")
                stop_reason = payload.get("stop_reason")
                evidence = payload.get("evidence")
            self._log_store.append(
                StructuredLogRecord(
                    run_id=run_id,
                    step=step,
                    event_type=type,
                    message=message,
                    phase=phase if isinstance(phase, str) else None,
                    model=model if isinstance(model, str) else None,
                    tokens=tokens if isinstance(tokens, dict) else None,
                    cost=float(cost) if isinstance(cost, (float, int)) else None,
                    elapsed_ms=int(elapsed_ms) if isinstance(elapsed_ms, (float, int)) else None,
                    decision=decision if isinstance(decision, str) else None,
                    summary=summary if isinstance(summary, str) else None,
                    error=error if isinstance(error, str) else None,
                    stop_reason=stop_reason if isinstance(stop_reason, str) else None,
                    result=payload if isinstance(payload, dict) else None,
                    evidence=evidence if isinstance(evidence, dict) else None,
                    hash=hash_value if isinstance(hash_value, str) else None,
                )
            )
        if self._queue_store:
            existing = self._queue_store.load(run_id)
            if existing is not None:
                existing.control.update(summarize_loop_state(run.loop_state, run.events, run))
                existing.updated_at = datetime.utcnow().isoformat() + "Z"
                self._queue_store.save(existing)

    def append_log(self, run_id: str, line: str) -> None:
        self.append_event(run_id, level="info", type="log", message=line)

    def update_loop_state(self, run_id: str, loop_state: Dict[str, Any]) -> None:
        run = self._runs.get(run_id) or self.get_run(run_id)
        if not run:
            return
        run.loop_state = dict(loop_state or {})
        self._persist(run)
        if self._queue_store:
            existing = self._queue_store.load(run_id)
            if existing is not None:
                effective_loop_state = dict(run.loop_state or {})
                existing.control["loop_state"] = effective_loop_state
                existing.control.update(summarize_loop_state(effective_loop_state, run.events, run))
                existing.updated_at = datetime.utcnow().isoformat() + "Z"
                self._queue_store.save(existing)

    def update_session_snapshot(self, run_id: str, snapshot: Optional[Dict[str, Any]]) -> None:
        run = self._runs.get(run_id) or self.get_run(run_id)
        if not run:
            return
        run.session_snapshot = _clone_session_snapshot(snapshot)
        self._persist(run)
        if self._queue_store:
            existing = self._queue_store.load(run_id)
            if existing is not None:
                if isinstance(run.session_snapshot, dict):
                    existing.control["session_state"] = run.session_snapshot.get("session_state")
                    existing.control["session_has_messages"] = bool(run.session_snapshot.get("messages"))
                    telemetry = run.session_snapshot.get("telemetry")
                    existing.control["session_has_telemetry"] = bool(isinstance(telemetry, dict) and telemetry)
                existing.updated_at = datetime.utcnow().isoformat() + "Z"
                self._queue_store.save(existing)

    @property
    def queue_store(self) -> Optional["QueueStore"]:
        return self._queue_store

    def _queue_status_for_run(self, status: str) -> str:
        from amof.queue import (
            QUEUE_STATUS_CANCELLED,
            QUEUE_STATUS_DONE,
            QUEUE_STATUS_ERROR,
            QUEUE_STATUS_PAUSED,
            QUEUE_STATUS_PENDING,
            QUEUE_STATUS_RUNNING,
        )

        mapping = {
            RUN_STATUS_QUEUED: QUEUE_STATUS_PENDING,
            RUN_STATUS_PAUSED: QUEUE_STATUS_PAUSED,
            RUN_STATUS_RUNNING: QUEUE_STATUS_RUNNING,
            RUN_STATUS_SUCCESS: QUEUE_STATUS_DONE,
            RUN_STATUS_FAILED: QUEUE_STATUS_ERROR,
            RUN_STATUS_CANCELLED: QUEUE_STATUS_CANCELLED,
        }
        return mapping.get(status, QUEUE_STATUS_PENDING)

    def _finalize_terminal_loop_state(self, run: RunRecord, status: str) -> Dict[str, Any]:
        state = dict(run.loop_state or {})
        state.setdefault("run_id", run.run_id)
        state.setdefault("task_id", run.run_id)
        if status == RUN_STATUS_SUCCESS:
            state["queue_state"] = "done"
            state["loop_state"] = "completed"
            state["decision"] = "DONE"
            state["stop_reason"] = "done"
        elif status == RUN_STATUS_CANCELLED:
            state["queue_state"] = "cancelled"
            state["loop_state"] = "cancelled"
            state["decision"] = "STOP"
            state["stop_reason"] = "cancelled"
            state["cancel_requested"] = True
        elif status == RUN_STATUS_FAILED:
            state["queue_state"] = "error"
            state["loop_state"] = "failed"
            state["decision"] = "STOP"
            state.setdefault("stop_reason", "failed")
        return state
