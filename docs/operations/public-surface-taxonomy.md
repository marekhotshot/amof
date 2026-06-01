# Public Surface Taxonomy

Status: public v3.0.2 Runtime Authority taxonomy
Date: 2026-06-01

AMOF keeps more code than the first-run public path because the repo also
contains source-checkout utilities, advanced evidence tooling, optional provider
templates, and maintainer workflows. This taxonomy keeps those surfaces
discoverable without turning first-run help or the README into an internal
toolbox.

## Public First-Run Surface

These commands belong in the public quickstart and default first-run help:

- `amof --help`
- `amof --version`
- `amof check`
- `amof doctor`
- `amof paths`
- `amof setup provider`
- `amof init --adopt .`
- `amof context <adopted-ecosystem>`
- `amof intake template --kind bounded_intake_task`
- `amof intake validate`
- `amof intake submit`
- `amof agent --plan "Inspect this repo"`
- `amof execution scan`
- `amof execution report`
- `amof loop`
- `amof bootstrap contract`
- `amof bootstrap bundle`
- `amof update`
- `amof uninstall`
- `amof troubleshoot`

The public install path is:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v3.0.2"
```

After pipx install, the public command is the `amof` shim. System `python -m
amof` is not a public pipx contract.

AMOF is a local-first CLI and Runtime Authority surface for governed AI work.
It is not just a chatbot or a generic AI wrapper.

## Advanced Public Surface

These commands can be useful, but should be labeled as advanced or manual:

- `amof status`
- `amof preview`
- `amof manifest`
- `amof generated-build`
- `amof profile`
- `amof director`
- `amof workspace`
- `amof mcp`
- `amof server`

Advanced commands may assume source checkouts, workspace state, local services,
or manual operator review. They should not appear as the default quickstart path.

## Workspace-Oriented Surface

These commands are for AMOF workspace workflows, not the adopted-repo public
happy path:

- `amof sync`
- `amof add-repo`
- `amof repo`
- `amof install`
- `amof open`
- `amof ticket`
- `amof discard`
- `amof archive`
- `amof archive-list`
- `amof ecosystem`

They should stay callable for compatibility, but default public help should make
clear that they are workspace-oriented.

## Maintainer-Only Surface

These commands can mutate branches, promotion state, versions, tags, or remotes:

- `amof push`
- `amof promote-main`
- `amof promote-main-revert`
- `amof release`
- `amof pr`

They must not be presented as public quickstart or normal adopted-repo publishing
commands. Public users should use ordinary Git review and pull request workflows
unless they have explicitly opted into an AMOF workspace/promotion process.

## Optional Integrations And Historical Surfaces

These commands or scripts require extra credentials, service assumptions, or
historical workflow context:

- `amof jira`
- `amof kb`
- `amof spin`
- `amof actor`
- `amof director-action`
- `amof eval` (private-backed maintainer command; unavailable in public OSS)
- `scripts/runpod_t1_drive.py`
- deployment/build/smoke scripts under `scripts/` that are not referenced by
  the README public quickstart

Keep them documented as optional, experimental, private-backed, maintainer-only,
or pending cleanup. Do not claim they are part of the clean public baseline
until they have their own public docs, tests, and smoke evidence.

## Provider And Secret Boundary

Provider setup stores environment variable references only. Public docs may show
redacted examples such as:

```bash
export OPENROUTER_API_KEY="<redacted>"
```

Public docs and scripts must not include raw provider keys, private keys,
kubeconfig contents, customer-specific material, or private deployment topology.
Live provider calls are manual and out of scope for no-key public smoke.
