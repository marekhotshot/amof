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
ED25519_KEY_LEN = 32

# Ed25519 field prime (RFC 8032). Canonical encodings require 0 <= y < p.
_ED25519_FIELD_P = (1 << 255) - 19

# Canonical encodings of the 8 torsion / small-order points on edwards25519.
# Rejected before verify: identity (and other low-order) pubs admit universal
# signatures under the cofactorless equation used by common libraries (BL3-1).
_ED25519_LOW_ORDER_PUBLIC_KEYS = frozenset(
    {
        bytes.fromhex(
            "0100000000000000000000000000000000000000000000000000000000000000"
        ),
        bytes.fromhex(
            "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f"
        ),
        bytes.fromhex(
            "0000000000000000000000000000000000000000000000000000000000000000"
        ),
        bytes.fromhex(
            "0000000000000000000000000000000000000000000000000000000000000080"
        ),
        bytes.fromhex(
            "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"
        ),
        bytes.fromhex(
            "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"
        ),
        bytes.fromhex(
            "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa"
        ),
        bytes.fromhex(
            "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85"
        ),
    }
)


def assert_canonical_ed25519_public_key(public_key_raw: bytes) -> bytes:
    """Reject low-order and non-canonical Ed25519 public-key encodings (BL3-1).

    Non-canonical means the encoded y-coordinate is not reduced mod p (y >= p).
    Low-order means one of the eight torsion subgroup encodings (including the
    identity point), which otherwise yield universal signatures.
    """
    if not isinstance(public_key_raw, (bytes, bytearray)) or len(public_key_raw) != ED25519_KEY_LEN:
        raise TrustIntegrityError(
            f"public key must be exactly {ED25519_KEY_LEN} bytes",
            code="malformed_key",
        )
    raw = bytes(public_key_raw)
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= _ED25519_FIELD_P:
        raise TrustIntegrityError(
            "ed25519 public key encoding is non-canonical (y >= p)",
            code="noncanonical_public_key",
        )
    if raw in _ED25519_LOW_ORDER_PUBLIC_KEYS:
        raise TrustIntegrityError(
            "ed25519 public key has low order (torsion/identity)",
            code="low_order_public_key",
        )
    return raw


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
        if len(private_key.private_key_raw) != ED25519_KEY_LEN:
            raise TrustIntegrityError(
                f"private key must be exactly {ED25519_KEY_LEN} bytes",
                code="malformed_key",
            )
        try:
            key = Ed25519PrivateKey.from_private_bytes(private_key.private_key_raw)
        except Exception as exc:
            raise TrustIntegrityError(
                "malformed ed25519 private key",
                code="malformed_key",
            ) from exc
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
        raw = assert_canonical_ed25519_public_key(public_key.public_key_raw)
        try:
            key = Ed25519PublicKey.from_public_bytes(raw)
        except Exception as exc:
            raise TrustIntegrityError(
                "malformed ed25519 public key",
                code="malformed_key",
            ) from exc
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
