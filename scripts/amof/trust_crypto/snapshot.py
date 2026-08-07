"""Immutable TRUST_AT_FINALIZATION snapshot for exportable packages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..trust_layer import TrustIntegrityError, utc_now, write_json_exclusive
from .anchors import canonical_json_digest
from .policy import TrustPolicy


SNAPSHOT_SCHEMA = "amof.trust_snapshot/v1"
SNAPSHOT_FILENAME = "trust_snapshot.json"


def policy_digest(policy: TrustPolicy) -> str:
    payload = {
        "schema": "amof.trust_policy/v1",
        "allowed_key_ids": sorted(policy.allowed_key_ids),
        "revoked_key_ids": sorted(policy.revoked_key_ids),
        "preferred_key_id": policy.preferred_key_id,
        "require_signatures": policy.require_signatures,
        "allow_unknown_keys": policy.allow_unknown_keys,
        "allow_unsigned": policy.allow_unsigned,
    }
    return canonical_json_digest(payload)


def build_trust_snapshot(
    *,
    run_id: str,
    policy: TrustPolicy,
    public_key_id: str,
    public_key_fingerprint: str,
    manifest_digest: str,
    evidence_digest: str,
    signature_digest: str,
    finalized_at: str | None = None,
) -> dict[str, Any]:
    kid = public_key_id.strip().lower()
    revoked = kid in policy.revoked_key_ids
    allowed = kid in policy.allowed_key_ids
    if revoked:
        decision = "REVOKED"
    elif allowed:
        decision = "ALLOWED"
    else:
        decision = "UNKNOWN"
    return {
        "schema": SNAPSHOT_SCHEMA,
        "kind": "TRUST_AT_FINALIZATION",
        "run_id": run_id,
        "trusted_key_id": kid,
        "public_key_fingerprint": public_key_fingerprint.strip().lower(),
        "trust_decision": decision,
        "policy_schema": "amof.trust_policy/v1",
        "policy_digest": policy_digest(policy),
        "revoked_at_finalization": revoked,
        "allowed_at_finalization": allowed,
        "require_signatures_at_finalization": policy.require_signatures,
        "finalized_at": finalized_at or utc_now(),
        "manifest_digest": manifest_digest.strip().lower(),
        "evidence_digest": evidence_digest.strip().lower(),
        "signature_digest": signature_digest.strip().lower(),
        "notes": (
            "TRUST_AT_FINALIZATION records the trust decision at packaging/finalization. "
            "It does not prove future non-revocation (TRUST_NOW is separate)."
        ),
    }


def write_trust_snapshot(path: Path, snapshot: dict[str, Any]) -> Path:
    write_json_exclusive(path, snapshot)
    return path


def verify_trust_snapshot(
    snapshot: dict[str, Any],
    *,
    run_id: str,
    public_key_id: str,
    public_key_fingerprint: str,
    manifest_digest: str,
    evidence_digest: str,
    signature_digest: str,
) -> dict[str, Any]:
    if snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise TrustIntegrityError(
            "unexpected trust_snapshot schema",
            code="invalid_trust_snapshot",
        )
    if snapshot.get("kind") != "TRUST_AT_FINALIZATION":
        raise TrustIntegrityError(
            "trust_snapshot kind must be TRUST_AT_FINALIZATION",
            code="invalid_trust_snapshot",
        )
    if str(snapshot.get("run_id") or "") != run_id:
        raise TrustIntegrityError("trust_snapshot run_id mismatch", code="snapshot_mismatch")
    if str(snapshot.get("trusted_key_id") or "").lower() != public_key_id.lower():
        raise TrustIntegrityError(
            "trust_snapshot trusted_key_id mismatch",
            code="snapshot_mismatch",
        )
    if str(snapshot.get("public_key_fingerprint") or "").lower() != public_key_fingerprint.lower():
        raise TrustIntegrityError(
            "trust_snapshot public_key_fingerprint mismatch",
            code="snapshot_mismatch",
        )
    if str(snapshot.get("manifest_digest") or "").lower() != manifest_digest.lower():
        raise TrustIntegrityError(
            "trust_snapshot manifest_digest mismatch",
            code="snapshot_mismatch",
        )
    if str(snapshot.get("evidence_digest") or "").lower() != evidence_digest.lower():
        raise TrustIntegrityError(
            "trust_snapshot evidence_digest mismatch",
            code="snapshot_mismatch",
        )
    if str(snapshot.get("signature_digest") or "").lower() != signature_digest.lower():
        raise TrustIntegrityError(
            "trust_snapshot signature_digest mismatch",
            code="snapshot_mismatch",
        )
    if snapshot.get("trust_decision") != "ALLOWED":
        raise TrustIntegrityError(
            f"trust_snapshot decision is not ALLOWED: {snapshot.get('trust_decision')!r}",
            code="snapshot_untrusted",
        )
    if bool(snapshot.get("revoked_at_finalization")):
        raise TrustIntegrityError(
            "trust_snapshot records revoked_at_finalization",
            code="snapshot_untrusted",
        )
    if not bool(snapshot.get("allowed_at_finalization")):
        raise TrustIntegrityError(
            "trust_snapshot records key not allowed at finalization",
            code="snapshot_untrusted",
        )
    return {"ok": True, "kind": "TRUST_AT_FINALIZATION", "trust_decision": "ALLOWED"}


def evaluate_trust_now(
    *,
    public_key_id: str,
    policy: TrustPolicy | None,
) -> dict[str, Any]:
    """Separate from TRUST_AT_FINALIZATION — current mutable policy view."""
    if policy is None:
        return {
            "status": "SKIPPED",
            "reason": "no current trust-policy available in this environment",
        }
    kid = public_key_id.strip().lower()
    if kid in policy.revoked_key_ids:
        return {"status": "REVOKED", "public_key_id": kid}
    if kid in policy.allowed_key_ids:
        return {"status": "ALLOWED", "public_key_id": kid}
    return {"status": "UNKNOWN", "public_key_id": kid}
