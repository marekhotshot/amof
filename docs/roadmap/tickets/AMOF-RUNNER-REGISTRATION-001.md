# AMOF-RUNNER-REGISTRATION-001

Status: Draft artifact (contract-first; bounded planning slice only)
Track: Runtime authority runner metadata
Release: AMOF 3.0 (v3.0.0)

## Ticket ID

`AMOF-RUNNER-REGISTRATION-001`

## Goal

Define a bounded runner registration contract that allows runner metadata to be registered, listed, inspected, and validated against intake packets in planning-only mode, without executing any work.

## Scope

- Define runner identity contract fields and constraints.
- Define runner capability contract fields and compatibility semantics.
- Define runner status/health vocabulary for registry visibility.
- Define runner registration lifecycle (register, update heartbeat/health, list, inspect, validate).
- Define context-aware runner visibility rules and fail-closed behavior.
- Define intake-to-runner matching as planning metadata only.
- No execution dispatch in this ticket.
- No remote execution in this ticket.
- No mutation execution in this ticket.

## Runner Contract (Proposed Fields)

Required/core fields:

- `runner_id`
- `name`
- `context`
- `capabilities`
- `status`
- `max_concurrency`
- `last_seen`
- `registration_source`
- `trust_level`
- `supported_task_kinds`
- `allowed_mutation_modes`

Optional fields:

- `labels` and/or `tags`
- `endpoint_ref` (opaque connection reference only, no credentials/secrets)

Prohibited content:

- Raw credentials, auth tokens, API keys, bearer tokens, private execution transport secrets.

## Required Capabilities

- Register runner metadata.
- List registered runners.
- Show one runner by id.
- Doctor runner registry readiness and schema validity.
- Validate whether a runner can support an intake packet in planning-only mode.
- Expose candidate runner/match details as metadata only.

## Registration Lifecycle (Contract)

1. Runner metadata registration request is submitted locally.
2. Contract validation runs and either accepts or rejects clearly.
3. Accepted metadata is stored in runner registry for selected context.
4. List/show/doctor surfaces expose runner metadata and health/status vocabulary.
5. Intake-to-runner matching returns eligible runners and match rationale.
6. Matching outcome is advisory planning metadata only; no dispatch side effects.

## Status and Health Vocabulary

Minimum vocabulary (extensible):

- `registered`
- `ready`
- `degraded`
- `unreachable`
- `disabled`
- `retired`

Contract requirement:

- `doctor` must report explicit readiness/failure reasons without attempting execution transport.

## Intake-to-Runner Matching Semantics (Planning Only)

- Matching evaluates declared capabilities against intake contract metadata.
- Matching must honor context selection and fail closed on context mismatch/unavailability.
- Matching must honor allowed mutation modes; planning-only intake must remain read-only.
- Matching output includes candidate runner ids and compatibility rationale.
- Matching output is metadata only and must not trigger queueing, dispatch, or execution.

## Possible CLI Shape (Implementation Slice Later)

- `amof runner register <file>`
- `amof runner list`
- `amof runner show <runner_id>`
- `amof runner doctor`
- `amof runner match <intake_id>` (or equivalent planning-only command)

## Required Behavior (Implementation Acceptance Contract)

- Runner metadata can be registered locally without secrets.
- Invalid registration fails clearly with actionable errors.
- List/show surfaces return deterministic runner metadata views.
- Doctor reports registry readiness without executing work.
- Intake packet can be matched to eligible runner metadata without dispatch.
- Runner registration respects resolved context selection and fail-closed rules.
- No remote execution occurs.
- No repo mutation occurs.
- No secrets are printed.
- No fake cost `0.0` appears for unknown cost states.
- Existing `amof intake` behavior remains working.
- Existing Operator Console intake behavior remains working.
- Existing `amof runs` behavior remains working.

## Explicit Out Of Scope

- Remote execution.
- Job dispatch.
- Queue worker runtime.
- Kubernetes runner deployment.
- SSH execution.
- Jira sync.
- Voice intake.
- Dashboard analytics.
- Provider/model ladder redesign.
- Cloudflare/security changes.
- OpenRouter/gateway auth changes.

## Public/Private Boundary Impact

Public:

- Runner contract fields and validation semantics.
- Runner registration lifecycle semantics.
- Capability matching semantics.
- Status/health vocabulary.
- No-execution guarantee.

Private (must not leak):

- Real runner credentials.
- Cluster credentials/topology details.
- Internal endpoint URLs when sensitive.
- Provider routing policy.
- Execution transport secrets.

## Expected Implementation Surfaces (Later Slice)

- `scripts/amof/commands/runner.py` (or equivalent)
- `scripts/amof/cli.py`
- `scripts/amof/entrypoint.py`
- Runner registry helper under public-safe path
- `tests/test_runner_registration.py`
- Docs updates only if command semantics require clarification

## Validation Plan (For Implementation Slice)

- Runner CLI help coverage.
- Valid runner registration fixture coverage.
- Invalid runner registration fixture coverage.
- List/show/doctor tests.
- Intake-to-runner match tests.
- No-execution assertion tests.
- Context fail-closed regression tests.
- CLI intake regression checks.
- Console intake unaffected checks.
- `git diff --check`
- Leakage check for key/token/bearer patterns

## Stop Conditions

Stop and escalate if any of the following is true:

- Registration requires execution transport secrets.
- Implementation tries to dispatch work.
- Implementation requires runner daemon as a hard dependency.
- Implementation requires remote execution.
- Public contract would leak private topology.
- Canonical repo is dirty at ticket start or before promotion.
- `promote-main` real ticket linkage fails.

## Next Ticket Dependency

This ticket unlocks:

- `AMOF-REMOTE-EXECUTION-SCAN-REPORT-001`

It does not unlock mutation execution.
