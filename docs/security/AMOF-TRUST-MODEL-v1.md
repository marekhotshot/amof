# AMOF Trust Model v1.0

**Status:** Engineering specification (claim-repaired; pending second independent red-team)  
**Process:** Bound by `docs/engineering/ADVERSARIAL-CLAIM-VALIDATION.md`  
**Implementation baseline:** Git commit that contains this file after promote (see Appendix G)  
**Scope:** Trust Layer Waves 001–004 behavior as implemented in the public AMOF repository  
**Classification:** Engineering / security architecture (not marketing)

| Field | Value |
|---|---|
| Document id | `AMOF-TRUST-MODEL-v1` |
| Primary code | `scripts/amof/trust_layer.py`, `scripts/amof/trust_crypto/` |
| Operator CLI | `amof trust {verify\|export\|verify-export\|keygen\|tlog-init}` |
| Repair history | Revised after five-model red-team (REVISE); see operator evidence `AMOF-TRUST-MODEL-v1-REDTEAM` |

This document describes only behavior present in the cited implementation. Claims that require Wave 005 (ML-DSA) appear only under **Future Evolution** and are labeled unpromoted.

---

## Executive Summary

AMOF Trust Layer v1.0 is a **runtime execution evidence integrity and local-signature system** for finalized runs. It binds a completed execution to a closed, content-addressed evidence bundle; signs digest bindings with a local Ed25519 key under an explicit producer trust policy; and can export a package that supports **package self-consistency verification** without the producer’s private keys, database, or git checkout.

### What the implementation can prove (when configured as specified)

1. **Closed-set content integrity** of `receipt.json`, `result.json`, `evidence.json`, `hashes.json`, and `manifest.json` via SHA-256, fail-closed on digest/extra/missing-file errors (`LOCAL_INTEGRITY`). *Proved by:* `verify_evidence_consistency` / `verify_evidence_bundle`.
2. **Structured provenance document** (`amof.runtime_provenance/v1`) included in that integrity chain. Fields are **producer-asserted**. *Proved by:* `build_provenance_document` + hash checks.
3. **In-runtime signature authenticity + policy authorization** when using `amof trust verify` / `verify_bundle_signature`: Ed25519 over `{manifest_digest, evidence_digest, version}` and `TrustPolicy.assert_key_usable`. *Proved by:* `bundle_sign.py` + `policy.py`.
4. **Explicit producer key authority:** keys via `amof trust keygen`; tlog keys via `amof trust tlog-init`; finalize does not auto-create keys. *Proved by:* handoff finalize + `missing_signing_authority` / `missing_tlog_authority`.
5. **Export-time trust snapshot** (`trust_snapshot.json`, wire `kind` may still say `TRUST_AT_FINALIZATION`) recording producer policy decision at **export packaging**. *Proved by:* `build_trust_snapshot` called from `export_trust_package`.
6. **Package self-consistency verify** (`amof trust verify-export`): digests, Ed25519 vs **embedded** public key, intra-package public-key/pin equality, export-time snapshot consistency, and (by default) package-embedded Merkle receipt. Producer seal directories are **not** required. *Proved by:* `verify_export_package(..., require_producer_seal=False)`.
7. **Optional verifier authorization root:** `--expect-key-id` fails closed if the exported key id differs. Without it, OVERALL does **not** prove authorization. *Proved by:* `unexpected_key_id`.

### What this system does NOT prove

1. That an offline OVERALL PASS means the signer is an **authorized** operator key — unless `--expect-key-id` (or equivalent out-of-band check) is used.
2. Global transparency, public Rekor/Sigstore equivalence, witnessed logs, truncation detection, or fork detection.
3. Timestamp authority (no RFC 3161 TSA).
4. Future non-revocation (`TRUST_NOW: REVOKED` does **not** fail offline OVERALL).
5. Post-quantum signatures on this baseline (Ed25519 only).
6. HSM/KMS, encrypted-at-rest private keys, dual-control policy, or certificate PKI.
7. Semantic correctness of missions, models, or write-scope — only recorded IDs and hashes.
8. Legal non-repudiation beyond local Ed25519 authenticity under keys the verifier accepts.

---

## Problem Statement

### Why runtime evidence differs from build integrity

Build signing answers whether an artifact came from a known pipeline. AMOF executions produce receipts, results, seals, and write-scope bindings. The engineering question is narrower:

> Can a verifier check that a declared evidence set is complete and untampered relative to its digests, that an Ed25519 key signed those digests, and—optionally—that the key id matches a verifier-supplied expectation, without needing the producer private key store?

### Threats addressed (with honest residual)

| Threat | Mechanism | Residual |
|---|---|---|
| Silent mutation of bundled content after hash | SHA-256 closed set | Compromised producer before hash |
| Incomplete/extra files in bundle/export | Closed-set checks | — |
| Missing signature when policy requires | `require_signatures` | Permissive unsigned policy |
| Unauthorized key **in-runtime** | allow/revoke/unknown policy | Host admin who edits policy |
| Auto-created signing/tlog keys | Explicit keygen/tlog-init | Mis-operated hosts |
| Export byte tamper / careless single-file key swap | Digests, signature, intra-package pin | **Coherent multi-file forgery by any package producer** unless `--expect-key-id` |
| Historical vs current policy confusion | Export snapshot vs TRUST_NOW report | TRUST_NOW non-gating on OVERALL |
| Missing producer seal after export | `require_producer_seal=False` on export verify | Absolute path may still appear in receipt text |

### Threats intentionally outside scope

- Compromise of the producer host before/during finalize
- Theft of private key material after keygen
- Public transparency ecosystems / CAs / TSAs
- Quantum adversaries against Ed25519
- Correctness of LLM output or business authorization beyond recorded IDs
- TPM/TEE / remote attestation

---

## Design Principles

### Producer authority is local and explicit

On the **producing** host, signing keys live under `<config_root>/trust/keys/` and tlog keys under `<config_root>/trust/tlog/`. Offline verifiers have **no built-in trust root** unless they supply `--expect-key-id` or perform an equivalent check.

### Fail closed (qualified)

| Path | Fail closed on |
|---|---|
| In-runtime verify / sign | digest mismatch, unknown/revoked keys (default policy), bad signatures, malformed keys |
| `verify-export` OVERALL | digest/closed-set failure, bad signature vs embedded key, pin mismatch, bad snapshot binding, missing external_anchor **unless** `--allow-missing-external-anchor`, mismatched `--expect-key-id` |
| `verify-export` OVERALL | **Not** fail closed on TRUST_NOW UNKNOWN/REVOKED |

### Explicit trust (producer policy)

A key is usable for in-runtime verify/sign only if allowed and not revoked (unless `allow_unknown_keys=true`).

### Producer-asserted provenance

Provenance is structured JSON in the integrity chain. It is not independent observation of execution.

### Canonical hashes

SHA-256, lowercase hex. Signatures authenticate digests of `manifest.json` and `evidence.json` only.

### Algorithm surface

Provider protocols exist (`Signer` / `Verifier` / `KeyProvider`). On this baseline only `ed25519` is dispatched. The signed payload does **not** bind `algorithm` or `public_key_id` (limitation; see Cryptographic Model).

### No hidden authority creation

No finalize auto-keygen. No export auto-tlog-keygen.

### Documentation may not overstate implementation

Bound by `ADVERSARIAL-CLAIM-VALIDATION.md`.

---

## Terminology (normative — one meaning each)

| Term | Definition in this document |
|---|---|
| **Integrity** | Digests match file bytes for the closed set |
| **Authenticity** | Ed25519 signature verifies under a stated public key |
| **Authorization** | Verifier (or producer policy) accepts that key id as allowed |
| **Trust** | Policy decision (ALLOWED/REVOKED/UNKNOWN); local, not global PKI |
| **Historical trust (export snapshot)** | Producer policy decision recorded at **export packaging** in `trust_snapshot.json` |
| **Current trust (`TRUST_NOW`)** | Evaluation against verifier host policy at verify time; informational for export OVERALL |
| **Offline verification** | Running `verify-export` without producer private keys/DB/git |
| **Package self-consistency** | What offline OVERALL means without `--expect-key-id` |
| **Transparency (Non-Claim)** | Multi-party witnessed logging — **not** provided |
| **Package Merkle receipt** | `external_anchor.json`: inclusion proof vs checkpoint signed by **embedded** log key |
| **Append-only (operational)** | Local `leaves.jsonl` write pattern; not cryptographically proven to verifiers |
| **Evidence** | Producer-generated bundle files, not independent observation |
| **Provenance** | `evidence.json` / `amof.runtime_provenance/v1` metadata |
| **FINALIZED** | Handoff finalize completed: seal + durable signed bundle + consistency; handoff state `finalized` |
| **Intra-package key equality** | `public_key.json` matches `trust_anchor.json` (formerly called “pin”) |
| **Guaranteed** | Enforceable against the stated adversary under stated assumptions |
| **Verified** | Computed/reported; not necessarily an OVERALL hard gate |
| **Best Effort** | Helpful; not fail-closed |
| **Out of Scope** | Not provided |
| **Non-Claim** | Easy to infer; explicitly disclaimed |

Deprecated wording in older drafts: “trust pin” as out-of-band root, “external anchor” as third-party attestation, “local transparency log” as Rekor-equivalent. Prefer the table above.

---

## Runtime Trust Architecture

```text
 Producer host                              Verifier host
 ┌─────────────────────────────┐            ┌──────────────────────────┐
 │ trust-policy.json / keys/   │            │ optional --expect-key-id │
 │ tlog/ (checkpoint key)      │            │ optional local policy    │
 └──────────────┬──────────────┘            └────────────▲─────────────┘
                │                                        │
 Intent→Exec→Seal→Bundle→Sign→FINALIZED                  │
                │ export                                 │
                ▼                                        │
        export package ──────────────────────────────────┘
        (embedded pubkey, intra-package equality file,
         export-time snapshot, optional Merkle receipt)
                │
                ▼
        LOCAL_INTEGRITY
        SIGNATURE_TRUST  (= authenticity vs embedded key)
        EXTERNAL_ANCHOR  (= package Merkle receipt; default required)
        TRUST_NOW        (report only)
                │
                ▼
        OVERALL = self-consistency (+ expect_key_id if set)
```

### FINALIZED order (actual)

```text
Seal → Bundle write → Sign → Consistency → Handoff FINALIZED
Then optional: Export → Package Merkle receipt → Offline verify
```

If signing fails after bundle write, the bundle directory is removed (no durable FINALIZED-claiming unsigned bundle).

---

## Trust Flow

### Intent / Execution

Outside Trust Layer crypto. Failure: no Trust Layer artifacts.

### Evidence (seal + bundle content)

| | |
|---|---|
| Input | Result bytes, receipt fields |
| Output | Seal; content files |
| Authority | Runtime finalize writer |
| Failure | Seal verify fail; bundle removed if later sign fails |

### Manifest / Provenance

| | |
|---|---|
| Output | `manifest.json`, `hashes.json`, `evidence.json` |
| Authority | Bundle writer |
| Failure | Hash/consistency errors |

### Signature

| | |
|---|---|
| Input | Digests + preferred private key |
| Output | `signature.json` |
| Authority | Explicit keygen + producer policy |
| Failure | `missing_signing_authority` / `signature_invalid` / policy errors |

### FINALIZED

Handoff state + signed bundle. Export requires `signature.json`.

### Export packaging

| | |
|---|---|
| Output | Portable directory + export-time snapshot + optional Merkle receipt |
| Authority | Producer keys + explicit tlog-init |
| Note | Snapshot is **not** sealed at finalize time |

### Offline verification

| | |
|---|---|
| Output | Mode report + OVERALL |
| Authority | Embedded package materials; optional `--expect-key-id` |
| Failure | Required mode FAIL |

---

## Security Model

Do not conflate:

- **Integrity** ≠ **Authenticity** ≠ **Authorization** ≠ **Trust**
- **Export-time snapshot** ≠ **finalize-time sealed trust** ≠ **TRUST_NOW**
- **Package self-consistency** ≠ **Offline authorization**
- **Package Merkle receipt** ≠ **Transparency**

Private keys on this baseline are raw unencrypted bytes under `0600` permissions (`NoEncryption()`). There is no HSM/KMS/passphrase mode in-tree.

---

## Threat Model

| Attack | Detection | Prevention | Recovery | Residual |
|---|---|---|---|---|
| Tamper bundle bytes | Digest fail | Closed hashes | Discard | Pre-hash producer compromise |
| Receipt forgery (unsigned) | Policy if signatures required | `require_signatures` | Refuse unsigned | Permissive policy |
| Signature forgery | Ed25519 verify fail | Key secrecy | Rotate/revoke (library/API; no revoke CLI) | Stolen key |
| Policy edit on producer | Operational | Host ACLs | Restore policy | Privileged admin |
| Single-file key swap in export | Pin equality / signature fail | Intra-package equality | Reject | — |
| **Coherent package forgery** (key+equality file+sig+snapshot[+receipt]) | **Only if `--expect-key-id` (or out-of-band check)** | Verifier trust root | Reject | **Without expect_key_id: residual YES** |
| Export add/remove/rename | Closed set | verify-export | Reject | — |
| Delete Merkle receipt + mark optional in unsigned metadata | Default require anchor | Metadata cannot relax | Reject | Use `--allow-missing-external-anchor` only intentionally |
| Snapshot field rewrite without trusted binding | Fails if Merkle receipt required and binding holds; else weak | Default require receipt | Reject | Package adversary who forges receipt too |
| Missing producer seal after export | N/A (skipped) | `require_producer_seal=False` | — | Absolute path string may remain in receipt |
| Algorithm field lie | Length/provider mismatch today (ed25519-only) | Single algorithm | Reject | Payload does not bind algorithm |
| Replay old valid package | Not prevented | Process controls | Process | Residual YES |
| Truncate `leaves.jsonl` | Not detected by package verify | Out of scope | Ops rebuild | Residual YES |
| Tlog fork/equivocation | Not detected | Out of scope | Out of scope | Residual YES |
| Symlink in key store | `unsafe_symlink` | path_safety | Fix store | Export symlink: content-based |

---

## Cryptographic Model

### Hashes

SHA-256 via `sha256_file`; `hash_algorithm: "sha256"`.

### Signatures (baseline)

| Item | Value |
|---|---|
| Algorithm id | `ed25519` |
| Library | `cryptography` |
| Key id | `sha256(public_key_raw)` hex |
| Schema | `amof.bundle_signature/v1`, version `1` |
| Signed payload | Compact JSON `evidence_digest`, `manifest_digest`, `version` + `\n` via `json.dumps(sort_keys=True, separators=(",", ":"))` |

**Limitations (explicit):**

- Payload does **not** bind `algorithm` or `public_key_id`.
- Canonicalization is the Python `json.dumps` form above — **not** RFC 8785 JCS. Cross-language verifiers must match this exact encoding or verification will fail.
- `timestamp` in `signature.json` is not signed.
- No domain-separation context string on Ed25519 messages.

### Keys / policy

Filesystem raw keys; `amof.trust_policy/v1`. Default unknown keys rejected for in-runtime paths.

### Package Merkle receipt

| Element | Construction |
|---|---|
| Body | Canonical JSON hashedrekord-style binding digests + key id + snapshot digest |
| Leaf / node | `sha256(0x00\|\|…)` / `sha256(0x01\|\|left\|\|right)`; unpaired last node promoted |
| Checkpoint | Signed Ed25519; **log public key embedded in receipt** |
| Offline verify | Inclusion vs that embedded checkpoint key |

Not RFC 6962 CT wire format. Tree shape rule differs from RFC 6962. Not Sigstore Rekor protocol. Name `hashedrekord` is local vocabulary only.

---

## Trust Claims

| Claim | Classification | Proof |
|---|---|---|
| Closed evidence set integrity (SHA-256) when verify runs | **Guaranteed** | `verify_evidence_consistency` / LOCAL_INTEGRITY |
| Provenance document hash-bound | **Guaranteed** | Bundle writer + verify |
| In-runtime Ed25519 authenticity + policy allow/deny/revoke (default) | **Guaranteed** | `verify_bundle_signature` + `assert_key_usable` |
| No finalize auto-keygen / no export auto-tlog-keygen | **Guaranteed** | Finalize/export paths + errors |
| Package self-consistency OVERALL without producer private keys/DB/git/seal dir | **Guaranteed** | `verify_export_package` + recovery tests |
| Ed25519 authenticity vs **embedded** export public key | **Guaranteed** | `verify_signature_with_exported_key` |
| Intra-package public-key / trust_anchor equality | **Guaranteed** (self-consistency only) | `LocalPinnedTrustAnchor` |
| Export-time snapshot consistent with digests/key id | **Guaranteed** (self-consistency) | `verify_trust_snapshot` |
| Package Merkle inclusion vs **embedded** checkpoint | **Guaranteed** (self-consistency) | `verify_external_anchor` |
| `--expect-key-id` enforcement when provided | **Guaranteed** | `unexpected_key_id` |
| TRUST_NOW reporting | **Verified** | Non-gating on OVERALL |
| Unsigned bundle integrity under permissive policy | **Best Effort** | Authenticity absent |
| Global append-only / truncation / fork detection | **Non-Claim** | Not implemented |
| Timestamp authority | **Non-Claim** | Local clock only |
| Future non-revocation | **Non-Claim** | TRUST_NOW may diverge |
| Offline OVERALL ⇒ authorized operator key (without expect_key_id) | **Non-Claim** | Explicit |
| Sigstore/Fulcio/public Rekor / TSA / HSM / KMS / PQC on baseline | **Out of Scope** | — |
| Dual-control policy / revocation distribution | **Out of Scope** | — |
| SLSA level / in-toto layouts / SCITT service | **Out of Scope** | — |
| Legal non-repudiation / intent correctness | **Out of Scope** / **Non-Claim** | — |

---

## Comparative Position

Different goals; no superiority claim.

| System | Goal | AMOF v1 difference |
|---|---|---|
| Sigstore | Public identity + log | Local keys; package-embedded receipt; no Fulcio |
| in-toto | Multi-step supply chain | Single runtime finalize bundle |
| SLSA | Build provenance levels | Execution metadata; not a SLSA level |
| SCITT | Transparency services | Local package receipt only |
| C2PA | Media content credentials | Agent/runtime evidence files |
| PKI | CA hierarchy | Local allowlists; optional expect_key_id |
| Package signing | Build artifacts | Runtime evidence digests |

---

## Current Limitations

1. Ed25519 only; signed payload omits algorithm/key id.
2. Private keys: raw plaintext files (`0600`), no HSM/KMS/passphrase.
3. Trust policy: unsigned, mutable, local; no dual control or distribution.
4. TRUST_NOW does not fail offline OVERALL.
5. Wire snapshot `kind` string still says `TRUST_AT_FINALIZATION` though capture is at export.
6. Receipt may still contain absolute `evidence_seal_path` strings (ignored if missing on export verify).
7. Package Merkle receipt checkpoint key is embedded.
8. Local `leaves.jsonl` lacks locking/consistency proofs/witnesses.
9. Replay of historically valid packages is accepted cryptographically.
10. JSON canonicalization is Python `dumps`, not RFC 8785.
11. No `amof trust revoke` CLI (library `revoke_key` only).
12. Wave 005 ML-DSA not on this baseline.

---

## Future Evolution

> Not authoritative for current main.

### Wave 005 — PQC (unpromoted unless on main)

ML-DSA-65 provider work exists on ticket history; treat as non-canonical until governed promote. Do not describe main as PQC-capable until then. Signature payload algorithm binding should be addressed before mixed-algorithm verify.

### Later candidates (unevaluated)

Hybrid signatures; HSM-backed KeyProvider; external transparency service; RFC 8785 canonicalization — each requires its own claim gate.

---

## Appendix

### A. Red-team and repair references (operator workspace)

- `evidence/AMOF-TRUST-MODEL-v1-REDTEAM/DIRECTOR-SYNTHESIS.md`
- `evidence/AMOF-TRUST-MODEL-v1-RECOVERY/CLAIM-REPAIR-MATRIX.md`
- Process: `docs/engineering/ADVERSARIAL-CLAIM-VALIDATION.md`

### B. Invariants

1. Hashes SHA-256; signatures authenticate digests.
2. Offline OVERALL without `--expect-key-id` = package self-consistency.
3. No hidden authority creation on finalize/export.
4. Export-time snapshot ≠ finalize-time sealed trust ≠ TRUST_NOW.
5. Package Merkle receipt ≠ global transparency.
6. FINALIZED ≠ externally notarized; FINALIZED ≠ PQC.

### C. Waves

| Wave | Focus | Status |
|---|---|---|
| 001–004 | Integrity, provenance, Ed25519, export | On main (implementation) |
| 005 | ML-DSA | Unpromoted unless main contains it |

### D. CLI

```text
amof trust verify RUN [--json]
amof trust export RUN [--output DIR] [--json] [--no-external-anchor]
amof trust verify-export PATH [--json] [--offline-only]
  [--expect-key-id HEX]
  [--allow-missing-external-anchor]
amof trust keygen [--json] [--preferred|--no-preferred] [--require-signatures]
amof trust tlog-init [--json]
```

### E. Claim → implementation index

| Theme | Symbol |
|---|---|
| Integrity | `verify_evidence_consistency`, `sha256_file` |
| Seal portability | `require_producer_seal=` |
| Sign/verify | `sign_evidence_bundle`, `verify_bundle_signature` |
| Export verify | `verify_export_package` |
| Expect key | `expect_key_id=` |
| Snapshot | `build_trust_snapshot` (export path) |
| Merkle receipt | `transparency.verify_external_anchor` |
| Finalize cleanup | handoff `rmtree` on sign failure |

### F. Tests (recovery)

- `tests/test_trust_model_v1_recovery.py` — clean-room without seal; expect_key_id; metadata cannot relax missing anchor; expect_key_id rejects wrong root
- Existing `tests/test_trust_layer_wave_00{1,2,3,4}*.py` regression

### G. Baseline SHA

After promote, readers must take the Git commit that contains this file on `origin/main` as the implementation baseline. Do not trust a stale SHA printed in older drafts.

---

**End of AMOF Trust Model v1.0 (claim-repaired)**

Publication as canonical still requires a **second independent five-model red-team** under `ADVERSARIAL-CLAIM-VALIDATION.md` after this repair lands on a reviewable branch/main candidate.
