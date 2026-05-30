# AMOF-CONSOLE-INTAKE-001

Status: Draft artifact (contract-first; bounded implementation planning only)
Track: Runtime authority intake clients
Release: AMOF 3.0 (v3.0.0)

## Ticket ID

`AMOF-CONSOLE-INTAKE-001`

## Goal

Add a bounded Operator Console intake surface that submits planning-only/no-mutation intake packets under the same intake contract semantics as `AMOF-CLI-INTAKE-001`.

## Scope

- Define a minimal console intake input surface (form or JSON/YAML paste path) for intake packet creation/submission.
- Define validation behavior against existing intake contract semantics from `AMOF-INTAKE-CONTRACT-001`.
- Define planning-only submission behavior consistent with current CLI intake constraints.
- Define context-aware submission behavior with fail-closed semantics from `AMOF-RUNTIME-CONTEXT-SWITCHING-001`.
- Define runtime log linkage and runs/sidebar visibility requirements for console submissions.
- Reuse existing sidebar polling/receipt discovery behavior where possible.
- No remote execution in this ticket.
- No runner dispatch in this ticket.
- No Jira sync and no voice intake in this ticket.

## Expected Console Behavior (MVP Target)

- User can create or paste an intake packet in the console.
- Console validates packet shape/required fields before submit.
- Console submit creates a compatible intake submission record through existing backend-compatible routes or local/private intake backend storage.
- Submission status/result is shown in console without exposing secrets.
- Submission appears in runs/sidebar without manual page refresh where backend polling already supports updates.
- Unknown cost is displayed as `unknown` and must never be shown as `0.0`.
- Raw `provider_generation_id` is never exposed in console output.

## Required Behavior

- Preserve planning-only/no-mutation semantics for this slice.
- Reject mutation/runner/remote-execution requests fail-closed for MVP.
- Preserve context selection visibility and fail-closed behavior (no silent fallback).
- Emit/record runtime metadata that links intake id, ticket id, context, and mutation mode.
- Keep UI/runtime outputs public-safe: no secret values, no private topology leakage.
- Keep existing CLI intake and runs read surfaces compatible.

## Acceptance Criteria

- Console can submit a valid planning-only intake packet.
- Invalid intake packet fails clearly before submission.
- Intake packets requiring mutation or remote execution fail closed for MVP.
- Selected/resolved context is visible in submission metadata or status.
- Submission creates/updates a run or receipt source consumed by sidebar/runs surfaces.
- Sidebar surfaces show new submission without manual refresh when polling path is available.
- Unknown cost remains unknown and is never rendered as `0.0`.
- No raw `provider_generation_id` in console-visible outputs.
- No secrets shown in UI/API payloads/logs.
- Existing CLI intake commands remain working.
- Existing runs CLI remains working.
- Existing console sidebar polling behavior remains working.

## Explicit Out Of Scope

- Runner registration (`AMOF-RUNNER-REGISTRATION-001`).
- Remote execution lanes.
- Mutation execution.
- Jira sync.
- Voice intake.
- Dashboard analytics.
- Model ladder redesign.
- Cloudflare/security changes.
- OpenRouter/gateway secret changes.

## Public/Private Boundary Impact

Public:

- Intake contract semantics.
- Validation and mutation-policy semantics.
- Context/fail-closed semantics.
- Runtime log linkage expectations.

Private (must not leak):

- Console implementation details.
- Private gateway/controlplane topology.
- Internal receipt proxy credentials and auth/session wiring.
- Customer-specific configuration and routing internals.

## Proposed Implementation Files (Later Slice)

Private repo likely surfaces:

- `services/operator-console/src/components/operator-console.tsx`
- `services/operator-console/src/lib/types.ts`
- `services/operator-console/src/app/api/...` (only if one bounded intake route is required)
- Focused console tests where available

Public repo only if shared contract behavior requires bounded helper reuse/update:

- `scripts/amof/commands/intake.py`
- Shared intake/runtime tests if a cross-surface contract helper changes

## Validation Plan (For Implementation Slice)

- Console typecheck/build (private repo implementation slice).
- Focused UI/data-layer tests for intake submission/status behavior where available.
- Deterministic mocked submission tests if browser automation is unavailable.
- Manual operator browser smoke:
  - open console
  - submit valid planning-only intake
  - verify status/result
  - verify sidebar updates
  - verify unknown cost behavior
  - verify no raw `provider_generation_id`
- CLI regression checks:
  - `amof intake validate`
  - `amof intake submit`
  - `amof runs list`
- `git diff --check`
- Leakage scan for token/key/bearer patterns

## Risk Classification

- Overall: Medium
- Primary risks:
  - Console/CLI drift for shared intake semantics and mutation policy handling.
  - Auth/session boundaries in private console path causing submission-readback mismatch.
  - Sidebar update path requiring larger backend redesign than bounded polling reuse.
  - Context handling regression that could reintroduce silent fallback behavior.

## Stop Conditions

Stop and escalate if any of the following is true:

- Implementation requires runner registration to satisfy MVP.
- Implementation requires remote execution to satisfy MVP.
- Console cannot reach intake backend source without auth/session redesign.
- Public/private boundary would leak private topology or credentials.
- Existing sidebar polling must be redesigned broadly instead of reused.
- Canonical public repo is dirty at ticket start or before promotion.
- Canonical private repo is dirty and no clean private worktree is available for implementation phase.

## Next Ticket Dependency

This ticket unlocks:

- `AMOF-RUNNER-REGISTRATION-001`

This ticket does not unlock remote execution directly.
