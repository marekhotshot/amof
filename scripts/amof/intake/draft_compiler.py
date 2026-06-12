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

# Semantic adoption classification
# (AMOF-INTAKE-ADOPTION-SEMANTIC-CLASSIFICATION-001): adoption missions are
# first-class, and lane verbs only count when they target the intake itself,
# never when they appear inside instructions ("ignore the legacy folder",
# "do not discard anything").
_ADOPTION_HINT = re.compile(
    r"\b(adopt|adoption|onboard|onboarding|take over|bring under (?:amof|governance|management))\b",
    re.IGNORECASE,
)
_ADOPTION_SUBJECT_HINT = re.compile(
    r"\b(repo|repos|repository|repositories|runtime|runtimes|site|website|domain|project|codebase|service)\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE = re.compile(
    r"\b(do(?:es)?\s*n[o']t|don't|never|no|without|avoid|must\s+not|should\s+not"
    r"|if|would|could|might|unless|in\s+case|were\s+to|whether\s+to)\s+(?:\w+\s+){0,2}$",
    re.IGNORECASE,
)
_LANE_SELF_TARGET = re.compile(r"\b(this|the)\s+(ticket|task|mission|intake|request|draft|item)\b", re.IGNORECASE)

# Repository identity extraction: Git URLs, owner/name pairs, bare domains.
_REPO_URL_HINT = re.compile(r"\b(?:https?://|git@)[A-Za-z0-9._/:@-]+", re.IGNORECASE)
_DOMAIN_HINT = re.compile(
    r"\b((?:[A-Za-z0-9-]+\.)+(?:com|org|net|dev|io|sk|cz|eu|app|ai|cloud))\b",
    re.IGNORECASE,
)
_RUNTIME_ID_HINT = re.compile(r"\b([a-z0-9][a-z0-9-]*runtime[a-z0-9-]*|[a-z0-9-]+-operator-host-\d+)\b", re.IGNORECASE)
# Noun-phrase runtime mentions ("the hotshot runtime") name a runtime by its
# qualifier; generic qualifiers are filtered so "the cloud runtime" stays
# unextracted while "the hotshot runtime" yields hotshot.
_RUNTIME_PHRASE_HINT = re.compile(r"\b([a-z0-9][a-z0-9-]{2,})\s+runtimes?\b", re.IGNORECASE)
_RUNTIME_PHRASE_STOPWORDS = frozenset(
    {
        "the", "and", "this", "that", "cloud", "local", "dev", "full",
        "stack", "amof", "operator", "every", "any", "each", "its", "their",
        "new", "old", "existing", "current", "target", "production",
    }
)
_FILE_PATH_HINT = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+/?$")


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


def _is_adoption(text: str) -> bool:
    for match in _ADOPTION_HINT.finditer(text):
        window = text[max(0, match.start() - 80): match.end() + 80]
        if _ADOPTION_SUBJECT_HINT.search(window) or _DOMAIN_HINT.search(window) or _REPO_URL_HINT.search(window):
            return True
    return False


def _lane_match_counts(text: str, pattern: re.Pattern[str], adoption: bool) -> bool:
    """A lane verb counts only when it is not negated and, for terminal verbs
    on adoption intakes, when it targets the intake itself."""
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 40): match.start()]
        if _NEGATION_BEFORE.search(prefix):
            continue
        if adoption:
            suffix = text[match.end(): match.end() + 60]
            if not (_LANE_SELF_TARGET.search(suffix) or _LANE_SELF_TARGET.search(prefix)):
                continue
        return True
    return False


def _derive_lane(text: str, adoption: bool = False) -> str:
    for lane, patterns in _LANE_HINTS:
        terminal = lane in ("kill", "defer")
        if any(_lane_match_counts(text, pattern, adoption and terminal) for pattern in patterns):
            return lane
    return "replay_now"


def _extract_repositories(text: str) -> list[str]:
    found: list[str] = []
    for pattern in (_REPO_URL_HINT, _DOMAIN_HINT):
        for match in pattern.finditer(text):
            value = match.group(0).rstrip(".,;:")
            if value not in found:
                found.append(value)
            if len(found) >= 8:
                return found
    return found


def _extract_runtimes(text: str) -> list[str]:
    found: list[str] = []
    for match in _RUNTIME_ID_HINT.finditer(text):
        value = match.group(1)
        if value.lower() in ("runtime", "runtimes"):
            continue
        if value not in found:
            found.append(value)
        if len(found) >= 8:
            break
    for match in _RUNTIME_PHRASE_HINT.finditer(text):
        qualifier = match.group(1)
        if qualifier.lower() in _RUNTIME_PHRASE_STOPWORDS or "runtime" in qualifier.lower():
            continue
        if qualifier not in found:
            found.append(qualifier)
        if len(found) >= 8:
            break
    return found


def _derive_scope(text: str, repositories: list[str] | None = None) -> list[str]:
    # Extraction fidelity: repository identities (domains, URLs) are not
    # filesystem paths; only genuine path-shaped tokens enter the scope.
    repo_tokens = set(repositories or [])
    deduped: list[str] = []
    for match in _PATH_HINT.finditer(text):
        item = _clean_line(match.group(1))
        if not item or item in deduped:
            continue
        if item in repo_tokens or _DOMAIN_HINT.fullmatch(item):
            continue
        if "/" not in item and not _FILE_PATH_HINT.match(item):
            # bare dotted token (version, abbreviation, file name without
            # directory): keep only when it looks like a real file
            if not re.search(r"\.(ts|tsx|js|mjs|py|go|rs|java|json|yaml|yml|md|css|html|sh|toml)$", item, re.IGNORECASE):
                continue
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


def _task_kind_for_lane(lane: str, adoption: bool = False) -> str:
    # Canonical adoption task kind
    # (AMOF-PREDATOR-DELIVERY-COCKPIT-CONVERGENCE-001 §E): repo/runtime
    # adoption missions classify as repo_runtime_adoption, decoupled from the
    # replay/kill lane verbs, and remain read-only by packet construction.
    if adoption and lane not in ("kill",):
        return "repo_runtime_adoption"
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
    adoption = _is_adoption(source)
    lane = _derive_lane(source, adoption)
    repositories = _extract_repositories(source)
    runtimes = _extract_runtimes(source)
    scope = _derive_scope(source, repositories)
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
        "task_kind": _task_kind_for_lane(lane, adoption),
        "repo_scope": scope,
        "paths_to_inspect": scope,
        "extracted_repositories": repositories,
        "extracted_runtimes": runtimes,
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
            "adoption": adoption,
            "extracted_repositories": repositories,
            "extracted_runtimes": runtimes,
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
