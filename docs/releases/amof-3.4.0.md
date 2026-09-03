# AMOF v3.4.0

Status: unpublished release-prep candidate notes  
Canonical version: `v3.4.0`  
Previous release: `v3.3.0`  
Date: 2026-09-02

AMOF `v3.4.0` packages **Native runtime, Trust Layer, acceptance honesty, and
a usable public Write-Scope lifecycle**. Workers still propose bounded
repository mutations; operators still approve finite grants; Runtime still
binds and enforces them. This release adds first-party Native and Cursor
backends, local trust verify/export, honest acceptance states, and
`amof scope import-result` so the public loop works without a private
execution backend.

Public lifecycle: `import-result → inspect → approve → bind → enforce → audit`.
See the repository README for the rendered public surface framing.

A `v3.4.0` git tag is applied only after promote-main of this candidate, and
only to the synthetic SHA that lands on `main`. Until that tag exists,
`amof --version` from this tree reports `AMOF v3.4.0` while the last
published GitHub tag remains the previous public tag.

## What changed for users

- You can dispatch Native and Cursor backends under the same write-scope and
  result envelope as Hermes.
- You can generate a local trust key, verify a finalized run bundle, and
  export a portable package for offline verify (including a local
  transparency log).
- You can import a worker `agent-run-result` file to create write-scope
  proposals without running an execution backend.
- A completed run whose required checks were not run is `UNVERIFIED` and
  cannot be closed as passed.
- Empty tool/grant paths are not repository-root write authority.
- Native waits 180s by default for a still-running Remote-IAL call (was 90s).
- `amof preview check-url --browser-backend local-playwright` can attach a
  bounded screenshot; unset still means HTTP.
- Generated `amof context` files land under `$AMOF_HOME/share/context/`, not
  in the adopted repo.
- Governed mutation runs through `amof handoff execute-agent --write-scope-approval --approve-capabilities bounded_write` or `amof agent --plan-execute --write-scope-approval --approve-capabilities bounded_write`. Without an approval the builtin path prints an ungoverned-local-mode banner.
- When a Binding exists, builtin writable roots are replaced by the Binding
  allowed roots. An out-of-scope write is `scope_exceeded`.
- `amof scope import-result --example src-only` imports a learning fixture.
  `amof help capabilities` lists the parser allow-list.
- A fresh Anthropic install plans without requiring a separate `httpx`
  package. When a CA bundle is present and neither `httpx` nor `httpx2` is
  installed, the default client is used.
- Planning with a current Anthropic SDK (1.3+) reaches the API instead of
  failing on a removed `temperature` argument; unsupported sampling knobs
  are omitted with one warning.
- `amof agent --plan-execute` checks write-scope flags before it constructs
  a planner or talks to a provider. Wrong capability names, `write` instead
  of `bounded_write`, mixed `--approve-writable-root`, or a
  missing/expired/revoked approval exit immediately. No Binding is created.
- The learning walkthrough is a fixture: import/list/approve/audit run
  without a model; `--plan-execute` needs a configured provider. A missing
  provider stops before planning and creates no Binding.

## Highlights

- Native + Cursor execution backends beside Hermes and Claude
- `amof trust keygen|verify|export|verify-export|tlog-init` and
  `amof bootstrap verify`
- `amof.validation_closure/v1` honesty mapping (`UNVERIFIED` ≠ `PASS`)
- `amof.native_loop_budget/v1` progress-aware Native loop
- Context-assembly receipts (hashed; no prompt bodies)
- `amof scope import-result` public Write-Scope entry path
- Additive `AgentRunResult.warnings[]` and `usage`

## Lifecycle example (import-result)

Complete public path when no execution backend ran. The operator imports a
worker result file and never authors roots.

```bash
amof scope import-result agent-run-result.json --run-id <run-id>
amof scope list --from-run <run-id>
amof scope show <proposal-id>
amof scope approve <proposal-id> --ttl 2h --approved-by <operator>
amof trust keygen --preferred   # one-time, if no preferred key exists
amof handoff execute-agent --handoff-id <id> \
  --write-scope-approval <approval-id> \
  --approve-capabilities bounded_write
amof scope audit <approval-id>
amof scope revoke <approval-id> --reason "..." --revoked-by <operator>
# if a mutating run crashed:
amof scope recover <binding-id> --decision restore|accept-partial|mark-failed
```

Governed mutation runs through `amof handoff execute-agent … --write-scope-approval … --approve-capabilities bounded_write` (execution backends) or `amof agent --plan-execute … --write-scope-approval … --approve-capabilities bounded_write` (builtin executor, roots restricted to the Binding). Without an approval the builtin path is an ungoverned local demo.

## Upgrade notes

- Re-check any automation that treated `completed` + validation `not_run` as
  a pass. That combination is now `UNVERIFIED`.
- Re-check Native / Hermes tool grants that used an empty path. Empty is no
  longer repo-root write.
- Re-check Native Remote-IAL clients that assumed a ~90s abort. Default wait
  is 180s (`AMOF_NATIVE_IAL_TIMEOUT_SECONDS`).
- `--browser-backend` is additive; existing HTTP preview checks are unchanged
  when the flag is unset.
- `--approve-writable-root` remains a deprecated compatibility shim and still
  does not mint historical Approvals or Bindings.
- Nested proposal fields on AgentRunResult remain readable; `amof scope
  import-result` persists them into durable Proposal records.
- Corrupt or unknown write-scope records fail closed, including
  `amof scope list`.
- After this candidate is promoted and tagged, install with
  `pipx install "git+https://github.com/marekhotshot/amof.git@v3.4.0"`.
  Until the tag exists, that pin will not resolve.

## Disclosures (fail-closed tightenings; not MAJOR)

- Empty-path grant is no longer treated as repo-root write.
- Bound builtin writes are restricted to Binding roots (replace, not append).
- `completed` + validation `not_run` is `UNVERIFIED`, never `PASS`.
- Native IAL wait default changed from 90s to 180s (env-overridable).
- `--browser-backend` was added; unset still means HTTP.

## Explicit non-claims

- No Predator, Workforce, or operator console in OSS
- Not multi-tenant
- Not a production-readiness claim
- No Kubernetes in OSS
- Builtin `amof agent --plan-execute` without `--write-scope-approval` is not governed by Write-Scope Authority
- Not a PyPI publication
- Not a public AMOF container image
- Not Sigstore / TSA / PQC trust
- Not transactional filesystem rollback
- Trust Model v1 is not canonically published by cutting this version
- Not perfect sandboxing or general OS containment
- Not autonomous approval
- Not long-running checkpoint mutation authority

## Validation commands

```bash
# after tag
pipx install "git+https://github.com/marekhotshot/amof.git@v3.4.0"
amof --version   # AMOF v3.4.0
amof help        # pipx example shows @v3.4.0
amof scope --help
amof scope import-result --help
amof trust --help
amof bootstrap verify --help
```

From this candidate tree (before tag):

```bash
PYTHONPATH=scripts python3 -m amof --version   # AMOF v3.4.0
PYTHONPATH=scripts python3 -m unittest discover -s tests
```
