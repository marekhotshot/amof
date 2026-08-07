"""ML-DSA-65 Signer/Verifier provider (FIPS 204 via pyca/cryptography)."""

from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.mldsa import (
    MLDSA65PrivateKey,
    MLDSA65PublicKey,
)

from ..trust_layer import TrustIntegrityError
from .algorithms import (
    ALGORITHM_ML_DSA_65,
    ML_DSA_65_PRIVATE_SEED_LEN,
    ML_DSA_65_PUBLIC_KEY_LEN,
    ML_DSA_65_SIGNATURE_LEN,
)
from .interfaces import PrivateKeyRecord, PublicKeyRecord, SignatureResult


ALGORITHM = ALGORITHM_ML_DSA_65


def public_key_id_from_raw(public_key_raw: bytes) -> str:
    """Stable key id: sha256(raw_public_key) hex (full 64 chars)."""
    return hashlib.sha256(public_key_raw).hexdigest()


def generate_ml_dsa_65_keypair() -> PrivateKeyRecord:
    try:
        private = MLDSA65PrivateKey.generate()
    except UnsupportedAlgorithm as exc:
        raise TrustIntegrityError(
            "ML-DSA-65 unavailable in cryptography backend "
            "(requires OpenSSL 3.5+ / cryptography>=47 with PQ support)",
            code="unsupported_algorithm",
        ) from exc
    # Store FIPS 204 seed form (32 bytes); reload via from_seed_bytes.
    private_raw = private.private_bytes_raw()
    public_raw = private.public_key().public_bytes_raw()
    if len(private_raw) != ML_DSA_65_PRIVATE_SEED_LEN:
        raise TrustIntegrityError(
            f"unexpected ML-DSA-65 seed length: {len(private_raw)}",
            code="malformed_key",
        )
    if len(public_raw) != ML_DSA_65_PUBLIC_KEY_LEN:
        raise TrustIntegrityError(
            f"unexpected ML-DSA-65 public key length: {len(public_raw)}",
            code="malformed_key",
        )
    key_id = public_key_id_from_raw(public_raw)
    return PrivateKeyRecord(
        key_id=key_id,
        algorithm=ALGORITHM,
        private_key_raw=private_raw,
        public_key_raw=public_raw,
    )


class MLDSA65Signer:
    algorithm = ALGORITHM

    def sign(self, payload: bytes, *, private_key: PrivateKeyRecord) -> SignatureResult:
        if private_key.algorithm != ALGORITHM:
            raise TrustIntegrityError(
                f"signer algorithm mismatch: {private_key.algorithm}",
                code="algorithm_mismatch",
            )
        if len(private_key.private_key_raw) != ML_DSA_65_PRIVATE_SEED_LEN:
            raise TrustIntegrityError(
                f"ML-DSA-65 private seed must be exactly {ML_DSA_65_PRIVATE_SEED_LEN} bytes",
                code="malformed_key",
            )
        try:
            key = MLDSA65PrivateKey.from_seed_bytes(private_key.private_key_raw)
        except Exception as exc:
            raise TrustIntegrityError(
                "malformed ml-dsa-65 private key",
                code="malformed_key",
            ) from exc
        try:
            signature = key.sign(payload)
        except UnsupportedAlgorithm as exc:
            raise TrustIntegrityError(
                "ML-DSA-65 signing unsupported by cryptography backend",
                code="unsupported_algorithm",
            ) from exc
        if len(signature) != ML_DSA_65_SIGNATURE_LEN:
            raise TrustIntegrityError(
                f"unexpected ML-DSA-65 signature length: {len(signature)}",
                code="invalid_signature",
            )
        return SignatureResult(
            algorithm=ALGORITHM,
            public_key_id=private_key.key_id,
            signature=signature,
            signed_payload=payload,
        )


class MLDSA65Verifier:
    algorithm = ALGORITHM

    def verify(
        self,
        payload: bytes,
        signature: bytes,
        *,
        public_key: PublicKeyRecord,
    ) -> None:
        if public_key.algorithm != ALGORITHM:
            raise TrustIntegrityError(
                f"verifier algorithm mismatch: {public_key.algorithm}",
                code="algorithm_mismatch",
            )
        if len(public_key.public_key_raw) != ML_DSA_65_PUBLIC_KEY_LEN:
            raise TrustIntegrityError(
                f"ML-DSA-65 public key must be exactly {ML_DSA_65_PUBLIC_KEY_LEN} bytes",
                code="malformed_key",
            )
        if len(signature) != ML_DSA_65_SIGNATURE_LEN:
            raise TrustIntegrityError(
                f"ML-DSA-65 signature must be exactly {ML_DSA_65_SIGNATURE_LEN} bytes",
                code="invalid_signature",
            )
        try:
            key = MLDSA65PublicKey.from_public_bytes(public_key.public_key_raw)
        except Exception as exc:
            raise TrustIntegrityError(
                "malformed ml-dsa-65 public key",
                code="malformed_key",
            ) from exc
        try:
            key.verify(signature, payload)
        except InvalidSignature as exc:
            raise TrustIntegrityError(
                "ml-dsa-65 signature verification failed",
                code="signature_invalid",
            ) from exc
        except UnsupportedAlgorithm as exc:
            raise TrustIntegrityError(
                "ML-DSA-65 verification unsupported by cryptography backend",
                code="unsupported_algorithm",
            ) from exc
