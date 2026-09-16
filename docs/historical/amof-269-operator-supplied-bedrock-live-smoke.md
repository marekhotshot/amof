# AMOF-269 Operator-Supplied Corp Laptop Bedrock Live Smoke

Status: portable release evidence note

This note materializes the AMOF-269 Bedrock live smoke result into a portable
artifact that can be reviewed from the AMOF source tree.

## Evidence Provenance

This is **operator-supplied corp-laptop evidence**. It is **not** locally
reproduced evidence from this workstation.

The original corp-laptop smoke was reported through transient `/tmp` paths that
are not required to exist on this workstation for release review.

## Intended Source Fix

- AMOF-268 smoke ref: `amof-268-bedrock-oob-smoke-20260518-212251`
- AMOF-268 commit: `462f7688f5f22e5b90cf62331c423e5d15fc4ab2`

This evidence is meant to validate the installed CLI Bedrock out-of-box fix set
introduced by that candidate.

## Operator-Supplied AMOF-269 Result

- Classification: `PASS_BEDROCK_INSTALLED_CLI_LIVE_PLAN`
- AWS profile used: `pncclaims-ai`
- AWS region: `eu-central-1`
- Bedrock model: `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
- CA mode: combined system CA plus corporate Zscaler CA

## Captured Product Assertions

According to the supplied smoke transcript/result summary:

- No manual `pipx inject amof requests` step was required.
- `amof setup provider bedrock` worked on the corp laptop.
- The installed CLI Bedrock live plan succeeded.
- The disposable target repo stayed clean according to the captured smoke
  output.
- Source pollution output was empty according to the captured smoke output.

## Release Interpretation

This evidence supports the product claim that the AMOF-268 installed CLI fix
set is sufficient for a Bedrock installed-CLI live plan on the corp laptop when
the enterprise TLS prerequisites are configured correctly.

This evidence does **not** support the claim that Bedrock works in intercepted
enterprise environments without extra TLS setup.

## Enterprise TLS Caveat

In enterprise TLS-intercepted environments, Bedrock requires an explicit
combined CA bundle containing:

- normal system/public trust roots
- the corporate interception CA, for example the Zscaler root

The same combined CA bundle should be applied to:

- `SSL_CERT_FILE`
- `REQUESTS_CA_BUNDLE`
- `AWS_CA_BUNDLE`

This distinguishes the **product fix** from the **enterprise bootstrap
requirement**:

- Product fix: installed metadata includes `requests`, Bedrock provider setup is
  first-class, and the selected Bedrock path does not fail first on unrelated
  provider integrations.
- Enterprise bootstrap requirement: corp-laptop TLS interception still requires
  a correctly assembled combined CA bundle before live Bedrock calls can be
  expected to work.

## Review Boundaries

- No secrets are recorded in this note.
- No claim of local reproduction is made here.
- This note is suitable for release-gate review when the original corp `/tmp`
  evidence paths are unavailable on the release workstation.
