# Public documentation

Status: current  
Canonical product truth: public `origin/main`, CLI, contracts, tests, and
accepted Runtime Authority missions.

This map is the public docs path. Dated plans and superseded positioning live
under [historical/](historical/INDEX.md).

## START HERE

- [README](../README.md)
- Curated public projection: [docs/public/](public/index.md) → [docs.amof.dev](https://docs.amof.dev)

## UNDERSTAND

- [Runtime Authority](runtime-authority.md)
- [Capability Authority](capability-authority.md)
- [Write-Scope Authority](write-scope-authority.md)
- [Public / private boundary](architecture/public-private-boundary.md)

## TRY IT

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
python3 scripts/amof.py demo
```

- Recommended: `python3 scripts/amof.py demo migration --non-interactive`
- Kubernetes fixture: `python3 scripts/amof.py demo kubernetes --non-interactive`
- Real Git: `python3 scripts/amof.py demo git --non-interactive`
- Install paths: [runbooks/install.md](runbooks/install.md)
- Adopt-and-plan (provider optional): [runbooks/happy-path-agent-workflow.md](runbooks/happy-path-agent-workflow.md)

These scenarios prove execution authority. They are not bank, hospital,
insurer, IAM, or migration products.

## VERIFY

```bash
python3 scripts/amof.py proof list
python3 scripts/amof.py proof show AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001
```

- [Public promotion proofs](proofs/INDEX.md)
- Write-Scope / Kubernetes / reference receipts: contracts in
  [contracts/INDEX.md](../contracts/INDEX.md)
- Trust verify/export is local (`amof trust`). The Trust Model markdown is
  **not** a published security claim; see historical.

## OPERATE

Current public runbooks only:

- [Install](runbooks/install.md)
- [Happy-path agent workflow](runbooks/happy-path-agent-workflow.md)
- [Source-checkout bootstrap](operations/source-checkout-bootstrap.md)
- [Read-only chat planning](operations/read-only-chat-planning.md)
- [Public surface taxonomy](operations/public-surface-taxonomy.md)
- [Public smoke matrix](operations/public-smoke-matrix.md)
- [Ticket delivery protocol](operations/ticket-delivery-protocol.md)

## REFERENCE

- [CLI first-run](../README.md#try-runtime-authority-in-15-minutes) — `amof demo`, `amof proof`, `amof scope`
- [contracts/INDEX.md](../contracts/INDEX.md)
- [Capability contracts](capability-authority.md)
- [Write-scope contracts](write-scope-authority.md)
- [Canonical execution chain](canonical-execution-chain.md)
- [Authority ledger](operations/authority-ledger.md)
- [Runtime logs contract](operations/runtime-logs-contract.md)
- [Claim-validation process](engineering/ADVERSARIAL-CLAIM-VALIDATION.md)
- [ADRs](adr/)

## HISTORICAL

- [historical/INDEX.md](historical/INDEX.md)
- Dated [releases/](releases/) notes (snapshots of what shipped then)

## Non-claims

- Not Predator, Workforce, or an operator console in OSS
- Not a Kubernetes platform or RBAC replacement
- Not a bank / hospital / insurer / IAM / migration product
- Not a published Trust Model until that gate completes
- Not autonomous approval or auto-promotion
