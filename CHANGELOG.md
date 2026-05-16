# Changelog

All notable public changes to AMOF are documented in this file.

AMOF uses a clean public lineage starting with `v2.0.1`. Earlier prototype, private workspace, runtime/operator, and pre-public development history is preserved outside the public release lineage and is intentionally not represented as public release history.

## [Unreleased]

- No unreleased changes.

## [2.2.1] - 2026-05-16

### Fixed

- Installed CLI agent runtime now includes the provider/runtime dependencies needed for public agent planning.
- Active provider profile is used as the default agent provider when no `--provider` is passed.
- Adopted repo agent runs keep journals and plan outputs in AMOF app-data by default.
- Public adopted repo planning loads packaged default guardrails instead of warning about no protections.
- Optional vector memory no longer prints noisy `chromadb` warnings in default plan mode.
- Agent install guidance now distinguishes AMOF runtime dependencies from target project dependencies.

### Validation

- Verified unit tests.
- Verified Docker pipx candidate smoke.
- Verified adopted repo stayed clean.
- Verified activated OpenRouter profile selected by default.
- Verified missing key reported `OPENROUTER_API_KEY`.
- Verified no `--ecosystem/-e is required` failure.
- Verified no missing Python module errors.
- Verified no `NO protections` warning.
- Verified no default `chromadb` noise.

## [2.2.0] - 2026-05-16

### Added

- Added `amof setup provider` for guided public provider profile setup.
- Added provider profile templates for OpenRouter, local Qwen/Ollama-compatible endpoints, OpenAI, Anthropic, xAI, and Runpod.
- Added app-data provider profile writes under the AMOF provider profiles config directory.
- Added provider profile activation into the current context `provider_profile_refs`.
- Added a no-secret provider setup model that records environment variable names and redacted metadata instead of raw API keys.

### Changed

- Updated happy-path documentation to include provider setup before live agent planning.
- Doctor and bootstrap evidence can surface activated provider profile refs without performing live provider checks.

### Validation

- Verified setup provider list/template/dry-run/write/activate flows.
- Verified doctor/bootstrap visibility for an activated profile.
- Verified setup performs no live provider call.
- Verified unit tests.

## [2.1.1] - 2026-05-16

### Added

- Added `amof update` for clean public CLI updates.
- Added `amof update --check` to report the current version and latest stable public tag without modifying the install.
- Added a pipx-aware update path that runs `pipx install --force` for pipx-managed AMOF installs.

### Fixed

- Fixed `amof uninstall` for pipx-managed installs so it uses `pipx uninstall amof` instead of half-uninstalling the package from inside the pipx venv.
- Source checkout updates now refuse self-update and point users to `git fetch`/checkout or `./scripts/install-amof.sh`.

## [2.1.0] - 2026-05-16

### Added

- Added `amof init --adopt .` for adopting an existing Git repository into AMOF app-data.
- Added app-data repo adoption bindings and minimal app-data manifests for single-repo public onboarding.
- Added no-`-e` agent planning resolution from an adopted repo, so `amof agent --plan "Inspect this repo"` can resolve the ecosystem without manual `--ecosystem` input.

### Changed

- Improved arbitrary-repo failure guidance to show the detected Git root and suggest `amof init --adopt .`.
- Kept default adoption non-invasive: app-data is updated, and the target repository is not written to unless a future explicit local-write flow is implemented.
- Agent runs from adopted repos now reach provider validation instead of failing first on ecosystem resolution.

### Notes

- `2.0.1` remains the clean public baseline and install release.
- This release does not add full guided provider setup or guarantee live LLM planning/execution without provider configuration.

## [2.0.1] - 2026-05-16

### Added

- First clean public release of AMOF as the **Agentic Operations Fabric**.
- Public installable CLI surface for `amof --help`, `amof check`, `amof doctor`, `amof bootstrap contract`, and `amof bootstrap bundle`.
- Clean public `v2.0.1` Git lineage rooted at the validated public tree.
- Fresh-clone install path via `./scripts/install-amof.sh`.
- Apache-2.0 licensing for public use, modification, and distribution.

### Changed

- Repositioned AMOF as a public installable product repo rather than a private runtime/operator workspace.
- Aligned package metadata, README wording, CLI help, version output, and changelog to `AMOF v2.0.1`.
- Reduced the public repository scope to install, check, doctor, bootstrap contracts, bootstrap bundle generation, and public-safe extension surfaces.

### Removed

- Established public refs that no longer expose the pre-`v2.0.1` runtime/operator/deploy lineage.
- Removed the old `v2.0.0` tag anchor before public visibility.
- Removed retired demo-only and runtime/operator entrypoints from the current public tree where they were not required for install/help/check/doctor/bootstrap validation.
- Verified that the checked retired runtime/operator paths are not reachable from current public refs.

### Validation

The `v2.0.1` public tree was verified from a fresh clone:

- `./scripts/install-amof.sh`
- `amof --version`
- `amof --help`
- `python -m amof --help`
- `amof check`
- `amof doctor --json`
- `amof bootstrap contract --json`
- `amof bootstrap bundle --json`
- `python3 -m unittest discover -s tests`

Fresh clone verification confirmed:

- `main` points to the clean public root.
- `v2.0.1` points to the same clean public root.
- `v2.0.0` is absent.
- The checked retired runtime/operator paths are not reachable from public refs.
