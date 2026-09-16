# AMOF-INTAKE-CONTRACT-001

Status: Draft artifact (contract-first; implementation bounded to contract surfaces)
Track: Runtime authority intake contract
Release: AMOF 3.0 (v3.0.0)

## Ticket ID

`AMOF-INTAKE-CONTRACT-001`

## Goal

Define the canonical AMOF intake contract that every intake client (CLI, console, editor adapters) must follow before any execution handoff.

## Scope

- Freeze public intake semantics as a planning-only contract.
- Define required truth-domain separation: `source_truth`, `runtime_truth`, `workspace_truth`.
- Define required mutation policy declaration (`allowed_mutations`, `forbidden_mutations`).
- Define mandatory validation gates, ambiguities, stop conditions, risk classification, and disposition semantics.
- Define required handoff envelope and schema alignment to Director intake execution contract.
- Provide clear public-safe boundaries for what intake can and cannot assert.

## Explicit Out Of Scope

- Runner registration and remote execution implementation.
- CLI intake implementation details (`AMOF-CLI-INTAKE-001`).
- Console intake implementation details (`AMOF-CONSOLE-INTAKE-001`).
- Jira sync and voice intake.
- Dashboard analytics.
- Cloudflare/security changes.
- Provider/model ladder redesign.
- Secret management or runtime credential rotation.

## Acceptance Criteria

- Intake is explicitly planning-only and never implies execution authorization.
- Contract requires all three truth domains and forbids collapsing unknown runtime truth into assumed facts.
- Contract requires explicit `allowed_mutations` and `forbidden_mutations`.
- Contract requires explicit validation gates before any execution handoff.
- Contract requires explicit stop conditions/ambiguities for under-evidenced or mixed-authority tasks.
- Contract defines final disposition values (`replay_now`, `replay_later`, `defer`, `kill`) and a bounded executor prompt.
- Contract remains public-safe and does not include private topology or secret values.

## Validation Commands

- `python3 -m py_compile scripts/amof/commands/chat.py scripts/amof/commands/director.py`
- `python3 tests/test_chat_planning.py`
- `python3 tests/test_runtime_logs_contract.py`
- `python3 tests/test_runs_cli.py`
- `python3 tests/test_remote_ial.py`
- `git diff --check`
- `rg -n "secret|token|Bearer|OPENROUTER_API_KEY|provider_generation_id" docs/contracts examples tests`

## Risk Classification

- Overall: Medium-High
- Primary risks:
  - Contract ambiguity causing client drift across CLI/console/editor adapters.
  - Blended truth domains causing unsafe handoff decisions.
  - Mutation boundaries underspecified, leading to accidental authority escalation.
  - Over-scoping this ticket into runtime implementation work.

## Public/Private Boundary Impact

Public:

- Intake contract semantics.
- Required truth domains and disposition model.
- Validation and mutation-boundary contract.
- Public schema and examples for handoff envelope behavior.

Private (must not leak):

- Runtime credentials, bearer tokens, API keys.
- Internal customer topology and private infrastructure details.
- Private provider routing or gateway internals.

## Expected Files

Primary contract surfaces for this ticket:

- `contracts/director-intake-client-contract.md`
- `contracts/director-intake-execution-contract.schema.json` (if contract fields require update)
- `contracts/examples/` intake examples (if needed for bounded canonical examples)
- `docs/roadmap/tickets/AMOF-INTAKE-CONTRACT-001.md` (this artifact)

## Stop Conditions

Stop and escalate if any of the following is true:

- Required changes cross into runtime execution implementation.
- Changes require private topology details in public contract files.
- Contract update requires broad governance/release workflow redesign.
- Canonical public repo is dirty or not on expected main before ticket start.
- Validation cannot prove planning-only behavior and boundary safety.

## Next Ticket Dependency

This ticket unlocks implementation-level intake slices:

- `AMOF-CLI-INTAKE-001`
- `AMOF-CONSOLE-INTAKE-001`

Sequencing note: do not start intake implementation tickets until this contract ticket is promoted.

