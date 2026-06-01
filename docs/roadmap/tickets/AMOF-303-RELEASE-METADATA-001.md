# AMOF-303-RELEASE-METADATA-001

Status: Draft artifact (release metadata slice only)  
Track: AMOF 3.0.3 runner-template packaging release  
Release: AMOF 3.0.3

## Ticket ID

`AMOF-303-RELEASE-METADATA-001`

## Title

Release v3.0.3 metadata for runner-template CLI dogfood path.

## Goal

Prepare the governed release metadata/docs slice so the already-promoted
post-`v3.0.2` main-truth fixes can be installed and tested from a real
`v3.0.3` tag.

## Included Fix Line

`v3.0.3` follows `v3.0.2` and packages:

- `amof runner template --kind local-planning`
- local runner `register`, `list`, `doctor`, and `match` readiness flow
- execution scan readiness reporting with `NO_EXECUTION_PERFORMED`
- standalone smoke current-version hygiene

## Scope

- package metadata and CLI version truth
- current install/update examples
- current release docs
- changelog/release note summary

## Non-Goals

- no runtime execution semantics change
- no execution dispatch or mutation behavior
- no tag creation in this slice
- no direct commit to `main`
- no raw push to `origin/main`
- no changes to `hotshot.sk`

## Validation

- `git diff --check`
- `rg -n "v2\.6\.1|2\.6\.1|v2\.8\.1|2\.8\.1|v3\.0\.0|3\.0\.0|v3\.0\.1|3\.0\.1|v3\.0\.2|3\.0\.2" README.md CHANGELOG.md docs pyproject.toml scripts tests || true`
- `PYTHONPATH=scripts python3 -m unittest tests.test_runner_registration tests.test_remote_execution_scan_report tests.test_cli_intake tests.test_repo_adoption tests.test_update_uninstall`
- `PYTHONPATH=scripts python3 -m amof --version`
- `PYTHONPATH=scripts python3 -m amof update --check --version v3.0.3`
- source smoke for runner template generation, local runner registration/match,
  and execution scan readiness

## Promotion Boundary

This release metadata slice must land through governed `promote-main`. The
future `v3.0.3` tag must target the promoted synthetic commit, not this branch
commit.
