<p align="center">
  <img src="docs/assets/amof-logo.svg" alt="AMOF logo" width="140" />
</p>

<h1 align="center">AMOF</h1>

<p align="center"><strong>Agentic Operations Fabric</strong></p>

<p align="center">Public installable CLI for governed agentic operations, bootstrap validation, and repository hygiene.</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="Apache-2.0 license" /></a>
  <img src="https://img.shields.io/badge/release-v2.3.0-0A7FFF.svg" alt="release v2.3.0" />
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
- `amof setup provider`
- `amof init --adopt .`
- `amof agent --plan "Inspect this repo"`
- `amof agent --plan-execute "Make a bounded change"`
- `amof bootstrap contract`
- `amof bootstrap bundle`

## Current Scope

What works on this reduced main:

- `./scripts/install-amof.sh`
- `./scripts/install-local.sh`
- `./install.sh`
- `amof --help`
- `python -m amof --help`
- `amof update --check`
- `amof update`
- `amof check`
- `amof paths --json`
- `amof doctor --json`
- `amof setup provider --list`
- `amof init --adopt .`
- `amof agent --plan "Inspect this repo"`
- bounded `amof agent --plan-execute` runs in disposable or intentionally
  prepared repos
- `amof bootstrap contract --json`
- `amof bootstrap bundle --json`

What is intentionally not included on this canonical main:

- runtime services
- Kubernetes or Helm deployment flows
- infrastructure, runtime adapters, and embedded workspace trees
- demo UIs, cloud/prod deployment stacks, and runtime operator surfaces

## Quick Install

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v2.3.0"
```

This is the recommended public install path for end users. It installs the
`amof` CLI from the public GitHub tag without requiring a manual AMOF source
checkout.

## Update AMOF

For pipx installs, use AMOF's update command instead of running
`pipx install` again by hand:

```bash
amof update --check
amof update
```

To target a specific public release:

```bash
amof update --version v2.3.0
```

`amof update` uses `pipx install --force` for pipx-managed installs, so pipx
does not stop with an "already installed" message.

## Uninstall AMOF

```bash
amof uninstall
```

For pipx-managed installs, `amof uninstall` delegates to `pipx uninstall amof`
so pipx metadata and shims are cleaned up together. If the AMOF command itself
is broken, run the same cleanup directly:

```bash
pipx uninstall amof
```

Uninstalling the CLI does not delete your repositories or AMOF app-data.

## Try AMOF In Another Repo

```bash
cd /path/to/your/repo
amof check
amof doctor
amof bootstrap contract
amof bootstrap bundle
```

`amof check` validates the required public prerequisites first. In a fresh user
environment it may still report optional warnings for Git identity, SSH keys,
or provider setup depending on the workflows you plan to use. Those warnings do
not block basic public install, `doctor`, or bootstrap evidence commands.

## Adopt A Repo For Agent Planning

Use this path when you want AMOF to remember an existing Git repository without
manually creating an ecosystem manifest or passing `-e` on every agent command:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v2.3.0"
cd /path/to/my-repo
git init  # only needed if this is not already a Git repo
amof init --adopt .
amof doctor
amof setup provider openrouter --name openrouter-default --activate
export OPENROUTER_API_KEY="<redacted>"
amof agent --plan "Inspect this repo" --no-follow-up
```

Adoption stores a repo binding and minimal single-repo manifest in AMOF app-data.
It does not write files, journals, or guardrail config into the target repo by
default. Live LLM planning or execution still requires provider configuration;
without provider keys, the agent should reach the provider setup/key validation
message rather than fail on missing `--ecosystem/-e`.

## Bounded Worker Execution

AMOF v2.3.0 includes a public default `code` runner for bounded
`amof agent --plan-execute` demos in adopted repositories. The default runner is
limited to repository read/write tools and does not include shell, delete,
checkpoint, commit, or push tools.

Use this path only when you are prepared to review the resulting Git diff:

```bash
amof agent --provider openrouter --plan-execute \
  "Add a small pure function and matching test. Do not commit." \
  --no-follow-up
```

Bounded worker execution must still be treated as draft output. Inspect
`git status`, review the diff, and run relevant tests before committing. AMOF
must not auto-commit or push worker changes.

## Configure A Provider Profile

Provider setup stores profile metadata and environment variable references in
AMOF app-data. It does not store raw API keys, and it does not call the provider
while writing the profile. Live agent calls still require the referenced
environment variables to be set in your shell.

OpenRouter:

```bash
amof setup provider openrouter --name openrouter-default --activate
export OPENROUTER_API_KEY="<redacted>"
amof agent --plan "Inspect this repo" --no-follow-up
```

An explicit provider flag still wins over an activated profile:

```bash
amof agent --provider openrouter --plan "Inspect this repo" --no-follow-up
```

Local Qwen/Ollama-compatible endpoint:

```bash
amof setup provider local-qwen --name local-qwen \
  --base-url http://localhost:11434/v1 \
  --model qwen2.5-coder:7b \
  --activate
```

Runpod:

```bash
export RUNPOD_API_KEY="<redacted>"
export RUNPOD_OPENAI_BASE_URL="<redacted>"
amof setup provider runpod --name runpod-heavy --activate
```

For scripts or CI, add `--yes` to skip the confirmation prompt. Do not put an
OpenRouter key into `ANTHROPIC_API_KEY`; provider setup stores environment
variable references and metadata only, and live calls still require the matching
environment variable to be exported. The xAI profile template is available for
planning/bootstrap records, but current live execution may require provider
resolver support before xAI can be used directly.

Vector memory is optional. For pipx installs that need it, inject it into the
AMOF app environment instead of the target repo:

```bash
pipx inject amof chromadb pysqlite3-binary
```

## Install From Source

Use this path when developing, testing, or contributing to AMOF itself. It does
not require GitHub write credentials for the default public install path:

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
./scripts/install-amof.sh
```

Maintainers who need to verify `promote-main` readiness can opt in to the
GitHub auth dry-run:

```bash
./scripts/install-amof.sh --check-promote-auth
```

That maintainer-only check may require a non-interactive GitHub credential. It
is not required for normal public install or CLI validation.

## Expected Validation Commands

After a public tool install, run the canonical checks:

```bash
amof --help
amof paths --json
amof doctor --json
amof bootstrap contract --json
amof bootstrap bundle --json
```

For source-checkout development installs, use the equivalent `./.venv/bin/amof`
and `./.venv/bin/python -m amof` commands from the AMOF repo root.

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
