# AMOF Install Runbook

Status: public first-touch install guidance

This runbook explains the three supported public install paths:

- primary standalone artifact path built from a source checkout
- checkout-local fallback path from a source checkout
- optional pipx path for an isolated Python install

## What AMOF Installs

AMOF installs a local CLI that can:

- check workstation prerequisites
- store app-data under user-local paths
- adopt an existing repo without polluting it
- store provider profile references
- run read-only planning or explicitly requested bounded execution
- expose Runtime Authority surfaces for runtime truth, bounded loops, execution
  evidence, intake templates, and runner/execution readiness checks

AMOF is evidence-first. It does not auto-commit or push on its own, and it does
not store raw provider secrets in profile setup.

## Primary Standalone Artifact Path

Use this path if you want a single-file public executable without `pipx`.

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
./scripts/build-standalone-amof.sh
./dist/amof --version
```

What this does:

- builds a single-file executable artifact at `./dist/amof`
- bundles the AMOF CLI and its Python dependencies into that artifact
- lets you run AMOF without pipx and without keeping `.venv/bin/amof` as the only no-pipx path

The standalone artifact is not a native binary. It is a Python-based executable
artifact and still requires a compatible `python3` runtime on the host unless
proven otherwise for your environment.

## Checkout-Local Fallback Path

Use this path if you prefer the existing local virtualenv install:

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
./scripts/install-amof.sh
./.venv/bin/amof --version
```

This fallback remains supported for development and for users who want the AMOF
runtime installed into a checkout-local virtualenv.

## Source Checkout Test Install

Use this path when developing AMOF itself or running focused operator tests from
a clean checkout:

```bash
python -m pip install -e ".[test]"
python -m pytest tests/test_remote_ial.py
```

The `test` extra is intentionally separate from AMOF runtime dependencies so
end-user installs do not pull in pytest.

## Optional Pipx Path

Use this path if you prefer an isolated user install:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v3.0.2"
amof --version
```

Expected version:

```text
AMOF v3.0.2
```

## First Commands After Install

```bash
amof check
amof doctor
amof setup provider --list
```

If you installed from a source checkout virtualenv, replace `amof` with
`./.venv/bin/amof`. If you built the standalone artifact, replace `amof` with
`./dist/amof`.

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
