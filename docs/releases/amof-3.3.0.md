# AMOF v3.3.0

Status: release notes for the Write-Scope Authority public release  
Canonical version: `v3.3.0`  
Previous release: `v3.2.0`  
Date: 2026-07-28

AMOF `v3.3.0` completes **Write-Scope Authority** for OSS runtimes: workers
propose bounded repository mutations, operators approve finite grants, Runtime
binds and enforces them, and receipts prove whether execution remained inside
scope.

Public lifecycle: `propose → inspect → approve → bind → enforce → audit`.
See the repository README for the rendered public surface framing.

## Highlights

- Durable WriteScopeProposal store + `amof scope list|show`
- Operator WriteScopeApproval with mandatory TTL and revocation
- Runtime WriteScopeBinding via `--write-scope-approval`
- Path enforcement with deny-wins + MutationReceipt on AgentRunResult
- `amof scope audit` lineage reconstruction
- `amof scope recover` crash recovery (no fabricated success, no automatic
  continuation of mutation authority)

## Explicit non-claims

- Not perfect sandboxing or general OS containment
- Not transactional filesystem rollback
- Not autonomous approval
- Not Workforce / Predator OSS features
- Not long-running checkpoint mutation authority (v4.0)

## Migration / deprecation

- Nested proposal fields on AgentRunResult remain readable; may be persisted
  into durable Proposal records.
- `--approve-writable-root` warns and never becomes a historical Approval.
- Corrupt/unknown write-scope records fail closed.

## Install expectation

```bash
# after tag
pip install "amof @ git+https://github.com/marekhotshot/amof.git@v3.3.0"
amof --version   # AMOF v3.3.0
amof scope --help
```
