# AMOF Public / Private Boundary

Status: public governance boundary
Date: 2026-05-25

## Principle

Public AMOF is the governed execution runtime.

Private AMOF is orchestration intelligence.

The public layer should make AMOF installable, auditable, and governed. The
private layer owns strategic decision-making, optimization, and automation
logic. Public materials must define boundaries and contracts without teaching
the private strategy behind them.

## Public Layer

Allowed in OSS:

- Governed execution runtime.
- Receipts and evidence envelopes.
- Promotion semantics and explicit promotion no-ops.
- Worktree governance and isolated workspace materialization.
- Approval contracts and control-grade mutation boundaries.
- Deterministic execution boundaries.
- Basic provider abstraction and client stubs.
- Canonical workspace materialization.
- Reproducible execution semantics.
- Fail-closed client behavior for private services.
- Public smoke surfaces that do not require secrets.

Public materials may explain:

- What boundary exists.
- What input, output, receipt, or evidence shape is required.
- Which mutation classes are allowed or forbidden.
- Which public contract an artifact satisfies.
- How public users can install, validate, and audit runtime behavior.

## Private Layer

Must not live in OSS:

- Adaptive orchestration logic.
- Model or provider routing strategy.
- Planning, decomposition, or task-splitting strategy.
- Execution heuristics.
- Operational memory convergence logic.
- Topology intelligence beyond public runtime boundaries.
- Evaluator datasets, scoring rubrics, or benchmark pipelines.
- Risk, ranking, or blast-radius heuristics.
- Organization-specific execution patterns.
- Advanced autonomous coordination logic.
- Private gateway ownership, hosted routing policy, redaction internals, and
  private deployment surfaces.

Public files must not include exact private file paths, symbol names, function
names, extraction orders, sensitive historical target details, or private
topology details for restricted logic.

## Public Classifications

Use these public labels in issues and pull requests:

- `PUBLIC_RUNTIME`: installability, runtime execution, receipts, worktrees,
  promotion semantics, trust boundaries, and no-key public smoke.
- `SHARED_CONTRACT`: schemas, interfaces, result shapes, and vocabulary that
  describe what must be proven.
- `PRIVATE_INTELLIGENCE`: strategic logic or heuristics that must not be
  published.
- `RESTRICTED_STRATEGIC_LOGIC`: highly sensitive orchestration, routing,
  planning, scoring, coordination, or evaluation logic.

If a change mixes public runtime and private intelligence, split it before
publication.

## Review Criteria

Before merging a public change, reviewers must ask:

- Is this needed for public installation, validation, receipts, governed
  execution, worktree materialization, promotion semantics, or user trust?
- Is this an interface or result shape rather than the strategy that produces
  the result?
- Does this expose how AMOF decides, routes, scores, decomposes, optimizes, or
  coordinates work?
- Does this expose provider priority, fallback behavior, model selection,
  thresholds, or role mapping?
- Does this expose eval goldens, expected outputs, pass/fail scoring, benchmark
  design, or noise-rejection logic?
- Does this expose private deployment topology, live control surfaces, secrets,
  kubeconfigs, private keys, or organization-specific runbooks?
- Can the public artifact be reduced to a schema, interface, refusal contract,
  or operator-visible receipt?

Approval requires concrete answers. Documentation is reviewed under the same
boundary rules as code.

## Public Checklist

Before adding material to the public repo:

- Keep installability and no-key smoke intact.
- Keep governed execution, receipts, and promotion semantics intact.
- Publish contracts and evidence shapes, not private decision strategy.
- Use redacted examples for providers, endpoints, secrets, and deployment
  surfaces.
- Avoid sensitive implementation identifiers, route maps, extraction sequences,
  and historical target lists.
- Keep benchmark data, scoring rules, and model/provider comparisons private.
- Keep organization-specific operational patterns private unless reduced to
  generic public guidance.

If any item cannot be satisfied, stop and perform private-boundary review before
publishing.

## Future Ticket Rules

Tickets that touch orchestration, routing, planning, provider behavior,
evaluation, scoring, topology, or autonomous coordination must include one of:

- `Public runtime only: no private intelligence added.`
- `Shared contract only: no executable policy or heuristic added.`
- `Private intelligence touched: do not publish without boundary review.`

If none is true, the ticket must remain private until split.

## Escalation Rules

Escalate for boundary review when a change includes:

- Strategic routing or fallback behavior.
- Planning or task decomposition logic.
- Risk, ranking, scoring, or blast-radius logic.
- Eval datasets, scoring rubrics, expected outputs, or benchmark tasks.
- Private topology, live operational surfaces, or organization-specific
  automation.
- A history rewrite or extraction plan with concrete sensitive targets.

Allowed escalation outcomes:

- Keep public as runtime or shared contract.
- Reduce to a generic public interface.
- Move details to private evidence.
- Block publication.

## Non-Negotiables

Public reduction must not break:

- Core public installability.
- Governed execution semantics.
- Receipt and evidence contracts.
- Promotion trust semantics.
- Worktree/materialization trust.
- Public no-key smoke surfaces.

Reducing public smartness is acceptable when it preserves the public trust model
and keeps strategic orchestration private.
