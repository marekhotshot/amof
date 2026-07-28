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
            # Bare "blocked" is too broad (charter/memory prose: "when blocked").
            # Require blocker intent against the intake/mission.
            re.compile(
                r"\b("
                r"blocked\s+by|blocked:\s|"
                r"this\s+(?:mission|task|ticket|intake|request)\s+is\s+blocked|"
                r"waiting\s+on|depends\s+on|dependency\s+on|pending\s+access|"
                r"cannot\s+proceed|can't\s+proceed"
                r")\b",
                re.IGNORECASE,
            ),
        ],
    ),
    (
        "replay_later",
        [
            # Noun "backlog" (e.g. "backlog verification mission") is not deferral.
            re.compile(
                r"\b("
                r"later|eventually|postpone|next week|follow up|tomorrow|"
                r"to the backlog|into the backlog|put (?:it |this )?on the backlog"
                r")\b",
                re.IGNORECASE,
            ),
        ],
    ),
    (
        "replay_now",
        [
            re.compile(r"\b(now|urgent|asap|today|immediately|broken|incident|failing)\b", re.IGNORECASE),
        ],
    ),
]

_BLOCKER_HINT = re.compile(
    r"\b(blocked\s+by|blocked:\s|blocking|waiting\s+on|missing|need|cannot\s+proceed|can't\s+proceed|dependency\s+on|permission)\b",
    re.IGNORECASE,
)
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

# AMOF-BL-109: verification / validation / test mission semantics (structured
# task_kind for BL-108 allow_no_change arming). Prefer explicit kind phrases
# plus intentional no-change markers; do not infer from vague "check" alone.
_VERIFICATION_KIND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "verification",
        re.compile(
            r"\b(verification(?:-only)?(?:\s+bounded)?(?:\s+write)?|verification\s+mission|"
            r"verify\s+that|verify\s+the|docs-only\s+verification)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "validation",
        re.compile(
            r"\b(validation(?:-only)?|validate\s+that|validate\s+the|validation\s+mission)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "test",
        re.compile(
            r"\b(test(?:-only)?\s+mission|testing-only|read-only\s+test)\b",
            re.IGNORECASE,
        ),
    ),
]
_ZERO_CHANGE_SEMANTICS = re.compile(
    r"\b("
    r"zero\s+file\s+changes|empty\s+diff\s+is\s+success|"
    r"make\s+no\s+changes|no\s+changes\s+at\s+all|"
    r"intentionally\s+(?:made\s+)?zero\s+changes|"
    r"expected\s+(?:and\s+correct\s+)?outcome\s+is\s+zero|"
    r"do\s+not\s+invent\s+an\s+edit"
    r")\b",
    re.IGNORECASE,
)
_MUTATION_SEMANTICS = re.compile(
    r"\b("
    r"implement(?:ation)?|fix\s+the\s+(?:bug|issue|defect)|"
    r"edit\s+the\s+file|update\s+the\s+(?:file|marker|code|line)|"
    r"apply\s+(?:a\s+)?(?:patch|change)|refactor|"
    r"add\s+(?:a\s+)?(?:feature|line|section)|"
    r"change\s+the\s+(?:code|implementation|file|marker)|"
    r"write\s+to\s+the\s+file|must\s+(?:edit|change|modify|update)\b|"
    r"make\s+the\s+(?:following\s+)?(?:edit|change|update)"
    r")\b",
    re.IGNORECASE,
)
_PROJECT_MEMORY_HEADER = "## Project memory\n"

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


def _operative_mission_text(text: str) -> str:
    """Return mission text after a structured project-memory inject prefix.

    Private injectors prepend ``projectMemoryContext()``, which is empty or a
    ``## Project memory`` section of ``- `` bullets (bodies may wrap). Lane and
    task_kind heuristics run on the operative mission only; ``rough_intent``
    still stores the full injected source.
    """
    if not text.startswith(_PROJECT_MEMORY_HEADER):
        return text
    rest = text[len(_PROJECT_MEMORY_HEADER) :]
    lines = rest.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("- "):
            index += 1
            while index < len(lines) and lines[index] not in ("\n", "\r\n") and not lines[index].startswith(
                "- "
            ) and not lines[index].startswith("## "):
                index += 1
            continue
        if line in ("\n", "\r\n") or not line.strip():
            index += 1
            if index < len(lines) and not lines[index].startswith("- "):
                operative = "".join(lines[index:]).strip()
                return operative or text
            continue
        operative = "".join(lines[index:]).strip()
        return operative or text
    return text


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


def _derive_verification_kind(text: str) -> str | None:
    """Return verification|validation|test when mission semantics match."""
    for kind, pattern in _VERIFICATION_KIND_PATTERNS:
        if pattern.search(text):
            return kind
    # Intentional no-change docs/verify language without an explicit kind word
    # still arms verification when zero-change semantics are explicit.
    if _ZERO_CHANGE_SEMANTICS.search(text) and re.search(
        r"\b(inspect|confirm|marker|docs-only)\b", text, re.IGNORECASE
    ):
        return "verification"
    return None


def _is_mutation_style(text: str) -> bool:
    for match in _MUTATION_SEMANTICS.finditer(text):
        prefix = text[max(0, match.start() - 40) : match.start()]
        if _NEGATION_BEFORE.search(prefix):
            continue
        return True
    return False


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


def _task_kind_for_lane(
    lane: str,
    *,
    adoption: bool = False,
    verification_kind: str | None = None,
    mutation: bool = False,
    ambiguous: bool = False,
) -> str:
    # Canonical adoption task kind
    # (AMOF-PREDATOR-DELIVERY-COCKPIT-CONVERGENCE-001 §E): repo/runtime
    # adoption missions classify as repo_runtime_adoption, decoupled from the
    # replay/kill lane verbs, and remain read-only by packet construction.
    if ambiguous:
        return "classification_ambiguous"
    if adoption and lane not in ("kill",):
        return "repo_runtime_adoption"
    if verification_kind in {"verification", "validation", "test"} and lane not in ("kill",):
        return verification_kind
    if lane == "kill":
        return "discard"
    if lane == "defer":
        return "blocked"
    if lane == "replay_later":
        return "deferred"
    if mutation:
        return "implementation"
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

    # Heuristics classify operative mission semantics; full source (including
    # structured project-memory inject) remains the rough_intent authority.
    operative = _operative_mission_text(source)
    title = _derive_title(operative)
    adoption = _is_adoption(operative)
    verification_kind = _derive_verification_kind(operative)
    mutation = _is_mutation_style(operative)
    ambiguous = bool(verification_kind and mutation)
    if ambiguous:
        lane = "defer"
        blockers = [
            "classification_ambiguous: mission signals both verification-style "
            "and mutation-style intent; refine to one structured task kind"
        ]
    elif verification_kind:
        lane = "replay_now"
        blockers = _derive_blockers(operative)
    else:
        lane = _derive_lane(operative, adoption)
        blockers = _derive_blockers(operative)
    repositories = _extract_repositories(operative)
    runtimes = _extract_runtimes(operative)
    scope = _derive_scope(operative, repositories)
    ticket_id = _derive_ticket_id(operative)
    bounded_goal = _derive_summary(operative)
    task_kind = _task_kind_for_lane(
        lane,
        adoption=adoption,
        verification_kind=None if ambiguous else verification_kind,
        mutation=mutation and not verification_kind,
        ambiguous=ambiguous,
    )
    classification = "ambiguous" if ambiguous else lane

    packet = {
        "id": _intake_id_for(title, "ambiguous" if ambiguous else lane),
        "version": "1.0.0",
        "kind": "bounded_intake_task",
        "ticket_id": ticket_id,
        "rough_intent": source,
        "bounded_goal": bounded_goal,
        "task_kind": task_kind,
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
            "classification": classification,
            "replay_lane": lane,
            "bounded_scope": scope,
            "blockers": blockers,
            "adoption": adoption,
            "verification_kind": verification_kind,
            "mutation": mutation,
            "ambiguous": ambiguous,
            "extracted_repositories": repositories,
            "extracted_runtimes": runtimes,
        },
    }

    return IntakeDraftResult(
        title=title,
        classification=classification,
        replay_lane=lane,
        bounded_scope=scope,
        blockers=blockers,
        governance_hints=_governance_hints(operative),
        packet_text=json.dumps(packet, indent=2),
    )


__all__ = ["IntakeDraftResult", "compile_intake_draft"]
