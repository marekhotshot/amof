"""Ed25519 Signer/Verifier provider (local deterministic only)."""

from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from ..trust_layer import TrustIntegrityError
from .interfaces import PrivateKeyRecord, PublicKeyRecord, SignatureResult


ALGORITHM = "ed25519"


def public_key_id_from_raw(public_key_raw: bytes) -> str:
    """Stable key id: sha256(raw_public_key) hex (full 64 chars)."""
    return hashlib.sha256(public_key_raw).hexdigest()


def generate_ed25519_keypair() -> PrivateKeyRecord:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    public_raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_id = public_key_id_from_raw(public_raw)
    return PrivateKeyRecord(
        key_id=key_id,
        algorithm=ALGORITHM,
        private_key_raw=private_raw,
        public_key_raw=public_raw,
    )


class Ed25519Signer:
    algorithm = ALGORITHM

    def sign(self, payload: bytes, *, private_key: PrivateKeyRecord) -> SignatureResult:
        if private_key.algorithm != ALGORITHM:
            raise TrustIntegrityError(
                f"signer algorithm mismatch: {private_key.algorithm}",
                code="algorithm_mismatch",
            )
        key = Ed25519PrivateKey.from_private_bytes(private_key.private_key_raw)
        signature = key.sign(payload)
        return SignatureResult(
            algorithm=ALGORITHM,
            public_key_id=private_key.key_id,
            signature=signature,
            signed_payload=payload,
        )


class Ed25519Verifier:
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
        key = Ed25519PublicKey.from_public_bytes(public_key.public_key_raw)
        try:
            key.verify(signature, payload)
        except InvalidSignature as exc:
            raise TrustIntegrityError(
                "ed25519 signature verification failed",
                code="signature_invalid",
            ) from exc


def signer_for_algorithm(algorithm: str) -> Ed25519Signer:
    if algorithm != ALGORITHM:
        raise TrustIntegrityError(
            f"unsupported signature algorithm: {algorithm}",
            code="unsupported_algorithm",
        )
    return Ed25519Signer()


def verifier_for_algorithm(algorithm: str) -> Ed25519Verifier:
    if algorithm != ALGORITHM:
        raise TrustIntegrityError(
            f"unsupported signature algorithm: {algorithm}",
            code="unsupported_algorithm",
        )
    return Ed25519Verifier()
