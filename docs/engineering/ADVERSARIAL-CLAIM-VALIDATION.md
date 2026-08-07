# Adversarial Claim Validation

**Status:** Canonical engineering process  
**Authority:** Public AMOF repository (`docs/engineering/`)  
**Applies to:** Architecture RFCs, security models, trust specifications, and any document that asserts security, trust, or runtime guarantees

---

## Purpose

Implementation correctness is necessary but not sufficient.

A feature may be correctly implemented and still be unsafe to describe.
A document may be carefully worded and still overstate what the code proves.

This principle binds **claims** to **adversarial survival**, not to author intent.

---

## Core Principles

1. **Implementation correctness is necessary but not sufficient.**
   Passing unit tests and green acceptance suites do not authorize security or trust language.

2. **Every security, trust, and architecture claim must survive independent adversarial review before becoming canonical.**
   Friendly review and author self-check are not substitutes.

3. **Implementation and documentation are separate authorities.**
   Code proves behavior. Documents assert claims. Neither may silently redefine the other.
   When they diverge, the false claim must be removed or the implementation must be changed — never papered over.

4. **Security claims must be explicitly classified** using exactly these tiers:

   | Tier | Meaning |
   |---|---|
   | **Guaranteed** | Holds against the stated adversary under the stated assumptions; enforceable by verify paths or equivalent |
   | **Verified** | Checked or reported by implementation, but not a hard acceptance gate (or not adversarially strong) |
   | **Best Effort** | Helpful property without fail-closed enforcement |
   | **Out of Scope** | Intentionally not provided by this system |
   | **Non-Claim** | Easy to infer; explicitly disclaimed so readers do not assume it |

5. **If implementation cannot prove a claim: either implement it, or stop claiming it.**
   There is no third option of aspirational language in canonical docs.

6. **Independent reviewers must work in isolation.**
   No shared drafts, no reading each other's findings before each report is final, no consensus building during review.

7. **Director merges findings only after all independent reviews finish.**
   Synthesis preserves disagreements. Do not average opinions. Minority technically valid findings remain in the record.

8. **A review that discovers blockers is considered SUCCESSFUL.**
   Approval is not the objective. Discovery of why a claim must not ship is success.

9. **Technical truthfulness is more important than approval.**
   Prefer REVISE / REJECT over PROMOTE when claims do not survive attack.

10. **Documentation may never overstate implementation.**
    Vocabulary that implies out-of-band trust, third-party attestation, global transparency, or authorization — when the code only provides package self-consistency — is forbidden in canonical text.

11. **Canonical publication requires the full gate:**

    ```text
    implementation
         → acceptance
         → independent red-team
         → claim audit
         → repair
         → publication
    ```

    Skipping any stage is a process failure. Publication without red-team and claim audit is not canonical.

---

## Process (normative)

### When this process is mandatory

- New or revised security / trust / architecture RFCs under `docs/security/`, `docs/architecture/`, or equivalent
- Any document that uses claim tiers (Guaranteed / Verified / …)
- Any promote that introduces or changes public trust, crypto, provenance, or authority semantics

### Review composition (minimum)

At least three independent reviewers spanning distinct expertise when the document asserts cryptographic or trust properties. For high-impact trust models, prefer five isolated reviewers (cryptography, supply-chain/security architecture, runtime/distributed systems, critical infrastructure/governance, hostile academic or equivalent).

### Isolation rules

- Each reviewer receives the candidate document and baseline SHA only.
- Reviewers must not read other reviewers' reports before filing their own.
- Reviewers should prefer finding why the document should **not** be published.
- Director synthesis starts only after all assigned reviews are complete (or formally failed and replaced).

### Claim audit

After red-team synthesis, perform a claim-by-claim audit:

- Quote each Guaranteed / Verified claim.
- Cite the implementation path that proves it — or delete/reclassify the claim.
- List every sentence that can be read as exaggeration; rewrite or remove before publication.

### Outcomes

| Outcome | Meaning |
|---|---|
| **PROMOTE** | Claims survived adversarial review and claim audit; documentation matches implementation |
| **REVISE** | Blockers or HIGH findings require document and/or implementation repair before another review cycle |
| **REJECT** | Document is unsafe to treat as canonical even after light edit |

A REVISE or REJECT with discovered blockers is a **successful** process execution.

---

## Rationale

The Trust Model v1.0 red-team mission (2026-08-07) demonstrated that a carefully scoped, Non-Claim-rich document can still:

- mark package self-consistency as **Guaranteed** authenticity/authorization,
- assert offline / clean-room independence while finalize embeds absolute producer paths,
- use “pin” / “external anchor” language that implies out-of-band trust roots,
- pass implementation acceptance while failing independent adversarial review.

Five isolated reviewers (cryptography, supply-chain, runtime, critical infrastructure, hostile academic) independently recommended against publication. Director synthesis outcome: **REVISE**, not PROMOTE.

That outcome is the intended use of this process — not an anomaly.

---

## References (Trust Model Red Team)

| Artifact | Location |
|---|---|
| Candidate document | `docs/security/AMOF-TRUST-MODEL-v1.md` (ticket; not canonical until revised + gated) |
| Director synthesis | Operator evidence: `evidence/AMOF-TRUST-MODEL-v1-REDTEAM/DIRECTOR-SYNTHESIS.md` |
| Isolated reviews | `evidence/AMOF-TRUST-MODEL-v1-REDTEAM/REVIEWER-{A,B,C,D,E}.md` |
| Baseline implementation (at review time) | `origin/main` @ `134978a` (Trust Waves 001–004) |

Operator workspace paths above are coordination evidence; product-repo truth remains Git main after governed promote of this principle document and of any future revised RFCs.

---

## Binding on future work

Future architecture and security RFCs **shall** follow this process before being treated as canonical publication.

Authors, Directors, and Foremen must not:

- promote trust/security claim documents on acceptance tests alone,
- treat markdown updates as authority over code,
- collapse independent reviews into a single shared critique pass,
- reclassify discovered blockers as “noise” without technical rebuttal in the synthesis record.

When in doubt: stop claiming, then implement, then re-enter the gate.

---

## Related process

- Governed promotion: operator `playbooks/PROMOTE-MAIN.md`
- Executor exit expectations: operator `playbooks/EXECUTOR-EXIT-GATE.md`
- Model / Director policy: operator `playbooks/MODEL-POLICY.md`

This file is the product-repo normative home for adversarial claim validation.
