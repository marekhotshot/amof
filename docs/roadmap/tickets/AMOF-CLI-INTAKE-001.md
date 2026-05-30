# AMOF-CLI-INTAKE-001

Status: Draft artifact (contract-first; bounded implementation planning only)
Track: Runtime authority intake clients
Release: AMOF 3.0 (v3.0.0)

## Ticket ID

`AMOF-CLI-INTAKE-001`

## Goal

Add a bounded AMOF CLI intake surface so operators can validate, submit, and inspect intake packets from the command line using the intake contract defined in `AMOF-INTAKE-CONTRACT-001`.

## Scope

- Define the CLI intake command surface for MVP planning and validation workflows.
- Define intake packet validation behavior for YAML/JSON contract conformance.
- Define planning-first submission semantics (read-only by default, no mutation by default).
- Define explicit context awareness using `AMOF-RUNTIME-CONTEXT-SWITCHING-001` semantics.
- Define required runtime logs/events linkage for intake identifiers and policy metadata.
- No remote execution in this ticket.
- No runner registration in this ticket.
- No console intake implementation in this ticket.

## Proposed CLI Shape (MVP Target)

- `amof intake validate <file>`
- `amof intake submit <file>`
- `amof intake show <intake_id>`
- `amof intake list`

Note: MVP may reduce this surface only if inventory proves a smaller slice is safer while still satisfying acceptance criteria.

## Required Behavior

- Validate intake YAML/JSON against canonical intake contract fields and semantics.
- Reject invalid intake packets with clear, actionable error messages.
- Respect `mutations.allowed` and `mutations.forbidden` policy semantics.
- Default to planning/read-only behavior and require explicit operator confirmation for any future mutation path.
- No silent fallback for context resolution; selected or resolved context must be explicit and fail closed when unavailable.
- Record intake metadata where applicable: `intake_id`, `ticket_id`, selected/resolved context, and mutation policy summary.
- Ensure runtime logs remain public-safe (no secret values, no private topology details).
- Unknown provider cost remains unknown/null and must never be coerced to fake `0.0`.

## Acceptance Criteria

- A valid intake artifact (for example `examples/intake/amof-self-scan.yaml`) validates successfully.
- Invalid intake fixtures fail clearly and non-zero.
- `submit` creates a local intake/run record or bounded planning run without mutation.
- Selected runtime context is visible in intake/run metadata and traceable in runtime events.
- Intake submission emits runtime lifecycle events that can be inspected later.
- If a run is created, `amof runs list/show/logs` can inspect it.
- Unknown cost status is preserved (`unknown` or null), never rewritten to `0.0`.
- Existing `amof chat plan --minimal-context` behavior continues to pass.
- Existing context commands (`list`, `show`, `use`, `doctor`) continue to pass with fail-closed semantics.

## Explicit Out Of Scope

- Console intake implementation (`AMOF-CONSOLE-INTAKE-001`).
- Runner registration.
- Remote execution lanes.
- Jira sync.
- Voice intake.
- Dashboard analytics.
- Model ladder redesign.
- Provider auth changes.
- Cloudflare/security changes.

## Public/Private Boundary Impact

Public:

- Intake schema and contract semantics.
- CLI validation/submission/read semantics.
- Mutation policy semantics.
- Context selection and fail-closed semantics.
- Runtime log linkage between intake and run metadata.

Private (must not leak):

- Customer-specific topology.
- Private credentials and bearer tokens.
- Gateway/provider routing policy internals.
- Internal runner credentials and private execution lane details.

## Expected Implementation Files (Later Slice)

- `scripts/amof/commands/intake.py` (or equivalent intake command module)
- `scripts/amof/cli.py`
- `scripts/amof/entrypoint.py`
- Existing intake contract helpers (only if already present and reusable)
- `tests/test_cli_intake.py`
- Runtime logs tests only where intake event linkage coverage is needed
- Public docs updates only if command semantics require release-facing documentation

## Validation Plan (For Implementation Slice)

- `python3 scripts/amof.py intake --help`
- `python3 scripts/amof.py intake validate examples/intake/amof-self-scan.yaml`
- Invalid fixture validation (expected non-zero)
- Submit a read-only intake in isolated `AMOF_HOME`
- Verify runtime events/log metadata and context linkage
- Verify `amof runs` CLI visibility for created run records, when applicable
- Context fail-closed regression checks
- `python3 tests/test_context_cli.py`
- `python3 tests/test_runs_cli.py`
- `python3 tests/test_runtime_logs_contract.py`
- `git diff --check`
- Leakage scan for token/key/bearer patterns

## Risk Classification

- Overall: Medium
- Primary risks:
  - Contract/CLI drift causing inconsistency with `AMOF-INTAKE-CONTRACT-001`.
  - Ambiguous mutation handling leading to accidental authority expansion.
  - Context handling regressions that could reintroduce silent local fallback.
  - Over-scoping into execution or runner registration before bounded gates are complete.

## Stop Conditions

Stop and escalate if any of the following is true:

- Implementation requires runner registration to deliver MVP behavior.
- Implementation requires remote execution to satisfy baseline acceptance.
- Mutation semantics become ambiguous and cannot be bounded safely.
- Context selection would silently fallback instead of failing closed.
- Intake schema is insufficient and would require broad contract redesign.
- Public/private boundary cannot be preserved without leaking private topology.
- Canonical public repo is dirty at ticket start or before promotion steps.

## Next Ticket Dependency

This ticket unlocks:

- `AMOF-CONSOLE-INTAKE-001`

This ticket does not unlock runner registration or remote execution.
