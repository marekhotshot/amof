"""Trust Layer Wave 003 — signer / verifier / key-provider abstractions.

Algorithm-specific logic lives only inside concrete providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PublicKeyRecord:
    key_id: str
    algorithm: str
    public_key_raw: bytes


@dataclass(frozen=True)
class PrivateKeyRecord:
    key_id: str
    algorithm: str
    private_key_raw: bytes
    public_key_raw: bytes


@dataclass(frozen=True)
class SignatureResult:
    algorithm: str
    public_key_id: str
    signature: bytes
    signed_payload: bytes


@runtime_checkable
class Signer(Protocol):
    algorithm: str

    def sign(self, payload: bytes, *, private_key: PrivateKeyRecord) -> SignatureResult:
        """Sign opaque payload bytes."""


@runtime_checkable
class Verifier(Protocol):
    algorithm: str

    def verify(
        self,
        payload: bytes,
        signature: bytes,
        *,
        public_key: PublicKeyRecord,
    ) -> None:
        """Verify signature; raise on failure."""


@runtime_checkable
class KeyProvider(Protocol):
    def generate_keypair(self, *, algorithm: str = "ed25519") -> PrivateKeyRecord:
        """Create and persist a new local keypair."""

    def get_private_key(self, key_id: str) -> PrivateKeyRecord:
        """Load a private key by id (signing)."""

    def get_public_key(self, key_id: str) -> PublicKeyRecord:
        """Load a public key by id (verification)."""

    def list_public_key_ids(self) -> list[str]:
        """Return known public key ids (for rotation)."""

    def export_public_key_pem_or_raw(self, key_id: str) -> bytes:
        """Return public key material for trust-policy enrollment."""
