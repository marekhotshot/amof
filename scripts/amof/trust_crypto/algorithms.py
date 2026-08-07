"""Algorithm identifiers and classes for Trust Layer signature providers.

CLASSICAL / PQC are documentation classes only — not separate trust systems.
"""

from __future__ import annotations

from ..trust_layer import TrustIntegrityError

# Canonical algorithm ids written into signature.json / key meta.
ALGORITHM_ED25519 = "ed25519"
ALGORITHM_ML_DSA_65 = "ml-dsa-65"

ALGORITHM_CLASS_CLASSICAL = "CLASSICAL"
ALGORITHM_CLASS_PQC = "PQC"

# Alias → canonical
_ALIASES: dict[str, str] = {
    "ed25519": ALGORITHM_ED25519,
    "eddsa": ALGORITHM_ED25519,
    "ml-dsa": ALGORITHM_ML_DSA_65,
    "ml-dsa-65": ALGORITHM_ML_DSA_65,
    "mldsa": ALGORITHM_ML_DSA_65,
    "mldsa65": ALGORITHM_ML_DSA_65,
    "mldsa-65": ALGORITHM_ML_DSA_65,
}

# FIPS 204 ML-DSA-65 sizes (cryptography public_bytes_raw / signature).
# Private material stored as 32-byte seed (private_bytes_raw / from_seed_bytes).
ED25519_PUBLIC_KEY_LEN = 32
ED25519_PRIVATE_KEY_LEN = 32
ED25519_SIGNATURE_LEN = 64

ML_DSA_65_PUBLIC_KEY_LEN = 1952
ML_DSA_65_PRIVATE_SEED_LEN = 32
ML_DSA_65_SIGNATURE_LEN = 3309

SUPPORTED_SIGNING_ALGORITHMS = frozenset({ALGORITHM_ED25519, ALGORITHM_ML_DSA_65})


def normalize_algorithm(algorithm: str | None) -> str:
    raw = str(algorithm or "").strip().lower()
    if not raw:
        raise TrustIntegrityError(
            "missing signature algorithm",
            code="unsupported_algorithm",
        )
    canonical = _ALIASES.get(raw)
    if canonical is None:
        raise TrustIntegrityError(
            f"unsupported signature algorithm: {algorithm}",
            code="unsupported_algorithm",
        )
    return canonical


def algorithm_class(algorithm: str) -> str:
    alg = normalize_algorithm(algorithm)
    if alg == ALGORITHM_ED25519:
        return ALGORITHM_CLASS_CLASSICAL
    if alg == ALGORITHM_ML_DSA_65:
        return ALGORITHM_CLASS_PQC
    raise TrustIntegrityError(
        f"unsupported signature algorithm: {algorithm}",
        code="unsupported_algorithm",
    )


def public_key_len(algorithm: str) -> int:
    alg = normalize_algorithm(algorithm)
    if alg == ALGORITHM_ED25519:
        return ED25519_PUBLIC_KEY_LEN
    if alg == ALGORITHM_ML_DSA_65:
        return ML_DSA_65_PUBLIC_KEY_LEN
    raise TrustIntegrityError(
        f"unsupported signature algorithm: {algorithm}",
        code="unsupported_algorithm",
    )


def private_key_len(algorithm: str) -> int:
    alg = normalize_algorithm(algorithm)
    if alg == ALGORITHM_ED25519:
        return ED25519_PRIVATE_KEY_LEN
    if alg == ALGORITHM_ML_DSA_65:
        return ML_DSA_65_PRIVATE_SEED_LEN
    raise TrustIntegrityError(
        f"unsupported signature algorithm: {algorithm}",
        code="unsupported_algorithm",
    )


def infer_algorithm_from_public_key_len(length: int) -> str:
    if length == ED25519_PUBLIC_KEY_LEN:
        return ALGORITHM_ED25519
    if length == ML_DSA_65_PUBLIC_KEY_LEN:
        return ALGORITHM_ML_DSA_65
    raise TrustIntegrityError(
        f"cannot infer algorithm from public key length {length}",
        code="malformed_key",
    )


def authenticity_label(algorithm: str) -> str:
    alg = normalize_algorithm(algorithm)
    if alg == ALGORITHM_ED25519:
        return "Ed25519 signature over digests"
    if alg == ALGORITHM_ML_DSA_65:
        return "ML-DSA-65 signature over digests"
    return f"{alg} signature over digests"
