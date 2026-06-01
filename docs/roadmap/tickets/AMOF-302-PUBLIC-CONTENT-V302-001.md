# AMOF-302-PUBLIC-CONTENT-V302-001

Status: Draft artifact (public content/docs slice only)  
Track: AMOF v3.0.2 public Runtime Authority content  
Release: AMOF 3.0.2

## Ticket ID

`AMOF-302-PUBLIC-CONTENT-V302-001`

## Title

Update public AMOF content for v3.0.2 Runtime Authority.

## Goal

Update the public README/docs surface that represents AMOF and amof.dev-facing
truth so the current public install and release framing point to `v3.0.2`.

## Scope

- public README and documentation wording only
- current install command:
  `pipx install "git+https://github.com/marekhotshot/amof.git@v3.0.2"`
- local-first Runtime Authority framing for governed AI work
- careful public dogfood truth for external repo adoption/context resolution,
  dotted repo names such as `hotshot.sk`, aggregate intake missing-field
  validation, and `amof intake template --kind bounded_intake_task`
- smoke/runbook examples that should no longer present older releases as current

## Non-Goals

- no AMOF runtime/core behavior change
- no runner template, UI, deployment, cloud-dev, or hosting changes
- no changes to `hotshot.sk`
- no release tag creation
- no direct commit to `main`
- no raw push to `origin/main`

## Known Cleanup Item

`scripts/smoke-standalone-amof.sh` still contains a stale `v2.6.1` expected
version message. That script cleanup is intentionally outside this content/docs
slice and should be handled by a separate ticket if the standalone smoke is
updated.

## Validation

- `git diff --check`
- stale public docs scan:
  `rg -n "v2\\.6\\.1|2\\.6\\.1|v2\\.8\\.1|2\\.8\\.1|v3\\.0\\.0|3\\.0\\.0|v3\\.0\\.1|3\\.0\\.1" README.md docs 2>/dev/null || true`
- `PYTHONPATH=scripts python3 -m amof --version`
- `PYTHONPATH=scripts python3 -m amof update --check --version v3.0.2`

## Promotion Boundary

This content slice must land through governed `promote-main` from the ticket
worktree. It must not be committed directly on public `main`, raw-pushed to
`origin/main`, or tagged.
