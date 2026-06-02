"""Deterministic intake draft compiler for canonical AMOF intake flows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any


ReplayLane = str

_LANE_HINTS: list[tuple[ReplayLane, list[re.Pattern[str]]]] = [
    (
        "kill",
        [
            re.compile(r"\b(cancel|drop|discard|obsolete|ignore|duplicate|spam)\b", re.IGNORECASE),
        ],
    ),
    (
        "defer",
        [
            re.compile(r"\b(blocked|waiting|dependency|depends on|pending access|cannot proceed|can't proceed)\b", re.IGNORECASE),
        ],
    ),
    (
        "replay_later",
        [
            re.compile(r"\b(later|after|eventually|postpone|backlog|next week|follow up|tomorrow)\b", re.IGNORECASE),
        ],
    ),
    (
        "replay_now",
        [
            re.compile(r"\b(now|urgent|asap|today|immediately|broken|incident|failing)\b", re.IGNORECASE),
        ],
    ),
]

_BLOCKER_HINT = re.compile(r"\b(blocked|blocking|waiting|missing|need|cannot|can't|dependency|permission)\b", re.IGNORECASE)
_PATH_HINT = re.compile(r"\b([A-Za-z0-9._-]+/[A-Za-z0-9._/-]*|[A-Za-z0-9._/-]+\.[A-Za-z0-9]+)\b")
_TICKET_HINT = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


@dataclass(frozen=True)
class IntakeDraftResult:
    title: str
    classification: str
    replay_lane: str
    bounded_scope: list[str]
    blockers: list[str]
    governance_hints: list[str]
    packet_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "classification": self.classification,
            "replay_lane": self.replay_lane,
            "bounded_scope": list(self.bounded_scope),
            "blockers": list(self.blockers),
            "governance_hints": list(self.governance_hints),
            "packet_text": self.packet_text,
        }


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = _clean_line(line)
        if cleaned:
            return cleaned
    return ""


def _derive_title(text: str) -> str:
    first = _first_non_empty_line(text)
    if not first:
        return "Untitled operator intake"
    return first.rstrip(".:;!?")[:90]


def _derive_lane(text: str) -> str:
    for lane, patterns in _LANE_HINTS:
        if any(pattern.search(text) for pattern in patterns):
            return lane
    return "replay_now"


def _derive_scope(text: str) -> list[str]:
    matches = [_clean_line(match.group(1)) for match in _PATH_HINT.finditer(text)]
    deduped: list[str] = []
    for item in matches:
        if item and item not in deduped:
            deduped.append(item)
        if len(deduped) >= 8:
            break
    return deduped or ["."]


def _derive_blockers(text: str) -> list[str]:
    blockers: list[str] = []
    for line in text.splitlines():
        cleaned = _clean_line(line)
        if not cleaned:
            continue
        if _BLOCKER_HINT.search(cleaned) and cleaned not in blockers:
            blockers.append(cleaned)
        if len(blockers) >= 6:
            break
    return blockers


def _derive_ticket_id(text: str) -> str:
    match = _TICKET_HINT.search(text)
    if match:
        return match.group(1).upper()
    return "AMOF-INTAKE-DRAFT-001"


def _derive_summary(text: str) -> str:
    normalized = _clean_line(text)
    if len(normalized) <= 220:
        return normalized
    return f"{normalized[:217]}..."


def _task_kind_for_lane(lane: str) -> str:
    if lane == "kill":
        return "discard"
    if lane == "defer":
        return "blocked"
    if lane == "replay_later":
        return "deferred"
    return "other"


def _intake_id_for(title: str, lane: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        slug = "operator-intake"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    return f"draft-{lane.replace('_', '-')}-{slug[:48]}-{stamp}"


def _governance_hints(raw_text: str) -> list[str]:
    hints = [
        "Planning-only intake: submit through canonical validate/submit contracts.",
        "No execution or provider calls are allowed from this draft path.",
    ]
    if re.search(r"\b(deploy|release|push|kubectl|ghcr)\b", raw_text, re.IGNORECASE):
        hints.append("Execution-related terms detected; keep mutations forbidden and approval explicit.")
    return hints


def compile_intake_draft(raw_text: str) -> IntakeDraftResult:
    source = str(raw_text or "").strip()
    if not source:
        raise ValueError("raw_text is required")

    title = _derive_title(source)
    lane = _derive_lane(source)
    scope = _derive_scope(source)
    blockers = _derive_blockers(source)
    ticket_id = _derive_ticket_id(source)
    bounded_goal = _derive_summary(source)

    packet = {
        "id": _intake_id_for(title, lane),
        "version": "1.0.0",
        "kind": "bounded_intake_task",
        "ticket_id": ticket_id,
        "rough_intent": source,
        "bounded_goal": bounded_goal,
        "task_kind": _task_kind_for_lane(lane),
        "repo_scope": scope,
        "paths_to_inspect": scope,
        "profile_ref": "amof-intake-draft-compiler-v1",
        "mutations": {
            "allowed": [],
            "forbidden": ["edit", "deploy", "promote", "push", "execute", "dispatch"],
        },
        "validation_gates": [
            {
                "name": "read_only",
                "requirement": "Intake remains planning-only.",
                "failure_action": "stop",
            },
            {
                "name": "governance_boundary",
                "requirement": "Submit only through canonical AMOF intake contracts.",
                "failure_action": "stop",
            },
        ],
        "cost_truth_policy": {
            "missing_cost_representation": "unknown",
        },
        "uc_classification": {
            "classification": lane,
            "replay_lane": lane,
            "bounded_scope": scope,
            "blockers": blockers,
        },
    }

    return IntakeDraftResult(
        title=title,
        classification=lane,
        replay_lane=lane,
        bounded_scope=scope,
        blockers=blockers,
        governance_hints=_governance_hints(source),
        packet_text=json.dumps(packet, indent=2),
    )


__all__ = ["IntakeDraftResult", "compile_intake_draft"]
