"""Risk-gated Critic role: one optional bounded inference pass over planning context.

Authority: Worker ontology Evolution rule + convergence Critic trigger table.
The Critic has no tools, no mutation, and no dialogue with the planner. It may
only emit optional PlanBundle cognition fields (dissent / interpretations) and
an evidenced gating decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CRITIC_SYSTEM_PROMPT = """You are the AMOF Critic role.

You perform one bounded critique pass over a proposal-only PlanBundle and the
same canonical planning context the planner already saw.

Hard rules:
- Do not execute anything.
- Do not invent tools, shell commands, or mutation authority.
- Do not dialogue with the planner; return one JSON object only.
- Critique the plan for risk, scope creep, missing validation, and unsafe
  assumptions. Prefer concrete dissent over praise.

Return strict JSON only with these keys:
  interpretations
  dissent

Requirements:
- `interpretations` must be an array of objects `{ "text": string, "role": "critic" }`
  (optional `source` string). Include at least one short critique framing entry.
- `dissent` must be an array of objects `{ "text": string }` with optional
  `severity` (`low`|`medium`|`high`) and `source`. Use an empty array only when
  you truly find no material dissent.
- Never include executable commands or write scopes to apply.
"""

_SECURITY_PATH_MARKERS = (
    "security/",
    "auth/",
    "secret",
    ".kube",
    "kubernetes",
    "helm",
    "cloudflare",
    "credential",
    "firewall",
    "iam",
)

_LOW_CONFIDENCE_THRESHOLD = 0.5


@dataclass(frozen=True)
class CriticRiskSignals:
    """Optional risk inputs for the Critic gate (absent => never always-on)."""

    mutation_ceiling: str | None = None
    prod_touching: bool = False
    security_sensitive: bool = False
    planner_confidence: float | None = None
    contract_disagreement: bool = False
    budget_exhausted: bool = False
    explore_readonly: bool = False


@dataclass(frozen=True)
class CriticGateDecision:
    run_critique: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    degrade_supervised: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_critique": self.run_critique,
            "reason_codes": list(self.reason_codes),
            "degrade_supervised": self.degrade_supervised,
        }


def infer_security_sensitive(files_to_inspect: list[str] | None) -> bool:
    joined = " ".join(str(item).lower() for item in (files_to_inspect or []))
    return any(marker in joined for marker in _SECURITY_PATH_MARKERS)


def risk_signals_from_mapping(value: Any) -> CriticRiskSignals:
    if not isinstance(value, dict):
        return CriticRiskSignals()
    confidence = value.get("planner_confidence", value.get("confidence"))
    parsed_confidence: float | None
    if confidence is None:
        parsed_confidence = None
    else:
        try:
            parsed_confidence = float(confidence)
        except (TypeError, ValueError):
            parsed_confidence = None
        if parsed_confidence is not None and (
            parsed_confidence < 0.0 or parsed_confidence > 1.0
        ):
            parsed_confidence = None
    return CriticRiskSignals(
        mutation_ceiling=str(value.get("mutation_ceiling") or "").strip() or None,
        prod_touching=bool(value.get("prod_touching")),
        security_sensitive=bool(value.get("security_sensitive")),
        planner_confidence=parsed_confidence,
        contract_disagreement=bool(value.get("contract_disagreement")),
        budget_exhausted=bool(value.get("budget_exhausted")),
        explore_readonly=bool(value.get("explore_readonly")),
    )


def decide_critic_gate(signals: CriticRiskSignals) -> CriticGateDecision:
    """Apply the accepted Critic trigger table. Never always-on."""
    if signals.budget_exhausted:
        return CriticGateDecision(
            run_critique=False,
            reason_codes=("budget_exhausted",),
            degrade_supervised=True,
        )

    reason_codes: list[str] = []
    if signals.mutation_ceiling in {"runtime_mutation", "bounded_worktree"}:
        reason_codes.append(f"high_mutation_ceiling:{signals.mutation_ceiling}")
    if signals.prod_touching:
        reason_codes.append("prod_touching")
    if signals.security_sensitive:
        reason_codes.append("security_sensitive")
    if (
        signals.planner_confidence is not None
        and signals.planner_confidence < _LOW_CONFIDENCE_THRESHOLD
    ):
        reason_codes.append("low_planner_confidence")
    if signals.contract_disagreement:
        reason_codes.append("contract_disagreement")

    if reason_codes:
        return CriticGateDecision(run_critique=True, reason_codes=tuple(reason_codes))

    if signals.explore_readonly or signals.mutation_ceiling == "read_only":
        return CriticGateDecision(
            run_critique=False,
            reason_codes=("read_only_explore",),
        )

    # No positive risk trigger and no explicit explore skip: do not spend.
    return CriticGateDecision(
        run_critique=False,
        reason_codes=("insufficient_risk_signals",),
    )


def critique_skip_interpretation(decision: CriticGateDecision) -> dict[str, str]:
    reasons = ",".join(decision.reason_codes) or "unspecified"
    text = f"critique_skipped:{reasons}"
    if decision.degrade_supervised:
        text += ";degrade=supervised"
    return {"text": text, "role": "critic", "source": "critic_gate"}


def critique_ran_interpretation(decision: CriticGateDecision) -> dict[str, str]:
    reasons = ",".join(decision.reason_codes) or "unspecified"
    return {
        "text": f"critique_ran:{reasons}",
        "role": "critic",
        "source": "critic_gate",
    }


def merge_critic_fields(
    *,
    packet_dict: dict[str, Any],
    decision: CriticGateDecision,
    critic_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a new PlanBundle dict with evidenced gate + optional Critic fields."""
    merged = dict(packet_dict)
    interpretations = list(merged.get("interpretations") or [])
    dissent = list(merged.get("dissent") or [])

    if not decision.run_critique:
        interpretations.append(critique_skip_interpretation(decision))
        merged["interpretations"] = interpretations
        # Keep dissent absent/empty on skip — do not invent critique content.
        if dissent:
            merged["dissent"] = dissent
        elif "dissent" in merged:
            del merged["dissent"]
        return merged

    interpretations.append(critique_ran_interpretation(decision))
    payload = critic_payload or {}
    for item in payload.get("interpretations") or []:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            entry = {"text": str(item["text"]).strip(), "role": "critic"}
            source = str(item.get("source") or "").strip()
            if source:
                entry["source"] = source
            interpretations.append(entry)
    for item in payload.get("dissent") or []:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            entry = {"text": str(item["text"]).strip()}
            severity = str(item.get("severity") or "").strip()
            source = str(item.get("source") or "").strip()
            if severity:
                entry["severity"] = severity
            if source:
                entry["source"] = source
            dissent.append(entry)

    merged["interpretations"] = interpretations
    if dissent:
        merged["dissent"] = dissent
    return merged
