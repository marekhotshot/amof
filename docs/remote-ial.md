# Remote IAL

Status: public client contract, verified with installed `AMOF v3.0.3`

Remote IAL lets installed AMOF route planning calls through an externally
operated inference gateway while keeping AMOF responsible for the local
governance loop: repository scope, receipts, evidence modes, approval
boundaries, and fail-closed error reporting.

The public repo contains the client contract only. It does not ship a gateway,
provider routing policy, model ladder, credentials, hosted receipt storage, or
deployment topology.

## Verified Capability

The `v3.0.3` release receipts verify:

- installed `amof setup provider remote-ial` writes provider profile references
  only, not raw secrets
- installed `amof chat plan` can use a configured remote IAL gateway
- `RemoteIALClient.chat_structured()` sends schema instructions through the
  existing plain chat endpoint and validates strict JSON locally
- invalid or schema-mismatched structured output fails closed with
  `ProviderError`
- upstream auth, network, and provider failures keep stable provider error
  classification
- receipts preserve transport/upstream attribution without storing provider
  credentials
- provider cost truth stays explicit: unknown cost remains unknown/null and is
  never rewritten as `0.0`

## Minimal Example

```bash
export AMOF_REMOTE_IAL_BASE_URL="https://ial.example.invalid"
export AMOF_REMOTE_IAL_API_KEY="<redacted>"

amof setup provider remote-ial --name remote-ial --activate --yes
amof chat plan \
  "Inspect this repo and propose a bounded plan." \
  --repo . \
  --file README.md \
  --max-files 1
```

For local operator smoke tests, the same contract can target a local
port-forward such as `http://127.0.0.1:18787`. That port-forward and its gateway
deployment are not part of the public package.

## Structured Output Contract

Public AMOF does not require a special structured endpoint. When a caller asks
for structured output, the client:

1. Adds a provider-neutral instruction requiring one strict JSON object.
2. Includes the requested Pydantic model JSON Schema.
3. Calls the existing remote chat endpoint.
4. Parses the returned text with the requested Pydantic model.
5. Raises `ProviderError` on empty, invalid, or schema-invalid output.

This keeps structured planning usable without publishing private gateway
implementation details.

## Evidence And Attribution

AMOF records local evidence in app-data. For remote IAL calls it may record:

- local provider: `remote-ial`
- upstream provider/model attribution when the gateway returns it
- request id
- input/output hashes
- token and latency metadata
- provider-neutral policy decision fields when present

Evidence modes such as `hash_only`, `redacted_local`, and disabled journals
remain local AMOF controls. Private gateway receipt policy stays outside the
public repo.

## Boundaries

Public AMOF must not include:

- hosted `/v1/ial` routers
- gateway service implementation
- provider routing or fallback policy
- model ladder behavior
- provider credentials or auth internals
- deployment manifests or kubeconfig assumptions
- private evaluation, scoring, or routing methodology

If a future change needs any of those details, split it into a private track or
reduce it to a public interface before publishing.
