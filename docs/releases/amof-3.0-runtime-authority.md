# AMOF 3.0 Runtime Authority is live.

Status: released
Canonical version: `v3.0.3`
Code name: `AMOF-ULTRAPLAN-300`
Related:
- `docs/roadmap/AMOF-ULTRAPLAN-300.md`
- `docs/governed-cognition-runtime.md`
- `docs/releases/amof-3.0-closeout.md`
- `docs/releases/amof-3.0.0-tag.md`

`v3.0.0` remains as historical evidence of a broken escaped release tag.
Current install/update release truth is `v3.0.3`.

`v3.0.3` packages the post-`v3.0.2` dogfood improvements already promoted on
public `main`: `amof runner template --kind local-planning`, local runner
register/list/doctor/match readiness flow, execution scan readiness reporting,
and standalone smoke version-text hygiene, without changing runtime execution
semantics.

AMOF now owns runtime truth across intake, context, runners, scans, bounded
loops, receipts, and evidence — while cognition workers remain replaceable.

AI agents are cheap. Runtime truth is not.

AMOF is a local-first CLI and governed cognition runtime that controls context,
execution readiness, policy attribution, receipts, and evidence before cognition
workers mutate anything. It is not just a chatbot or a generic AI wrapper.

## Runtime Authority

AMOF does not treat chat output as runtime truth. Runtime truth is recorded and
exposed through:

- context selection records
- intake validation/submission records
- runner registry metadata
- execution scan/report artifacts
- bounded loop summaries
- run inspection records
- runtime logs
- receipts and evidence surfaces

## Governed Intake

AMOF accepts messy work through intake, validates contract shape and policy
constraints, preserves planning-only behavior where required, and routes work
toward governed execution readiness instead of ad hoc prompting.

## Context Discipline

AMOF context is explicit. If required remote/cloud context is unavailable,
AMOF fails closed with a clear error and does not silently fallback to local.

## Runner Registry

Workers/runners are metadata-driven and replaceable. The AMOF runtime owns
coordination and authority; any individual cognition worker is not the runtime
authority.

## Execution Scan / Report

AMOF can scan readiness, match intake to runners, identify blockers, and emit
reports without dispatching remote execution.

Truth markers:
- `NO_EXECUTION_PERFORMED`

## Bounded Loops

AMOF supports controlled long-running loops with stop conditions, evidence, and
policy discipline.

Truth markers:
- `NO_MUTATION_PERFORMED`
- `NO_REMOTE_EXECUTION_DISPATCHED`

## Receipts and Evidence

AMOF records runtime facts through receipts and evidence surfaces while
preserving public-safe boundaries:

- no secrets
- no raw prompts
- no raw `provider_generation_id`
- no private customer topology
- no sensitive auth material in public surfaces

AMOF preserves provider cost truth:

- missing provider cost remains unknown/null
- missing provider cost is never reported as fake `0.0`

## Operator Console Preview

Label: **Cloud-dev live preview**

The cloud-dev Operator Console exposes AMOF runtime receipts, intake
submissions, selected runs, policy attribution, and sanitized evidence/debug
surfaces. It is a live preview over the current AMOF runtime path, not a fake
demo surface.

Caution: Cloud-dev preview. Public-safe runtime surfaces only. Known gaps are
tracked as follow-up slices.

CTA: [Open Operator Console Preview](https://console-cloud-dev.amof.dev/)

IAL reference (auth-bound surface): [https://ial-cloud-dev.amof.dev/](https://ial-cloud-dev.amof.dev/)

## Current Known Next Slices

- Runtime logs viewer contract and minimal UI
- Receipt count semantics contract
- Console rollout guardrail comparing deployed hash vs intended source

## Scope Boundaries

- This release does not claim production readiness.
- This release does not claim enterprise/customer deployment.
- This release does not claim autonomous remote dispatch is live.
- Public scope remains scan/report/read-only/planning-first with bounded loops.
- Local planning runner registration and readiness remain planning-only with no
  execution dispatch or mutation behavior introduced.
- `hotshot.sk` external repo dogfood passed for public CLI adoption/context and
  intake-template validation; that is dogfood evidence, not a claim of full
  enterprise platform completion.
