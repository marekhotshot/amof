"""Trust Layer Wave 003 — local cryptographic identity (Ed25519)."""

from .bundle_sign import (
    SIGNATURE_FILENAME,
    sign_evidence_bundle,
    verify_bundle_signature,
)
from .ed25519_provider import Ed25519Signer, Ed25519Verifier
from .filesystem_keys import FilesystemKeyProvider, trust_authority_root
from .interfaces import KeyProvider, PrivateKeyRecord, PublicKeyRecord, Signer, Verifier
from .policy import (
    TrustPolicy,
    enroll_key,
    load_trust_policy,
    revoke_key,
    write_trust_policy,
)

__all__ = [
    "Ed25519Signer",
    "Ed25519Verifier",
    "FilesystemKeyProvider",
    "KeyProvider",
    "PrivateKeyRecord",
    "PublicKeyRecord",
    "SIGNATURE_FILENAME",
    "Signer",
    "TrustPolicy",
    "Verifier",
    "enroll_key",
    "load_trust_policy",
    "revoke_key",
    "sign_evidence_bundle",
    "trust_authority_root",
    "verify_bundle_signature",
    "write_trust_policy",
]
