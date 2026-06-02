# Authority Ledger

Status: minimal public contract
Scope: `AMOF-AUTHORITY-LEDGER-001`

## Purpose

The authority ledger records why one evaluated intake is answered, allowed as a
bounded action, blocked for approval, refused, or escalated.

This slice does not create UI behavior, voice behavior, memory redesign, MCP
breadth, or autonomous orchestration. The implementation is the pure intake
evaluator in `scripts/amof/intake/authority_ledger.py`.

## Context Trust Classes

- `operator_asserted`: authority explicitly supplied by the operator.
- `repo_truth`: evidence read from the current repository.
- `runtime_truth`: evidence emitted by the running AMOF process or runtime.
- `external_untrusted`: external input that cannot grant authority.
- `memory_untrusted`: recalled memory that cannot grant authority.
- `transcript_untrusted`: transcript content that cannot grant authority.
- `tool_output_untrusted`: tool output that cannot grant new authority.

Only `operator_asserted`, `repo_truth`, and `runtime_truth` can satisfy an action
tool policy. Untrusted classes may be present as context, but they cannot upgrade
an intake into action eligibility.

## Intake Decision Classes

- `answer_only`: answer without selecting execution tools.
- `bounded_action`: select only tools whose policy metadata matches the present
  authority context.
- `privileged_action`: requires explicit approval before tool eligibility.
- `refuse`: emit a machine-readable refusal reason and no eligible tools.
- `escalate`: stop for missing approval or another operator-level blocker.

## Tool Policy Metadata

Each tool policy records:

- `risk_class`
- `requires_approval`
- `allowed_context_classes`
- `minimum_evidence`
- `refusal_reason_template`

The default minimal policy catalog covers `Read`, `Grep`, `Glob`, `Shell`, and
`StrReplace`. Unknown tools are ineligible until policy metadata exists for
them.

## Intake Integration

`amof intake validate <file> --authority-json` validates the intake packet and
prints only the authority decision artifact. If the packet does not provide an
`authority` object, validation evaluates a conservative `bounded_action` with
`operator_asserted` plus `repo_truth` context and read-only tools.

`amof intake submit <file> --authority-artifact <path>` validates the packet,
evaluates authority, writes the authority artifact to the requested path, and
records `authority_decision_path` in the local submission record. If authority
evaluation returns `refuse` or `escalate`, submit fails closed after writing the
artifact.

`amof runner match <intake_ref> --authority-artifact <path>` consumes an
authority decision artifact while selecting eligible planning runners. If
`intake_ref` is a submitted intake id and its submission record contains
`authority_decision_path`, runner match consumes that artifact automatically.
Without an artifact, runner match preserves the existing planning-only matching
behavior.

Runner authority gating accepts `bounded_action` artifacts with eligible tools
and rejects `refuse`, `escalate`, `privileged_action`, `answer_only`, or
`bounded_action` artifacts with no eligible tools. JSON output includes
`authority_gate`, candidate `authority_evidence`, and `ineligible_candidates`
when an artifact is consumed.

Optional packet fields live under `authority`:

- `decision_class`
- `rationale`
- `present_context_classes`
- `requested_tools`
- `approval_granted`
- `blockers`
- `emitted_evidence_refs`

## Decision Artifact

Every evaluated intake returns a machine-readable artifact with:

- `decision_class`
- `rationale`
- `present_context_classes`
- `eligible_tools`
- `ineligible_tools`
- `blockers`
- `expected_evidence`
- `emitted_evidence_refs`

Examples:

- `contracts/examples/authority-ledger-bounded-action.example.json`
- `contracts/examples/authority-ledger-refuse.example.json`
- `contracts/examples/intake-authority-evaluation-bounded-action.example.json`
- `contracts/examples/runner-authority-gating-bounded-match.example.json`

## Operator Inspection Rule

For one intake decision, inspect:

1. `decision_class` to see the final disposition.
2. `present_context_classes` to see which authority classes were available.
3. `eligible_tools` and `ineligible_tools` to see exact tool policy outcomes.
4. `blockers` to see why the decision stopped.
5. `expected_evidence` and `emitted_evidence_refs` to see what evidence the
   decision requires or produced.

