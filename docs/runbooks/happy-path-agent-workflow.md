# AMOF Happy Path: Adopt a Repo and Run an Agent Plan

Status: public v3.1.1 runbook

This runbook is for a public user who wants to try AMOF on an existing Git
repository without learning the workspace and maintainer machinery first.

It proves four things:

- AMOF installs through the public `amof` shim.
- Adoption stores AMOF state in app-data, not in the target repo.
- Provider setup stores environment variable references only.
- A no-key agent run reaches provider validation instead of requiring
  `--ecosystem/-e`.

## Prerequisites

You need Git, Python 3.11 or newer, `pipx`, and an existing Git repository.
Provider keys are optional and are needed only for live agent calls.

Adoption and no-key validation do not require provider keys. Do not paste real
keys into terminal transcripts, bug reports, or public docs.

## Install AMOF

Install the public v3.1.1 release:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v3.1.1"
```

Verify the CLI through the pipx-installed shim:

```bash
amof --version
amof --help
pipx runpip amof show amof
```

Expected version:

```text
AMOF v3.1.1
```

System `python -m amof` is not the public pipx contract. For source checkouts,
use the checkout's virtualenv command, for example `./.venv/bin/python -m amof
--help` from the AMOF repo root.

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
- AMOF stores a binding for that repo in AMOF app-data.
- AMOF creates a minimal app-data manifest for one writable repo.
- The target repo stays clean.
- No `.amof`, `ecosystems`, or `context` directory is written into the target
  repo by default.

Check that the repo stayed clean:

```bash
git status --short
```

Expected: no output.

AMOF v3.1.1 also resolves the adopted ecosystem through `amof context`; dotted
repo names such as `hotshot.sk` are covered by the public dogfood fix.

## Configure A Provider Profile

Provider setup writes profile metadata and environment variable references to
AMOF app-data. It does not store raw API keys, and it does not call the provider
while creating the profile.

List available templates:

```bash
amof setup provider --list
```

OpenRouter example:

```bash
amof setup provider openrouter --name openrouter-default --activate
export OPENROUTER_API_KEY="<redacted>"
```

For scripted no-key smoke runs, leave keys unset and add `--yes`:

```bash
amof setup provider openrouter --name openrouter-default --activate --yes
```

Runpod profile setup is available as an advanced/manual provider template. The
template records environment variable references; live pod operation or provider
calls are outside the no-key public smoke path.

## Run A No-Key Agent Validation

First prove adoption resolution works without provider keys:

```bash
unset ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY
amof agent --plan "Inspect this repo" --no-follow-up
```

Expected after activating the OpenRouter profile without exporting a key:

```text
[agent] OPENROUTER_API_KEY not set.
```

The exact provider message depends on the provider you activate or select. The
important v3.1.1 behavior is that the command reaches provider validation and
does not fail with:

```text
--ecosystem/-e is required
```

With a real provider key in your own shell, a read-only plan can be run with:

```bash
amof agent --provider openrouter --plan "Inspect this repo" --no-follow-up
```

## Bounded Worker Execution

`amof agent --plan-execute` is demoable only in a disposable or intentionally
prepared repo. It must produce reviewable diffs only. AMOF must not auto-commit,
auto-push, tag, or promote worker changes.

Use this path only when you are prepared to review the resulting Git diff:

```bash
amof agent --provider openrouter --plan-execute \
  "Add a small pure function and matching test. Do not commit." \
  --no-follow-up
```

Before committing any output:

```bash
git status --short
git diff --stat
```

Then inspect the diff, run the repo's tests, and commit manually if the change is
correct.

## Copy/Paste No-Key Smoke

This demo does not call an external provider. It proves adoption works, the repo
stays clean, provider setup stores references only, and agent planning reaches
provider validation instead of ecosystem resolution failure.

```bash
set -euo pipefail

DEMO_ROOT="$(mktemp -d /tmp/amof-happy-path-demo.XXXXXX)"
TARGET="$DEMO_ROOT/target"
export AMOF_HOME="$DEMO_ROOT/amof-home"

mkdir -p "$TARGET"
cd "$TARGET"

git init -b main
git config user.email "demo@example.local"
git config user.name "AMOF Demo"
printf '# AMOF Adopt Demo\n' > README.md
git add README.md
git commit -m "init"

unset ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY

amof init --adopt .
amof setup provider openrouter --name openrouter-default --activate --yes
amof agent --plan "Inspect this repo" --no-follow-up > "$DEMO_ROOT/agent.txt" 2>&1 || true
cat "$DEMO_ROOT/agent.txt"

if grep -q -- "--ecosystem/-e is required" "$DEMO_ROOT/agent.txt"; then
  echo "FAIL: ecosystem resolution still requires -e"
  exit 1
fi

if grep -q "NO protections" "$DEMO_ROOT/agent.txt"; then
  echo "FAIL: unsafe guardrails warning appeared"
  exit 1
fi

if grep -q "Vector memory unavailable" "$DEMO_ROOT/agent.txt"; then
  echo "FAIL: optional vector memory warning polluted default output"
  exit 1
fi

if ! grep -Eq "OPENROUTER_API_KEY not set|API_KEY not set|provider|key|configuration" "$DEMO_ROOT/agent.txt"; then
  echo "FAIL: expected provider validation message was not observed"
  exit 1
fi

if [ -n "$(git status --short)" ]; then
  echo "FAIL: target repo changed"
  git status --short
  exit 1
fi

find . -maxdepth 3 -type d \( -name .amof -o -name ecosystems -o -name context \) -print > "$DEMO_ROOT/source-noise.txt"
if [ -s "$DEMO_ROOT/source-noise.txt" ]; then
  echo "FAIL: source pollution detected"
  cat "$DEMO_ROOT/source-noise.txt"
  exit 1
fi

echo "AMOF_HAPPY_PATH_ADOPTION_SMOKE_PASS"
echo "Demo root: $DEMO_ROOT"
```

Expected final line:

```text
AMOF_HAPPY_PATH_ADOPTION_SMOKE_PASS
```

## Current Boundaries

- Public first-run UX is pipx install, check/doctor, adoption, provider profile
  setup, read-only planning, and bootstrap evidence.
- Workspace commands such as `sync`, `install`, `ticket`, `archive`, `discard`,
  and `push` are advanced/workspace-oriented, not the public adopted-repo happy
  path.
- Maintainer commands such as `release`, `promote-main`, and
  `promote-main-revert` are not public publishing commands.
- Live provider calls and bounded worker demos require explicit provider keys in
  your shell and must be run in a repo where you are ready to review the diff.
