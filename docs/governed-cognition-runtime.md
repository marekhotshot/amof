# Governed Cognition Runtime

Status: public architecture narrative for `v3.0.2`

AMOF is a governed cognition runtime with infrastructure awareness. It is not a
replacement for a developer, editor, CI system, or production control plane.

The core idea is simple: AMOF owns the loop around source truth, runtime truth,
receipts, and approvals. LLMs, local models, hosted providers, and remote IAL
gateways are cognition workers inside that loop.

```text
workspace truth + runtime evidence
        |
        v
AMOF governance layer
  - scope
  - receipts
  - provenance
  - approval boundaries
  - fail-closed errors
        |
        v
optional cognition worker
  - local model
  - hosted provider
  - remote IAL gateway
        |
        v
proposal, plan, handoff artifact, or reviewed bounded diff
```

## What AMOF Owns

AMOF owns the local operating contract:

- which repository is in scope
- where evidence is written
- whether a command is read-only or allowed to mutate
- what approval artifact is required before handoff or execution
- how provider failures are classified locally
- how receipts preserve provenance without turning target repos into state
  directories

This makes AMOF useful even when a cognition worker fails. A failed provider,
bad structured output, missing runtime evidence, or stale workspace should
become a visible receipt, not a hidden success claim.

## What Cognition Workers Own

Cognition workers generate text, plans, or structured proposals. They do not own
source truth, runtime truth, or mutation authority.

Examples:

- a local model can draft a plan
- a hosted provider can inspect bounded context
- a remote IAL gateway can route inference behind a public client contract

The public AMOF contract treats those workers as replaceable. Provider routing,
model ladders, and private gateway policy are not public runtime truth.

## Receipts And Runtime Truth

AMOF favors evidence that can be inspected after the fact:

- source repo status
- planning context receipts
- chat plan run directories
- provider profile references
- request ids and hashes
- validation logs
- fresh-clone verification

Receipts should describe what was verified, what failed, and which boundary
stopped the run. They should not store raw secrets or private orchestration
strategy.

## Policy Direction

The public direction is to keep policy visible as contracts and result shapes:

- fail closed on invalid inputs or invalid structured output
- distinguish local transport provider from upstream provider attribution
- preserve request ids and hashes when available
- require explicit approval before execution handoff
- keep private routing strategy out of public docs and code

Future policy work should publish stable public interfaces, not the private
decision logic that chooses providers, models, or operational routes.

## Current Verified Surface

As of `v3.0.2`, the verified public Runtime Authority surface includes:

- explicit runtime context selection (`amof context`)
- governed intake validation/submission (`amof intake`)
- `amof intake template --kind bounded_intake_task`
- runner registry metadata (`amof runner`)
- execution readiness scan/report (`amof execution`) with
  `NO_EXECUTION_PERFORMED`
- bounded loops (`amof loop`) with `NO_MUTATION_PERFORMED` and
  `NO_REMOTE_EXECUTION_DISPATCHED`
- run inspection and runtime logs evidence (`amof runs`)
- remote IAL client support with cost-truth semantics
  (`REMOTE_IAL_SMOKE_STATUS_EXPLICIT=PASS`)
- adopted repo context resolution for app-data ecosystems
- aggregate intake missing-field validation reporting

Current cloud-dev verification summary:

- `VERIFIED_RUNTIME_SURFACES=console-cloud-dev.amof.dev:LOW_RISK_EXPOSURE;ial-cloud-dev.amof.dev:SAFE_PUBLIC_SURFACE`
- `REGRESSIONS_FOUND=receipt_sidebar_polling_regression_detected_and_fixed;admin_context_switch_surfaces_missing_with_mixed_intent_classification`

This is the shipped surface. Anything beyond it should be documented as future
direction or private/operator evidence, not as public capability.
