# AMOF Happy Path: Adopt a Repo and Run an Agent Plan

This runbook is for a public OSS user who wants to try AMOF on an existing Git
repository without learning AMOF internals first.

It covers:

- installing AMOF v2.2.1
- adopting an existing Git repo into AMOF app-data
- creating a work ticket if the current command supports your repo layout
- running a safe agent plan
- validating results
- handing work back to a human, or continuing with execution if you have
  provider and runner configuration
- closing the ticket or work item if the current command supports your repo
  layout

The supported v2.2.1 happy path is adoption plus safe planning without passing
`-e/--ecosystem`. AMOF-238 adds a bounded public worker default for
`--plan-execute`, but live worker execution should be treated as a smoke-tested
slice, not a blanket autonomous-coding guarantee.

## Prerequisites

You need:

- Git
- Python 3.11 or newer
- `pipx`
- an existing Git repository
- optionally, an LLM provider key for live agent planning or execution

Adoption smoke tests do not require provider keys. Agent planning does require a
provider key once you want the model to actually respond.

OpenRouter example:

```bash
export OPENROUTER_API_KEY="<redacted>"
```

Do not paste real keys into terminal transcripts, bug reports, or public docs.

## Install AMOF

Install the public v2.2.1 release:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v2.2.1"
```

Verify the CLI:

```bash
amof --version
amof check
amof doctor
```

Expected:

```text
AMOF v2.2.1
```

`amof check` should pass required prerequisites. `amof doctor` may report
warnings in a fresh install, especially if no provider profile or API key is
configured yet.

## Adopt An Existing Repo

Start inside the repo you want AMOF to inspect:

```bash
cd /path/to/my-repo
git status --short
amof init --adopt .
amof doctor
```

Expected `amof init --adopt .` output includes:

```text
[init] Adopted repository: /path/to/my-repo
[init] Ecosystem: my-repo
[init] Manifest source: appdata

Next commands:
  amof doctor
  amof agent --plan "Inspect this repo"
```

What happens:

- AMOF detects the current Git root.
- AMOF stores an app-data binding for that repo.
- AMOF creates a minimal app-data manifest for one writable repo.
- The target repo stays clean.
- No `.amof` files are written into the target repo by default.

Check that the repo stayed clean:

```bash
git status --short
```

Expected: no output.

## Configure A Provider Profile

Provider setup writes profile metadata and environment variable references to
AMOF app-data. It does not store raw API keys, and it does not call the provider
while creating the profile. Live agent calls still require the referenced
environment variables to be set.

List available templates:

```bash
amof setup provider --list
```

OpenRouter:

```bash
amof setup provider openrouter --name openrouter-default --activate
export OPENROUTER_API_KEY="<redacted>"
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

For scripted runs, add `--yes` to skip the confirmation prompt. For
adoption-only smoke tests, leave keys unset. The correct failure is provider
validation, not ecosystem resolution. Do not put an OpenRouter key into
`ANTHROPIC_API_KEY`; provider setup stores environment variable references and
metadata only, and live calls still require the matching environment variable to
be exported. The xAI template is available for planning/bootstrap records, but
current live execution may require provider resolver support before xAI can be
used directly.

Vector memory is optional. For pipx installs that need it:

```bash
pipx inject amof chromadb pysqlite3-binary
```

## Start A Ticket Or Work Item

AMOF has ticket commands:

```bash
amof ticket --help
amof ticket start --help
```

The visible syntax is:

```bash
amof ticket start <ticket-id>
```

Current limitation: in v2.2.0, `amof ticket start` is still workspace-state
oriented. In an app-data adopted repo, it can report:

```text
[ticket] Not in a workspace.
[ticket] Run 'amof -e <ecosystem> install' first.
```

For the adopted-repo happy path, use a normal Git branch as the public-safe work
item:

```bash
git switch -c feat/AMOF-DEMO-add-name-argument
```

Recommended naming:

```text
feat/AMOF-DEMO-add-name-argument
```

This keeps the workflow compatible with your normal Git hosting and pull request
process while AMOF adopted-repo ticket lifecycle support matures.

## Run A Safe Agent Plan

First prove no provider keys are needed for adoption resolution:

```bash
unset ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY
amof agent --plan "Inspect this repo and propose a minimal improvement" --no-follow-up
```

Expected without provider configuration, after activating OpenRouter:

```text
[agent] OPENROUTER_API_KEY not set.
```

The exact provider message depends on the provider you activate or select. The
important v2.2.1 behavior is that the command should not fail with:

```text
--ecosystem/-e is required
```

Provider-enabled OpenRouter example:

```bash
export OPENROUTER_API_KEY="<redacted>"
amof agent --plan "Add a --name argument, preserve default behavior, update tests and README" --no-follow-up
```

Use an explicit provider when you want to override the activated profile:

```bash
amof agent --provider openrouter --plan "Inspect this repo" --no-follow-up
```

What happens:

- AMOF resolves the adopted repo from app-data.
- `--plan` runs the agent in read-only planning mode.
- `--no-follow-up` skips the interactive post-run menu for copy/paste runs.
- With a valid provider key and dependencies, the agent can produce a plan.

## AMOF-238 Public Agent Tiers

These tiers describe the current public surface after AMOF v2.2.1 validation.

Read-only public planning is the recommended demo path:

```bash
amof init --adopt .
amof setup provider openrouter --name openrouter-default --activate
export OPENROUTER_API_KEY="<redacted>"
amof agent --provider openrouter --plan "Inspect this repo and propose one safe improvement" --no-follow-up
```

Expected behavior:

- The adopted repo stays clean.
- Journals and event logs go to AMOF app-data.
- No `.amof`, `ecosystems`, or `context` directory is written into the target
  repo by default.

Interactive chat is demoable for help and read-only prompts, but it still needs
a live provider key before the shell opens:

```bash
amof agent --provider openrouter
/help
/quit
```

For a read-only live prompt:

```bash
amof agent --provider openrouter
Inspect this repo at a high level. Do not modify files.
/quit
```

Worker execution is bounded but should be introduced honestly. `--plan-execute`
now has a packaged public `code` runner default when no `runners.yaml` exists.
That default runner can use `Read`, `Write`, `StrReplace`, `Glob`, `LS`, and
`ReadLints`; it does not include `Shell`, `Delete`, or `GitCheckpoint`.
For adopted repos, write-class tools are confined to the adopted repo root.
With `--provider openrouter`, AMOF uses OpenRouter-compatible defaults
(`anthropic/claude-sonnet-4.5` for planning and `openai/gpt-4o-mini` for the
worker/default model) unless you pass explicit model flags.
Plan execution verifies tool and repository outcomes after the worker runs. A
mutation-intent plan is not considered successful if write-class tools fail or
if no target repository diff appears after claimed edits.
`--no-follow-up` only skips the post-run menu; it does not approve execution.
For unattended disposable-repo smoke tests, pass `--approve-plan` after you are
comfortable with the generated plan behavior.

Use a disposable repo for worker demos. Run this from an interactive terminal
and approve the generated plan only after reviewing it:

```bash
amof agent --provider openrouter --plan-execute \
  "Add a small pure function farewell(name) and a matching unit test. Do not commit." \
  --no-follow-up
```

Non-interactive approval for CI or a disposable smoke:

```bash
amof agent --provider openrouter --plan-execute \
  "Add a small pure function farewell(name) and a matching unit test. Do not commit." \
  --approve-plan \
  --no-follow-up
```

Do not claim live worker success until a provider-key smoke has passed in that
disposable repo and `git status --short` shows only the intentional app/test
diffs. A truthful failed execution is still useful evidence, but it is not a
worker execution demo.

## Execute Or Hand Back

The public `amof agent --help` includes:

```bash
amof agent --plan-execute
amof agent --model-ladder
amof agent --planner-model <model>
```

Current limitation: full autonomous execution is experimental for public
adopted repos. AMOF-238 provides a narrow built-in `code` runner default for
`--plan-execute`; broader worker delegation still depends on provider
configuration and optional runner configuration such as `runners.yaml`.

Recommended default path: use `--plan` for public planning and hand back to a
human worker unless you are intentionally validating `--plan-execute` in a
disposable repo.

```bash
git diff --stat
git status --short
```

Then:

- review the agent plan
- apply changes manually or with your preferred editor
- run the repo's tests
- commit changes on your feature branch

Example:

```bash
git add .
git commit -m "feat: add name argument"
```

Use autonomous execution only when you have intentionally configured provider,
guardrail, and runner settings for your repo.

## Validate Changes

Use commands that fit the target repo. Generic checks:

```bash
git diff --stat
git status --short
python3 -m unittest discover -s tests || true
pytest || true
amof doctor
amof bootstrap bundle --json
```

Notes:

- `python3 -m unittest discover -s tests` only helps if the target repo has
  unittest tests under `tests`.
- `pytest` only helps if the target repo uses pytest.
- Keep `git status --short` visible before handing off or opening a PR.
- `amof bootstrap bundle --json` records bootstrap evidence; it may warn if
  provider configuration is still absent.

## Promote Or Publish Changes

AMOF v2.2.0 exposes:

```bash
amof push --help
amof promote-main --help
```

Current public-safe guidance:

- `amof promote-main` is for AMOF-controlled promotion flows with explicit
  candidate branches, source SHAs, expected `origin/main`, and audit evidence.
  Do not treat it as the default publishing command for arbitrary adopted repos.
- `amof push` is workspace-oriented and may commit/push the current workspace
  branch and configured manifest repos. Do not use it as the default adopted
  repo publish path unless you have verified it matches your repo workflow.

For arbitrary public user repos, use your normal Git workflow:

```bash
git status --short
git push -u origin feat/AMOF-DEMO-add-name-argument
```

Then open a pull request using your repository's normal process.

## Close Ticket

AMOF exposes:

```bash
amof ticket end --help
amof archive --help
```

Current limitation: `amof ticket end` and `amof archive` are workspace-state
oriented. For an adopted repo using the manual branch fallback, close manually:

```bash
git status --short
```

Then:

- ensure the repo is clean or intentionally has only the changes you expect
- record a short summary in your issue, ticket, or PR
- merge through your repo's normal review process
- delete the feature branch after merge if that is your team's convention

## Full Copy/Paste Demo Script

This demo does not call an external LLM provider. It proves install availability
is assumed, adoption works, the repo stays clean, and agent planning reaches
provider validation instead of ecosystem resolution failure.

```bash
set -euo pipefail

DEMO_ROOT="$(mktemp -d /tmp/amof-happy-path-demo.XXXXXX)"
TARGET="$DEMO_ROOT/target"
AMOF_HOME_DIR="$DEMO_ROOT/amof-home"

mkdir -p "$TARGET"
cd "$TARGET"

git init -b main
git config user.email "demo@example.local"
git config user.name "AMOF Demo"
printf '# AMOF Adopt Demo\n' > README.md
git add README.md
git commit -m "init"

export AMOF_HOME="$AMOF_HOME_DIR"
unset ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY

amof --version
amof init --adopt .
amof doctor --json >/tmp/amof-happy-path-doctor.json
amof setup provider openrouter --name openrouter-default --activate --yes

amof agent --plan "Inspect this repo" --no-follow-up > "$DEMO_ROOT/agent.txt" 2>&1 || true
cat "$DEMO_ROOT/agent.txt"

if grep -q -- "--ecosystem/-e is required" "$DEMO_ROOT/agent.txt"; then
  echo "FAIL: ecosystem resolution still requires -e"
  exit 1
fi

if ! grep -Eq "API_KEY not set|provider|key|configuration" "$DEMO_ROOT/agent.txt"; then
  echo "FAIL: expected provider validation message was not observed"
  exit 1
fi

if grep -q "NO protections" "$DEMO_ROOT/agent.txt"; then
  echo "FAIL: guardrails warning is unsafe for public demos"
  exit 1
fi

if grep -q "Vector memory unavailable" "$DEMO_ROOT/agent.txt"; then
  echo "FAIL: optional vector memory warning should not pollute default plan output"
  exit 1
fi

if [ -n "$(git status --short)" ]; then
  echo "FAIL: target repo changed during adoption"
  git status --short
  exit 1
fi

echo "AMOF_HAPPY_PATH_ADOPTION_SMOKE_PASS"
echo "Demo root: $DEMO_ROOT"
```

Expected final line:

```text
AMOF_HAPPY_PATH_ADOPTION_SMOKE_PASS
```

## Current Limitations

- Full autonomous `--plan-execute` still requires a live provider key and a
  disposable-repo smoke before demo claims. The built-in public runner is
  intentionally narrow and shell-free.
- `amof ticket start`, `amof ticket end`, `amof archive`, and `amof push` are
  still workspace-oriented; adopted-repo lifecycle support is not fully polished.
- `amof promote-main` is for AMOF-controlled promotion workflows unless you have
  explicitly modeled and validated your repo in that flow.
- Public v2.2.1 adoption removes `-e` friction for planning. AMOF-238 adds a
  bounded worker default, but not full one-click autonomous execution.

## Next Roadmap

Planned improvements:

- default prompts and runners for public adopted repos
- adopted-repo ticket lifecycle polish
- local/Ollama worker profile
- clearer planner/worker model ladder UX
