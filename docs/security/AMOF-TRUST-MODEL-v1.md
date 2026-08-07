# AMOF Trust Model v1.0

**Status:** Canonical technical specification  
**Authority:** Git `origin/main` @ `134978a37b553cb099ccbbe6c733ff170c955fbb`  
**Scope:** Trust Layer Waves 001–004 as implemented in the public AMOF repository  
**Classification:** Engineering / security architecture (not marketing)

| Field | Value |
|---|---|
| Document id | `AMOF-TRUST-MODEL-v1` |
| Implementation baseline | `134978a` (`chore(promote-main): promote AMOF-TRUST-LAYER-WAVE-004 candidate bundle`) |
| Primary code | `scripts/amof/trust_layer.py`, `scripts/amof/trust_crypto/` |
| Operator CLI | `amof trust {verify\|export\|verify-export\|keygen\|tlog-init}` |

This document describes only behavior present on the cited main SHA. Claims that require Wave 005 (ML-DSA / PQC signing) or later work appear exclusively in **Future Evolution** and are labeled unpromoted.

---

## Executive Summary

AMOF Trust Layer v1.0 is a **runtime execution trust system**. It binds a completed agent/runtime execution to a closed, content-addressed evidence bundle; optionally authenticates that bundle with a local Ed25519 signature under an explicit trust policy; and optionally exports a portable package that an external verifier can check without the original workspace, database, git checkout, or private keys.

### What AMOF guarantees (when configured and FINALIZED as specified)

1. **Content integrity of a closed evidence set** via SHA-256 digests over a fixed file set (`receipt.json`, `result.json`, `evidence.json`, `hashes.json`, `manifest.json`), verified fail-closed on consume and on offline export verify (`LOCAL_INTEGRITY`).
2. **Deterministic provenance** as a structured document (`amof.runtime_provenance/v1`) included in the bundle and covered by the same integrity chain.
3. **Authenticity of digest bindings** when signatures are required: Ed25519 over a canonical payload of `{manifest_digest, evidence_digest, version}` under a key listed in local `trust-policy.json` (`SIGNATURE_TRUST`).
4. **Explicit operator key authority**: signing keys are created only by `amof trust keygen`; finalize does not auto-generate keys.
5. **Historical trust snapshot at packaging** (`TRUST_AT_FINALIZATION` in `trust_snapshot.json`), distinct from later live policy evaluation (`TRUST_NOW`).
6. **Portable offline verification** of an export package: closed file set, exported public key + local pin, signature, snapshot, and (when present) package-bound Merkle inclusion under a signed local checkpoint (`EXTERNAL_ANCHOR`).
7. **Fail-closed defaults** for unknown keys, malformed keys, digest mismatch, extra/missing export files, and invalid signatures.

### What AMOF does NOT guarantee

1. **Global transparency**, public Rekor/Sigstore equivalence, or multi-party witnessed logs.
2. **Log truncation detection**, **equivocation/fork detection**, or proof of **global append-only history** beyond a single self-contained receipt.
3. **Timestamp authority** (no RFC 3161 TSA); timestamps are local wall-clock metadata.
4. **Future non-revocation**: a key may be revoked later (`TRUST_NOW: REVOKED`) while `TRUST_AT_FINALIZATION` remains valid for the historical package.
5. **Post-quantum signatures** on `main` (Ed25519 only at baseline SHA).
6. **HSM/KMS**, certificate PKI, Fulcio, threshold signing, or remote signing services.
7. **Semantic correctness** of write-scope, mission intent, or model behavior—only recorded identifiers and content hashes.
8. **Non-repudiation** in a legal or multi-party sense beyond local cryptographic authenticity under operator-controlled keys.

---

## Problem Statement

### Why runtime trust differs from traditional software integrity

Traditional software integrity (package signing, container image digests, binary attestation) answers: *Was this artifact built from known inputs by a known pipeline?*

AMOF executions produce **runtime outcomes**: receipts, results, seals, write-scope bindings, and workspace-relative effects. The trust question is:

> Given a completed run, can a verifier establish that the evidence set is complete, untampered, attributable to an authorized local signing key at finalization time, and—optionally—independently re-checkable without the producing host?

That requires binding **execution evidence**, not only build artifacts.

### Threats addressed

| Threat class | Mechanism on main |
|---|---|
| Silent mutation of receipt/result/evidence after seal | SHA-256 seal + bundle hashes; fail-closed verify-on-consume |
| Incomplete or substituted evidence set | Closed file set; extra/missing files fail |
| Unsigned “final” presentation where policy requires signatures | `require_signatures` / missing `signature.json` |
| Unauthorized signing key | Trust policy allowed/revoked/unknown; preferred key for finalize |
| Auto-created signing or tlog authority | Explicit `keygen` / `tlog-init` only |
| Export package tampering / key replacement | Pin + signature + digests + snapshot + optional anchor binding |
| Conflation of historical vs current trust | `TRUST_AT_FINALIZATION` vs `TRUST_NOW` |

### Threats intentionally outside scope

- Compromise of the operator host before or during finalize (malicious producer with valid keys)
- Physical or logical theft of private keys after keygen
- Public transparency ecosystems, certificate authorities, or timestamp authorities
- Quantum adversaries against Ed25519 (not mitigated on this baseline)
- Correctness of LLM output, policy intent, or business authorization beyond recorded IDs
- Network attestation, remote attestation (TPM/TEE), or confidential computing

---

## Design Principles

### Authority

Cryptographic and policy authority is **local and explicit**. Operator signing keys live under `<config_root>/trust/keys/`. Transparency-log checkpoint keys live under `<config_root>/trust/tlog/` and are created only by `amof trust tlog-init`. Finalize and export do not create authority as a side effect.

### Fail Closed

Verification paths raise `TrustIntegrityError` (or equivalent CLI FAIL) on missing required artifacts, digest mismatch, unknown/revoked keys (default policy), unsupported algorithms, malformed keys, and invalid signatures. Permissive unsigned verification exists only when policy explicitly allows it (`allow_unsigned` with `require_signatures=false`).

### Explicit Trust

A key is trusted for verify/sign only if listed in `allowed_key_ids` and not in `revoked_key_ids` (unless `allow_unknown_keys=true`, which weakens the default). Preferred signing key must be in the allowed set.

### Deterministic Provenance

Provenance is a canonical JSON document (`amof.runtime_provenance/v1`) derived from receipt, result, and seal metadata. It is stored as `evidence.json` and included in the integrity chain.

### Canonical Hashes

Content addressing uses **SHA-256** (`hash_algorithm: "sha256"`). Digests are lowercase hex. Signatures authenticate digests of `manifest.json` and `evidence.json`, not an alternate hash function.

### Algorithm Agility (abstraction)

Wave 003 introduces algorithm-neutral protocols: `Signer`, `Verifier`, `KeyProvider` (`trust_crypto/interfaces.py`). On this baseline, the only concrete signing provider is Ed25519 (`ed25519_provider.py`). The abstraction exists so additional algorithms can be added without redesigning provenance or evidence bundles; **no second signing algorithm is available on main at `134978a`.**

### No Hidden Authority

No finalize-time auto-keygen. No export-time auto-creation of tlog keys. Missing authority fails with `missing_signing_authority` / `missing_tlog_authority` / `missing_key`.

### No Silent Trust Creation

Enrollment into `trust-policy.json` occurs through explicit keygen enrollment (or equivalent policy write). Verification does not invent allowed keys.

---

## Runtime Trust Architecture

```text
                    ┌─────────────────────────────────────────┐
                    │           Operator authority             │
                    │  trust-policy.json  │  keys/  │  tlog/  │
                    └───────────────┬─────────────┬───────────┘
                                    │             │
 Intent → Execution → Seal ─────────┼─────────────┤
                    │               │             │
                    ▼               ▼             ▼
            ┌──────────────┐  Ed25519 sign   append hashedrekord
            │ Evidence     │       │             │
            │ Bundle       │◄──────┘             │
            │ (content +   │                     │
            │  hashes +    │                     │
            │  manifest +  │                     │
            │  signature)  │                     │
            └──────┬───────┘                     │
                   │ export                      │
                   ▼                             ▼
            ┌──────────────────────────────────────────┐
            │ Portable export package                  │
            │ + public_key + pin + snapshot + metadata │
            │ + external_anchor (optional)             │
            └──────────────────┬───────────────────────┘
                               │ amof trust verify-export
                               ▼
                    LOCAL_INTEGRITY
                    SIGNATURE_TRUST
                    EXTERNAL_ANCHOR
                    TRUST_NOW (report only)
                               │
                               ▼
                         OVERALL PASS/FAIL
```

### Artifacts

Runtime outputs recorded into the bundle: `receipt.json`, `result.json`, and provenance (`evidence.json`). Seal artifacts may exist under a seal directory referenced by the receipt; finalize copies sealed result content into the bundle path as specified by the seal/bundle writers.

### Evidence Bundle

Directory: `<data_root>/trust/runs/<run_id>/`.

| File | Role |
|---|---|
| `receipt.json` | Execution receipt |
| `result.json` | Result payload |
| `evidence.json` | Provenance document |
| `hashes.json` | `amof.evidence_bundle_hashes/v1` — SHA-256 of content files |
| `manifest.json` | `amof.evidence_bundle_manifest/v1` — closed set listing |
| `signature.json` | Optional/required by policy — `amof.bundle_signature/v1` |

Layout is flat; subdirectories are rejected as `extra_file`.

### Manifest

Lists content files and `hashes.json`; excludes itself from self-hash. `hash_algorithm` must be `"sha256"`.

### Provenance

Schema `amof.runtime_provenance/v1`. Records who/what/when/where/why/how, input/output digests, write-scope identifiers, git SHAs as recorded, and seal references. Provenance integrity is content hashing; authenticity requires signature + policy/pin.

### Trust Policy

Schema `amof.trust_policy/v1` at `<config_root>/trust/trust-policy.json`:

- `allowed_key_ids`, `revoked_key_ids`, `preferred_key_id`
- `require_signatures`, `allow_unknown_keys`, `allow_unsigned`

### Signature Provider

Concrete provider on main: Ed25519 via `cryptography` (`Ed25519Signer` / `Ed25519Verifier`). Dispatch: `signer_for_algorithm` / `verifier_for_algorithm` (Ed25519-only).

### Trust Anchor

Protocol `TrustAnchor`. Implemented: `LocalPinnedTrustAnchor` (`anchor_kind: local_pinned`). Proves the export’s public key material matches `trust_anchor.json`. Explicitly **not** a transparency proof.

### Offline Verification

`amof trust verify-export PATH` → `verify_export_package()`. Does not require original `AMOF_HOME` keys, tlog store, DB, or git.

### FINALIZED

Local runtime terminal state produced by `_seal_and_finalize_execution` when seal, canonical bundle, signature (with preferred key), and consistency verification all succeed. Receipt carries `finalized=true` and `evidence.finalization="FINALIZED"`. Export requires `signature.json` (signed finalized run).

---

## Trust Flow

```text
Intent → Execution → Evidence → Manifest → Provenance
       → Signature → Anchor → Verification → FINALIZED
```

### Intent

| | |
|---|---|
| **Input** | Operator/mission request, write-scope approvals as applicable |
| **Output** | Execution plan / handoff binding (outside Trust Layer crypto) |
| **Authority** | Runtime + write-scope systems |
| **Failure** | Execution not started; no Trust Layer artifact |

### Execution

| | |
|---|---|
| **Input** | Bound work item, runner/backend |
| **Output** | Receipt + result (+ optional seal inputs) |
| **Authority** | Runtime executor |
| **Failure** | Non-final statuses; Trust Layer finalize path not entered |

### Evidence (seal + bundle content)

| | |
|---|---|
| **Input** | Result bytes, receipt fields |
| **Output** | Evidence seal (`amof.runtime_evidence_seal/v1`); content files |
| **Authority** | Runtime finalize writer |
| **Failure** | Seal verify fail → not FINALIZED |

### Manifest

| | |
|---|---|
| **Input** | Content file digests |
| **Output** | `manifest.json` + `hashes.json` |
| **Authority** | Bundle writer (`write_canonical_evidence_bundle`) |
| **Failure** | Hash/manifest mismatch on verify |

### Provenance

| | |
|---|---|
| **Input** | Receipt, result, seal, mission metadata |
| **Output** | `evidence.json` (`amof.runtime_provenance/v1`) |
| **Authority** | `build_provenance_document` |
| **Failure** | Consistency checks fail (`verify_evidence_consistency`) |

### Signature

| | |
|---|---|
| **Input** | `manifest_digest`, `evidence_digest`; preferred private key |
| **Output** | `signature.json` |
| **Authority** | Explicit key from `amof trust keygen` + policy |
| **Failure** | `missing_signing_authority` / `missing_key` / `signature_invalid` / `revoked_key` / `unknown_key` |

### Anchor (export-time)

| | |
|---|---|
| **Input** | Digests + `trust_snapshot_digest` + public_key_id |
| **Output** | `external_anchor.json` (hashedrekord + inclusion + checkpoint) |
| **Authority** | Explicit `amof trust tlog-init` log key |
| **Failure** | `missing_tlog_authority`; binding/inclusion/checkpoint verify fail |

### Verification

| | |
|---|---|
| **Input** | Bundle or export path |
| **Output** | Mode report + OVERALL PASS/FAIL |
| **Authority** | Verifier using public material (+ live policy for TRUST_NOW) |
| **Failure** | Any required mode FAIL |

### FINALIZED

| | |
|---|---|
| **Input** | Successful seal + bundle + signature + consistency |
| **Output** | Finalized receipt/state; exportable signed bundle |
| **Authority** | Same as signature + runtime |
| **Failure** | Partial state; export refused without `signature.json` |

---

## Security Model

Do not conflate the following properties.

### Integrity

SHA-256 content digests and closed artifact sets. Detects accidental or adversarial modification of bundled files relative to recorded digests. **Does not** identify who produced the bundle.

### Authenticity

Ed25519 verification that the digest payload was signed by the private key corresponding to an exported/local public key. **Does not** alone decide whether that key is trusted.

### Trust

Policy decision: key allowed, revoked, or unknown; snapshot decision `ALLOWED` / `REVOKED` / `UNKNOWN` at finalization packaging. Trust is **local policy**, not a global PKI.

### Authority

Who may create keys and enroll them: operator via explicit CLI. Who may sign checkpoints: tlog key from `tlog-init`. Authority is not inferred from signature presence alone.

### Historical Trust (`TRUST_AT_FINALIZATION`)

Immutable snapshot in the export package describing trust decision and digests at packaging. Bound into the external anchor body via `trust_snapshot_digest`. Survives later revocation for offline historical verification of SIGNATURE_TRUST.

### Current Trust (`TRUST_NOW`)

Evaluation against the verifier host’s current `trust-policy.json`. Informational for `verify-export` (does not fail OVERALL when `REVOKED`). Used to surface “trusted then, revoked now.”

### Offline Trust

Verification using only the export package’s public material. No private keys, no producer DB, no git, no network requirement in the implemented verify path.

### Policy

Mutable local configuration. Overwrites of `trust-policy.json` are intentional authority actions. Key material and signatures use create-exclusive write patterns; policy updates are allowed in place (non-symlink).

---

## Threat Model

| Attack | Detection | Prevention | Recovery | Residual risk |
|---|---|---|---|---|
| Tamper receipt/result/evidence bytes | Digest mismatch on verify | Closed hashes/manifest; fail-closed | Re-run from trusted producer; discard package | Compromised producer before hash |
| Receipt forgery (new fake bundle) | Fails signature/policy if signatures required; integrity alone insufficient for attribution | `require_signatures` + allowed keys | Revoke keys; refuse unsigned | Unsigned policy mode |
| Signature forgery | Ed25519 verify fail | Key secrecy; algorithm fixed | Rotate/revoke keys | Stolen private key |
| Policy modification (add attacker key) | Operational/audit outside crypto | Host access control on config | Restore policy; revoke attacker key | Privileged host admin |
| Key substitution in export | Pin + fingerprint + signature fail | `LocalPinnedTrustAnchor` + signature | Reject package | None beyond producer malice |
| Export file add/remove/rename | Closed set check | `verify_export_package` extras/missing | Reject | — |
| One-byte artifact mutation | Digest/signature fail | Same | Reject | — |
| Anchor leaf/proof/root mutation | Inclusion/checkpoint verify fail | Binding digests + Merkle + sig | Reject | — |
| Cross-run anchor/signature copy | Binding mismatch (`run_id`/digests) | Body includes digests + snapshot digest | Reject | — |
| Snapshot field rewrite | Snapshot verify / anchor `trust_snapshot_digest` | Bound snapshot digest | Reject | — |
| Workspace/DB/git deletion after export | N/A (offline package) | Self-contained export | Use export | Producer-side loss of non-exported state |
| Downgrade / wrong algorithm field | `unsupported_algorithm` (non-ed25519) | Ed25519-only verify | Reject | — |
| Algorithm confusion | Provider algorithm mismatch checks | Single algorithm on main | Reject | — |
| Cross-run signature substitution | Manifest/evidence digest mismatch | Digests in signed payload | Reject | — |
| Replay of valid old package | Not prevented as “freshness”; package remains historically valid | Operational acceptance policy | Process controls | Replay of historically valid exports |
| Truncate operator `leaves.jsonl` | **Not detected** by package verifier | Out of scope for single-receipt verify | Rebuild operationally | Residual: **yes** |
| Equivocate/fork tlog | **Not detected** without witnesses | Out of scope | Out of scope | Residual: **yes** |
| Symlink key paths in authority store | `unsafe_symlink` | `assert_not_symlink` / private modes | Fix store | Export package symlink: content-based fail only |
| Auto-authority creation | N/A (removed) | Explicit keygen/tlog-init | — | Mis-operated hosts |

---

## Cryptographic Model

### Canonical hashes

- Algorithm: **SHA-256**
- Encoding: lowercase hex (64 chars)
- Primary APIs: `sha256_file`, bundle `hashes.json` / `manifest.json`

### Signatures (main @ `134978a`)

| Item | Value |
|---|---|
| Algorithm id | `ed25519` |
| Library | `cryptography` (Ed25519) |
| Public/private raw length | 32 bytes |
| Key id | `sha256(public_key_raw)` hex |
| Signature schema | `amof.bundle_signature/v1`, `version: 1` |
| Signed payload | Canonical JSON of `evidence_digest`, `manifest_digest`, `version` + `\n` |

`timestamp` in `signature.json` is **not** part of the signed payload.

### Trust policy and keys

- Filesystem key provider under `<config_root>/trust/keys/<key_id>/`
- Policy schema `amof.trust_policy/v1`
- Default: unknown keys rejected; signatures not required until policy says so

### Key authority

| Operation | Command / API |
|---|---|
| Create operator key + enroll | `amof trust keygen` |
| Create tlog checkpoint key | `amof trust tlog-init` |
| Sign bundle | Finalize / `sign_evidence_bundle` with preferred key |
| Revoke | Policy update via `revoke_key` + `write_trust_policy` |

### Algorithm agility

Provider protocols exist (`Signer` / `Verifier` / `KeyProvider`). On this baseline, dispatch accepts only `ed25519`. Adding algorithms is an implementation change to providers/dispatch; it is **not** present on main.

### Why a provider architecture exists

To keep provenance, hashing, bundle layout, and export verification **algorithm-neutral at the trust-flow level**, while confining algorithm-specific logic to provider modules. This is an engineering boundary, not a claim of multi-algorithm production support on `134978a`.

### Local transparency construction (package-bound)

| Element | Construction |
|---|---|
| Body | Canonical JSON `hashedrekord` v0.0.1 binding digests + `public_key_id` + `trust_snapshot_digest` |
| `body_digest` | SHA-256 of canonical body |
| Leaf | `sha256(0x00 \|\| body_digest_ascii)` |
| Internal node | `sha256(0x01 \|\| left \|\| right)`; unpaired last node promoted |
| Checkpoint | `{origin: amof-local-tlog/v1, tree_size, root_hash, log_key_id}` signed Ed25519 |
| Offline verify | Inclusion against embedded checkpoint + embedded log public key |

This is **not** RFC 6962 Certificate Transparency wire format and **not** Sigstore Rekor protocol compatibility.

---

## Trust Claims

| Claim | Classification | Basis |
|---|---|---|
| Closed evidence set integrity (SHA-256) | **Guaranteed** (when verify runs) | `verify_evidence_consistency` / LOCAL_INTEGRITY |
| Provenance document present and hash-bound | **Guaranteed** | Bundle writer + verify |
| Ed25519 authenticity of digest payload | **Guaranteed** when signatures required and keys valid | `verify_bundle_signature` / SIGNATURE_TRUST |
| Policy allow/deny/revoke enforcement (default) | **Guaranteed** | `TrustPolicy.assert_key_usable` |
| No finalize auto-keygen | **Guaranteed** | Finalize path + tests |
| No export auto-tlog keygen | **Guaranteed** | `missing_tlog_authority` |
| Offline export verify without producer host state | **Guaranteed** | `verify_export_package` + clean-room acceptance |
| Local pin of exported public key | **Guaranteed** | `LocalPinnedTrustAnchor` |
| TRUST_AT_FINALIZATION snapshot binding | **Guaranteed** in export path | snapshot + anchor body digest |
| TRUST_NOW reporting | **Verified** (informational) | Does not fail OVERALL on revoke |
| Package Merkle inclusion vs embedded checkpoint | **Guaranteed** for that receipt | `verify_external_anchor` |
| Global append-only history | **Non-Claim** | Explicit `does_not_prove` / no gossip |
| Truncation detection | **Non-Claim** | Not implemented |
| Fork / equivocation detection | **Non-Claim** | Not implemented |
| Timestamp authority | **Non-Claim** | Local `utc_now` only |
| Future non-revocation | **Non-Claim** | TRUST_NOW may diverge |
| Sigstore / Fulcio / public Rekor | **Out of Scope** | Not used |
| TSA / blockchain / HSM / KMS | **Out of Scope** | Not used |
| PQC / ML-DSA signatures | **Out of Scope** on this baseline | Not on main @ `134978a` |
| Legal non-repudiation | **Non-Claim** | Local authenticity only |
| Intent / model output correctness | **Out of Scope** | Not a Trust Layer property |
| SLSA level certification | **Out of Scope** | Not claimed |
| Unsigned bundle integrity under permissive policy | **Best Effort** integrity only | Authenticity absent |

---

## Comparative Position

Comparisons describe **different goals and assumptions**. No superiority claim.

### Sigstore

| | Sigstore | AMOF Trust v1.0 |
|---|---|---|
| Goal | Public key transparency + artifact signing ecosystem | Local runtime execution evidence + optional portable verify |
| Identity | Fulcio certificates, OIDC-ish issuance | Operator filesystem keys + local policy |
| Log | Public Rekor | Local append-only hashedrekord; package-embedded receipt |
| Network | Typically online issuance/log | Offline verify path requires no network |

### in-toto

| | in-toto | AMOF Trust v1.0 |
|---|---|---|
| Goal | Supply-chain step layouts and functionary keys | Single runtime finalize bundle for an execution |
| Model | Multi-step link metadata | One evidence bundle + optional signature/export |
| Overlap | Digest binding of products | Digest binding of receipt/result/provenance |

### SLSA

| | SLSA | AMOF Trust v1.0 |
|---|---|---|
| Goal | Build provenance levels for artifacts | Runtime execution finalization evidence |
| Provenance | Build system provenance | `amof.runtime_provenance/v1` execution provenance |
| Levels | Graduated track | Not a SLSA level claim |

### SCITT

| | SCITT | AMOF Trust v1.0 |
|---|---|---|
| Goal | Standardized transparency services for statements | Local package-bound anchor MVP |
| Service | External transparency service assumptions | Embedded checkpoint in export |

### C2PA

| | C2PA | AMOF Trust v1.0 |
|---|---|---|
| Goal | Content provenance for media assets | Agent/runtime execution evidence |
| Binding | Content credentials in assets | Bundle files + digests |

### Traditional PKI

| | PKI | AMOF Trust v1.0 |
|---|---|---|
| Trust root | CA hierarchy | Local `trust-policy.json` allowlists |
| Revocation | CRL/OCSP (typical) | Local revoked list + TRUST_NOW report |
| Naming | X.509 identities | Key ids = SHA-256 of raw public keys |

### Supply-chain signing (general)

Package/container signing attests **build artifacts**. AMOF attests **runtime evidence of an execution**. They compose rather than substitute: an AMOF binary may be supply-chain signed, while Trust Layer signs that binary’s execution outputs.

---

## Current Limitations

Limited to implementation on `134978a`:

1. **Ed25519 only** for bundle and checkpoint signatures.
2. **Local trust policy only**; no distributed policy or enterprise directory binding.
3. **Local tlog** without witnesses, gossip, or splitting detection.
4. **Checkpoint public key is embedded in the export receipt**; offline verify trusts that embedded key for checkpoint signature validation (binding + inclusion still required).
5. **TRUST_NOW does not fail** offline OVERALL on revocation.
6. **Signed payload covers digests only**, not raw file bytes directly (files are bound via digests).
7. **`signature.json` is outside the manifest self-set** (optional/excluded from manifest hashing).
8. **Export package path symlink hardening** is content-based, not `assert_not_symlink` on every export leaf.
9. **No KMS/HSM** backing for private keys.
10. **No TSA**, Sigstore, or public log integration.
11. **Permissive unsigned mode** remains available when policy allows.
12. **Replay of historically valid packages** is not a cryptographic failure.
13. **Wave 005 ML-DSA** is not on this main SHA.

---

## Future Evolution

> Separated from implemented truth. Not authoritative for `main` @ `134978a`.

### Wave 005 — PQC provider (unpromoted at document baseline)

Implemented on branch `ticket/AMOF-TRUST-LAYER-WAVE-005-pqc` @ `827aa42` (not an ancestor of `origin/main` at baseline). Intended scope:

- ML-DSA-65 (`ml-dsa-65`) via `cryptography` FIPS 204 APIs
- `amof trust keygen --algorithm ml-dsa`
- Same trust flow; algorithm-neutral registry
- Ed25519 retained; mixed historical verification

Until promoted via governed `promote-main`, **main must not be described as PQC-capable**.

### Wave 006+ (candidates only)

Possible subsequent work (unevaluated here as requirements):

- Hybrid classical + PQ signatures for one execution
- Remote or HSM-backed `KeyProvider`
- Standards/compliance export profiles
- External transparency service integration (still without overclaiming global guarantees)

---

## Appendix

### A. Evidence references (operator workspace)

| Evidence | Path |
|---|---|
| Wave 004 promote acceptance | `evidence/AMOF-TRUST-LAYER-WAVE-004-PROMOTE-ACCEPTANCE/` |
| Wave 004 clean-room | `…/runtime/clean-room-acceptance.json` |
| Wave 003 / promote adversarial | `evidence/AMOF-TRUST-LAYER-PROMOTE-ACCEPTANCE/` (if present) |
| Wave 005 implementation (unpromoted) | `evidence/AMOF-TRUST-LAYER-WAVE-005/` |
| Prior crypto trust audit (historical) | `docs/evidence/crypto-trust-audit/` |

### B. Trust invariants

1. Hashes remain SHA-256; signatures authenticate digests.
2. FAIL_CLOSED on integrity and authenticity failures in required modes.
3. No hidden authority creation on finalize or export.
4. `TRUST_AT_FINALIZATION ≠ TRUST_NOW`.
5. Local tlog receipt ≠ global transparency.
6. FINALIZED ≠ “externally notarized”; FINALIZED ≠ “PQC”.

### C. Acceptance missions (implemented waves)

| Wave | Focus | Main outcome |
|---|---|---|
| 001 | Fail-closed integrity / seals / bootstrap verify | On main |
| 002 | Canonical evidence bundle + provenance | On main |
| 003 | Ed25519 + trust policy + explicit keygen | On main |
| 004 | Export, offline verify, snapshot, local anchor | On main @ `134978a` |
| 005 | ML-DSA provider | Ticket only at baseline |

### D. Canonical terminology

| Term | Meaning in AMOF Trust v1.0 |
|---|---|
| Evidence bundle | Closed directory under `trust/runs/<run_id>/` |
| Provenance | `evidence.json` / `amof.runtime_provenance/v1` |
| FINALIZED | Runtime finalize completed with seal + signed bundle (policy-dependent signing) |
| TRUST_AT_FINALIZATION | Snapshot in export at packaging |
| TRUST_NOW | Live policy evaluation at verify time |
| Local pin | `trust_anchor.json` / `LocalPinnedTrustAnchor` |
| External anchor | Package `external_anchor.json` (local hashedrekord MVP) |
| OVERALL | Conjunction of required verify-export modes (excludes TRUST_NOW hard fail) |

### E. Glossary

| Term | Definition |
|---|---|
| Fail closed | Prefer refusal over acceptance when checks cannot complete successfully |
| Hashedrekord | Local leaf body binding content digests (name borrowed conceptually; not Rekor wire API) |
| Key id | SHA-256 of raw public key bytes (hex) |
| Clean-room verify | Verify export after removing producer runtime state |
| Provider | Concrete `Signer`/`Verifier`/`KeyProvider` implementation |

### F. CLI surface (baseline)

```text
amof trust verify RUN|--json
amof trust export RUN [--output DIR] [--json] [--no-external-anchor]
amof trust verify-export PATH [--json] [--offline-only]
amof trust keygen [--json] [--preferred|--no-preferred] [--require-signatures]
amof trust tlog-init [--json]
```

### G. Claim-to-implementation index

| Claim theme | Implementation |
|---|---|
| Integrity | `trust_layer.verify_evidence_consistency`, `sha256_file` |
| Provenance | `build_provenance_document` |
| Sign/verify | `trust_crypto.bundle_sign` |
| Ed25519 | `trust_crypto.ed25519_provider` |
| Policy | `trust_crypto.policy` |
| Keys | `trust_crypto.filesystem_keys` |
| Export | `trust_crypto.export_package` |
| Offline verify | `trust_crypto.verify_export` |
| Snapshot | `trust_crypto.snapshot` |
| Pin | `trust_crypto.anchors` |
| Tlog | `trust_crypto.transparency` |
| Finalize | `handoff._seal_and_finalize_execution` |
| Path safety | `trust_crypto.path_safety` |

---

**End of AMOF Trust Model v1.0**

Document baseline SHA: `134978a37b553cb099ccbbe6c733ff170c955fbb`.  
Any description of signing algorithms other than Ed25519, or of global transparency properties, is outside this baseline and must not be read as current main truth.
