# AMOF-301-RELEASE-CORRECTION-001

Status: Draft artifact (release-correction slice only)  
Track: AMOF 3.0.1 release correction  
Release: AMOF 3.0.1

## Ticket ID

`AMOF-301-RELEASE-CORRECTION-001`

## Title

Correct v3.0.1 package and install truth after broken v3.0.0 tag.

## Goal

Prepare the governed correction slice that fixes release identity and install
truth for AMOF 3.0.1 without changing runtime behavior beyond version/update
metadata reporting.

## Scope

- package metadata version correction in `pyproject.toml`
- AMOF internal `__version__` source sync
- current install/update truth in `README.md`
- release/runtime docs that describe the current installable public version
- one minimal version sync test

## Required Outcomes

- package metadata installs as `amof 3.0.1`
- CLI version reports `AMOF v3.0.1`
- update flows target `v3.0.1` as current correction truth
- current-state docs no longer claim stale earlier releases as the latest release
- historical `v3.0.0` references remain only when clearly marked as history or evidence

## Explicit Non-Goals

- no runtime feature changes
- no new release tag during this slice
- no rewrite or deletion of the existing `v3.0.0` tag
- no direct commit on canonical `main`
- no direct push to `origin/main`

## Validation

- `git diff --check`
- `rg -n "v2\.8\.1|2\.8\.1" README.md docs pyproject.toml scripts tests || true`
- `rg -n "v3\.0\.0" README.md docs pyproject.toml scripts tests || true`
- `PYTHONPATH=scripts python3 -m unittest tests.test_update_uninstall`
- `PYTHONPATH=scripts python3 -m amof --version`
- `PYTHONPATH=scripts python3 -m amof update --check --version v3.0.1`

## Promotion Boundary

This slice must move through AMOF governed `promote-main` as a candidate bundle
from a release-prep branch/worktree. The synthetic promoted commit on `main`
becomes the only valid future tag target for `v3.0.1`.
