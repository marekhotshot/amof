# Write-Scope Authority

Status: public Runtime Authority surface (v3.4.0)  
Audience: OSS operators using the public AMOF CLI (no Predator required)

## Model

```text
workers propose → operators approve → Runtime binds and enforces → receipts prove compliance
```

| Object | Role | Authority? |
|---|---|---|
| WriteScopeProposal | Worker evidence of requested roots at `base_sha` | No |
| WriteScopeApproval | Operator grant with mandatory TTL; revocable | Yes (grant only) |
| WriteScopeBinding | Runtime binds one Approval to one mutating attempt | Yes (execution reservation) |
| MutationReceipt | Runtime compliance proof for changed paths | Yes (compliance) |
| Revocation | Durable revoke record | Ends grant |

Approval alone does **not** enable mutation. Runtime must create a Binding via
`--write-scope-approval` before mutation authority exists.

## CLI

```bash
# Import worker-authored proposal evidence (no execution backend required)
amof scope import-result <agent-run-result.json> --run-id <run-id>
# Learning fixture (worker-shaped, not evidence):
amof scope import-result --example src-only --run-id <run-id>

# Inspect proposals emitted by a discovery / agent run (or import-result)
amof scope list --from-run <run-id>
amof scope show <proposal-id>

# Operator grant (TTL mandatory)
amof scope approve <proposal-id> --ttl 2h --approved-by operator:you

# Mutating execute under bound Approval (execution backends)
amof handoff execute-agent --handoff-id <id> \
  --write-scope-approval <approval-id> \
  --approve-capabilities bounded_write
# Builtin executor (roots restricted to the Binding)
amof agent --plan-execute "…" --write-scope-approval <approval-id> \
  --approve-capabilities bounded_write --no-follow-up

# Audit lineage
amof scope audit <approval-id>
# also accepts proposal_id, binding_id, or run_id

# Revoke
amof scope revoke <approval-id> --reason "operator abort" --revoked-by operator:you

# Crash / restart recovery (never fabricates success)
amof scope recover <binding-id> --decision restore
# decisions: auto | restore | accept-partial | mark-failed
```

## Failure model (residual authority)

| Failure | Residual mutation authority |
|---|---|
| Expired / revoked / consumed Approval | None |
| Scope breach | None (revoke-by-breach) |
| base_sha mismatch | None for that attempt (no silent retarget) |
| Crash with dirty workspace | None until explicit `amof scope recover` |
| Successful in-scope mutation | Approval consumed (single-use) |
| No mutation | Approval may remain reusable for a new Binding |

## Legacy `--approve-writable-root`

Deprecated compatibility path elevation. Emits a warning. Does **not** create
WriteScopeApproval or WriteScopeBinding evidence. Prefer
`--write-scope-approval`. Naked flags are never migrated into historical
Approvals. Passing `--approve-writable-root` together with
`--write-scope-approval` is refused (mixed authority). A bound builtin run
replaces guardrail writable roots with the Binding roots; the flag cannot
widen that set.

## What this is not

- Not perfect OS sandboxing or transactional rollback (`rollback_atomic: false`)
- Not autonomous approval
- Not Predator / Workforce / ladder selection
- Not long-running checkpoint mutation authority (deferred)

## Worked OSS example

Complete public lifecycle when no execution backend (cursor-agent / hermes /
amof-native) ran. The operator imports a worker result file and never authors
roots.

1. Obtain an `agent-run-result.json` that contains `write_scope_proposal` or
   `write_scope_proposals[]` (worker-authored evidence at a `base_sha`).
2. `amof scope import-result agent-run-result.json --run-id <run-id>` prints
   one or more `wsp-...` ids. Invalid envelopes fail closed.
3. `amof scope list` (or `amof scope list --from-run <run-id>`) shows the
   `wsp-...` with status `proposed`.
4. `amof scope show wsp-...` inspects allowed/denied roots.
5. Operator runs `amof scope approve wsp-... --ttl 30m --approved-by operator:alice`
   → prints `wsa-...`.
6. One-time `amof trust keygen --preferred` if no preferred signing key exists.
7. `amof handoff execute-agent --handoff-id <id> --write-scope-approval wsa-...
   --approve-capabilities bounded_write --confirm` → Runtime creates `wsb-...`,
   enforces paths, emits `wmr-...` MutationReceipt.
8. `amof scope audit wsa-...` reconstructs proposal, approval, binding, receipt,
   and terminal residual authority.
9. If the process crashes mid-mutation, restart leaves the Binding
   `active`/`suspended`; `amof scope recover wsb-... --decision restore` marks
   the Binding failed and requires a new Approval for any future mutation.

Governed mutation runs through `amof handoff execute-agent … --write-scope-approval … --approve-capabilities bounded_write` (execution backends) or `amof agent --plan-execute … --write-scope-approval … --approve-capabilities bounded_write` (builtin executor, roots restricted to the Binding). Without an approval the builtin path is an ungoverned local demo.

Learning walkthrough (fixture, not evidence):

import/list/approve/audit run without any model; the `amof agent --plan-execute` step needs a configured provider (`amof setup provider …` or `ANTHROPIC_API_KEY`). With a missing provider the run stops before planning and no Binding is created.

```bash
amof scope import-result --example src-only --run-id learn-001
amof scope list
amof scope approve <wsp-...> --ttl 2h --approved-by operator:you
amof agent --plan-execute "Write src/ok.py only" --write-scope-approval <wsa-...> --approve-capabilities bounded_write --no-follow-up
amof scope audit <wsa-...>
```

App-data layout (under `AMOF_HOME` / XDG data root):

```text
write-scopes/proposals/{proposal_id}.json
write-scopes/approvals/{approval_id}.json
write-scopes/bindings/{binding_id}.json
write-scopes/revocations/{revocation_id}.json
write-scopes/receipts/{receipt_id}.json
write-scopes/events/write-scope-events.jsonl
```
