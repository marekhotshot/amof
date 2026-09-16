# AMOF Cryptographic Trust & Evidence Audit (canonical for Trust Layer Wave 001)

**Date:** 2026-08-06 (session census) / promoted into tree 2026-08-07  
**Mode:** CURRENT TRUTH ONLY  
**Authority:** public `origin/main` code > tests > runtime > docs

This file records the audit findings that authorize Trust Layer Wave 001 P0 work.
It is not a redesign document.

## P0 gaps (implementation targets)

1. `result_sha256` produced by handoff execution receipts without verify-on-consume.
2. Bootstrap `bootstrap-sha256-manifest.json` generated without verify-on-consume.
3. `audit_receipt_path` in public generated-build candidate envelope is an unused writer / intentional public placeholder.
4. Operator evidence sealing existed as a proposal tool only; not runtime-bound to FINALIZED.

## Explicit non-goals (later waves)

Ed25519, PQC, Sigstore, transparency logs, Merkle redesign, trust anchors, external KMS/HSM.

## Related product surfaces

- `scripts/amof/orchestrator/merkle.py` — index freshness / compact roots (not authenticity)
- `scripts/amof/commands/handoff.py` — payload SHA + execution `result_sha256`
- `scripts/amof/commands/bootstrap.py` — SHA256 manifest producer
- `scripts/amof/write_scope_*.py` — fail-closed body_hash / mutation receipts
- `scripts/amof/generated_build/candidate.py` — `audit_receipt_path` placeholder
