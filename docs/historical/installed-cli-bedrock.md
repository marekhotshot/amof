# AMOF Installed CLI Bedrock Runbook

Status: release-candidate guidance for the installed CLI Bedrock path

This runbook documents the Bedrock setup and smoke assumptions for the AMOF
installed CLI after the AMOF-268/269 Bedrock out-of-box work.

## What This Covers

- Fresh installed CLI provider metadata includes the runtime dependencies needed
  for the Bedrock path, including `requests`.
- `amof setup provider bedrock` is available as a first-class profile template.
- Provider setup remains secret-reference-only and does not make a live AWS or
  Bedrock call while writing the profile.
- Installed CLI Bedrock startup should fail on Bedrock prerequisites such as
  `AWS_REGION` before it fails on unrelated provider integrations.

## Important Enterprise TLS Caveat

Bedrock should not be described as "works without extra setup" in TLS
intercepted corporate environments.

In an enterprise environment with TLS interception, AMOF's Bedrock path
typically requires an explicit combined CA bundle that contains:

- the normal system/public trust roots
- the corporate interception CA, for example the Zscaler root

Use the same combined CA bundle path for:

- `SSL_CERT_FILE`
- `REQUESTS_CA_BUNDLE`
- `AWS_CA_BUNDLE`

Why all three matter:

- `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` cover Anthropic's Bedrock client over
  `httpx`
- `AWS_CA_BUNDLE` covers AWS SDK trust

If the AWS CLI works but the AMOF Bedrock path still fails with a TLS/network
error, the missing piece is often the combined bundle applied to the
Anthropic/httpx side as well.

## Installed CLI Setup

Install from the release candidate or released tag:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@<ref>"
```

No manual `pipx inject amof requests` step should be required for this path.

List the provider templates:

```bash
amof setup provider --list
```

Print the Bedrock template:

```bash
amof setup provider bedrock --print-template
```

Write and activate the Bedrock profile:

```bash
amof setup provider bedrock --name bedrock-default --activate --yes
```

This writes provider metadata and environment variable references only. It does
not store raw provider secrets and does not call AWS during setup.

## Bedrock Environment

```bash
export AWS_PROFILE="<your-profile>"
export AWS_REGION="eu-central-1"
export AMOF_BEDROCK_STANDARD_MODEL_ID="eu.anthropic.claude-haiku-4-5-20251001-v1:0"
export SSL_CERT_FILE="/path/to/combined-ca.pem"
export REQUESTS_CA_BUNDLE="/path/to/combined-ca.pem"
export AWS_CA_BUNDLE="/path/to/combined-ca.pem"
```

The Bedrock default model should be an inference-profile-compatible model ID.

## Adopt A Repo Before Live Planning

The installed CLI still needs an adopted repo/app-data ecosystem before
`amof agent` can run:

```bash
cd /path/to/your/repo
amof init --adopt . --name installed-smoke
```

## Smoke Commands

No-key/no-cloud prerequisite smoke:

```bash
amof setup provider bedrock --print-template
```

Installed CLI prerequisite failure smoke:

```bash
amof -e installed-smoke agent --provider bedrock --plan "Inspect this repo" --no-follow-up
```

Expected prerequisite failure in a shell without Bedrock env configured:

```text
[agent] AWS_REGION not set for Bedrock.
```

That failure should happen before:

- `No module named requests`
- `OPENROUTER_API_KEY not set`
- `RUNPOD_API_KEY not set`

## Release-Prep Evidence

Operator-supplied evidence for the current release candidate:

- AMOF-268 smoke ref: `amof-268-bedrock-oob-smoke-20260518-212251`
- AMOF-269 classification: `PASS_BEDROCK_INSTALLED_CLI_LIVE_PLAN`
- AMOF-269 report path supplied during release prep:
  `/tmp/amof-AMOF-269-corp-bedrock-live-smoke-report.md`

This runbook intentionally does not claim Bedrock works in intercepted corporate
environments without enterprise TLS setup.
