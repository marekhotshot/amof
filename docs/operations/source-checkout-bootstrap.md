# Source Checkout Bootstrap

Status: maintainer/source-checkout runbook
Date: 2026-05-17

This runbook is for maintainers and contributors who need to validate AMOF from
a clean source checkout. Public end users should normally install the released
CLI with pipx:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v3.1.1"
```

## Goal

Bootstrap AMOF from a clean checkout without relying on private workspace
topology, local stash state, or hidden runtime services.

The source-checkout path validates:

- the local virtualenv install
- the `amof` console script inside that virtualenv
- source-checkout `python -m amof` behavior inside that same virtualenv
- AMOF app-data roots outside the source checkout
- bootstrap/doctor evidence commands

System `python -m amof` after a pipx install is not a public contract.

## Prerequisites

- Linux or macOS shell with `bash`
- `git`
- Python 3.11 or newer with `venv`
- network access to GitHub and Python package indexes

GitHub write credentials are not required for the default source-checkout
bootstrap. Promotion or release auth checks are maintainer-only and must be
requested explicitly.

## Clone

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
git status --short
```

Expected: clean working tree.

## Install

```bash
./scripts/install-amof.sh
```

What the installer does:

1. creates `.venv`
2. installs Python dependencies
3. installs editable AMOF CLI
4. checks `./.venv/bin/amof --help`
5. checks `./.venv/bin/python -m amof --help`
6. bootstraps AMOF app-data roots and the default `local` context
7. runs `amof doctor`

Maintainers who intentionally need promotion-auth evidence can opt in:

```bash
./scripts/install-amof.sh --check-promote-auth
```

That check may require non-interactive GitHub credentials. It is not required
for public install, source-checkout install, or CLI validation.

## Validation

Run these after install:

```bash
./.venv/bin/amof --help
./.venv/bin/python -m amof --help
./.venv/bin/amof doctor
./.venv/bin/amof doctor --json
./.venv/bin/amof bootstrap contract --json
./.venv/bin/amof bootstrap bundle --json
./.venv/bin/amof paths --json
git status --short
```

Expected results:

- `./.venv/bin/amof --help`: PASS
- `./.venv/bin/python -m amof --help`: PASS for source checkout
- `./.venv/bin/amof doctor`: `PASS` or acceptable public-source `WARN`
- bootstrap commands emit truthful `PASS`, `WARN`, or `BLOCKED` evidence
- `./.venv/bin/amof paths --json` shows app-data roots outside the checkout
- `git status --short` is empty

## Troubleshooting

If install or doctor fails:

1. confirm the checkout is clean
2. confirm Python has `venv` support
3. confirm imports resolve under `scripts/amof`
4. rerun the relevant command from the repo root

```bash
./.venv/bin/amof doctor
```

If failure text says the import source is outside the checkout's `scripts/amof`
tree, treat that as a blocker and capture the exact output.

## Ready State

The checkout is source-bootstrap-ready when all of the following are true:

- `./.venv/bin/amof --help` works
- `./.venv/bin/python -m amof --help` works inside the source virtualenv
- `./.venv/bin/amof doctor` is `PASS` or an understood `WARN`
- bootstrap evidence commands run without private topology or provider keys
- app-data roots resolve outside the source checkout
- `git status --short` is clean
