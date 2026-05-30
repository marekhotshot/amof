# AMOF-300-LONG-RUN-BOUNDED-LOOPS-001

Status: Draft artifact (contract-first; bounded planning slice only)  
Track: Runtime authority long-run loop discipline  
Release: AMOF 3.0 (v3.0.0)

## Ticket ID

`AMOF-300-LONG-RUN-BOUNDED-LOOPS-001`

## Mission

Define the first controlled AMOF 3.0 long-run bounded-loop proof.

## Goal

Prove AMOF can run a bounded, observable, stop-condition-driven operator loop using existing safe surfaces only:

- intake
- runtime context
- runner registry
- execution scan/report
- runtime logs
- receipts/evidence
- cost truth

The first long-run is not an autonomy demo. It is a loop-discipline proof.

## Core Principle

bounded loop first; execution authority later

## Required Scope

- Define loop packet/run objective shape.
- Define max loop count semantics.
- Define per-loop evidence requirements.
- Define cost and status accounting semantics.
- Define stop conditions and terminal semantics.
- Define failure classification model.
- Define required long-run artifacts.
- Define runtime logs and runs CLI inspection/link expectations.
- Define how execution scan/report participates without execution.
- Define explicit no-mutation/no-dispatch boundary.
- No mutation execution in this ticket.
- No remote execution dispatch in this ticket.
- No queue worker in this ticket.
- No Kubernetes job execution in this ticket.
- No SSH/agent transport execution in this ticket.
- No autonomous repo edits in this ticket.
- No Jira sync in this ticket.
- No voice intake in this ticket.

## Proposed Loop Shape (Implementation Later)

1. Load intake packet.
2. Resolve runtime context.
3. Match eligible runner metadata.
4. Produce execution scan/report.
5. Record runtime events.
6. Evaluate stop condition.
7. Repeat until max loop count or terminal condition.
8. Produce final long-run report.

## Loop Contract and Packet Shape (Planning Contract)

Minimum long-run contract fields (extensible):

- `loop_run_id`
- `ticket_id`
- `intake_reference`
- `resolved_context`
- `max_loops`
- `stop_policy`
- `failure_policy`
- `evidence_policy`
- `cost_policy`
- `no_mutation_mode` (must be enabled for this first slice)
- `no_dispatch_mode` (must be enabled for this first slice)

## Required Final Report Fields

Long-run report output must include at minimum:

- `run_id`
- `ticket_id`
- `intake_id`
- `context`
- `runner_candidates`
- `scan_ids`
- `loop_count`
- `max_loops`
- `status`
- `stop_reason`
- `cost_status`
- `estimated_cost` (only when observed)
- `events_path`
- `reports_path`
- `receipts_path` (if available)
- `final_verdict`
- explicit mutation/dispatch status

Required fixed phrases in successful reports:

- `NO_MUTATION_PERFORMED`
- `NO_REMOTE_EXECUTION_DISPATCHED`

## Stop Conditions and Failure Classification

Stop conditions:

- `max_loops_reached`
- `terminal_success_condition_met`
- `fail_closed_gate_triggered`
- `operator_cancelled`
- `data_contract_invalid`

Failure classes (minimum vocabulary):

- `context_resolution_failure`
- `intake_validation_failure`
- `runner_eligibility_failure`
- `execution_scan_failure`
- `artifact_write_failure`
- `policy_violation_blocked`

Any fail-closed gate must stop the loop immediately and produce a clear final report state.

## Cost Truth and Status Semantics

- Cost status must be explicit per iteration and final report (`unknown`, `observed`, `blocked` or equivalent).
- Unknown cost must remain unknown and must not be represented as `0.0`.
- `estimated_cost` may appear only when a trusted observed value exists.
- Runtime events should capture cost status transitions when they occur.

## Runtime Logs, Runs CLI, and Artifact Linkage Contract

- Each loop iteration should emit runtime events with loop iteration index and decision outcome.
- Final long-run report must be discoverable through existing runtime log/runs surfaces or linkable via explicit paths.
- Long-run events must reference related execution scan IDs without implying dispatch.
- Long-run contract must preserve:
  - `NO_MUTATION_PERFORMED`
  - `NO_REMOTE_EXECUTION_DISPATCHED`

Known carry-forward risk:

- Execution scan artifacts currently write under `AMOF_HOME/share/execution-scans`.
- Runs CLI currently discovers `AMOF_HOME/share/runs`.
- Later implementation must choose one of:
  - write loop artifacts under `share/runs`, or
  - add a cross-surface index, or
  - explicitly link execution scans from long-run events.
- This artifact records the decision point as an implementation acceptance requirement, not an implementation decision.

## Acceptance Criteria (For Later Implementation)

- Valid intake plus eligible runner can run bounded loop in no-mutation mode.
- Operator can set `--max-loops` count.
- Loop stops at max count.
- Loop stops on fail-closed gate.
- Loop emits runtime events per iteration.
- Loop produces final report with required fields.
- Cost is unknown/observed and never fake `0.0`.
- Existing intake CLI behavior still works.
- Existing runner CLI behavior still works.
- Existing execution scan/report behavior still works.
- Existing runs CLI can inspect or at least link to long-run artifacts.
- No repo mutation occurs.
- No remote command is dispatched.
- No queue worker or Kubernetes job is created.
- No secrets are printed.

## Candidate CLI Shape (Implementation Slice Later)

Primary candidate:

- `amof loop run <intake_id_or_file> --max-loops N`
- `amof loop show <loop_run_id>`
- `amof loop logs <loop_run_id>`

Alternative taxonomy may be selected during implementation if inventory proves a better fit. Final command shape is intentionally not fixed by this artifact if uncertainty remains.

## Explicit Out of Scope

- Actual remote execution.
- Mutation execution.
- Queue worker runtime.
- Kubernetes job creation.
- SSH/agent transport.
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

- Loop contract and packet semantics.
- Stop condition and failure classification semantics.
- No-mutation/no-dispatch semantics.
- Runtime log and runs linkage contract.
- Long-run report schema.
- Cost truth semantics.

Private (must not leak):

- Private execution credentials.
- Customer topology.
- Private runner endpoints.
- Dispatch policy internals.
- Provider routing policy internals.
- Internal infrastructure credentials.

## Expected Implementation Surfaces (Later Slice)

- `scripts/amof/commands/loop.py` (or equivalent)
- `scripts/amof/cli.py`
- `scripts/amof/entrypoint.py`
- loop report helper
- runtime event integration helpers
- `tests/test_long_run_bounded_loops.py`
- runs CLI integration tests if needed

## Validation Plan (For Later Implementation)

- loop CLI help coverage
- valid bounded loop fixture
- max-loop stop condition fixture
- fail-closed stop condition fixture
- no-mutation/no-dispatch assertion checks
- execution scan/report regression checks
- runner registry regression checks
- CLI intake regression checks
- runtime logs regression checks
- runs CLI inspection or link assertion
- `git diff --check`
- leakage check for key/token/bearer patterns

## Stop Conditions (Operator Gate)

Stop and escalate if any of the following is true:

- Implementation tries to dispatch work.
- Implementation needs remote credentials.
- Implementation mutates repository content.
- Implementation requires queue worker runtime.
- Implementation creates Kubernetes jobs.
- Implementation introduces autonomous file edits.
- Implementation requires private topology in public code.
- Command taxonomy conflicts with existing CLI design.
- Canonical repo is dirty.
- `promote-main` real ticket linkage fails.

## Next Ticket Dependency

This ticket unlocks:

- `AMOF-300-RELEASE-CLOSEOUT-001`

It does not unlock mutation execution.
