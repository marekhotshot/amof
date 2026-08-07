"""Sign and verify canonical evidence bundles (hashes remain canonical)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ..trust_layer import (
    BUNDLE_MANIFEST_FILE,
    TrustIntegrityError,
    sha256_file,
    utc_now,
    write_json_exclusive,
)
from .filesystem_keys import FilesystemKeyProvider
from .interfaces import KeyProvider, PrivateKeyRecord
from .policy import TrustPolicy, load_trust_policy
from .registry import assert_supported_signing_algorithm, signer_for_algorithm, verifier_for_algorithm


SIGNATURE_SCHEMA = "amof.bundle_signature/v1"
SIGNATURE_FILENAME = "signature.json"
SIGNATURE_VERSION = 1

# Backward-compatible alias (Wave 003 tests / imports).
ALGORITHM = "ed25519"


def canonical_signed_payload(
    *,
    manifest_digest: str,
    evidence_digest: str,
    version: int = SIGNATURE_VERSION,
) -> bytes:
    """Deterministic bytes that the signature authenticates (digests only)."""
    payload = {
        "evidence_digest": str(evidence_digest).strip().lower(),
        "manifest_digest": str(manifest_digest).strip().lower(),
        "version": int(version),
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _resolve_signing_key(
    provider: KeyProvider,
    policy: TrustPolicy,
    *,
    key_id: str | None = None,
) -> PrivateKeyRecord:
    preferred = str(key_id or policy.preferred_key_id or "").strip().lower() or None
    if preferred:
        policy.assert_key_usable(preferred)
        return provider.get_private_key(preferred)
    # Auto-generate and enroll is caller's job; here fail closed if no preferred key.
    ids = provider.list_public_key_ids()
    for kid in ids:
        try:
            policy.assert_key_usable(kid)
            return provider.get_private_key(kid)
        except TrustIntegrityError:
            continue
    raise TrustIntegrityError(
        "no usable signing key; run amof trust keygen",
        code="missing_key",
    )


def sign_evidence_bundle(
    bundle_dir: Path | str,
    *,
    key_provider: KeyProvider | None = None,
    policy: TrustPolicy | None = None,
    key_id: str | None = None,
) -> dict[str, Any]:
    """Write signature.json authenticating manifest + evidence digests."""
    root = Path(bundle_dir)
    manifest_path = root / BUNDLE_MANIFEST_FILE
    evidence_path = root / "evidence.json"
    if not manifest_path.is_file() or not evidence_path.is_file():
        raise TrustIntegrityError(
            "bundle missing manifest.json or evidence.json",
            code="missing_file",
        )
    sig_path = root / SIGNATURE_FILENAME
    if sig_path.exists():
        raise TrustIntegrityError(
            f"refuse overwrite of existing signature: {sig_path}",
            code="signature_exists",
        )

    provider = key_provider or FilesystemKeyProvider()
    trust_policy = policy if policy is not None else load_trust_policy()
    private_key = _resolve_signing_key(provider, trust_policy, key_id=key_id)

    manifest_digest = sha256_file(manifest_path)
    evidence_digest = sha256_file(evidence_path)
    payload = canonical_signed_payload(
        manifest_digest=manifest_digest,
        evidence_digest=evidence_digest,
    )
    signer = signer_for_algorithm(private_key.algorithm)
    signed = signer.sign(payload, private_key=private_key)

    signature_obj = {
        "schema": SIGNATURE_SCHEMA,
        "version": SIGNATURE_VERSION,
        "algorithm": signed.algorithm,
        "public_key_id": signed.public_key_id,
        "public_key_fingerprint": signed.public_key_id,
        "signature": base64.b64encode(signed.signature).decode("ascii"),
        "manifest_digest": manifest_digest,
        "evidence_digest": evidence_digest,
        "timestamp": utc_now(),
    }
    write_json_exclusive(sig_path, signature_obj)
    return signature_obj


def verify_bundle_signature(
    bundle_dir: Path | str,
    *,
    key_provider: KeyProvider | None = None,
    policy: TrustPolicy | None = None,
) -> dict[str, Any]:
    """Fail-closed signature verification against policy + public keys."""
    root = Path(bundle_dir)
    sig_path = root / SIGNATURE_FILENAME
    trust_policy = policy if policy is not None else load_trust_policy()
    provider = key_provider or FilesystemKeyProvider()

    if not sig_path.is_file():
        if trust_policy.require_signatures or not trust_policy.allow_unsigned:
            raise TrustIntegrityError(
                "missing signature.json",
                code="missing_signature",
            )
        return {"ok": True, "signed": False}

    try:
        signature_obj = json.loads(sig_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustIntegrityError(
            "signature.json is not valid JSON",
            code="invalid_signature",
        ) from exc
    if not isinstance(signature_obj, dict):
        raise TrustIntegrityError("signature.json must be an object", code="invalid_signature")
    if signature_obj.get("schema") != SIGNATURE_SCHEMA:
        raise TrustIntegrityError(
            f"unexpected signature schema: {signature_obj.get('schema')!r}",
            code="invalid_signature",
        )

    algorithm = assert_supported_signing_algorithm(
        str(signature_obj.get("algorithm") or "")
    )
    key_id = str(signature_obj.get("public_key_id") or "").strip().lower()
    fingerprint = str(signature_obj.get("public_key_fingerprint") or "").strip().lower()
    manifest_digest = str(signature_obj.get("manifest_digest") or "").strip().lower()
    evidence_digest = str(signature_obj.get("evidence_digest") or "").strip().lower()
    version = int(signature_obj.get("version") or 0)
    sig_b64 = str(signature_obj.get("signature") or "").strip()

    if version != SIGNATURE_VERSION:
        raise TrustIntegrityError(
            f"unsupported signature version: {version}",
            code="invalid_signature",
        )
    if not sig_b64:
        raise TrustIntegrityError("signature missing", code="invalid_signature")
    # Backward compatible: Wave 003 signatures omit public_key_fingerprint.
    if fingerprint and fingerprint != key_id:
        raise TrustIntegrityError(
            "public_key_fingerprint does not match public_key_id",
            code="key_id_mismatch",
        )

    trust_policy.assert_key_usable(key_id)
    public_key = provider.get_public_key(key_id)
    if public_key.algorithm != algorithm:
        raise TrustIntegrityError(
            f"algorithm/key mismatch: signature={algorithm} key={public_key.algorithm}",
            code="algorithm_mismatch",
        )

    actual_manifest = sha256_file(root / BUNDLE_MANIFEST_FILE)
    actual_evidence = sha256_file(root / "evidence.json")
    if actual_manifest != manifest_digest:
        raise TrustIntegrityError(
            "manifest_digest mismatch versus signature.json",
            code="manifest_digest_mismatch",
        )
    if actual_evidence != evidence_digest:
        raise TrustIntegrityError(
            "evidence_digest / provenance digest mismatch versus signature.json",
            code="evidence_digest_mismatch",
        )

    try:
        signature = base64.b64decode(sig_b64, validate=True)
    except Exception as exc:
        raise TrustIntegrityError(
            "signature is not valid base64",
            code="invalid_signature",
        ) from exc

    payload = canonical_signed_payload(
        manifest_digest=manifest_digest,
        evidence_digest=evidence_digest,
        version=version,
    )
    verifier = verifier_for_algorithm(algorithm)
    verifier.verify(payload, signature, public_key=public_key)
    return {
        "ok": True,
        "signed": True,
        "public_key_id": key_id,
        "algorithm": algorithm,
        "manifest_digest": manifest_digest,
        "evidence_digest": evidence_digest,
    }
