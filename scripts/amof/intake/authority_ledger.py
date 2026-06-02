"""Minimal authority ledger for intake decision artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping


class ContextTrustClass(StrEnum):
    OPERATOR_ASSERTED = "operator_asserted"
    REPO_TRUTH = "repo_truth"
    RUNTIME_TRUTH = "runtime_truth"
    EXTERNAL_UNTRUSTED = "external_untrusted"
    MEMORY_UNTRUSTED = "memory_untrusted"
    TRANSCRIPT_UNTRUSTED = "transcript_untrusted"
    TOOL_OUTPUT_UNTRUSTED = "tool_output_untrusted"


class IntakeDecisionClass(StrEnum):
    ANSWER_ONLY = "answer_only"
    BOUNDED_ACTION = "bounded_action"
    PRIVILEGED_ACTION = "privileged_action"
    REFUSE = "refuse"
    ESCALATE = "escalate"


_AUTHORITY_CONTEXT_CLASSES = frozenset(
    {
        ContextTrustClass.OPERATOR_ASSERTED,
        ContextTrustClass.REPO_TRUTH,
        ContextTrustClass.RUNTIME_TRUTH,
    }
)


@dataclass(frozen=True)
class ToolPolicyMetadata:
    risk_class: str
    requires_approval: bool
    allowed_context_classes: tuple[ContextTrustClass, ...]
    minimum_evidence: tuple[str, ...]
    refusal_reason_template: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_class": self.risk_class,
            "requires_approval": self.requires_approval,
            "allowed_context_classes": [item.value for item in self.allowed_context_classes],
            "minimum_evidence": list(self.minimum_evidence),
            "refusal_reason_template": self.refusal_reason_template,
        }


@dataclass(frozen=True)
class ToolEligibility:
    tool_name: str
    policy: dict[str, Any] | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"tool_name": self.tool_name}
        if self.policy is not None:
            payload["policy"] = self.policy
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True)
class AuthorityDecisionArtifact:
    decision_class: IntakeDecisionClass
    rationale: str
    present_context_classes: tuple[ContextTrustClass, ...]
    eligible_tools: tuple[ToolEligibility, ...] = ()
    ineligible_tools: tuple[ToolEligibility, ...] = ()
    blockers: tuple[str, ...] = ()
    expected_evidence: tuple[str, ...] = ()
    emitted_evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_class": self.decision_class.value,
            "rationale": self.rationale,
            "present_context_classes": [item.value for item in self.present_context_classes],
            "eligible_tools": [item.to_dict() for item in self.eligible_tools],
            "ineligible_tools": [item.to_dict() for item in self.ineligible_tools],
            "blockers": list(self.blockers),
            "expected_evidence": list(self.expected_evidence),
            "emitted_evidence_refs": list(self.emitted_evidence_refs),
        }


DEFAULT_TOOL_POLICIES: dict[str, ToolPolicyMetadata] = {
    "Read": ToolPolicyMetadata(
        risk_class="read_only",
        requires_approval=False,
        allowed_context_classes=(ContextTrustClass.OPERATOR_ASSERTED, ContextTrustClass.REPO_TRUTH),
        minimum_evidence=("operator_intent", "path_scope"),
        refusal_reason_template="{tool_name} requires operator intent and bounded path scope.",
    ),
    "Grep": ToolPolicyMetadata(
        risk_class="read_only",
        requires_approval=False,
        allowed_context_classes=(ContextTrustClass.OPERATOR_ASSERTED, ContextTrustClass.REPO_TRUTH),
        minimum_evidence=("operator_intent", "search_scope"),
        refusal_reason_template="{tool_name} requires operator intent and bounded search scope.",
    ),
    "Glob": ToolPolicyMetadata(
        risk_class="read_only",
        requires_approval=False,
        allowed_context_classes=(ContextTrustClass.OPERATOR_ASSERTED, ContextTrustClass.REPO_TRUTH),
        minimum_evidence=("operator_intent", "path_scope"),
        refusal_reason_template="{tool_name} requires operator intent and bounded path scope.",
    ),
    "Shell": ToolPolicyMetadata(
        risk_class="execution",
        requires_approval=True,
        allowed_context_classes=(ContextTrustClass.OPERATOR_ASSERTED, ContextTrustClass.RUNTIME_TRUTH),
        minimum_evidence=("operator_intent", "command_scope", "approval_ref"),
        refusal_reason_template="{tool_name} requires explicit approval before command execution.",
    ),
    "StrReplace": ToolPolicyMetadata(
        risk_class="mutation",
        requires_approval=True,
        allowed_context_classes=(ContextTrustClass.OPERATOR_ASSERTED, ContextTrustClass.REPO_TRUTH),
        minimum_evidence=("operator_intent", "path_scope", "observed_old_string", "approval_ref"),
        refusal_reason_template="{tool_name} requires explicit approval and observed repo evidence.",
    ),
}


def evaluate_intake_authority(
    *,
    requested_decision_class: IntakeDecisionClass | str,
    present_context_classes: Iterable[ContextTrustClass | str],
    requested_tools: Iterable[str] = (),
    tool_policies: Mapping[str, ToolPolicyMetadata] | None = None,
    approval_granted: bool = False,
    rationale: str = "",
    blockers: Iterable[str] = (),
    emitted_evidence_refs: Iterable[str] = (),
) -> AuthorityDecisionArtifact:
    """Evaluate one intake decision and return a machine-readable artifact."""

    requested_class = _coerce_decision_class(requested_decision_class)
    context_classes = _coerce_context_classes(present_context_classes)
    tool_names = tuple(str(tool).strip() for tool in requested_tools if str(tool).strip())
    policies = dict(tool_policies or DEFAULT_TOOL_POLICIES)
    artifact_blockers = [str(blocker) for blocker in blockers if str(blocker)]
    emitted_refs = tuple(str(ref) for ref in emitted_evidence_refs if str(ref))

    if requested_class == IntakeDecisionClass.ANSWER_ONLY:
        return AuthorityDecisionArtifact(
            decision_class=IntakeDecisionClass.ANSWER_ONLY,
            rationale=rationale or "Answer-only intake does not select execution tools.",
            present_context_classes=context_classes,
            ineligible_tools=tuple(
                ToolEligibility(tool_name=tool, reason="answer_only avoids execution tool selection")
                for tool in tool_names
            ),
            blockers=tuple(artifact_blockers),
            emitted_evidence_refs=emitted_refs,
        )

    if requested_class == IntakeDecisionClass.REFUSE:
        refusal_reason = rationale or "Intake refused by authority ledger."
        return AuthorityDecisionArtifact(
            decision_class=IntakeDecisionClass.REFUSE,
            rationale=refusal_reason,
            present_context_classes=context_classes,
            ineligible_tools=tuple(
                ToolEligibility(tool_name=tool, reason=refusal_reason) for tool in tool_names
            ),
            blockers=tuple(artifact_blockers or [refusal_reason]),
            emitted_evidence_refs=emitted_refs,
        )

    if requested_class == IntakeDecisionClass.PRIVILEGED_ACTION and not approval_granted:
        reason = "privileged_action requires explicit approval before tool eligibility"
        return AuthorityDecisionArtifact(
            decision_class=IntakeDecisionClass.ESCALATE,
            rationale=rationale or reason,
            present_context_classes=context_classes,
            ineligible_tools=tuple(ToolEligibility(tool_name=tool, reason=reason) for tool in tool_names),
            blockers=tuple(artifact_blockers or [reason]),
            expected_evidence=("approval_ref",),
            emitted_evidence_refs=emitted_refs,
        )

    if requested_class not in {IntakeDecisionClass.BOUNDED_ACTION, IntakeDecisionClass.PRIVILEGED_ACTION}:
        reason = f"Unsupported intake decision class: {requested_class.value}"
        return AuthorityDecisionArtifact(
            decision_class=IntakeDecisionClass.REFUSE,
            rationale=reason,
            present_context_classes=context_classes,
            blockers=tuple(artifact_blockers or [reason]),
            emitted_evidence_refs=emitted_refs,
        )

    if not _has_authority_context(context_classes):
        reason = "untrusted context cannot upgrade authority for action eligibility"
        return AuthorityDecisionArtifact(
            decision_class=IntakeDecisionClass.REFUSE,
            rationale=rationale or reason,
            present_context_classes=context_classes,
            ineligible_tools=tuple(ToolEligibility(tool_name=tool, reason=reason) for tool in tool_names),
            blockers=tuple(artifact_blockers or [reason]),
            emitted_evidence_refs=emitted_refs,
        )

    eligible: list[ToolEligibility] = []
    ineligible: list[ToolEligibility] = []
    expected_evidence: list[str] = []

    for tool_name in tool_names:
        policy = policies.get(tool_name)
        if policy is None:
            ineligible.append(
                ToolEligibility(
                    tool_name=tool_name,
                    reason="missing tool policy metadata",
                )
            )
            continue
        if policy.requires_approval and not approval_granted:
            ineligible.append(
                ToolEligibility(
                    tool_name=tool_name,
                    policy=policy.to_dict(),
                    reason=policy.refusal_reason_template.format(tool_name=tool_name),
                )
            )
            continue
        if not _policy_context_matches(policy, context_classes):
            ineligible.append(
                ToolEligibility(
                    tool_name=tool_name,
                    policy=policy.to_dict(),
                    reason=policy.refusal_reason_template.format(tool_name=tool_name),
                )
            )
            continue
        eligible.append(ToolEligibility(tool_name=tool_name, policy=policy.to_dict()))
        expected_evidence.extend(policy.minimum_evidence)

    if requested_class == IntakeDecisionClass.BOUNDED_ACTION and not eligible:
        reason = "bounded_action requires at least one requested tool with matching policy metadata"
        return AuthorityDecisionArtifact(
            decision_class=IntakeDecisionClass.REFUSE,
            rationale=rationale or reason,
            present_context_classes=context_classes,
            eligible_tools=tuple(eligible),
            ineligible_tools=tuple(ineligible),
            blockers=tuple(artifact_blockers or [reason]),
            expected_evidence=tuple(_dedupe(expected_evidence)),
            emitted_evidence_refs=emitted_refs,
        )

    decision_class = requested_class
    if ineligible and requested_class == IntakeDecisionClass.PRIVILEGED_ACTION:
        decision_class = IntakeDecisionClass.ESCALATE

    return AuthorityDecisionArtifact(
        decision_class=decision_class,
        rationale=rationale or f"{requested_class.value} evaluated against authority ledger.",
        present_context_classes=context_classes,
        eligible_tools=tuple(eligible),
        ineligible_tools=tuple(ineligible),
        blockers=tuple(artifact_blockers),
        expected_evidence=tuple(_dedupe(expected_evidence)),
        emitted_evidence_refs=emitted_refs,
    )


def _coerce_decision_class(value: IntakeDecisionClass | str) -> IntakeDecisionClass:
    if isinstance(value, IntakeDecisionClass):
        return value
    return IntakeDecisionClass(str(value))


def _coerce_context_classes(
    values: Iterable[ContextTrustClass | str],
) -> tuple[ContextTrustClass, ...]:
    seen: set[ContextTrustClass] = set()
    normalized: list[ContextTrustClass] = []
    for value in values:
        context_class = value if isinstance(value, ContextTrustClass) else ContextTrustClass(str(value))
        if context_class not in seen:
            normalized.append(context_class)
            seen.add(context_class)
    return tuple(normalized)


def _has_authority_context(context_classes: Iterable[ContextTrustClass]) -> bool:
    return bool(set(context_classes) & _AUTHORITY_CONTEXT_CLASSES)


def _policy_context_matches(
    policy: ToolPolicyMetadata,
    context_classes: Iterable[ContextTrustClass],
) -> bool:
    return bool(set(policy.allowed_context_classes) & set(context_classes) & _AUTHORITY_CONTEXT_CLASSES)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result

