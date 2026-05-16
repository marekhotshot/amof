# AMOF-198: App-Data, Context, and Scope Boundary

Status: accepted
Owner: AMOF platform
Date: 2026-05-13
## Decision

AMOF runtime state moves out of source workspaces and into AMOF-owned app-data directories.

AMOF source repos are not AMOF runtime storage.

The CLI resolves app-data using this precedence:

1. explicit AMOF env overrides such as `AMOF_HOME`, `AMOF_CONFIG_HOME`, `AMOF_DATA_HOME`, `AMOF_CACHE_HOME`, and `AMOF_STATE_HOME`
2. XDG defaults on Linux
3. the flat app-data tree rooted at the directory named by `AMOF_HOME`

The first implementation wave standardizes these roots:

- config: `~/.config/amof/`
- data: `~/.local/share/amof/`
- cache: `~/.cache/amof/`
- state: `~/.local/state/amof/`

The CLI owns these files:

- `config.yaml`
- `contexts.yaml`
- `workspaces.yaml`
- `state.json`

The CLI owns these durable runtime directories:

- `evidence/`
- `runs/`
- `workspaces/`
- `receipts/`

The CLI owns these operational directories:

- `logs/`
- `locks/`
- `queue/`

## Context Contract

AMOF operational contexts are named configuration entries stored in `contexts.yaml`.

Each context defines:

- execution backend
- workspace backend
- evidence backend
- credential references
- safety defaults
- promotion policy

The default context is `local`:

- execution backend: `local`
- workspace backend: `local-appdata`
- evidence backend: `local-appdata`

## Workspace Boundary

A source workspace may contain:

- source code
- project docs and tests
- normal repo metadata
- optional small AMOF pointers later

A source workspace must not contain default AMOF runtime state such as:

- evidence receipts
- run summaries
- materialized exact-SHA workspaces
- queues
- locks
- provider session logs
- cloud runtime scratch state

## Migration Rules

Current bootstrap guidance is app-data only. Legacy workspace-local locations are
historical compatibility details, not part of the canonical first-run model.

## Consequences

Positive:

- installed CLI can be reused across repos
- Git workspaces stay cleaner
- local and future remote contexts are explicit
- ad-hoc repo onboarding no longer requires ecosystem creation

Trade-offs:

- some legacy commands still need follow-up migration beyond the MVP
- app-data inspection becomes part of operator troubleshooting
- context and workspace registry storage must now be treated as durable user config
