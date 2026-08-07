"""Algorithm-neutral Signer/Verifier/Keygen dispatch.

All Ed25519 / ML-DSA branching stays inside providers + this registry.
"""

from __future__ import annotations

from typing import Any

from ..trust_layer import TrustIntegrityError
from .algorithms import (
    ALGORITHM_ED25519,
    ALGORITHM_ML_DSA_65,
    SUPPORTED_SIGNING_ALGORITHMS,
    normalize_algorithm,
)
from .ed25519_provider import Ed25519Signer, Ed25519Verifier, generate_ed25519_keypair
from .interfaces import PrivateKeyRecord, Signer, Verifier
from .ml_dsa_provider import MLDSA65Signer, MLDSA65Verifier, generate_ml_dsa_65_keypair


def signer_for_algorithm(algorithm: str) -> Signer:
    alg = normalize_algorithm(algorithm)
    if alg == ALGORITHM_ED25519:
        return Ed25519Signer()
    if alg == ALGORITHM_ML_DSA_65:
        return MLDSA65Signer()
    raise TrustIntegrityError(
        f"unsupported signature algorithm: {algorithm}",
        code="unsupported_algorithm",
    )


def verifier_for_algorithm(algorithm: str) -> Verifier:
    alg = normalize_algorithm(algorithm)
    if alg == ALGORITHM_ED25519:
        return Ed25519Verifier()
    if alg == ALGORITHM_ML_DSA_65:
        return MLDSA65Verifier()
    raise TrustIntegrityError(
        f"unsupported signature algorithm: {algorithm}",
        code="unsupported_algorithm",
    )


def generate_keypair_for_algorithm(algorithm: str) -> PrivateKeyRecord:
    alg = normalize_algorithm(algorithm)
    if alg == ALGORITHM_ED25519:
        return generate_ed25519_keypair()
    if alg == ALGORITHM_ML_DSA_65:
        return generate_ml_dsa_65_keypair()
    raise TrustIntegrityError(
        f"unsupported key algorithm: {algorithm}",
        code="unsupported_algorithm",
    )


def assert_supported_signing_algorithm(algorithm: str) -> str:
    alg = normalize_algorithm(algorithm)
    if alg not in SUPPORTED_SIGNING_ALGORITHMS:
        raise TrustIntegrityError(
            f"unsupported signature algorithm: {algorithm}",
            code="unsupported_algorithm",
        )
    return alg


def provider_status() -> dict[str, Any]:
    """Diagnostic: which signing providers are importable/usable."""
    status: dict[str, Any] = {
        ALGORITHM_ED25519: {"available": True, "class": "CLASSICAL"},
        ALGORITHM_ML_DSA_65: {"available": False, "class": "PQC"},
    }
    try:
        generate_ml_dsa_65_keypair()
        status[ALGORITHM_ML_DSA_65]["available"] = True
    except TrustIntegrityError as exc:
        status[ALGORITHM_ML_DSA_65]["error"] = str(exc)
    return status
