# AMOF-300-RELEASE-TAG-DECISION-001

Status: Draft artifact (contract-first; bounded planning slice only)  
Track: AMOF 3.0 release/tag decision gate  
Release: AMOF 3.0 (v3.0.0)

## Ticket ID

`AMOF-300-RELEASE-TAG-DECISION-001`

## Mission

Define the decision gate for AMOF 3.0 tagging.

## Goal

Decide whether AMOF 3.0 may be tagged as `v3.0.0`, and whether a fresh remote IAL smoke is required before tag.

No tag is created in this ticket. No release implementation occurs in this ticket.

## Decision Marker

`AMOF_300_RELEASE_TAG_DECISION`

Required decision semantics:

- `NO_MUTATION_PERFORMED`
- `NO_REMOTE_EXECUTION_DISPATCHED`
- `REMOTE_IAL_SMOKE_STATUS_EXPLICIT`
- `TAG_DECISION_REQUIRES_OPERATOR_APPROVAL`

## Inputs and Current State

- Current public main: `f25961a4464704e1bec4aeebabaa05031302b68a`.
- `AMOF-300-RELEASE-CLOSEOUT-001` is complete and promoted.
- Closeout report exists at `docs/releases/amof-3.0-closeout.md`.
- Closeout fresh clone exists:
  - `/home/hotshot/work/amof-operating/receipts/fresh-clones/AMOF-300-RELEASE-CLOSEOUT-001-amof-f25961a`
- Local closeout chain passed (context/intake/runner/execution/loop/runs surfaces).
- Most recent remote IAL smoke during closeout is blocked:
  - classification: `REMOTE_IAL_SMOKE_BLOCKED`
  - cause: token expired (`[remote-ial/401/auth] Run token has expired`)
- Prior known-good remote IAL smoke exists:
  - request id `f5701fd4-61ab-401a-9371-7c3c1e2909c6`

## Required Decision Options

- `TAG_NOW_WITH_REMOTE_SMOKE_CAVEAT`
- `REFRESH_TOKEN_AND_RERUN_REMOTE_SMOKE_BEFORE_TAG`
- `BLOCK_RELEASE`

Recommended default:

- `REFRESH_TOKEN_AND_RERUN_REMOTE_SMOKE_BEFORE_TAG`

Reason:

- Local runtime authority proof is strong and complete.
- Most recent remote IAL validation failed due to operational token freshness, not known product regression.
- A release tag decision should avoid carrying an avoidable blocked validation when a bounded refresh/rerun can clear it.

## Decision Acceptance Criteria (For Later Implementation)

- Public main is clean.
- Fresh clone verification is present.
- Closeout report exists and is readable.
- Local closeout smoke passed and is evidenced.
- Known limitations are documented in release closeout materials.
- Remote IAL status is explicitly classified as one of:
  - fresh pass, or
  - blocked by token expiry with known-good prior evidence and explicit operator caveat acceptance.
- No mutation/dispatch claims are overstated.
- Release notes keep scan/report-only and bounded-loop-only limitations explicit.

## Required Decision Output Contract (Later Slice)

Decision output must include:

- selected decision option
- operator approval state
- rationale summary
- supporting evidence paths
- latest public main SHA
- remote IAL smoke status classification
- caveat text when remote IAL is blocked
- explicit no-mutation/no-dispatch posture

## Out of Scope

- Creating the tag.
- Changing version values.
- Publishing packages.
- Changing auth/secret/token handling.
- Rerunning deployment flows.
- Adding product features.
- Implementing remote execution.
- Mutation execution.
- Queue worker.
- Kubernetes job runtime.
- Jira/voice/dashboard work.

## Public/Private Boundary

Public:

- release decision criteria
- evidence requirements
- risk/caveat classification
- no-mutation/no-dispatch semantics

Private (must not leak):

- secrets and provider keys
- private token material
- private gateway internals
- customer topology and internal routing policy

## Stop Conditions

Stop and escalate if any of the following is true:

- Ticket attempts to create tags in this artifact slice.
- Ticket attempts to add features instead of decision criteria.
- Ticket hides blocked remote smoke status.
- Ticket changes provider/auth/secret configuration.
- Canonical repo is dirty.
- `promote-main` real ticket linkage fails.

## Next Possible Tickets

- `AMOF-300-REMOTE-IAL-SMOKE-REFRESH-001`
- `AMOF-300-RELEASE-TAG-001`
- `AMOF-300-PACKAGE-PUBLISH-001`
