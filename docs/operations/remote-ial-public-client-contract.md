# Remote IAL Public Client Contract

Ticket: `AMOF-IAL-PUBLIC-CLIENT-001`

Status: `PUBLIC_CLIENT_CONTRACT_ONLY`, updated for `v3.1.1`

## Purpose

Public `amof` may expose only the local client-side contract needed to call a
private IAL gateway. Public `amof` must remain installable and trustworthy
without embedding hosted gateway behavior, provider routing policy, receipt
storage internals, or control-plane/private deployment assumptions.

The promotion-ready public surface is limited to client/profile/evidence
semantics that were proven through the private gateway split, compatibility
smoke, and OpenRouter live smoke.

## Included In Public AMOF

The following pieces are public-safe:

- `RemoteIALClient` as an `LLMClient` implementation that sends model calls to a
  configured private IAL gateway
- `remote-ial` provider profile/template schema and CLI/setup wiring
- additive local evidence/event fields:
  - `provider=remote-ial`
  - `upstream_provider`
  - `upstream_model`
  - `request_id`
  - `policy_decision`
  - `input_hash`
  - `output_hash`
- evidence storage modes:
  - `evidence.messages: raw_local | redacted_local | hash_only`
  - `evidence.journal: enabled | redacted | disabled`
- failure-path correlation preservation so truthful non-success outcomes still
  retain gateway correlation fields locally when available
- generic structured-output fallback through `RemoteIALClient.chat_structured()`
  using the existing remote chat endpoint, strict JSON instructions, and local
  Pydantic validation
- tests proving:
  - auth/network/provider failures remain non-success
  - valid structured JSON parses into the requested public response model
  - invalid structured output fails closed with `ProviderError`
  - `/api/v1/ial/chat` is not publicly exposed
  - local evidence does not store bearer tokens or provider API keys in the
    hardened modes

## Public Contract

### Gateway paths

Public `amof` may target the private gateway contract paths:

- `GET /v1/ial/healthz`
- `GET /v1/ial/providers`
- `POST /v1/ial/chat`

Public `amof` must not host these routes.

### Request payload

Public `amof` may send:

- `system`
- `messages`
- `tools`
- `model`
- `max_tokens`
- `temperature`

Public `amof` must not add hosted provider routing or model ladder semantics to
this contract.

### Response payload

Public `amof` may consume and forward:

- `request_id`
- `provider`
- `model`
- `policy_decision`
- `tokens`
- `latency_ms`
- `input_hash`
- `output_hash`
- `stop_reason`
- `text`
- `tool_calls`
- `thinking`

The local provider identity remains `remote-ial`; truthful upstream identity is
recorded separately under `upstream_provider` and `upstream_model`.

### Structured output

Public AMOF does not require a hosted structured endpoint. The client may request
structured output by:

- adding a provider-neutral instruction that requires one strict JSON object
- including the requested Pydantic model JSON Schema
- calling `POST /v1/ial/chat`
- validating the returned text locally

Empty, invalid JSON, or schema-invalid responses must fail closed with
`ProviderError`. Upstream provider errors from the gateway must keep their
existing failure classification.

## Evidence Modes

Public `amof` defaults remain deterministic:

- messages default: `raw_local`
- journal default: `enabled`

Optional hardening modes:

- `redacted_local` replaces bearer tokens and env-sourced secrets with
  `[REDACTED]`
- `hash_only` stores stable hashes and char counts instead of raw message text
- `journal: disabled` skips journal generation entirely
- `journal: redacted` preserves the journal workflow while redacting sensitive
  session content
- unknown/missing provider cost remains unknown/null and is never represented
  as fake `0.0`

These are local evidence controls only. Public `amof` does not own private
gateway receipt policy.

## Explicit Exclusions

The following must not live in public `amof`:

- hosted `/v1/ial` FastAPI routers
- `services/ial-gateway`
- `ial_service.py` hosted gateway implementation
- provider routing implementation
- provider/model auto-selection policy
- OpenRouter/Bedrock/RunPod routing internals
- hosted receipt filesystem paths
- private redaction policy internals
- tenant/workspace policy
- private gateway deployment logic
- private model ladder policy owned by the gateway

## Validation Baseline

The public client/evidence contract has been proven against the private gateway:

- mock compatibility smoke passed before live-provider work
- OpenRouter live smoke passed end to end through:
  - public client
  - private gateway
  - live OpenRouter upstream
- installed `AMOF v3.1.1` completed `amof chat plan` through the remote IAL
  client after the cloud-dev gateway provider secret was refreshed
- private receipts remained hash-only
- public messages remained hash-only when configured
- no journals were created when disabled
- disposable target repo stayed clean
- no bearer token or provider API key leaked into public/private evidence

## Promotion Boundary Verdict

Public `amof` is promotion-ready only as a client/evidence contract. The private
gateway remains the owner of routing, policy, receipt storage, and live-provider
adapter behavior.
