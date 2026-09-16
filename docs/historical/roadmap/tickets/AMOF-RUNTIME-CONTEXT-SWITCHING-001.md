# AMOF-RUNTIME-CONTEXT-SWITCHING-001

Status: Draft artifact (implementation blocked pending review/approval)
Track: Runtime authority context contract
Release: AMOF 3.0 (v3.0.0)

## Ticket ID

`AMOF-RUNTIME-CONTEXT-SWITCHING-001`

## Goal

Add explicit runtime context selection semantics for AMOF operator runs so the operator can choose and inspect where work is running (`local`, `cloud-dev`, `msg-aws-dev`, and future customer contexts).

## Core Principle

No silent fallback. If the selected context is unavailable, AMOF fails closed with a clear operator-facing explanation and does not silently run locally.

## Approved Implementation Constraints

### Active Context Storage

Active context is stored in user-local AMOF config/state. The repository is not mutated by default.

### Context Resolution

Context resolution order is explicit:

1. explicit CLI flag, if introduced later
2. selected user-local AMOF context
3. profile default context
4. built-in default: local

### Fail-Closed Rule

If a remote/cloud context is selected and unavailable, AMOF fails closed and must not silently fall back to local.

## Scope

- Define a minimal context-selection contract for CLI/runtime behavior.
- Define how operators list, inspect, and select active context.
- Define validation/doctor behavior for selected context health and readiness.
- Define runtime metadata/event requirements that expose selected context for run correlation.
- Preserve existing `amof chat plan --minimal-context` behavior and cost-truth guarantees.
- Keep remote IAL profile/model behavior explicit and non-implicit.

## Required Capabilities

- List available contexts.
- Show active context.
- Select active context.
- Validate selected context (`doctor`-style contract).
- Expose active context in run metadata/events where relevant.
- Preserve current minimal-context planning behavior.
- Keep remote provider profile/model explicit (no hidden switching).

Possible CLI shape for implementation ticket:

- `amof context list`
- `amof context show`
- `amof context use <name>`
- `amof context doctor`

MVP note: final command shape may be reduced if existing CLI taxonomy requires a smaller bounded surface.

## Out Of Scope

- Runner registration.
- Remote execution implementation.
- Console intake implementation.
- Jira sync.
- Voice intake.
- Dashboard analytics.
- Cloudflare/security changes.
- OpenRouter secret rotation.
- Provider model ladder redesign.

## Acceptance Criteria

- Operator can explicitly select a context.
- Active context is visible in redacted config/render/doctor output.
- Minimal-context run records selected context (or `local`) in runtime logs.
- Invalid context fails with a clear error.
- Remote context unavailable does not silently fall back to local.
- Existing `amof chat plan --minimal-context` continues to work.
- No secrets are printed.
- No provider cost is rendered as fake `0.0`; unknown remains explicit.

## Validation Commands (Planned)

- `python3 -m py_compile scripts/amof/cli.py scripts/amof/commands/chat.py`
- `python3 scripts/amof.py context --help` (or final chosen command group help)
- `python3 -m unittest tests.test_chat_planning` (plus focused context-switch tests when added)
- `python3 -m unittest tests.test_runtime_logs_contract` (context field/log contract coverage)
- `python3 scripts/amof.py chat plan --minimal-context --help`
- Minimal-context smoke in selected context (bounded, operator-safe fixture/profile)
- `git diff --check`
- Leakage check for secret-like keys/tokens in changed files (no secret output in docs/CLI)

## Risk Classification

- Overall: High (runtime authority and fail-closed semantics are safety-critical).
- Primary risks:
  - Silent fallback risk (`remote -> local`) causing wrong execution surface.
  - Operator confusion if active context is not visible or auditable.
  - Contract drift between CLI context state and runtime event metadata.
  - Regression risk to existing minimal-context planning path.

## Public/Private Boundary Impact

Public contract surface:

- Context names and selection semantics.
- CLI and config contract behavior.
- Fail-closed semantics.
- Redacted context visibility in logs/status/doctor outputs.

Private-only details (must not leak):

- Gateway secrets and raw credentials.
- Customer-specific topology/internal routing details.
- Private cluster credentials/policies.

## Expected Files (Implementation Planning)

Public repo candidates (bounded, subject to final inventory):

- `scripts/amof/cli.py` (context command group wiring)
- `scripts/amof/commands/` (new or extended context command module)
- `scripts/amof/commands/chat.py` (runtime metadata hook-in only if required)
- `scripts/amof/orchestrator/events.py` (context field in event contract, if absent)
- `tests/` focused context-switching tests (CLI behavior + fail-closed + runtime logs)
- `docs/operations/runtime-logs-contract.md` (only if contract text requires update)
- `docs/roadmap/tickets/AMOF-RUNTIME-CONTEXT-SWITCHING-001.md` (this artifact)

## Stop Conditions

Stop before implementation and escalate if any of the following is true:

- Context switching requires runner registration or remote execution implementation.
- Required behavior depends on private topology details that would leak in public code/docs.
- Work implies broad config platform redesign beyond this bounded MVP.
- Canonical target repo is dirty and cannot safely host bounded ticket work.
- Validation cannot prove fail-closed behavior and non-regression for minimal-context runs.

## Next Ticket Dependency

Upstream dependency status: ready (prior items in sequence complete).

Downstream dependency this ticket unlocks:

- `AMOF-INTAKE-CONTRACT-001`

Sequencing note: do not start `AMOF-INTAKE-CONTRACT-001` until this context contract ticket is implemented, validated, and promoted.

## Implementation Gate

This artifact is intentionally planning/contract-only. No implementation work should begin until this ticket artifact is reviewed or explicitly approved.
