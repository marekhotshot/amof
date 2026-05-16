<p align="center">
  <img src="docs/assets/amof-logo.svg" alt="AMOF logo" width="140" />
</p>

<h1 align="center">AMOF</h1>

<p align="center"><strong>Agentic Operations Fabric</strong></p>

<p align="center">Public installable CLI for governed agentic operations, bootstrap validation, and repository hygiene.</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="Apache-2.0 license" /></a>
  <img src="https://img.shields.io/badge/release-v2.0.1-0A7FFF.svg" alt="release v2.0.1" />
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg" alt="Python 3.11+" />
</p>

AMOF is currently published on canonical `main` as a governed bootstrap and
contract-first CLI source tree.

## What AMOF Does

AMOF is a local-first CLI for turning a repository or workspace into a
governed agentic operations surface. It validates prerequisites, exposes
bootstrap contracts, produces evidence bundles, and keeps automation
boundaries explicit before an orchestrator or agent acts on a codebase.

## What AMOF Does Not Do

The public repo does not ship private deployment topology, live runtime
operators, kubeconfigs, customer environments, or internal cloud workflows.
Those belong outside the public product tree.

## Public Surface

This public `main` intentionally keeps a narrow, installable surface:

- `./scripts/install-amof.sh`
- `amof check`
- `amof doctor`
- `amof bootstrap contract`
- `amof bootstrap bundle`

## Current Scope

What works on this reduced main:

- `./scripts/install-amof.sh`
- `./scripts/install-local.sh`
- `./install.sh`
- `amof --help`
- `python -m amof --help`
- `amof check`
- `amof paths --json`
- `amof doctor --json`
- `amof bootstrap contract --json`
- `amof bootstrap bundle --json`

What is intentionally not included on this canonical main:

- runtime services
- Kubernetes or Helm deployment flows
- infrastructure, runtime adapters, and embedded workspace trees
- demo UIs, cloud/prod deployment stacks, and runtime operator surfaces

## Quick Start From Fresh Clone

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
./scripts/install-amof.sh
```

## Expected Validation Commands

After install, run the canonical checks:

```bash
./.venv/bin/amof --help
./.venv/bin/python -m amof --help
./.venv/bin/amof paths --json
./.venv/bin/amof doctor --json
./.venv/bin/amof bootstrap contract --json
./.venv/bin/amof bootstrap bundle --json
```

In a brand-new isolated AMOF home, `amof doctor` and the bootstrap commands may
report a non-blocking `WARN` when no provider profile references are configured
yet. That warning is acceptable for fresh public installs and does not imply any
private runtime, cluster, or operator prerequisite.

AMOF runtime state does not belong in the source checkout. By default the CLI
uses XDG roots such as `~/.config/amof`, `~/.local/share/amof`,
`~/.cache/amof`, and `~/.local/state/amof`. When `AMOF_HOME` is set, AMOF uses
that directory as a flat app-data root.

## Documentation

The retained operator docs for this slice are:

- `docs/operations/corp-laptop-bootstrap.md`
- `docs/adr/AMOF-198-app-data-context-scope.md`
- `docs/adr/AMOF-201-installer-bootstrap-design.md`
- `contracts/README.md`
- `contracts/INDEX.md`

## Plan State

- UP10 governed workstation bootstrap is complete and remains the baseline for
  this reduced main.
- UP11 runtime-observer work has not started here beyond the contract-first
  source reduction needed to make canonical `main` truthful.

## Change History

`CHANGELOG.md` records release and reduction history for this repo. The current
top entry should be read as the authoritative statement of what canonical
`main` contains.

## License

AMOF is licensed under the Apache License 2.0. See `LICENSE`.
