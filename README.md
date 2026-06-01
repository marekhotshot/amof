<p align="center">
  <img src="docs/assets/amof-logo.svg" alt="AMOF logo" width="140" />
</p>

<h1 align="center">AMOF 3.0 Runtime Authority is live.</h1>

<p align="center"><strong>Agentic Operations Fabric</strong></p>

<p align="center">AMOF now owns runtime truth across intake, context, runners, scans, bounded loops, receipts, and evidence — while cognition workers remain replaceable.</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="Apache-2.0 license" /></a>
  <img src="https://img.shields.io/badge/release-v3.0.3-0A7FFF.svg" alt="release v3.0.3" />
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg" alt="Python 3.11+" />
</p>

AI agents are cheap. Runtime truth is not.

AMOF v3.0.3 is a local-first CLI and Runtime Authority surface for governed AI
work. It controls context, execution readiness, policy attribution, receipts,
and evidence before cognition workers mutate anything. It validates the
workstation, stores app-data and receipts outside the target repo, records
provider profile references, and packages the post-`v3.0.2` local-planning
runner template/readiness dogfood path without changing runtime execution
semantics.

AMOF is for platform and DevOps engineers who want an auditable runtime loop:
LLM calls are workers inside a governed runtime, not the authority for source
truth, runtime truth, or mutation policy.

It is not just a chatbot and not a generic AI wrapper. The public contract is a
governed cognition runtime around bounded loops, runtime truth, and execution
evidence.

## What AMOF Is

AMOF turns a repository into a governed cognition runtime:

- `amof check` and `amof doctor` verify the workstation and app-data layout.
- `amof init --adopt .` binds an existing Git repo into AMOF app-data.
- `amof setup provider ...` stores provider references, not raw secrets.
- `amof chat plan` produces a non-executable proposal through remote IAL.
- `amof chat start|ask|status|finalize` shape a bounded proposal-only intake session.
- `amof chat approve` and `amof chat handoff` create explicit approval and handoff artifacts only.
- `amof agent --plan` is read-only planning.
- `amof execution scan|report` provides readiness/evidence surfaces without
  execution dispatch.
- `amof loop` provides bounded non-mutation runtime loops with evidence output.

```text
source repo + runtime evidence
        |
        v
AMOF governance loop ---- receipts / provenance / approvals
        |
        v
optional cognition worker: local model, hosted provider, or remote IAL gateway
        |
        v
proposal, bounded plan, or explicitly approved execution artifact
```

AMOF owns the loop around source truth, runtime truth, receipts, and approval
boundaries. Vendor runtimes and local models are optional cognition workers
behind that loop.

## Runtime Authority

AMOF does not trust chat output as runtime truth. Runtime truth is emitted as
inspectable evidence through receipts, runtime logs, run records, intake
records, selected context, runner metadata, and bounded loop reports.

Public v3.0.3 runtime authority surfaces:

- context selection via `amof context`
- governed intake validation/submission via `amof intake`
- runner registry metadata via `amof runner`
- execution readiness scan/report via `amof execution` (`NO_EXECUTION_PERFORMED`)
- bounded loops via `amof loop` (`NO_MUTATION_PERFORMED`, `NO_REMOTE_EXECUTION_DISPATCHED`)
- run inspection via `amof runs`
- runtime logs and receipt/evidence surfaces with public-safe metadata

## Governed Intake

AMOF accepts messy work through intake, validates contract shape and runtime
constraints, preserves planning-only behavior when required, and routes work to
governed execution readiness instead of ad hoc prompting.

## Context Discipline

AMOF uses explicit context selection. If required remote/cloud context is
unavailable, AMOF fails closed and returns an error. It does not silently
fallback to local execution.

## Runner Registry

Runners are metadata-driven and replaceable. Runtime coordination and policy
attribution live in AMOF; individual cognition workers are not the authority.

## Execution Scan / Report

AMOF can preview readiness, match intake packets to eligible runners, surface
blockers, and generate reports without dispatching remote execution.

## Bounded Loops

AMOF supports controlled long-running loops with explicit stop conditions,
evidence output, and policy discipline.

## Receipts and Evidence

AMOF records runtime facts through receipts and evidence surfaces while keeping
public safety boundaries: no secrets, no raw prompts, no raw
`provider_generation_id`, no private customer topology, and no sensitive auth
material in public surfaces.

AMOF preserves provider cost truth:

- missing provider cost remains unknown/null
- missing cost is never reported as fake `0.0`

## Operator Console Preview

Label: **Cloud-dev live preview**

The cloud-dev Operator Console exposes AMOF runtime receipts, intake
submissions, selected runs, policy attribution, and sanitized evidence/debug
surfaces. It is a live preview over the current AMOF runtime path, not a fake
demo surface.

Caution: Cloud-dev preview. Public-safe runtime surfaces only. Known gaps are
tracked as follow-up slices.

CTA: [Open Operator Console Preview](https://console-cloud-dev.amof.dev/)

IAL reference (auth-bound gateway surface): [https://ial-cloud-dev.amof.dev/](https://ial-cloud-dev.amof.dev/)

## Current Known Next Slices

- Runtime logs viewer contract and minimal UI
- Receipt count semantics contract
- Console rollout guardrail comparing deployed hash vs intended source

## Why Evidence-First

AMOF keeps evidence and runtime state in app-data instead of spraying files into
the target repo. The goal is to make agent actions reviewable:

- the target repo stays clean until you explicitly ask for mutation
- provider setup records secret references only
- journals, runs, contexts, and bootstrap records live in AMOF app-data
- AMOF does not mutate, commit, or push unless you explicitly ask it to do so

## What AMOF Does Not Do

The public repo does not ship private deployment topology, live runtime
operators, kubeconfigs, customer environments, or internal cloud workflows.
Those belong outside the public product tree.

## Public Surface

This public `main` intentionally keeps a narrow, installable v3.0.3 surface:

- `./scripts/install-amof.sh`
- `./scripts/build-standalone-amof.sh`
- `./dist/amof`
- `amof check`
- `amof doctor`
- `amof setup provider`
- `amof init --adopt .`
- `amof chat plan "Inspect this repo"`
- `amof chat start "Clarify this repo"`
- `amof chat approve <session-id>`
- `amof chat handoff <approval-id-or-path>`
- `amof agent --plan "Inspect this repo"`
- `amof execution scan --help`
- `amof execution report --help`
- `amof runner template --kind local-planning`
- `amof loop --help`
- `amof bootstrap contract`
- `amof bootstrap bundle`

## Released Public CLI Surface

What works in v3.0.3:

- `./scripts/install-amof.sh`
- `./scripts/build-standalone-amof.sh`
- `./scripts/install-local.sh`
- `./install.sh`
- `./dist/amof --version` after a local standalone build
- `amof --help`
- `pipx runpip amof show amof` for installed package metadata
- `amof update --check`
- `amof update`
- `amof check`
- `amof paths --json`
- `amof doctor --json`
- `amof setup provider --list`
- `amof init --adopt .`
- `amof chat plan "Inspect this repo" --repo .`
- `amof chat start "Clarify this repo" --repo .`
- `amof chat ask <session-id> "Bounded answer"`
- `amof chat status <session-id>`
- `amof chat finalize <session-id>`
- `amof chat approve <session-id>`
- `amof chat handoff <approval-id-or-path>`
- `amof agent --plan "Inspect this repo"`
- `amof runner template --kind local-planning`
- `amof runner register <runner.yaml>`
- `amof runner list`
- `amof runner doctor`
- `amof runner match <intake.yaml>`
- `amof execution scan <intake.yaml>`
- bounded non-mutation runtime loops via `amof loop`
- `amof bootstrap contract --json`
- `amof bootstrap bundle --json`

What is intentionally not included on this canonical main:

- runtime services
- Kubernetes or Helm deployment flows
- infrastructure, runtime adapters, and embedded workspace trees
- demo UIs, cloud/prod deployment stacks, and runtime operator surfaces

## 60-Second Quickstart

Single-file public executable path:

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
./scripts/build-standalone-amof.sh
./dist/amof check
./dist/amof doctor
```

What this gives you:

- a generated single-file executable at `./dist/amof`
- no checkout-local virtualenv is required just to run the artifact
- no `pipx`, `node`, `npm`, or `npx` required

The standalone artifact is not a native binary. It is a single-file Python
executable built locally with `PEX`, and it still requires a compatible
`python3` runtime on the host unless proven otherwise for your environment.

## Install Paths

### Standalone artifact

Use this if you want a single-file public executable without pipx:

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
./scripts/build-standalone-amof.sh
./dist/amof --version
```

The generated artifact:

- lives at `./dist/amof`
- is a single file built locally from the AMOF checkout
- is not a native binary
- still requires `python3` on the host

For a focused walkthrough, see `docs/runbooks/install.md`.

### Checkout-local fallback

Use this if you want the existing source-checkout install path:

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
./scripts/install-amof.sh
./.venv/bin/amof --version
```

This fallback remains supported for local development and for users who prefer
an explicit checkout-local virtualenv.

### Optional pipx path

Use this if you prefer an isolated user install:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v3.0.3"
```

This installs the `amof` CLI from the public GitHub tag into a pipx-managed
environment.

After a `pipx install`, use the `amof` command installed by pipx:

```bash
amof --version
pipx runpip amof show amof
```

System `python -m amof` is not the public pipx contract because the system
interpreter is outside the pipx-managed AMOF virtualenv.

## Update AMOF

For pipx installs, use AMOF's update command instead of running
`pipx install` again by hand:

```bash
amof update --check
amof update
```

To target a specific public release:

```bash
amof update --version v3.0.3
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

## After Install

The normal public path after install is:

1. adopt a repo with `amof init --adopt .`
2. inspect app-data health with `amof doctor`
3. configure a provider profile with `amof setup provider ...`
4. run read-only planning with `amof agent --plan`
5. use `amof execution scan|report` and `amof loop` for governed non-mutation
   runtime loops

AMOF stores repo bindings, contexts, journals, run logs, and provider-profile
references in app-data. It does not write `.amof`, `ecosystems`, or `context`
directories into the adopted target repo by default.

## Local Planning Runner Dogfood Path

Use this local-only path to move from intake capture to runner matching and an
execution readiness scan without dispatching work:

```bash
amof init --adopt "$PWD" --name my-repo
amof context my-repo
amof intake template --kind bounded_intake_task > intake.yaml
amof intake validate intake.yaml
amof runner template --kind local-planning > runner.yaml
amof runner register runner.yaml
amof runner match intake.yaml
amof execution scan intake.yaml
```

The generated runner is planning/readiness metadata only: local context,
`read_only` mutation mode, no endpoint URL, no credentials, no dispatch, and no
execution.

## Adopt A Repo For Agent Planning

Use this path when you want AMOF to remember an existing Git repository without
manually creating an ecosystem manifest or passing `-e` on every agent command:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v3.0.3"
cd /path/to/my-repo
git init  # only needed if this is not already a Git repo
amof init --adopt .
amof doctor
amof setup provider openrouter --name openrouter-default --activate
# Ensure OPENROUTER_API_KEY is set in your shell before running this command.
amof agent --plan "Inspect this repo" --no-follow-up
```

Adoption stores a repo binding and minimal single-repo manifest in AMOF app-data.
It does not write files, journals, or guardrail config into the target repo by
default. Live LLM planning or execution still requires provider configuration;
without provider keys, the agent should reach the provider setup/key validation
message rather than fail on missing `--ecosystem/-e`.

## Bounded Loops and Scan/Report

The v3.0.3 Runtime Authority release packages the post-`v3.0.2` local-planning
runner/readiness dogfood path while keeping governed non-mutation runtime
flows:

- `amof execution scan` and `amof execution report` for readiness and evidence
  (`NO_EXECUTION_PERFORMED`)
- `amof loop` for bounded long-running loops with stop conditions and evidence
  (`NO_MUTATION_PERFORMED`, `NO_REMOTE_EXECUTION_DISPATCHED`)

This release framing does not claim autonomous remote dispatch or unrestricted
mutation execution in public runtime surfaces.

## Configure A Provider Profile

Provider setup stores profile metadata and environment variable references in
AMOF app-data. It does not store raw API keys, and it does not call the provider
while writing the profile. Live agent calls still require the referenced
environment variables to be set in your shell.

OpenRouter:

```bash
amof setup provider openrouter --name openrouter-default --activate
# Ensure OPENROUTER_API_KEY is set in your shell before running this command.
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

Bedrock:

```bash
amof setup provider bedrock --name bedrock-default --activate
export AWS_PROFILE="<your-profile>"
export AWS_REGION="eu-central-1"
export SSL_CERT_FILE="/path/to/combined-ca.pem"
export REQUESTS_CA_BUNDLE="/path/to/combined-ca.pem"
export AWS_CA_BUNDLE="/path/to/combined-ca.pem"
amof agent --provider bedrock --plan "Inspect this repo" --no-follow-up
```

The installed CLI Bedrock path no longer requires `pipx inject amof requests`.
In enterprise TLS-intercepted environments, Bedrock is not expected to work
without an explicit combined CA bundle containing the system trust roots plus
the corporate interception CA. `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` cover
Anthropic/httpx trust; `AWS_CA_BUNDLE` covers AWS SDK trust.

Remote IAL:

```bash
export AMOF_REMOTE_IAL_BASE_URL="https://ial.example.invalid"
export AMOF_REMOTE_IAL_API_KEY="<redacted>"
amof setup provider remote-ial --name remote-ial --activate --yes
amof chat plan "Inspect this repo" --repo . --file README.md --max-files 1
```

The verified public contract is client-side only. The installed CLI can call a
configured remote IAL gateway, request structured planning output, parse strict
JSON locally, and fail closed with `ProviderError` when the response is not
valid for the expected schema. Public AMOF does not ship the gateway, provider
routing policy, model ladder, credentials, or deployment topology.

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
amof --version
amof --help
pipx runpip amof show amof
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

Additional public docs retained in this repo include:

- `docs/governed-cognition-runtime.md`
- `docs/remote-ial.md`
- `docs/runbooks/install.md`
- `docs/runbooks/happy-path-agent-workflow.md`
- `docs/runbooks/installed-cli-bedrock.md`
- `docs/operations/amof-269-operator-supplied-bedrock-live-smoke.md`
- `docs/operations/source-checkout-bootstrap.md`
- `docs/operations/public-surface-taxonomy.md`
- `docs/operations/public-smoke-matrix.md`
- `docs/adr/AMOF-198-app-data-context-scope.md`
- `docs/adr/AMOF-201-installer-bootstrap-design.md`
- `contracts/README.md`
- `contracts/INDEX.md`

## Release State

- `AMOF_300_RELEASE_PUBLIC_DOCS_BACKFILL`
- `v3.0.3` is the current AMOF 3.0 Runtime Authority release packaging the
  post-`v3.0.2` runner-template dogfood fixes.
- `v3.0.0` remains as a historical broken escaped tag and is not rewritten.
- `v3.0.1` remains as the prior correction release in this line.
- Runtime Authority framing for public `v3.0.3` includes:
  - explicit runtime context via `amof context`
  - intake contract and CLI intake via `amof intake`
  - runner capability registry via `amof runner`
  - `amof runner template --kind local-planning`
  - local runner registration/list/doctor/match without dispatch
  - execution scan/report surfaces with `NO_EXECUTION_PERFORMED`
  - bounded loops with `NO_MUTATION_PERFORMED` and `NO_REMOTE_EXECUTION_DISPATCHED`
  - runtime evidence inspection via `amof runs` and runtime logs contract tests
  - remote IAL cost truth with `REMOTE_IAL_SMOKE_STATUS_EXPLICIT=PASS`
  - adopted repo context resolution for app-data ecosystems
  - external repo dogfood through `hotshot.sk`, including dotted repo-name
    context resolution
  - aggregate intake missing-field reporting
  - `amof intake template --kind bounded_intake_task`
  - standalone smoke current-version hygiene for released artifacts
- Current `v3.0.3` limitations:
  - no remote execution dispatch
  - no mutation execution
  - console runtime logs viewer is not part of `v3.0.3`
- Release evidence docs:
  - `docs/releases/amof-3.0-closeout.md`
  - `docs/releases/amof-3.0.0-tag.md`
- `docs/releases/amof-3.0-runtime-authority.md` tracks the current release truth (`v3.0.3`).
- The `v3.0.0` tag documentation remains historical evidence of the broken escaped release.

## Change History

`CHANGELOG.md` records release and reduction history for this repo. The current
top entry should be read as the authoritative statement of what canonical
`main` contains.

Future release rule: release docs means README, CHANGELOG, and release notes/closeout docs. README and CHANGELOG must be committed and fresh-clone verified before future version tags. Only tag object SHA and remote push verification may be documented after tag creation.

## License

AMOF is licensed under the Apache License 2.0. See `LICENSE`.
