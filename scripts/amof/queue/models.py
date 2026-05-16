"""Queue models and transition rules for durable runtime dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

QUEUE_STATUS_PENDING = "pending"
QUEUE_STATUS_PAUSED = "paused"
QUEUE_STATUS_RUNNING = "running"
QUEUE_STATUS_DONE = "done"
QUEUE_STATUS_ERROR = "error"
QUEUE_STATUS_CANCELLED = "cancelled"

QUEUE_TERMINAL_STATUSES = {
    QUEUE_STATUS_DONE,
    QUEUE_STATUS_ERROR,
    QUEUE_STATUS_CANCELLED,
}

QUEUE_ALLOWED_TRANSITIONS = {
    QUEUE_STATUS_PENDING: {
        QUEUE_STATUS_PENDING,
        QUEUE_STATUS_PAUSED,
        QUEUE_STATUS_RUNNING,
        QUEUE_STATUS_CANCELLED,
    },
    QUEUE_STATUS_PAUSED: {
        QUEUE_STATUS_PAUSED,
        QUEUE_STATUS_PENDING,
        QUEUE_STATUS_CANCELLED,
    },
    QUEUE_STATUS_RUNNING: {
        QUEUE_STATUS_RUNNING,
        QUEUE_STATUS_DONE,
        QUEUE_STATUS_ERROR,
        QUEUE_STATUS_CANCELLED,
    },
    QUEUE_STATUS_DONE: {QUEUE_STATUS_DONE},
    QUEUE_STATUS_ERROR: {QUEUE_STATUS_ERROR},
    QUEUE_STATUS_CANCELLED: {QUEUE_STATUS_CANCELLED},
}


@dataclass
class QueueTaskRecord:
    run_id: str
    ecosystem: str
    action: str
    status: str
    created_at: str
    updated_at: str
    session_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    control: Dict[str, Any] = field(default_factory=dict)

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in QUEUE_ALLOWED_TRANSITIONS.get(self.status, set())
