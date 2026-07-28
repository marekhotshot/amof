# Write-Scope Authority

Status: public Runtime Authority surface (v3.3 candidate)  
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
# Inspect proposals emitted by a discovery / agent run
amof scope list --from-run <run-id>
amof scope show <proposal-id>

# Operator grant (TTL mandatory)
amof scope approve <proposal-id> --ttl 2h --approved-by operator:you

# Mutating execute under bound Approval
amof handoff execute-agent --handoff-id <id> \
  --write-scope-approval <approval-id> \
  --approve-capabilities bounded_write

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
Approvals.

## What this is not

- Not perfect OS sandboxing or transactional rollback (`rollback_atomic: false`)
- Not autonomous approval
- Not Predator / Workforce / ladder selection
- Not long-running checkpoint mutation authority (deferred)

## Worked OSS example

1. Discovery run emits a structured proposal for `docs/launch-readiness/report.md`
   at the current `HEAD` (`base_sha`).
2. `amof scope list --from-run <run-id>` shows `wsp-...` with status `proposed`.
3. Operator runs `amof scope approve wsp-... --ttl 30m --approved-by operator:alice`
   → prints `wsa-...`.
4. Mutating handoff/plan-execute passes `--write-scope-approval wsa-...` with
   `bounded_write` → Runtime creates `wsb-...`, enforces paths, emits
   `wmr-...` MutationReceipt (`within_scope` or fail-closed compliance).
5. `amof scope audit wsa-...` reconstructs proposal, approval, binding, receipt,
   and terminal residual authority.
6. If the process crashes mid-mutation, restart leaves the Binding
   `active`/`suspended`; `amof scope recover wsb-... --decision restore` marks
   the Binding failed and requires a new Approval for any future mutation.

App-data layout (under `AMOF_HOME` / XDG data root):

```text
write-scopes/proposals/{proposal_id}.json
write-scopes/approvals/{approval_id}.json
write-scopes/bindings/{binding_id}.json
write-scopes/revocations/{revocation_id}.json
write-scopes/receipts/{receipt_id}.json
write-scopes/events/write-scope-events.jsonl
```
