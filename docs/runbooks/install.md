# AMOF Install Runbook

Status: public first-touch install guidance

This runbook explains the two supported public install paths:

- primary no-pipx path from a source checkout
- optional pipx path for an isolated Python install

## What AMOF Installs

AMOF installs a local CLI that can:

- check workstation prerequisites
- store app-data under user-local paths
- adopt an existing repo without polluting it
- store provider profile references
- run read-only planning or explicitly requested bounded execution

AMOF is evidence-first. It does not auto-commit or push on its own, and it does
not store raw provider secrets in profile setup.

## Primary No-Pipx Path

Use this path if you do not want `pipx`.

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
./scripts/install-amof.sh
./.venv/bin/amof --version
```

What this does:

- creates a checkout-local virtualenv at `.venv`
- installs AMOF and its Python dependencies into that virtualenv
- gives you an executable CLI at `./.venv/bin/amof`

This is not yet a true standalone binary or `pyz`. The current public no-pipx
path is a Python virtualenv install that produces an executable `amof` shim.

## Optional Pipx Path

Use this path if you prefer an isolated user install:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v2.5.2"
amof --version
```

## First Commands After Install

```bash
amof check
amof doctor
amof setup provider --list
```

If you installed from a source checkout virtualenv, replace `amof` with
`./.venv/bin/amof`.

## Adopt A Repo

From the repo you want AMOF to work with:

```bash
amof init --adopt .
amof doctor
```

Adoption records bindings and evidence in AMOF app-data. It does not write
AMOF runtime directories into the target repo by default.

## Provider Profiles

Provider setup records metadata and environment variable references only:

```bash
amof setup provider --list
amof setup provider bedrock --print-template
```

Live provider use still requires the relevant environment variables in your
shell. AMOF does not store raw provider secrets in setup profiles.

## Planning vs Execution

Read-only planning:

```bash
amof agent --plan "Inspect this repo" --no-follow-up
```

Bounded execution:

```bash
amof agent --plan-execute "Make a bounded change. Do not commit." --no-follow-up
```

Execution must still be reviewed as a Git diff. AMOF does not mutate, commit,
or push unless you explicitly ask it to do so.

## Bedrock Precision

Installed CLI Bedrock no longer requires `pipx inject amof requests`.

In enterprise TLS-intercepted environments, Bedrock still requires a combined
CA bundle containing:

- system/public trust roots
- the corporate interception CA, for example Zscaler

Use the combined bundle with:

- `SSL_CERT_FILE`
- `REQUESTS_CA_BUNDLE`
- `AWS_CA_BUNDLE`
