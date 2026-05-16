# Changelog

All notable changes to AMOF will be documented in this file.

## [Unreleased]

- No unreleased changes.

## [2.0.1] - 2026-05-16

### Changed
- Reduced canonical `main` to the public-safe install, help, `check`, `doctor`, and governed bootstrap evidence surfaces needed for the first UP11 contract-first slice.
- Removed the remaining public deploy, runtime, database, smoke, and private DNS/operator entrypoints that depended on kubeconfig, live clusters, or other private topology assumptions.
- Aligned package and release metadata with the reduced public 2.x canonical main after the AMOF-223 surface-reduction promotion.

### Validation
- `./scripts/install-local.sh --dry-run --install-dir /tmp/amof-local-bin --amof-home /tmp/amof-local-home --context local --no-shell-profile`
- `./install.sh --dry-run --install-dir /tmp/amof-remote-bin --amof-home /tmp/amof-remote-home --context local --no-shell-profile`
- `./.venv/bin/amof --help`
- `./.venv/bin/python -m amof --help`
- `./.venv/bin/amof check`
- `./.venv/bin/amof doctor --json`
- `./.venv/bin/amof bootstrap contract --json`
- `./.venv/bin/amof bootstrap bundle --json --output-dir /tmp/amof-canonical-reduction-check`
- `python3 -m unittest discover -s tests`

## [1.5.0-rc.1] - 2026-03-23

### Changed
- Consolidated the AMOF platform workspace around canonical `repos/*` edit paths and aligned the tracked core repo set, including `opensandbox`.
- Hardened live operator/runtime surfaces across prod-dev and cloud-dev, including auth lock-down, preserved lifecycle evidence, and the prod UltraConsole rollout on `uc-amof.hotshot.sk`.
- Preserved one canonical bounded-run proof path and added backup/retention foundations so noisy interactive history can be archived without losing operator evidence.

## [1.1.0-beta.2] - 2026-02-16

### Added
- **`amof troubleshoot` command** — Diagnoses environment, workspace, recent agent errors, and configuration issues with actionable fix suggestions
- **`amof help <command>` command** — Extended help with real-world examples, workflow guidance, and documentation links for every CLI command
- **Workflow documentation** — Step-by-step guides for common workflows (see `amof help <command>` for each topic)
- **ErrorExplainer integration** — All orchestrator tools (Read, Write, StrReplace, Delete, Shell) now provide context-aware error messages with "Did you mean?" file suggestions, recovery guidance, and next-step hints
- **High-risk file identification** — `CodebaseIndex.high_risk_files()` scores files by complexity, dependency count, entry point status, and symbol count. Top 5 high-risk files included in planner context
- **Manifest `--strict` flag** — `amof manifest validate --strict` treats warnings as errors. Added value range checks for `context.max_files` and `context.summary_tokens`

### Changed
- **README.md slimmed down** — Moved detailed CLI reference, manifest examples, workspace structure, and architecture details to docs/. README now focuses on quick start + documentation links
- **ENHANCEMENTS.md** — Updated with status tracking for all implemented items

## [1.0.2-alpha.2] - 2026-02-09

### Added
- **`amof release` command** - Automated versioning, changelog updates, commit, tag, push
  - SemVer with pre-release suffixes: `amof release patch --alpha` → `v1.0.3-alpha.1`
  - Auto-increment: running same bump+stage again increments pre-release number
  - Promote workflow: `amof release promote --beta` (alpha→beta), `amof release promote` (→stable)
  - Updates `__version__` in `__init__.py`, `CHANGELOG.md` header, `README.md` status line
  - `--dry-run` to preview, `-y` to skip confirmation, `--no-push` for local-only
- **`/release` slash command** in interactive shell — pick next version from a menu
- **`/review` slash command** in interactive shell — quick `git diff --stat`
- **`[t] Tag release` option** in post-run follow-up menu
- **`auto_tag_on_complete` config** in `.amof/agent.yaml` — auto-tag after agent task completion
  - Set to `"alpha"`, `"beta"`, or `"rc"` to auto-tag; `false` to disable

## [1.0.2-alpha] - 2026-02-09

### Added
- **Interactive guardrail confirmation** for sensitive and dangerous commands in interactive mode
  - Agent prompts user with `[y]es / [n]o / [a]lways` when a `sensitive_command` or `dangerous_pattern` is detected
  - "Always allow" persists to `.amof/rules/allowed.yaml` — future invocations of the same pattern skip the prompt
  - Unattended/executor mode: both categories are now consistently blocked (previously dangerous patterns only warned)
  - Telemetry tracks `user_confirmed` and `user_rejected` counters
  - Colored prompt output consistent with the interactive shell theme

### Changed
- `Guardrails` class accepts `confirm_fn` callback for interactive confirmation
- `check_shell()` uses unified `_confirm_or_block()` flow for both dangerous and sensitive patterns
- `ToolRegistry._check_guardrails()` no longer logs separate warnings for dangerous patterns (handled in `check_shell`)
- `guardrails.yaml` comments updated to document the new confirmation behavior

## [1.0.1-alpha] - 2026-02-09

### Added
- **Merkle Tree Codebase Index** (`scripts/amof/orchestrator/merkle.py`) - Efficient incremental change detection for repos
  - SHA256 hash tree: per-file leaves, per-directory interior nodes, single root fingerprint
  - `MerkleTree.diff()` compares two trees, skips matching subtrees, identifies added/modified/deleted files
  - Incremental re-indexing: only changed files sent to LLM (~$0.01-0.10 vs $0.10-0.50 for full re-index)
- **Codebase Index in Planner Context** - Planner now receives structured file descriptions, architecture, dependency graph from repos/
  - `CodebaseIndex.to_context_string()` produces compact 2-5k token representation
  - `ContextBuilder` accepts optional `codebase_index` parameter
  - First run: full index; subsequent: Merkle diff + incremental update; cache hit: instant
- **Index on Install** - `amof install` creates Merkle tree + LLM index after repo profiling
  - Skips LLM index if no API key (builds Merkle tree only, indexes on first `amof agent` run)
- **Auto-index on Agent Startup** - Checks Merkle root hash, incremental update if stale
- **Operational Tools** - Agent can now interact with deployments and infrastructure
  - `K8sTool` - Kubernetes: pods, logs, env, describe (wraps `scripts/tools/debug/k8s.sh`)
  - `HelmTool` - Helm: template, diff, sync (wraps `scripts/tools/helm/`)
  - `ImagesTool` - Container images: discover, diff, verify, migrate (wraps `scripts/tools/images/`)
  - `JenkinsTool` - Trigger Jenkins pipelines (wraps `scripts/tools/jenkins/trigger.sh`)
  - `AuditTool` - Record changelog entries (wraps `scripts/tools/audit/record.sh`)
  - Conditionally registered (only if scripts exist on disk)
- **Extended Thinking** for Claude Opus 4.6 and future thinking models
  - Auto-detects thinking models, sets `temperature=1`, uses `thinking.type=adaptive`
  - Captures thinking blocks in `LLMResponse.thinking`, displays in shell (faded italic)
  - Configurable `thinking_budget` in `.amof/agent.yaml`
  - Conditional assistant prefill (disabled for thinking models)
- **Robust Planner JSON Parser** - 4-strategy approach: direct parse, code fence extraction, brace-counting, truncated JSON repair
- **Auto-install Dependencies** - `amof agent` installs from `requirements.txt` if packages are missing

### Changed
- **Linting: end-of-task instead of per-file** - Removed per-file `_auto_lint()` hook; lint all modified files once before task completion
  - `ToolRegistry` tracks `_modified_files` set during session
  - `Agent.run()` checks lints before completing; if issues found, injects them back for the agent to fix
  - Config: `lint_on_complete: true` replaces `auto_lint: true`
- **Config-driven agent startup** - `.amof/agent.yaml` loaded by Python CLI directly
  - No need for `-e` flag when `default_ecosystem` is set
  - All settings (verbose, model_ladder, max_cost, provider, thinking_budget) loaded from config
  - CLI flags override config values
- **`amof-agent` moved** from project root to `scripts/amof-agent`, simplified to .env loading + passthrough
- **Indexer scoped to repos/ only** - Does not index AMOF framework code; agent knows AMOF via system prompt/rules
- **Index storage per-ecosystem** - `ecosystems/<name>/index/` instead of global `.amof/`
- Planner `max_tokens` increased to 16384 with 3-attempt retry loop

## [1.0.0-alpha] - 2026-02-08

### Added
- **Custom Orchestrator** (`scripts/amof/orchestrator/`) - Full agent system replacing Cursor dependency
  - **Agent loop** (`agent.py`) - message -> LLM -> tool calls -> execute -> loop with cost ceiling and max iteration guard
  - **8 tools** mirroring Cursor's interface: Read, Write, StrReplace, Delete, Shell, Grep, Glob, LS
  - **Tool guardrails** - Hard enforcement at tool level: no_touch_paths, readonly repos, plan mode blocking, command allowlist
  - **Pluggable LLM backends** (`llm/base.py`) - Abstract interface with cost estimation and context window tracking
  - **Anthropic Claude backend** (`llm/anthropic.py`) - First implementation with auto SSL cert detection for corporate proxies
  - **Telemetry** (`telemetry.py`) - Per-call and cumulative token/cost/latency tracking (PRD section 9)
  - **Event log** (`events.py`) - Append-only JSONL at `.amof/runs/<session>/events.jsonl` (PRD section 11)
  - **Session management** (`session.py`) - Conversation state with persistence
  - **Context builder** (`context/builder.py`) - Assembles system prompt from repo profiles + manifest + guardrails + rules
  - **Rule loader** (`context/rules.py`) - Loads `.amof/rules/` or `.cursor/rules/` for agent instructions
- **Repo Profiler** (`scripts/amof/commands/profile.py`) - Content-aware tech stack detection
  - Reads actual manifest files (Chart.yaml, package.json, pom.xml, Jenkinsfile, Dockerfile, etc.)
  - Generates `.amof/profile.md` inside each repo with tech stack, structure, key files, cross-repo deps, guardrails, git activity
  - Replaces generic regex-based `context.py` approach that produced identical output for all repos
  - Auto-runs on `amof install` and `amof sync`
- **`amof agent` command** - Run the orchestrator agent
  - Single-shot: `amof -e eco agent "task description"`
  - Interactive REPL: `amof -e eco agent`
  - Plan mode: `amof -e eco agent --plan "design task"`
  - Options: `--verbose`, `--model`, `--max-cost`
- **`amof profile` command** - Generate repo profiles
  - All repos: `amof -e eco profile --all`
  - Single repo: `amof -e eco profile <name>`
- **Base prompts** (`prompts/master.md`, `prompts/runner.md`) - Configurable agent instructions
- **Design reference docs** (consolidated into `docs/ORCHESTRATOR_ARCHITECTURE.md`)
- **`requirements.txt`** - Python dependencies (anthropic SDK)

### Changed
- `amof install` now auto-generates repo profiles after sync
- `amof sync` now auto-updates repo profiles after sync
- `env` template includes `ANTHROPIC_API_KEY` placeholder
- README updated with orchestrator documentation and CLI reference
- Roadmap Phase 5 (Orchestrator) marked as alpha

## [0.11.0] - 2026-02-06

### Added
- **Simplified workspace model** - Per-ecosystem workspace branches instead of per-ticket
  - `workspace/<ecosystem>` branches (e.g., `workspace/demo-dev`)
  - One persistent branch per ecosystem, not per ticket
  - Ticket work managed via feature branches in repos only
- **`amof ticket` subcommand** - Manage tickets within ecosystem workspace
  - `ticket start <id>` - Create feature branches in repos, track in state
  - `ticket list` - Show all tickets and their repo branches
  - `ticket switch <id>` - Switch active ticket (auto-commits dirty repos)
  - `ticket end <id>` - Mark complete, optional `--cleanup` to delete branches
- **Multi-ticket tracking** - Work on multiple tickets in same ecosystem
  - State v3 schema tracks tickets with repo branch mappings
  - `active_ticket` field for current focus
- **Dynamic workspace files** - `*.code-workspace` generated on demand
  - Added to `.gitignore` to prevent conflicts
  - Regenerate with `amof workspace`
- **Template ecosystem** - `ecosystems/my-project/` as starter template
  - Copy and customize for new ecosystems
  - Includes README, journal, kb, playbooks structure

### Changed
- **`amof install`** no longer requires ticket ID
  - Creates `workspace/<ecosystem>` branch
  - Use `amof ticket start` to begin work on specific ticket
- **`amof archive`** now keeps workspace branch by default
  - New `--delete-workspace` flag to remove workspace branch
  - New `--cleanup-features` flag to delete all feature branches
- **Branch structure**
  - `main` contains only AMOF framework + template
  - Real ecosystems live in `workspace/<ecosystem>` branches
- **State schema** upgraded to v3 for multi-ticket support

### Removed
- Ticket ID argument from `install` command
- Auto-open Cursor after install (use `amof open` instead)
- Context generation during install (run manually if needed)

## [0.10.0] - 2025-12-10

> **Note:** Bitbucket/Jira/Confluence integrations are implemented but **not yet tested**; testing will be done once API access is available.

### Added
- **`amof pr` command** - Create pull requests for all changed repos
  - Uses Bitbucket REST API
  - Auto-generates title and description from ticket ID and commits
  - Supports `--reviewers` and `--dry-run` flags
  - Updates state.json with PR URLs
- **`amof jira` command** - Jira ticket operations
  - `jira info <ticket>` - Show ticket details
  - `jira context <ticket>` - Generate AI context markdown
  - Saves to `context/_ticket/<ticket>.md`
- **`amof kb` command** - Knowledge base sync with Confluence
  - `kb pull` - Pull articles from Confluence
  - `kb push` - Push local KB to Confluence
  - `kb diff` - Show differences
  - `kb sync` - Bi-directional sync
  - Uses frontmatter for tracking (confluence_id, last_synced)
- **Environment variables** for integrations:
  - Bitbucket: `BITBUCKET_URL`, `BITBUCKET_USER`, `BITBUCKET_TOKEN`
  - Atlassian: `ATLASSIAN_URL`, `ATLASSIAN_USER`, `ATLASSIAN_TOKEN`
  - Confluence: `CONFLUENCE_URL`, `CONFLUENCE_SPACE`
  - Jira: `JIRA_PROJECT`

## [0.9.0] - 2025-12-10

### Added
- **`amof repo promote <name>`** - Promote readonly repo to writable
  - Creates feature branch from current branch
  - Updates state.json with promotion tracking
  - Allows making changes to repos initially cloned as readonly
- **`amof repo cleanup`** - Delete unused feature branches
  - Removes feature branches with no commits
  - Helps clean up repos that were never modified

### Changed
- State tracking now includes `promoted_branch` and `promoted_from` fields
- `add-repo` command now supports `--readonly` flag

## [0.8.0] - 2025-12-10

### Added
- **`amof archive` command** - Finish workspace and preserve repo branches
  - Pushes all changes (repos + workspace branch)
  - Saves state to `ecosystems/<name>/archives/<ticket>.json`
  - Deletes workspace branch (local + remote)
  - **Keeps** repo feature branches for pending PRs
  - Returns to main branch
- **`amof archive-list` command** - List archived workspaces for ecosystem
- **Archives directory** - `ecosystems/<name>/archives/` stores completed workspace snapshots

### Changed
- Updated ENHANCEMENTS.md with clarified archive workflow
- Updated TODO.md with current focus

### Archive vs Discard
| | `amof discard` | `amof archive` |
|---|----------------|----------------|
| Push changes first | No | Yes |
| Save state | No | Yes (archives/) |
| Delete workspace branch | Yes | Yes |
| Delete repo feature branches | **Yes** | **No** |
| Use case | Abandon work | Finish work |

## [0.7.1] - 2025-12-09

### Fixed
- Release packaging and documentation updates

## [0.7.0] - 2025-12-09

### Added
- **Commit tracking** - Records commit hashes in `state.json` after each push
  - Tracks `branch`, `commit`, `commit_full`, `pushed_at` per repo
  - Enables reproducibility: know exact commits deployed together
  - Audit trail: track what changed between pushes
- **UNPUSHED detection** - Status shows when local differs from last pushed commit
- **Commit display** - `amof status` now shows current commit hash per repo
- **Workspace branch prefix** - Use `workspace/<ticket>` naming convention
- **oauth2-setup playbook** - Complete guide for oauth2-proxy configuration
- **Jenkins trigger tool** - `scripts/tools/jenkins/trigger.sh` for pipeline deployment

### Changed
- `amof push` now records commits to `state.json` after successful push
- `amof status` shows compact output with commit column
- Workspace branches now use `workspace/` prefix instead of `feature/`

## [0.6.0] - 2025-12-09

### Added
- **Manifest Validation** - Schema validation on load, catches errors early
- **`amof check` command** - Verify prerequisites (git, docker, helm, aws, kubectl)
- **`--dry-run` flag** - Preview install/discard actions before execution
- **Shell completion** - Bash and Zsh completion scripts
- **Workspace state** - `.amof/state.json` tracks workspace metadata
- **Retry with backoff** - Network operations retry on transient failures
- **HOWTOs** - Copy-paste ready examples in `.cursor/rules/tools.mdc`
- **Unit tests** - 45 pytest tests for core modules

### Changed
- `install` shows detailed dry-run preview with `--dry-run`
- `discard` shows what would be deleted with `--dry-run`
- `sync` uses retry for clone/fetch/pull operations
- `status` shows workspace info when in a workspace

## [0.5.0] - 2025-12-08

### Changed
- **Modular Architecture** - Refactored monolithic `amof.py` (2200+ lines) into organized package:
  - `scripts/amof/` - Package root with version
  - `scripts/amof/cli.py` - Argument parsing
  - `scripts/amof/manifest.py` - YAML parsing and manifest management
  - `scripts/amof/utils.py` - Shared utilities
  - `scripts/amof/commands/` - Command implementations:
    - `sync.py`, `status.py`, `context.py`, `install.py`
    - `workspace.py`, `discard.py`, `repo.py`
    - `ecosystem.py`, `actor.py`
    - `helm.py`, `images.py`, `audit.py`
- Entry point `scripts/amof.py` is now a thin wrapper (~100 lines)
- Improved maintainability and testability

## [0.4.0] - 2025-12-08

### Added
- **Enhanced Context Generation** - Smart code analysis for AI agents
  - `--type` flag: api, config, structure, impact, chunks (comma-separated)
  - `--format` flag: json or markdown output
  - `--incremental` flag: only process changed files
  - **API Surface Extraction**: REST endpoints, events published/consumed, gRPC services
  - **Configuration Map**: env vars, config files, secrets references, feature flags
  - **Code Structure Analysis**: modules, classes, functions, imports (internal/external)
  - **Change Impact Hints**: high-risk files, frequently changed, test coverage gaps
  - **Semantic Chunks**: classes with docstrings, functions with signatures
  - **Cross-Repo Relationships**: dependency graph between workspace repos
- **Ecosystems** - Persistent branch templates for related work
  - `amof ecosystem create <name>` - Create ecosystem branch
  - `amof ecosystem list` - List available ecosystems
  - `manifest.yaml` for actor/customer tracking
- **Actor management** - Track customers within ecosystems
  - `amof actor add/list/update` commands
- **Helm operations** - Chart management via CLI
  - `amof helm sync` - Sync from source repo
  - `amof helm diff` - Compare versions
  - `amof helm template` - Render for inspection
- **Image operations** - Container image migration
  - `amof images discover` - Find images in chart
  - `amof images diff` - Compare chart vs ECR
  - `amof images migrate` - Pull/tag/push with audit
  - `amof images verify` - Check all images exist
- **Audit system** - Migration changelog for compliance
  - `amof audit list/show/record` commands
  - YAML changelog format in `changelogs/`
- **Centralized tools** - Reusable bash scripts
  - `scripts/tools/helm/` - Helm operations
  - `scripts/tools/images/` - Image migration
  - `scripts/tools/debug/` - K8s and AWS debugging
  - `scripts/tools/audit/` - Changelog recording
  - `scripts/lib/` - Shared bash functions
- **Cursor rules for tools** - `.cursor/rules/tools.mdc` documents all tools for agents
- **DEMO migration ecosystem** - Full documentation structure
  - `ecosystems/demo-dev/` with playbooks, kb, diagrams

## [0.3.3] - 2025-12-08

### Added
- **Discard command** - `amof discard` deletes workspace and all feature branches, returns to main
- `--force` flag to skip confirmation prompt

## [0.3.2] - 2025-12-08

### Added
- **Push command** - `amof push` pushes all branches to origin in one command
- **Shell aliases** - Install automatically adds `amof` command to shell config
- **Help & version** - `amof --help` and `amof --version` now available
- `scripts/amof-aliases.sh` for manual alias setup

### Changed
- **Local-first workflow** - Install now works locally by default, use `--push` to push branches
- Changed `--no-push` flag to `--push` (inverted default behavior)

## [0.3.1] - 2025-12-08

### Added
- **Auto context generation** - Install now generates context for all repos automatically
- **Readonly repo protection** - Cursor rules now explicitly protect readonly repos from modification
- Agent can read/analyze readonly repos but must propose changes as suggestions

## [0.3.0] - 2025-12-08

### Added
- **Readonly repos** - Add `readonly: true` to repo config to clone without creating feature branches
- **Multi-root workspace** - `amof.code-workspace` file for proper git tracking in all repos
- **Auto-open Cursor** - Install command opens Cursor automatically when complete
- **Workspace command** - `python scripts/amof.py workspace` to regenerate workspace file
- **Improved status** - Shows MODE column (RO/RW) and recognizes feature branches as valid

### Changed
- Status command now accepts feature branches as valid for RW repos (not WRONG_BRANCH)
- Status output includes MODE column showing readonly/read-write status

## [0.2.0] - 2025-12-08

### Added
- **Install command** - `python scripts/amof.py install <ticket-id>` bootstraps complete workspace
- **Devcontainer support** - Include/exclude `.devcontainer/` via manifest config
- **Environment config** - `env` template file for Git/K8s credentials
- **Nested Cursor rules** - `.cursor/rules/` with domain-specific `.mdc` files

### Changed
- Consolidated documentation into README.md
- Merged AMOF_SYSTEM_DESIGN.md with ROADMAP.md

## [0.1.0] - 2025-12-08

### Added
- Initial AMOF framework
- **Manifest** - `ecosystem.yaml` for repository definitions
- **CLI commands** - sync, status, context, add-repo
- **Guardrails** - Protected paths and agent constraints
- **Context generation** - index.json and summary.md for AI consumption

