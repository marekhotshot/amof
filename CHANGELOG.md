# Changelog

All notable public changes to AMOF are documented in this file.

AMOF uses a clean public lineage starting with `v2.0.1`. Earlier prototype, private workspace, runtime/operator, and pre-public development history is preserved outside the public release lineage and is intentionally not represented as public release history.

## [Unreleased]

- No unreleased changes.

## [2.6.4] - 2026-05-22

### Fixed

- plan-execute now stops immediately on fatal subtask failures such as `cost_exceeded`, trust-boundary denial, missing required tools, writable-root denial, and invalid execution preconditions.
- remaining subtasks are skipped instead of attempted after fatal failure.
- execution-readiness preflight detects missing tools/capabilities before expensive execution.
- plan-scoped capability elevation supports explicit `secret` approval without weakening global guardrails.
- approved ops tool packs enable controlled shell-limited execution for Jenkins, K8s, and Helm workflows.
- writable report roots can be approved per plan/session without globally allowing arbitrary writes.
- fatal stop reasons are preserved and checkpoints are saved for resume/manual approval.

### Validation

- `python3 -m unittest tests.test_agent_runtime_profile.PlanExecuteFatalStopTests` passed (9 tests).
- `python3 -m unittest tests.test_agent_runtime_profile.PlanExecuteToolPackReadinessTests` passed (30 tests).
- `python3 -m unittest tests.test_agent_runtime_profile` passed (133 tests).
- `python3 -m unittest` passed (209 tests, 1 skipped).
- `python3 -m compileall scripts/amof -q` passed.
- `git diff --check` passed.

## [2.6.3] - 2026-05-21

### Fixed

- plan-execute readiness now understands tool packs for Jenkins, K8s, Helm render/deploy, reports, and code-edit workflows.
- readiness accounts for delegated runner Shell availability instead of checking only parent tools.
- helper scripts are classified as read/executable inputs, not writable report paths.
- scoped writable-root approval supports report output directories without globally authorizing `/tmp`.
- plan-scoped tool-pack and secret approvals remain explicit and non-global.
- budget aliases are handled consistently across `--budget`, `--max-cost`, and `--cost-limit`.

## [2.6.2] - 2026-05-21

### Fixed

- plan-execute now stops immediately on fatal subtask failures such as `cost_exceeded`, trust-boundary denial, missing required tools, writable-root denial, and invalid execution preconditions.
- remaining subtasks are skipped instead of attempted after fatal failure.
- execution-readiness preflight detects missing tools/capabilities before expensive execution.
- plan-scoped capability elevation allows explicit approval of required capabilities such as `secret` without weakening global guardrails.
- resume supports optional operator follow-up via inline text or file.
- budget controls are explicit via `--budget`, `--cost-limit`, `--subtask-budget`, `--add-budget`, `--require-budget-approval`, `--budget-strict`, and `--budget-status`.
- fatal stop reasons, budget approvals, follow-up metadata, and capability elevation metadata are recorded without storing raw secrets.

### Notes

- Checkpoint-guided resume restores completed subtasks and retries the failed subtask; full automatic resume without a checkpoint is not claimed.

### Validation

- `python3 -m unittest tests.test_agent_runtime_profile.PlanExecuteFatalStopTests` passed (9 tests).
- `python3 -m unittest tests.test_agent_runtime_profile.ResumeFollowupAndBudgetTests` passed (10 tests).
- `python3 -m unittest tests.test_agent_runtime_profile` passed (96 tests).
- `python3 -m unittest` passed (172 tests, 2 skipped).
- `python3 -m compileall scripts/amof -q` passed.
- `git diff --check` passed.

## [2.6.1] - 2026-05-20

### Fixed

- Added first-class `runpod` provider handling for AMOF agent profiles.
- Normalized RunPod OpenAI-compatible base URLs to exactly one `/v1` suffix.
- Added non-secret RunPod endpoint diagnostics for provider/path failures.
- Sent proxy-safe non-secret headers for RunPod OpenAI-compatible SDK calls.

### Changed

- Added a `test` optional dependency extra so focused operator tests can install `pytest` deterministically without adding it to runtime installs.
- Documented the source checkout test install path for AMOF development and operator validation.

### Validation

- `python -m pip install -e ".[test]"` passed.
- Focused provider/runtime tests passed.
- Full pytest passed in AMOF-272G validation.
- Runtime-only install was verified not to require `pytest`.
- AMOF-272F was promoted and remote-verified at `cde6ae34721d957af34bba51f78b64f4192eb42e`.

## [2.6.0] - 2026-05-19

### Added

- Added `./scripts/build-standalone-amof.sh` to build a single-file public `PEX` artifact at `./dist/amof`.
- Added `./scripts/smoke-standalone-amof.sh` to validate the standalone artifact against the public no-key smoke surface.

### Changed

- Public install docs now describe three install paths: a standalone artifact build, the checkout-local `./scripts/install-amof.sh` fallback, and the optional pipx path.
- The public smoke matrix now includes a standalone artifact gate for version, bounded `check`, `doctor`, provider setup, and clean `init --adopt .` adoption.

### Notes

- The standalone artifact is a Python-based executable built locally with `PEX`; it is not claimed as a native binary.
- The standalone artifact still requires a compatible `python3` runtime on the host unless proven otherwise for a specific environment.

### Validation

- `git diff --check` passed.
- `python3 -m unittest tests.test_check` passed.
- Full unit test suite passed.
- `./scripts/smoke-no-pipx-install.sh` passed.
- `./scripts/build-standalone-amof.sh` passed.
- `./scripts/smoke-standalone-amof.sh` passed, including:
  - executable artifact creation at `dist/amof`
  - `amof --version`
  - bounded `amof check` with a fake hanging `cursor`
  - `amof doctor`
  - `amof setup provider --list`
  - `amof setup provider bedrock --print-template`
  - clean adopted target repo with no source pollution

## [2.5.2] - 2026-05-19

### Fixed

- `amof check` version probes now time out instead of hanging indefinitely on optional tools such as `cursor` in WSL/corporate environments.

### Changed

- Public first-touch docs now explain what AMOF is, why it is evidence-first, and what happens after install in a short platform-engineer-focused quickstart.
- Public install guidance now treats the source-checkout no-pipx path as a primary install option, with pipx kept as an optional isolated path.

### Notes

- The primary no-pipx path is a checkout-local virtualenv install that produces an executable `amof` shim at `.venv/bin/amof`.
- This release does not claim a standalone `pyz` or single-file executable artifact yet.

### Validation

- `git diff --check` passed.
- `python3 -m unittest tests.test_check` passed.
- Full unit test suite passed.
- `./scripts/smoke-no-pipx-install.sh` passed, including:
  - no-pipx install
  - `amof --version`
  - bounded `amof check` with a fake hanging `cursor`
  - `amof doctor`
  - `amof setup provider --list`
  - `amof setup provider bedrock --print-template`
  - clean adopted target repo with no source pollution

## [2.5.1] - 2026-05-18

### Fixed

- Installed CLI package metadata now includes `requests`, so Bedrock/provider startup no longer requires `pipx inject amof requests`.
- `amof setup provider` now includes a first-class `bedrock` template/example that stores secret references only and does not call AWS during setup.
- Bedrock startup now keeps RunPod/profile-catalog loading lazy enough to avoid unrelated provider credential failures on the selected Bedrock path.
- Bedrock CA handling now honors `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, and `AWS_CA_BUNDLE`.
- First-run ecosystem resolution guidance now shows the exact adopt command plus the next agent command.

### Notes

- In enterprise TLS-intercepted environments, Bedrock is not expected to work out of the box without an explicit combined CA bundle that includes the system trust roots plus the corporate interception CA (for example, Zscaler).
- Release evidence for this version includes:
  - AMOF-268 smoke ref: `amof-268-bedrock-oob-smoke-20260518-212251`
  - AMOF-269 operator-supplied corp-laptop classification: `PASS_BEDROCK_INSTALLED_CLI_LIVE_PLAN`

### Validation

- Full unit test suite passed.
- `amof setup provider --list` includes `bedrock`.
- `amof setup provider bedrock --print-template` works.
- No-key/no-cloud app-data write/activate smoke passed.
- Installed-candidate metadata check confirmed `Requires-Dist: requests`.
- Installed-candidate Bedrock startup reached `AWS_REGION not set for Bedrock` instead of missing dependency or unrelated provider credential failures.

## [2.5.0] - 2026-05-17

### Changed

- Narrowed first-run CLI help to the clean public surface while preserving advanced, workspace, maintainer, and optional integration commands as callable documented topics.
- Replaced stale and corporate-facing public docs with v2.5.0-safe happy-path, source-checkout, public surface taxonomy, and smoke matrix documentation.
- Clarified public boundaries for pipx installs, no-key adoption, provider profile references, bounded worker diffs, and mandatory human review/test/commit.

### Validation

- Full unit test suite passed.
- CLI help matrix passed for all known top-level commands.
- Default help no longer presents maintainer mutation commands as first-run quickstart commands.
- Docs/stale grep and secret grep gates passed with allowlisted references only.
- No-key adoption/provider-validation smoke passed without target repo source pollution.

## [2.3.0] - 2026-05-17

### Added

- Public bounded worker execution path for adopted repositories.
- Default public `code` runner for `amof agent --plan-execute`.
- Truth gates for worker execution summaries, including failed tool-call accounting and mutation-intent verification.
- Bounded diff guardrails that detect destructive rewrites, unrelated file changes, and no-diff mutation failures.
- Dogfood proof that AMOF can operate on a disposable AMOF clone and produce a bounded documentation diff.

### Changed

- `amof agent --plan-execute` now produces more truthful execution summaries with completed, failed, and skipped subtask counts.
- Public runner defaults preserve source hygiene by writing AMOF runtime artifacts to app-data instead of target repositories.
- Planner and runner defaults are safer for public demos and bounded edits.
- Interactive agent shell now treats `exit`, `quit`, and `q` as exit commands instead of LLM tasks.

### Fixed

- Failed worker tool calls no longer count as successful subtasks.
- Mutation-intent tasks with no resulting diff are no longer reported as successful.
- Trust-boundary write intent detection now recognizes add, append, and change requests.
- Destructive full-file rewrites are blocked or reported instead of being accepted as successful bounded edits.
- Noninteractive `plan-execute` flows no longer hang on planner clarification prompts.

### Validation

- Full unit test suite passed.
- AMOF-238 tiny worker smoke produced a bounded `farewell()` diff in a disposable repo.
- AMOF-239 dogfood smoke modified only `docs/runbooks/happy-path-agent-workflow.md` in a disposable AMOF clone.
- Source pollution checks passed.
- Secret scans passed.

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
