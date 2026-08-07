"""TrustAnchor abstraction — provider-shaped external/local trust anchors.

Does not hardcode Sigstore/TSA/PQC into runtime verify paths.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from ..trust_layer import TrustIntegrityError, sha256_file, write_json_exclusive
from .ed25519_provider import ALGORITHM, Ed25519Verifier
from .interfaces import PublicKeyRecord


@runtime_checkable
class TrustAnchor(Protocol):
    """Future providers: local pin, transparency log, TSA, Sigstore, PQ anchors."""

    anchor_kind: str

    def verify_package_identity(
        self,
        *,
        public_key_id: str,
        public_key_raw: bytes,
        trust_material: dict[str, Any],
    ) -> None:
        """Fail-closed identity check for exported signer material."""


@dataclass(frozen=True)
class LocalPinnedTrustAnchor:
    """Offline pin: exported public key must match trust_anchor.json material."""

    anchor_kind: str = "local_pinned"

    def verify_package_identity(
        self,
        *,
        public_key_id: str,
        public_key_raw: bytes,
        trust_material: dict[str, Any],
    ) -> None:
        if trust_material.get("schema") != "amof.local_pinned_trust_anchor/v1":
            raise TrustIntegrityError(
                "unexpected local trust anchor schema",
                code="invalid_trust_anchor",
            )
        pinned_id = str(trust_material.get("public_key_id") or "").strip().lower()
        pinned_b64 = str(trust_material.get("public_key_raw_b64") or "").strip()
        if pinned_id != public_key_id.strip().lower():
            raise TrustIntegrityError(
                "pinned public_key_id mismatch",
                code="trust_anchor_mismatch",
            )
        try:
            pinned_raw = base64.b64decode(pinned_b64, validate=True)
        except Exception as exc:
            raise TrustIntegrityError(
                "pinned public key is not valid base64",
                code="invalid_trust_anchor",
            ) from exc
        if pinned_raw != public_key_raw:
            raise TrustIntegrityError(
                "pinned public key material mismatch (replaced key)",
                code="trust_anchor_mismatch",
            )
        fingerprint = hashlib.sha256(public_key_raw).hexdigest()
        if fingerprint != pinned_id:
            raise TrustIntegrityError(
                "public key fingerprint mismatch",
                code="key_id_mismatch",
            )


def build_local_pinned_trust_anchor(
    *,
    public_key_id: str,
    public_key_raw: bytes,
    algorithm: str | None = None,
) -> dict[str, Any]:
    from .algorithms import infer_algorithm_from_public_key_len, normalize_algorithm

    alg = (
        normalize_algorithm(algorithm)
        if algorithm
        else infer_algorithm_from_public_key_len(len(public_key_raw))
    )
    return {
        "schema": "amof.local_pinned_trust_anchor/v1",
        "anchor_kind": "local_pinned",
        "algorithm": alg,
        "public_key_id": public_key_id.strip().lower(),
        "public_key_fingerprint": hashlib.sha256(public_key_raw).hexdigest(),
        "public_key_raw_b64": base64.b64encode(public_key_raw).decode("ascii"),
        "network": False,
        "notes": (
            "Local pin only. Proves the export carries the signer public key "
            "that verified the signature at packaging time. Not a transparency proof."
        ),
    }


def write_local_pinned_trust_anchor(path: Path, material: dict[str, Any]) -> Path:
    write_json_exclusive(path, material)
    return path


def load_json_object(path: Path, *, code: str) -> dict[str, Any]:
    if not path.is_file():
        raise TrustIntegrityError(f"missing {path.name}", code=code)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustIntegrityError(f"{path.name} is not valid JSON", code=code) from exc
    if not isinstance(payload, dict):
        raise TrustIntegrityError(f"{path.name} must be an object", code=code)
    return payload


def verify_ed25519_detached(
    *,
    payload: bytes,
    signature_b64: str,
    public_key_raw: bytes,
    public_key_id: str,
) -> None:
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise TrustIntegrityError(
            "signature is not valid base64",
            code="invalid_signature",
        ) from exc
    Ed25519Verifier().verify(
        payload,
        signature,
        public_key=PublicKeyRecord(
            key_id=public_key_id,
            algorithm=ALGORITHM,
            public_key_raw=public_key_raw,
        ),
    )


def digest_hex_of_file(path: Path) -> str:
    return sha256_file(path)


def canonical_json_digest(payload: Mapping[str, Any]) -> str:
    raw = (
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
