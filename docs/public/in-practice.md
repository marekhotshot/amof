# In practice

A stranger with the public repo can run one Runtime Authority scenario to a
verified receipt in about 15 minutes. No provider key. No cluster.

These scenarios prove execution authority. They are **LOCAL REFERENCE**
fixtures (except Git, which uses a disposable local repository). They are
not enterprise products.

## Migration (recommended)

Mode: LOCAL REFERENCE SYSTEM.

```bash
python3 scripts/amof.py demo migration --non-interactive
```

Proposal → approval → binding → allowed migrate → blocked out-of-scope
actions → verification → receipt.

AMOF does not perform or automate real workload migration.

## Kubernetes

Mode: LOCAL REFERENCE SYSTEM by default. `--live` is an optional disposable
local cluster, not a published environment identity.

```bash
python3 scripts/amof.py demo kubernetes --non-interactive
```

## Git

Mode: REAL local repository + MutationReceipt. The out-of-scope path is
refused before write.

```bash
python3 scripts/amof.py demo git --non-interactive
```

## Other reference fixtures

Insurance, banking, healthcare, and IAM use the same `reference.action`
primitive. They are not product integrations.

```bash
python3 scripts/amof.py demo
```

## Then verify

```bash
python3 scripts/amof.py proof list
python3 scripts/amof.py proof show AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001
```

Canonical detail: [Runtime Authority](runtime-authority.md) ·
[Evidence](evidence.md) ·
[docs/INDEX.md](https://github.com/marekhotshot/amof/blob/v3.5.0/docs/INDEX.md)
