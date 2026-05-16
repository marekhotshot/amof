"""Durable serial queue store and state helpers."""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .models import (
    QUEUE_STATUS_CANCELLED,
    QUEUE_STATUS_DONE,
    QUEUE_STATUS_ERROR,
    QUEUE_STATUS_PAUSED,
    QUEUE_STATUS_PENDING,
    QUEUE_STATUS_RUNNING,
    QueueTaskRecord,
)


logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueTransitionError(ValueError):
    """Raised when an invalid queue state transition is requested."""


class QueueStore:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, run_id: str) -> Path:
        return self.base_dir / f"{run_id}.json"

    def save(self, item: QueueTaskRecord) -> QueueTaskRecord:
        normalized = self._normalize(item)
        path = self._path(normalized.run_id)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        payload = json.dumps(asdict(normalized), indent=2) + "\n"
        with self._lock:
            try:
                tmp_path.write_text(payload, encoding="utf-8")
                tmp_path.replace(path)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
        return normalized

    def _load_path(self, path: Path) -> Optional[QueueTaskRecord]:
        try:
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                logger.warning("Skipping empty queue task record: %s", path)
                return None
            data = json.loads(raw)
            return self._record_from_data(data)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping invalid queue task record %s: %s", path, exc)
            return None

    def load(self, run_id: str) -> Optional[QueueTaskRecord]:
        path = self._path(run_id)
        if not path.exists():
            return None
        return self._load_path(path)

    def list(self) -> List[QueueTaskRecord]:
        items: List[QueueTaskRecord] = []
        for path in sorted(self.base_dir.glob("*.json")):
            item = self._load_path(path)
            if item is not None:
                items.append(item)
        items.sort(key=lambda item: (item.created_at, item.run_id))
        return items

    def claim_next(self) -> Optional[QueueTaskRecord]:
        with self._lock:
            for item in self.list():
                if item.status != QUEUE_STATUS_PENDING:
                    continue
                return self.transition(
                    item.run_id,
                    QUEUE_STATUS_RUNNING,
                    control_updates={
                        "claimed_at": utc_now_iso(),
                        "pause_requested": False,
                        "stop_requested": False,
                    },
                )
        return None

    def transition(
        self,
        run_id: str,
        new_status: str,
        *,
        control_updates: Optional[Dict[str, object]] = None,
    ) -> QueueTaskRecord:
        with self._lock:
            item = self.load(run_id)
            if item is None:
                raise FileNotFoundError(f"Queue task '{run_id}' not found")
            if not item.can_transition_to(new_status):
                raise QueueTransitionError(
                    f"Queue task '{run_id}' cannot transition from {item.status} to {new_status}"
                )
            item.status = new_status
            item.updated_at = utc_now_iso()
            if control_updates:
                item.control.update(control_updates)
            return self.save(item)

    def request_pause(self, run_id: str) -> QueueTaskRecord:
        item = self.load(run_id)
        if item is None:
            raise FileNotFoundError(f"Queue task '{run_id}' not found")
        if item.status == QUEUE_STATUS_PENDING:
            return self.transition(
                run_id,
                QUEUE_STATUS_PAUSED,
                control_updates={"pause_requested": True, "pause_requested_at": utc_now_iso()},
            )
        if item.status == QUEUE_STATUS_RUNNING:
            item.control["pause_requested"] = True
            item.control["pause_requested_at"] = utc_now_iso()
            item.updated_at = utc_now_iso()
            return self.save(item)
        raise QueueTransitionError(f"Pause is not supported from queue status '{item.status}'")

    def request_resume(self, run_id: str, *, resume_choice: Optional[str] = None) -> QueueTaskRecord:
        item = self.load(run_id)
        if item is None:
            raise FileNotFoundError(f"Queue task '{run_id}' not found")
        if item.status == QUEUE_STATUS_PAUSED:
            return self.transition(
                run_id,
                QUEUE_STATUS_PENDING,
                control_updates={
                    "pause_requested": False,
                    "pause_requested_at": None,
                    "resume_choice": resume_choice,
                    "resume_requested_at": utc_now_iso(),
                },
            )
        if item.status == QUEUE_STATUS_RUNNING:
            item.control["pause_requested"] = False
            item.control["pause_requested_at"] = None
            item.control["resume_choice"] = resume_choice
            item.control["resume_requested_at"] = utc_now_iso()
            item.updated_at = utc_now_iso()
            return self.save(item)
        raise QueueTransitionError(f"Resume is not supported from queue status '{item.status}'")

    def request_stop(self, run_id: str) -> QueueTaskRecord:
        item = self.load(run_id)
        if item is None:
            raise FileNotFoundError(f"Queue task '{run_id}' not found")
        if item.status in {QUEUE_STATUS_PENDING, QUEUE_STATUS_PAUSED}:
            return self.transition(
                run_id,
                QUEUE_STATUS_CANCELLED,
                control_updates={"stop_requested": True, "stop_requested_at": utc_now_iso()},
            )
        if item.status == QUEUE_STATUS_RUNNING:
            item.control["stop_requested"] = True
            item.control["stop_requested_at"] = utc_now_iso()
            item.updated_at = utc_now_iso()
            return self.save(item)
        raise QueueTransitionError(f"Stop is not supported from queue status '{item.status}'")

    def mark_done(self, run_id: str) -> QueueTaskRecord:
        return self.transition(run_id, QUEUE_STATUS_DONE)

    def mark_error(self, run_id: str, error: Optional[str] = None) -> QueueTaskRecord:
        updates: Dict[str, object] = {}
        if error:
            updates["last_error"] = error
        return self.transition(run_id, QUEUE_STATUS_ERROR, control_updates=updates)

    def mark_cancelled(self, run_id: str) -> QueueTaskRecord:
        return self.transition(run_id, QUEUE_STATUS_CANCELLED)

    def runtime_state(self) -> Dict[str, object]:
        items = self.list()
        counts: Dict[str, int] = {}
        for item in items:
            counts[item.status] = counts.get(item.status, 0) + 1
        running = next((item for item in items if item.status == QUEUE_STATUS_RUNNING), None)
        return {
            "counts": counts,
            "active_run_id": running.run_id if running else None,
            "active_task": asdict(running) if running else None,
            "queue_depth": counts.get(QUEUE_STATUS_PENDING, 0) + counts.get(QUEUE_STATUS_PAUSED, 0),
        }

    def _normalize(self, item: QueueTaskRecord) -> QueueTaskRecord:
        item.payload = dict(item.payload or {})
        item.control = dict(item.control or {})
        if not item.updated_at:
            item.updated_at = item.created_at or utc_now_iso()
        return item

    def _record_from_data(self, data: Dict[str, object]) -> QueueTaskRecord:
        return self._normalize(
            QueueTaskRecord(
                run_id=str(data["run_id"]),
                ecosystem=str(data["ecosystem"]),
                action=str(data["action"]),
                status=str(data.get("status") or QUEUE_STATUS_PENDING),
                created_at=str(data.get("created_at") or utc_now_iso()),
                updated_at=str(data.get("updated_at") or data.get("created_at") or utc_now_iso()),
                session_id=str(data["session_id"]) if data.get("session_id") is not None else None,
                payload=dict(data.get("payload") or {}),
                control=dict(data.get("control") or {}),
            )
        )


_SERIAL_QUEUE_LOCK = threading.RLock()


@contextmanager
def serial_queue_slot():
    """Single-process serial execution gate for the active runtime."""

    with _SERIAL_QUEUE_LOCK:
        yield
