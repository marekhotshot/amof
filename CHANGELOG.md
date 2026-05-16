# Changelog

All notable public changes to AMOF are documented in this file.

AMOF uses a clean public lineage starting with `v2.0.1`. Earlier prototype, private workspace, runtime/operator, and pre-public development history is preserved outside the public release lineage and is intentionally not represented as public release history.

## [Unreleased]

- No unreleased changes.

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
