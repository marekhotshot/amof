# AMOF-302-RUNNER-TEMPLATE-001

Status: Draft artifact (public runner template dogfood slice)  
Track: intake-to-runner-to-execution-scan readiness  
Release line: AMOF v3.0.2 follow-up

## Ticket ID

`AMOF-302-RUNNER-TEMPLATE-001`

## Title

Add local planning runner template for intake-to-execution scan dogfood.

## Goal

Provide a boring public runner metadata template so a local operator can move
from bounded intake capture to runner registration, runner matching, and
execution scan without reverse-engineering the runner schema.

## Scope

- `amof runner template --kind local-planning`
- YAML output to stdout with no file writes by default
- generated metadata is immediately valid for `amof runner register <file>`
- local planning/readiness defaults only
- focused regression coverage for template, register, doctor, list, match, and
  no-execution scan
- minimal public documentation for the dogfood path

## Non-Goals

- no runtime execution semantics change
- no dispatch or remote execution
- no write/mutation permission
- no endpoint URL, credentials, or provider secrets
- no `hotshot.sk` changes
- no UI, model ladder, agent arena, cloud-dev, deployment, Kubernetes, or GHCR
- no release tag creation
- no direct commit to `main`
- no raw push to `origin/main`

## Safety Defaults

The local planning template uses:

- `context: local`
- `status: available`
- capabilities for intake validation, intake planning, and execution scan report
- `allowed_mutation_modes: [read_only]`
- `max_concurrency: 1`
- labels: `local`, `planning-only`, `no-dispatch`
- local trust and template registration source metadata

## Validation

- `git diff --check`
- focused runner template/registration tests
- focused intake tests
- focused adoption/context tests
- focused execution scan tests
- `PYTHONPATH=scripts python3 -m unittest tests.test_cli_intake tests.test_repo_adoption tests.test_update_uninstall`
- `PYTHONPATH=scripts python3 -m amof --version`
- `PYTHONPATH=scripts python3 -m amof update --check --version v3.0.2`
- disposable local CLI smoke from init/adopt through intake, runner register,
  runner match, and execution scan

## Promotion Boundary

This public product slice must land through governed `promote-main` from a
clean ticket worktree. No direct `main` commit, raw `main` push, force-push, or
tag creation is allowed.
