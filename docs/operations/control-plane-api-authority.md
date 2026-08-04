# Control Plane API Authority (public advanced)

Status: canonical pointer for public HTTP control-plane + related transports  
Ticket: `AMOF-API-AUTHORITY-MAP-001`  
Audited public SHA: `587a13c10f18051168124f215151f2b7d9f8c352`

## Source of truth

1. FastAPI app: `scripts/amof/api/main.py` and `scripts/amof/api/routers/*`
2. Auth boundary: `scripts/amof/api/auth.py`
3. JSON object contracts: `contracts/*.schema.json` (mirrored under `scripts/amof/contracts/`)
4. CLI taxonomy: `docs/operations/public-surface-taxonomy.md`
5. Remote IAL **client** paths: `docs/operations/remote-ial-public-client-contract.md`
6. Cross-repo inventory (operator workspace): `evidence/AMOF-API-AUTHORITY-MAP-001/`

**Code beats prose.** Prefer exporting OpenAPI from the FastAPI app over
hand-maintaining a second endpoint list. Do not treat `amof.dev/openapi.json`
or empty site stubs as product API truth.

## Boundaries

| Surface | Classification |
|---|---|
| Public first-run CLI + contracts | Public product |
| `amof mcp` (stdio, 39 tools) | Advanced public |
| `amof server` FastAPI `/api/v1` + `/health` `/ready` | Advanced public / local |
| `/api/v1/control/*` | Internal-control mirror (credential header) |
| Hosted private `/v1/ial/*`, operator console `/api/*` | **Out of this repo** |
| Marketing site `/openapi.json` | **Not** product API authority (static HTML as of 2026-08 audit) |

## Authentication

- `/api/v1/*`: session cookie `amof_access_token` (and related session routes)
- `/api/v1/control/*`: `x-amof-internal-control-credential` (+ actor header)
- `/health`, `/ready`: unauthenticated liveness

Exact claim checks live in `scripts/amof/api/auth.py`.

## Endpoint reference (how to read)

Handlers are registered once and mounted under both `/api/v1` and
`/api/v1/control` unless marked control-only.

Control-only examples:

- `POST /api/v1/control/generated-builds/candidates/promote`
- `POST /api/v1/control/repo-adoption/analyses`

Router groups (prefix relative to mount):

| Router file | Prefix | Notes |
|---|---|---|
| `auth.py` | `/auth` | session, reauth, launch helpers |
| `users.py` | `/users` | |
| `ecosystem.py` | `/ecosystems` | CRUD + actions |
| `release.py` | `/ecosystems` | release/lifecycle/env |
| `generated_build.py` | `/generated-builds` | + control promote |
| `gateway.py` | `/gateway` | OpenAI-compat; may SSE |
| `intake.py` | `/intake` | draft/github/amof commit |
| `run.py` | `/runs` | includes SSE stream |
| `logs.py` | `/logs` | SSE stream |
| `models.py` | `/models` | |
| `deployments.py` | `/deployments` | |
| `agents_catalog.py` | `/agents` | |
| `runpod.py` | `/runpod` | |
| `settings.py` | `/settings` | |
| `repo_adoption.py` | `/repo-adoption` | control mount only |

Full method+path inventory for an audited SHA is generated into the operator
workspace registry (`public-control-plane-routes.json`), not duplicated here by
hand.

## Errors, streaming, side effects

- Errors: FastAPI/`HTTPException` `detail` shapes (not fully normalized across routers).
- SSE: `GET /runs/{run_id}/stream`, `GET /logs/stream`; gateway chat may use
  `text/event-stream`.
- Side effects: ecosystem/release/runpod/intake actions mutate local workspace or
  external provider state — inspect the specific router before documenting
  guarantees.
- Idempotency / pagination / ordering: **unverified** unless a named test proves
  the behavior for that route.

## Examples

- Contract examples: `contracts/examples/*`
- Intake API tests: `tests/test_intake_draft_api.py`
- Repo adoption (control): `tests/test_repo_adoption_analysis_api.py`

## Versioning policy

- HTTP prefix `/api/v1` is the control-plane version token for this surface.
- Object schemas version via fields inside `contracts/*.schema.json`.
- Release notes / `amof --version` describe product releases; do not invent
  parallel OpenAPI product versions on the marketing site without a GTM ticket.

## Generation / update procedure

```bash
# From a public checkout with FastAPI/uvicorn available (optional advanced deps):
# export OpenAPI from amof.api.main:app and store under docs/operations/generated/
# or refresh operator-workspace evidence/AMOF-API-AUTHORITY-MAP-001/api-registry.json
```

Checks that should eventually fail the docs job on drift:

- every production router decorator appears in the registry/spec
- every documented path exists in code
- no duplicate method+path on the same mount
- deprecated routes labeled
- examples validate against schemas where schemas exist

Until that CI exists, treat the operator-workspace `validate-registry.sh` as the
audit check for the inventory pack.
