# AMOF-301-DOGFOOD-HOTSHOT-ADOPT-INTAKE-001

Status: Draft artifact (public dogfood/product-fix slice only)  
Track: external repo adoption + intake usability  
Release line: AMOF v3.0.1 follow-up

## Ticket ID

`AMOF-301-DOGFOOD-HOTSHOT-ADOPT-INTAKE-001`

## Title

Fix external repo adoption context and intake schema UX from hotshot.sk dogfood.

## Goal

Repair the public AMOF dogfood path so an adopted external repository can be
used by a documented context-generation command, and intake validation/template
UX is usable without trial-and-error field discovery.

## Scope

- adopted app-data ecosystem resolution for `amof context`
- idempotent same-repo re-adoption without confusing duplicate ecosystem state
- aggregate missing-field reporting in `amof intake validate`
- `amof intake template --kind bounded_intake_task`
- focused regression tests only

## Non-Goals

- no changes to `hotshot.sk` repository content
- no UI/cloud-dev/deployment/amof.dev work
- no release tag creation
- no direct commit to `main`
- no raw push to `origin/main`

## Required Validation

- `git diff --check`
- focused repo adoption/context tests
- focused intake tests
- `PYTHONPATH=scripts python3 -m unittest tests.test_update_uninstall`
- `PYTHONPATH=scripts python3 -m amof --version`
- `PYTHONPATH=scripts python3 -m amof update --check --version v3.0.1`

## Promotion Boundary

This public product-fix slice must land through governed `promote-main` from a
clean ticket worktree. No direct `main` commit or raw `main` push is allowed.
