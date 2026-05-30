# AMOF 3.0.0 Tag Result (Post-Tag Backfill)

Marker: `AMOF_300_RELEASE_TAG_DOCS_BACKFILL`  
Release version: `v3.0.0`

This document was committed after the v3.0.0 tag and documents the tag result. It is not part of the original tagged source tree.

## Tag Identity

- version: `v3.0.0`
- tagged commit SHA: `bd2314c0229c6a802cc71e14c7463cbbf51df245`
- annotated tag object SHA: `b6ecabb78dbbb73a2f4624767f47d8cd48982830`
- remote tag verification: `b6ecabb78dbbb73a2f4624767f47d8cd48982830 refs/tags/v3.0.0`
- release decision: `TAG_V3_0_0_APPROVED`

## Final Remote IAL Evidence At Tag Decision

- remote IAL smoke status: `REMOTE_IAL_SMOKE_STATUS_EXPLICIT=PASS`
- request id: `461a726e-8877-49ae-947a-b1d83a616692`
- run dir: `receipts/client-ial-smoke/AMOF-CLIENT-IAL-SMOKE-CONTRACT-001/runs/run-20260530-201829`
- receipt checks: IAL detail `200`, console detail `200`, console list `200`
- cost status: `observed`
- estimated cost: `0.000354`
- sanitization verdict: `sanitized_hash_only`
- raw `provider_generation_id` in public output/events: not present

## Source Documents

- closeout report path: `docs/releases/amof-3.0-closeout.md`
- local release receipt path: `receipts/release-tags/AMOF-300-RELEASE-TAG-001/report.md`

## Known Caveats

- Console runtime logs viewer is not implemented.
- Console refresh/polling follow-up is still needed.
- Receipt count `50` may reflect page-size/limit semantics and needs clarification.
- CLI runs/runtime logs are the canonical v3.0 inspection path.

## Future Rule

Release docs must be committed before tag; only tag object SHA and remote push verification may be recorded after tag.
