# Runtime Authority

Status: current public product truth  
Audience: OSS operators using the public AMOF CLI

Models are workers. Runtime is authority.

AMOF owns proposal, approval, binding, enforcement, execution, verification,
and receipts. Chat output is not runtime truth. This page points at the
surfaces that already exist. It does not introduce a second architecture.

## Lifecycle

```text
proposal → approval → binding → execution → verification → receipt
```

A worker proposal is never authority. Execution without a valid Approval and
Binding fails closed.

## Capability kinds on public main

```text
Authority
 ├── git.write            Write-Scope Authority
 ├── kubernetes.read      Kubernetes sibling
 │   kubernetes.mutate
 └── reference.action     local reference-system sibling (`amof demo`)
```

| Kind | Doc | First command |
|---|---|---|
| `git.write` | [write-scope-authority.md](write-scope-authority.md) | `amof demo git --non-interactive` |
| `kubernetes.*` | [capability-authority.md](capability-authority.md) | `amof demo kubernetes --non-interactive` |
| `reference.action` | [capability-authority.md](capability-authority.md) | `amof demo migration --non-interactive` |

`reference.action` is one object/action/state primitive. Vertical demos
(migration, IAM, insurance, banking, healthcare) are fixtures over that
primitive.

## Try it

```bash
python3 scripts/amof.py demo
python3 scripts/amof.py demo migration --non-interactive
```

No provider key. No cluster. IDs are carried internally.

## Verify

```bash
python3 scripts/amof.py proof list
python3 scripts/amof.py proof show AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001
```

Public proofs project governed `promote-main` identity. They do not contain
app-data. `GitOps-SHA` is optional env-commit identity and is `none` for
current code-only promotions. See [proofs/INDEX.md](proofs/INDEX.md).

## Boundary

Public AMOF is the installable governed runtime. Predator is a private
operator console and is not in this repository. See
[architecture/public-private-boundary.md](architecture/public-private-boundary.md).

## Non-claims

- Not perfect OS or container isolation
- Not Kubernetes RBAC, an admission controller, or a cluster platform
- Not autonomous approval
- Not Predator / Workforce / runner-architecture replacement
- Not a published Trust Model

Older “governed cognition runtime” and Remote IAL positioning is archived
under [historical/](historical/INDEX.md).
