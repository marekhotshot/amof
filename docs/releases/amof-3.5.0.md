# AMOF v3.5.0

Status: release-prep candidate notes  
Canonical version: `v3.5.0`  
Previous release: `v3.4.0`  
Date: 2026-09-17

AMOF `v3.5.0` makes Runtime Authority **executable and independently
verifiable**. Workers still propose; operators still approve; Runtime still
binds, enforces, and records receipts. This increment adds a real Kubernetes
capability lane, a one-command public demo, portable promotion proofs, and
reconciled current-truth docs.

A `v3.5.0` git tag is applied only after promote-main of this candidate, and
only to the synthetic SHA that lands on `main`.

## What changed for users

- `python3 scripts/amof.py demo` (or `amof demo`) runs proposal → approval →
  binding → allowed action → blocked out-of-scope action → verification →
  receipt. No provider key. No cluster. IDs stay inside the command.
- Migration is the recommended first scenario and is a **LOCAL REFERENCE
  SYSTEM**. Insurance, banking, healthcare, and IAM are the same fixture
  primitive. They are not enterprise products.
- Kubernetes demo defaults to a fixture. `--live` is an optional disposable
  local cluster, not a published environment identity.
- `python3 scripts/amof.py proof show AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001`
  binds a promoted SHA to public-safe verification without app-data.
- Public docs start at [`docs/INDEX.md`](../INDEX.md).

## First command

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
python3 scripts/amof.py demo migration --non-interactive
python3 scripts/amof.py proof show AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001
```

Or after install:

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v3.5.0"
amof --version   # AMOF v3.5.0
amof demo
```

## Non-claims

- Not “AI does more.” AI gets explicit bounded authority and must prove what
  happened.
- Not Predator, Workforce, or an operator console in this repository.
- Not Kubernetes RBAC or a cluster platform.
- Not a published Trust Model.

Previous line: [`amof-3.4.0.md`](amof-3.4.0.md).
