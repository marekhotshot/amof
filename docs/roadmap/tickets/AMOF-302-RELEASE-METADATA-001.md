# AMOF-302-RELEASE-METADATA-001

Status: Draft artifact (release metadata slice only)  
Track: AMOF 3.0.2 dogfood UX fix release  
Release: AMOF 3.0.2

## Ticket ID

`AMOF-302-RELEASE-METADATA-001`

## Title

Release v3.0.2 metadata for hotshot.sk dogfood fixes.

## Goal

Prepare the governed release metadata/docs slice so the already-promoted
dogfood fixes can be installed and tested from a real `v3.0.2` tag.

## Included Fix Line

`v3.0.2` follows `v3.0.1` and includes:

- adopted repo context resolution for app-data ecosystems
- duplicate adoption cleanup for same-path re-adoption
- aggregate intake missing-field validation reporting
- `amof intake template --kind bounded_intake_task`

## Scope

- package metadata and CLI version truth
- current install/update examples
- current release docs
- changelog/release note summary

## Non-Goals

- no runtime execution semantics change
- no tag creation in this slice
- no direct commit to `main`
- no raw push to `origin/main`
- no changes to `hotshot.sk`

## Validation

- `git diff --check`
- `rg -n "v2\.8\.1|2\.8\.1|v3\.0\.1|3\.0\.1" README.md docs pyproject.toml scripts tests || true`
- `PYTHONPATH=scripts python3 -m unittest tests.test_repo_adoption tests.test_cli_intake tests.test_update_uninstall`
- `PYTHONPATH=scripts python3 -m amof --version`
- `PYTHONPATH=scripts python3 -m amof update --check --version v3.0.2`

## Promotion Boundary

This release metadata slice must land through governed `promote-main`. The
future `v3.0.2` tag must target the promoted synthetic commit, not this branch
commit.
