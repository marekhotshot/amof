# AMOF-201: Installer And Bootstrap Design

Status: accepted
Owner: AMOF platform
Date: 2026-05-13
Related:
- `docs/adr/AMOF-198-app-data-context-scope.md`

## Decision

AMOF will implement a first installer/bootstrap model for an installed-local CLI that preserves the AMOF-198 app-data contract.

The installer is responsible for exposing the CLI, initializing app-data roots and local bootstrap config, and guiding the operator into the first safe commands. The installer is not a deploy tool and must not mutate runtime, Kubernetes, provider credentials, or source workspaces.

## Installer UX

AMOF will support three bootstrap entry shapes:

1. repo checkout bootstrap:
   - `./scripts/install-amof.sh`
2. remote install:
   - `curl -fsSL <remoteurl>/install.sh | bash`
3. safer remote install:
   - `curl -fsSL <remoteurl>/install.sh -o install-amof.sh`
   - `less install-amof.sh`
   - `bash install-amof.sh`
4. local dev install:
   - `./scripts/install-local.sh`

The initial installer surface should accept these flags:

- `--channel stable|dev|pinned`
- `--version <version-or-sha>`
- `--context local`
- `--install-dir <path>`
- `--amof-home <path>`
- `--no-shell-profile`
- `--dry-run`

The first implementation should default to:

- channel: `stable`
- context: `local`
- install root chosen automatically unless `--install-dir` is provided
- app-data roots resolved from `AMOF_HOME` when passed, else XDG defaults

## What Install Must Do

Install/bootstrap must:

- install or expose the `amof` CLI entrypoint
- create AMOF app-data directories as needed
- initialize default config files only when absent
- initialize the default `local` operational context
- verify the installed CLI by running `amof paths`
- optionally register the current workspace when the operator explicitly asks
- print clear next commands for first use

The installer must be idempotent:

- rerunning it should not destroy app-data
- rerunning it should not overwrite operator edits without explicit opt-in
- rerunning it should confirm the resolved install location, app-data roots, and effective channel/version

## What Install Must Never Do

Install/bootstrap must never:

- deploy anything
- run Helm
- mutate Kubernetes
- build or push images
- touch OpenSandbox
- perform runtime-sync
- fetch, copy, or prompt for provider secrets
- write runtime state into the project workspace by default
- mutate branches, commits, remotes, or any other Git state without explicit operator action

## Filesystem Model

The design separates three concerns:

1. CLI entrypoint location
2. AMOF app-data location
3. shell PATH integration

The installer should support:

- a user-selected `--install-dir`
- a default user-local executable location suitable for PATH integration
- local dev mode where the entrypoint resolves back into a cloned repo

App-data remains governed by AMOF-198:

- default config root: `~/.config/amof/`
- default data root: `~/.local/share/amof/`
- default cache root: `~/.cache/amof/`
- default state root: `~/.local/state/amof/`
- `AMOF_HOME` remains the explicit override for a flat app-data root

Permissions model:

- install locations must remain user-writable without requiring root
- app-data must be created with user-only expectations appropriate for local config/state
- installer must refuse to silently escalate privileges

Uninstall model:

- uninstall must remove the installed CLI exposure
- uninstall must not delete app-data by default
- app-data deletion, if offered later, must be explicit and separately confirmed

## Security Model

The design accepts that `curl | bash` is convenient but higher-risk.

Therefore the design requires:

- a documented safer download-and-inspect path
- no secrets embedded in install scripts
- no credential copying from source repos, shell env, or kubeconfig locations
- explicit confirmation before editing shell profiles unless the operator requested non-interactive behavior
- no protected contexts created by default

Future hardening, but not part of the first slice:

- published checksums
- signature verification
- release metadata suitable for pinned installs

## Local Dev Install

AMOF must support development from a cloned repo without breaking the installed-local mental model.

Local dev install should:

- expose a local wrapper such as `./scripts/install-local.sh`
- support editable/dev mode from the cloned source tree
- preserve app-data semantics under fresh `HOME` exactly as proven by AMOF-200
- avoid writing runtime state into the cloned repo workspace by default

Local dev install should not try to behave like a package manager. It is a development convenience wrapper for exposing the CLI while keeping the same app-data contract.

## Channels And Versioning

The installer design supports:

- `stable`: latest released installable version
- `dev`: development channel intended for local or pre-release testing
- `pinned`: exact version or exact source SHA

Installer metadata should record:

- installed channel
- requested version or SHA when pinned
- install source metadata sufficient for later diagnostics

Later update behavior should be a separate command surface rather than implicit installer mutation. Install should not silently self-update.

## Bootstrap After Install

After successful install, the CLI should print a minimal first-run sequence:

1. `amof paths`
2. `amof context current`
3. `amof doctor --json`
4. `amof bootstrap contract --json`
5. `amof bootstrap bundle --json`

Future bootstrap may extend to:

- `amof project init`

That future project bootstrap is explicitly out of scope for this ADR.

## Non-Goals

This design does not include:

- remote worker installation
- cloud-dev bootstrap
- SaaS login or hosted operator login
- templates
- project generation
- Kubernetes deployment
- runtime execution beyond install, doctor, and bootstrap evidence

## Consequences

Positive:

- AMOF gets a coherent installed-local entry story to match the app-data model already proven in AMOF-200
- operators get a safer and more predictable bootstrap path
- app-data isolation remains intact across both packaged and local dev usage

Trade-offs:

- installer work introduces version/channel metadata that must remain truthful
- uninstall and update behaviors need explicit follow-up tickets
- shell profile integration must be conservative to avoid surprising operators

## Implementation Roadmap

Future tickets:

- `AMOF-202`: local dev installer script
- `AMOF-203`: remote `install.sh` skeleton
- `AMOF-204`: installer dry-run and idempotency tests
- `AMOF-205`: uninstall command/design
- `AMOF-206`: version/channel metadata
- `AMOF-207`: workspace registration during install
- `AMOF-208`: package/release path
