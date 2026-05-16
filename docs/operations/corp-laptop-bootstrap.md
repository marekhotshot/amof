# Corp Laptop Bootstrap

Status: canonical
Date: 2026-05-06

## Goal

Bootstrap AMOF from current `origin/main` on a clean workstation or
corporate laptop without local repo history, stash state, or split-workspace
setup.

Supported layouts:

1. standalone clone: `/path/amof`
2. split workspace: `/path/amof-platform/repos/amof`

This runbook documents the validated standalone clean-clone path.

## Prerequisites

- Linux or macOS shell with `bash`
- `git`
- `python3` with `venv`
- network access to GitHub and PyPI
- non-interactive GitHub auth available to git:
  - `GITHUB_TOKEN` in the environment, or
  - a configured git `credential.helper`

Token expectation for GitHub HTTPS remotes:

- classic token: `repo`
- fine-grained token: repository Contents read/write

## Clone

```bash
git clone --branch main --single-branch https://github.com/marekhotshot/amof.git
cd amof
```

## Install

```bash
./scripts/install-amof.sh
```

What the installer does:

1. creates `.venv`
2. installs Python dependencies
3. installs editable AMOF CLI
4. checks `amof --help`
5. checks `python -m amof --help`
6. bootstraps AMOF app-data roots and the default `local` context
7. verifies git fetch and push `--dry-run` auth behavior
8. runs `amof doctor`

Expected successful tail output:

```text
[install-amof] git auth check passed
[install-amof] running amof doctor
AMOF doctor: PASS
  layout: standalone_repo
  canonical AMOF:   /path/to/amof
  import source:    /path/to/amof/scripts/amof/__init__.py
ready for promote
```

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
```

Expected results:

- `./.venv/bin/amof --help`: PASS
- `./.venv/bin/python -m amof --help`: PASS
- `./.venv/bin/amof doctor`: `PASS` or acceptable `WARN`
- `./.venv/bin/amof doctor --json`: emits machine-readable bootstrap evidence
- `./.venv/bin/amof bootstrap contract --json`: emits the governed workstation bootstrap contract with truthful `PASS`, `WARN`, or `BLOCKED` status
- `./.venv/bin/amof bootstrap bundle --json`: emits a summary plus linked source/toolchain/provider receipts and a SHA256 manifest
- `./.venv/bin/amof paths --json`: shows app-data roots outside the source checkout
- `git status --short`: empty

Standalone `doctor` expectations:

- `layout: standalone_repo`
- `canonical AMOF` is the repo root
- `import source` resolves under `repo_root/scripts/amof`
- missing sibling repos like `amof-ui` is not a `FAIL`
- app-data roots resolve outside the repo checkout
- required contracts under `contracts/` exist
- missing provider profile refs may show as `WARN`

## Troubleshooting

### GitHub Auth Failure

Symptoms:

- `git fetch auth check failed (auth_error)`
- `git push --dry-run auth check failed (auth_error)`
- `Authentication failed`
- `Invalid username or token`

Fix:

```bash
export GITHUB_TOKEN=...
./scripts/install-amof.sh
```

Or configure a non-interactive git credential helper for GitHub HTTPS.

### Doctor FAIL

Healthy standalone-clone result is `PASS`.

If `doctor` fails:

1. confirm you are on current `origin/main`
2. confirm import resolves under `scripts/amof`
3. rerun:

```bash
./.venv/bin/amof doctor
```

If failure text says import is outside canonical `scripts/amof`, treat that
as a blocker and capture the exact output.

### Python Venv Issue

Symptoms:

- `python3: command not found`
- `No module named venv`
- `.venv/bin/amof: No such file or directory`

Fix:

1. install a Python 3 build with `venv` support
2. remove the broken venv if needed
3. rerun install

```bash
rm -rf .venv
./scripts/install-amof.sh
```

## Ready State

The machine is bootstrap-ready when all of the following are true:

- installer ends with `ready for promote`
- `amof --help` works
- `python -m amof --help` works
- `amof doctor` is `PASS` or acceptable `WARN`
