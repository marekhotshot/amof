# AMOF-300-RELEASE-CLOSEOUT-001

Status: Draft artifact (contract-first; bounded planning slice only)  
Track: AMOF 3.0 runtime authority release closeout  
Release: AMOF 3.0 (v3.0.0)

## Ticket ID

`AMOF-300-RELEASE-CLOSEOUT-001`

## Mission

Define the AMOF 3.0 release closeout evidence and readiness slice.

## Goal

Produce the final AMOF 3.0 release closeout plan proving the runtime authority spine works end-to-end without mutation and without remote dispatch.

This is not a feature slice. This is release evidence consolidation.

## Core Principle

release evidence first; new capability later

## Release Statement Contract

The closeout report must explicitly assert:

`AMOF_300_RUNTIME_AUTHORITY_CLOSEOUT`

AMOF 3.0 proves:

- explicit runtime context
- bounded intake
- runner capability registry
- no-execution scan/report
- bounded loop discipline
- runtime logs and run visibility
- remote IAL cost truth
- fail-closed behavior
- no fake cost `0.0`
- no mutation
- no remote dispatch

Required fixed phrases in closeout report:

- `NO_MUTATION_PERFORMED`
- `NO_REMOTE_EXECUTION_DISPATCHED`
- `COST_TRUTH_OBSERVED_OR_EXPLICIT_UNKNOWN`
- `FAIL_CLOSED_NO_SILENT_FALLBACK`

## Required Closeout Proof Chain (Implementation Later)

- Fresh public `main` state.
- Fresh clone verification.
- Installable CLI smoke.
- Config/minimal-context smoke.
- Runtime logs contract smoke.
- Runs CLI smoke.
- Context switching smoke.
- CLI intake smoke.
- Runner registration smoke.
- Execution scan/report smoke.
- Bounded loop smoke.
- Remote IAL smoke with cost truth.
- Console intake/live-sidebar evidence reference.
- Security edge redirect evidence reference.
- Final release notes and readiness report.

## Required Closeout Artifacts (Implementation Later)

- Release closeout report under `docs/releases/` or `receipts/release-closeout/`.
- Evidence index containing links/paths to fresh-clone and smoke receipts.
- Final public main SHA recorded in closeout outputs.
- Public/private boundary summary.
- Known limitations summary.
- Next-after-3.0 recommendations.

Suggested report path:

- `docs/releases/amof-3.0-closeout.md`

Suggested receipts path:

- `receipts/release-closeout/AMOF-300-RELEASE-CLOSEOUT-001/`

## Required Validation Scope (Implementation Later)

- `python3 scripts/amof.py --version`
- `python3 scripts/amof.py check`
- `python3 scripts/amof.py chat plan --minimal-context --help`
- `python3 scripts/amof.py context list`
- `python3 scripts/amof.py context show`
- `python3 scripts/amof.py context doctor`
- `python3 scripts/amof.py intake validate`
- `python3 scripts/amof.py intake submit`
- `python3 scripts/amof.py intake list`
- `python3 scripts/amof.py intake show`
- `python3 scripts/amof.py runner register`
- `python3 scripts/amof.py runner list`
- `python3 scripts/amof.py runner show`
- `python3 scripts/amof.py runner doctor`
- `python3 scripts/amof.py runner match`
- `python3 scripts/amof.py execution scan`
- `python3 scripts/amof.py execution report`
- `python3 scripts/amof.py loop run`
- `python3 scripts/amof.py loop show`
- `python3 scripts/amof.py loop logs`
- `python3 scripts/amof.py runs list`
- `python3 scripts/amof.py runs show`
- `python3 scripts/amof.py runs logs`
- Remote IAL smoke only when token/key state is valid
- `git diff --check`
- leakage check

## Required Acceptance Criteria (For Later Implementation)

- Closeout report includes the full runtime authority proof chain.
- Closeout report includes all required fixed phrases.
- Closeout report records final public main SHA and fresh-clone verification path.
- Closeout report links objective evidence for each required smoke surface.
- Cost truth is recorded as observed or explicit unknown; never fake `0.0`.
- Fail-closed behavior is explicitly proven; no silent fallback claims.
- No new features are introduced by the closeout slice.
- No mutation execution is introduced.
- No remote dispatch is introduced.
- No provider/auth/secret handling changes are introduced.
- No Cloudflare/security changes are introduced.

## Explicit Out of Scope

- New CLI features.
- New console features.
- Remote execution dispatch.
- Mutation execution.
- Queue worker runtime.
- Kubernetes job runtime.
- SSH/agent transport.
- Jira sync.
- Voice intake.
- Dashboard analytics.
- Model ladder redesign.
- Provider/auth/secret changes.
- Cloudflare/security changes.

## Public/Private Boundary Impact

Public:

- Release evidence.
- Runtime authority proof chain.
- CLI contract and validation surfaces.
- No-mutation/no-dispatch semantics.
- Failure classifications.
- Cost truth status.
- Known limitations.

Private (must not leak):

- Secrets.
- Private gateway internals.
- Provider keys.
- Private topology.
- Customer-specific infrastructure.
- Internal routing/dispatch policy.

## Known Limitations to Capture in Closeout

- Current execution is scan/report only.
- Long-run loops are bounded scan/report loops only.
- Runner registration is metadata/capability registry only.
- No remote command dispatch exists yet.
- No mutation execution exists yet.
- Console intake is planning-only.
- Any future remote execution requires a separate ticket and review.
- Broad leakage scans may include baseline/test/internal matches; closeout must classify changed and output-facing surfaces.

## Stop Conditions

Stop and escalate if any of the following is true:

- Implementation attempts to add capability instead of closeout evidence.
- Implementation tries to dispatch work.
- Implementation mutates repositories as part of loop validation.
- Implementation changes provider/auth/secret config.
- Implementation requires private topology in public docs.
- Canonical repo is dirty.
- `promote-main` real ticket linkage fails.

## Next Dependency

This ticket closes the UP300 train and unlocks a future release/tag decision:

- `AMOF-300-RELEASE-TAG-001` (or equivalent)

It does not unlock mutation execution.
