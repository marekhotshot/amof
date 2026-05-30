# AMOF-REMOTE-EXECUTION-SCAN-REPORT-001

Status: Draft artifact (contract-first; bounded planning slice only)
Track: Runtime authority remote execution readiness
Release: AMOF 3.0 (v3.0.0)

## Ticket ID

`AMOF-REMOTE-EXECUTION-SCAN-REPORT-001`

## Goal

Define the first bounded remote execution slice as scan/report only: AMOF evaluates intake and eligible runner metadata, then produces a readiness/plan report without performing execution.

## Core Principle

scan/report first; execution later

## Scope

- Define remote execution readiness scan contract.
- Define runner eligibility confirmation contract.
- Define intake compatibility validation for scan/report.
- Define active-context availability and fail-closed checks for scan/report.
- Define execution scan/report artifact shape and report vocabulary.
- Define runtime log/runs linkage expectations for scan/report outcomes.
- No actual execution in this ticket.
- No repo mutation in this ticket.
- No queue worker in this ticket.
- No Kubernetes job dispatch in this ticket.
- No SSH command execution in this ticket.
- No automatic PR/commit/push in this ticket.
- No Jira sync in this ticket.

## Expected Scan/Report Output (Later Implementation)

Given an intake packet and runner registry metadata, output must include:

- selected intake reference/id
- selected and eligible runner candidates
- selected runtime context
- required capabilities
- missing capabilities (if any)
- forbidden mutation checks
- safety gate results
- proposed execution plan steps (advisory only)
- explicit reason why no execution was performed
- explicit marker: `NO_EXECUTION_PERFORMED`

## Required Behavior (Implementation Acceptance Contract)

- Valid intake plus eligible runner metadata produces one scan/report artifact.
- Invalid intake fails clearly with actionable error output.
- No eligible runner fails clearly.
- Context mismatch is fail-closed and not silently ignored.
- Mutation or remote-execution request remains blocked unless scan-only contract is satisfied.
- Report states `NO_EXECUTION_PERFORMED`.
- No files are edited.
- No commands are dispatched to remote systems.
- No secrets are printed.
- No fake cost `0.0` appears for unknown cost states.
- Existing CLI intake behavior remains working.
- Existing Operator Console intake behavior remains working.
- Existing runner registry behavior remains working.
- Existing runs CLI behavior remains working.

## Candidate CLI Shape (Implementation Slice Later)

Final command taxonomy must be chosen only after implementation inventory:

- `amof execution scan <intake_id_or_file>`
- `amof execution report <scan_id>`

Alternative if inventory proves stronger CLI alignment:

- `amof runner scan <intake_id_or_file>`

## Report Semantics and Safety Vocabulary

Minimum report fields (extensible):

- `scan_id`
- `intake_id`
- `ticket_id`
- `active_context`
- `candidate_runners`
- `eligible_runners`
- `required_capabilities`
- `missing_capabilities`
- `mutation_policy_check`
- `safety_gates`
- `proposed_plan`
- `execution_status` (`NO_EXECUTION_PERFORMED`)
- `reason_no_execution`

Required safety vocabulary:

- `pass`
- `fail`
- `blocked`
- `insufficient_capability`
- `context_unavailable`

## Runtime Logs and Runs Linkage (Contract)

- Scan/report should be discoverable through existing runtime logs/runs surfaces where practical.
- Event names may include:
  - `execution_scan_started`
  - `execution_scan_completed`
  - `execution_report_written`
- Events must preserve no-execution semantics and never imply dispatch.
- Cost truth must preserve unknown as unknown (not `0.0`).

## Explicit Out Of Scope

- Actual remote execution.
- Mutation execution.
- Queue worker runtime.
- Kubernetes job creation.
- SSH/agent transport execution.
- Git write/commit/push.
- PR creation.
- Jira sync.
- Voice intake.
- Dashboard analytics.
- Model ladder redesign.
- Cloudflare/security changes.
- Provider auth/secret changes.

## Public/Private Boundary Impact

Public:

- Scan/report schema and vocabulary.
- No-execution semantics.
- Runner capability matching semantics for scan/report.
- Safety gate vocabulary.
- Runtime log linkage expectations.

Private (must not leak):

- Runner credentials.
- Transport credentials.
- Customer topology details.
- Private cluster details.
- Internal dispatch policy.
- Provider routing policy.

## Expected Implementation Surfaces (Later Slice)

- `scripts/amof/commands/execution.py` (or equivalent)
- `scripts/amof/cli.py`
- `scripts/amof/entrypoint.py`
- Execution scan/report helper module
- `tests/test_remote_execution_scan_report.py`
- Runtime logs/runs integration tests only if scan/report contract requires them

## Validation Plan (For Implementation Slice)

- Execution CLI help coverage.
- Valid scan fixture coverage.
- No eligible runner fixture coverage.
- Mutation blocked fixture coverage.
- Context mismatch fixture coverage.
- No-dispatch/no-mutation assertion tests.
- Runner registry regression checks.
- CLI intake regression checks.
- Console intake unaffected checks.
- Runs CLI regression checks.
- `git diff --check`
- Leakage check for key/token/bearer patterns

## Stop Conditions

Stop and escalate if any of the following is true:

- Implementation tries to dispatch work.
- Implementation needs remote credentials.
- Implementation needs queue worker runtime.
- Implementation mutates repository content.
- Implementation requires private topology in public code.
- Command taxonomy conflicts with existing CLI design and cannot be resolved boundedly.
- Canonical repo is dirty at ticket start or before promotion.
- `promote-main` real ticket linkage fails.

## Next Ticket Dependency

This ticket unlocks:

- `AMOF-300-LONG-RUN-BOUNDED-LOOPS-001`

It does not unlock mutation execution.
