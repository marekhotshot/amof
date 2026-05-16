"""Structured log record models for durable run reconstruction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StructuredLogRecord:
    run_id: str
    step: int
    event_type: str
    message: str
    phase: Optional[str] = None
    model: Optional[str] = None
    tokens: Optional[Dict[str, Any]] = None
    cost: Optional[float] = None
    elapsed_ms: Optional[int] = None
    decision: Optional[str] = None
    summary: Optional[str] = None
    error: Optional[str] = None
    stop_reason: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    evidence: Optional[Dict[str, Any]] = None
    hash: Optional[str] = None
    timestamp: str = field(default_factory=now_iso)
