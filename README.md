# AMOF

AMOF is currently published on canonical `main` as a governed bootstrap and
contract-first CLI source tree.

This reduced main is intentionally narrow. It keeps the source, contracts,
install paths, and focused docs needed to:

- install the CLI from a fresh clone
- verify the checkout with `amof doctor`
- emit governed bootstrap evidence with `amof bootstrap contract`
- emit a receipt bundle with `amof bootstrap bundle`
- resolve AMOF app-data roots with XDG defaults or `AMOF_HOME`

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

## Bootstrap

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
./scripts/install-amof.sh
```

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
