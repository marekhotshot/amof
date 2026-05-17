# Public Smoke Matrix

Status: clean-baseline gate
Date: 2026-05-17

This matrix defines the evidence required before AMOF can reasonably become a
clean public baseline release. The default gate is no-key and local-only. Live
provider calls are optional/manual and must never run as part of the default
public smoke.

## 1. Public Pipx Install Smoke

- Command: `pipx install --force "git+https://github.com/marekhotshot/amof.git@v2.3.0" && amof --version && pipx runpip amof show amof`
- Expected result: installs public tag and reports AMOF v2.3.0 plus package metadata
- Requires network: yes
- Requires provider key: no
- Mutates target repo: no
- Pass/fail criteria: pass if install exits 0, `amof --version` is v2.3.0, and package metadata name is `amof`; fail on system `python -m amof` assumptions

## 2. Source Checkout Install Smoke

- Command: `git clone --branch v2.3.0 --single-branch https://github.com/marekhotshot/amof.git /tmp/amof-src && cd /tmp/amof-src && ./scripts/install-amof.sh && ./.venv/bin/amof doctor`
- Expected result: clean source checkout creates local venv and doctor passes or reports only understood warnings
- Requires network: yes
- Requires provider key: no
- Mutates target repo: disposable checkout only
- Pass/fail criteria: pass if installer and doctor succeed without private topology or provider keys

## 3. CLI Help Matrix

- Command: `amof --help; amof help; for each known command: amof "$cmd" --help`
- Expected result: default help shows public first-run commands; known hidden commands remain help-callable
- Requires network: no
- Requires provider key: no
- Mutates target repo: no
- Pass/fail criteria: pass if help exits 0, default help does not present maintainer mutation commands as public quickstart, and hidden commands such as `promote-main --help` still work

## 4. No-Key Adoption Smoke

- Command: in a disposable Git repo, unset provider keys, set `AMOF_HOME`, run `amof init --adopt .`, then inspect `git status --short` and source-pollution directories
- Expected result: adoption stores AMOF metadata in app-data and leaves target repo clean
- Requires network: no
- Requires provider key: no
- Mutates target repo: no, beyond initial test fixture commit
- Pass/fail criteria: pass if no target repo changes and no `.amof`, `ecosystems`, or `context` directories are created

## 5. Provider Setup No-Secret Smoke

- Command: `AMOF_HOME=/tmp/... amof setup provider openrouter --name openrouter-default --activate --yes`
- Expected result: profile stores `OPENROUTER_API_KEY` reference, not raw key material
- Requires network: no
- Requires provider key: no
- Mutates target repo: no, app-data only
- Pass/fail criteria: pass if profile contains env var references only and setup reports that no raw API keys were written

## 6. Bounded Worker No-Key Failure Smoke

- Command: with provider keys unset, `amof agent --plan "Inspect this repo" --no-follow-up`
- Expected result: agent reaches provider validation without ecosystem-resolution failure
- Requires network: no
- Requires provider key: no
- Mutates target repo: no
- Pass/fail criteria: pass if output indicates missing provider/key/configuration and target repo remains clean; fail on `--ecosystem/-e is required`, unsafe guardrail warning, optional vector memory warning pollution, or repo changes

## 7. Bounded Worker Live Smoke

- Command: in a disposable repo with explicit provider key, `amof agent --plan-execute "Make one trivial bounded change. Do not commit." --approve-plan --no-follow-up`
- Expected result: produces a minimal reviewable local diff only
- Requires network: maybe, provider API
- Requires provider key: yes
- Mutates target repo: yes, disposable target repo only
- Pass/fail criteria: pass if diff is minimal/reviewable, tests/checks are reported, and no commit/push/tag occurs; fail on unbounded edits, private access, or auto-commit
- Default gate status: optional/manual only

## 8. Docs Grep Gate

- Command: `rg -n -i "corp|corporate|customer|kubeconfig|amof-platform|v2\\.2|v2\\.1|UP10|UP11" README.md docs`
- Expected result: no unreviewed corporate/private/stale-version references outside ADRs or explicit boundary statements
- Requires network: no
- Requires provider key: no
- Mutates target repo: no
- Pass/fail criteria: pass if all hits are allowlisted; fail on current runbook/README stale or private topology references

## 9. Secret Grep Gate

- Command: `rg -n "BEGIN .*PRIVATE|ssh-rsa|AKIA[0-9A-Z]{16}|ghp_|github_pat_|sk-[A-Za-z0-9_-]{20,}|OPENROUTER_API_KEY=|ANTHROPIC_API_KEY=|OPENAI_API_KEY=" scripts docs README.md`
- Expected result: no raw secrets, private keys, token literals, or unredacted provider keys
- Requires network: no
- Requires provider key: no
- Mutates target repo: no
- Pass/fail criteria: pass if only redacted placeholders and detector strings are present; fail immediately on real secret material

## 10. Package Metadata Check

- Command: `pipx runpip amof show amof` for installed package, or source-checkout inspection of `pyproject.toml`
- Expected result: package metadata exposes only the `amof` console script, correct version, Python requirement, and explainable dependencies
- Requires network: maybe, depending on install path
- Requires provider key: no
- Mutates target repo: no
- Pass/fail criteria: pass if project name/version/script match the public contract and no private package names or extra console scripts appear

## Release Candidate Rule

Prepare a v2.5.0 release candidate only after local/no-key gates pass and any
optional live smoke is explicitly marked manual. Do not push, tag, promote main,
or release as part of the smoke gate itself.
