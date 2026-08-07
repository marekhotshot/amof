"""Filesystem KeyProvider under AMOF runtime authority (local only)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..app_paths import ensure_app_roots, get_app_paths
from ..trust_layer import TrustIntegrityError
from .ed25519_provider import (
    ALGORITHM,
    ED25519_KEY_LEN,
    generate_ed25519_keypair,
    public_key_id_from_raw,
)
from .interfaces import PrivateKeyRecord, PublicKeyRecord
from .path_safety import assert_not_symlink, assert_private_mode, write_bytes_exclusive


def trust_authority_root() -> Path:
    """Keys + policy live under config_root/trust (runtime authority)."""
    ensure_app_roots()
    root = get_app_paths().config_root / "trust"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    assert_not_symlink(root, what="trust authority root")
    assert_private_mode(root, what="trust authority root")
    return root


def keys_dir() -> Path:
    path = trust_authority_root() / "keys"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    assert_not_symlink(path, what="trust keys directory")
    assert_private_mode(path, what="trust keys directory")
    return path


def _assert_key_material(raw: bytes, *, what: str) -> bytes:
    if len(raw) != ED25519_KEY_LEN:
        raise TrustIntegrityError(
            f"{what} must be exactly {ED25519_KEY_LEN} bytes (got {len(raw)})",
            code="malformed_key",
        )
    return raw


class FilesystemKeyProvider:
    """Local key store: <config>/trust/keys/<key_id>/{public,private}.raw + meta.json."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else keys_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        assert_not_symlink(self.root, what="trust keys directory")
        assert_private_mode(self.root, what="trust keys directory")

    def _key_dir(self, key_id: str) -> Path:
        kid = str(key_id or "").strip().lower()
        if len(kid) != 64 or any(ch not in "0123456789abcdef" for ch in kid):
            raise TrustIntegrityError(
                f"invalid key_id: {key_id!r}",
                code="invalid_key_id",
            )
        # Reject path traversal via key_id (already hex-only) and symlink dirs.
        key_dir = self.root / kid
        if ".." in kid or "/" in kid or "\\" in kid:
            raise TrustIntegrityError(
                f"invalid key_id: {key_id!r}",
                code="invalid_key_id",
            )
        if key_dir.exists():
            assert_not_symlink(key_dir, what="key directory")
        return key_dir

    def generate_keypair(self, *, algorithm: str = ALGORITHM) -> PrivateKeyRecord:
        if algorithm != ALGORITHM:
            raise TrustIntegrityError(
                f"unsupported key algorithm: {algorithm}",
                code="unsupported_algorithm",
            )
        record = generate_ed25519_keypair()
        key_dir = self._key_dir(record.key_id)
        if key_dir.exists():
            raise TrustIntegrityError(
                f"key already exists: {record.key_id}",
                code="key_exists",
            )
        key_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(key_dir, 0o700)
        assert_not_symlink(key_dir, what="key directory")
        pub_path = key_dir / "public.raw"
        priv_path = key_dir / "private.raw"
        meta_path = key_dir / "meta.json"
        write_bytes_exclusive(pub_path, record.public_key_raw, mode=0o600)
        write_bytes_exclusive(priv_path, record.private_key_raw, mode=0o600)
        meta = {
            "key_id": record.key_id,
            "algorithm": record.algorithm,
            "public_key_sha256": record.key_id,
        }
        write_bytes_exclusive(
            meta_path,
            (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )
        return record

    def get_private_key(self, key_id: str) -> PrivateKeyRecord:
        key_dir = self._key_dir(key_id)
        assert_not_symlink(key_dir, what="key directory")
        priv_path = key_dir / "private.raw"
        pub_path = key_dir / "public.raw"
        assert_not_symlink(priv_path, what="private key")
        assert_not_symlink(pub_path, what="public key")
        if not priv_path.is_file():
            raise TrustIntegrityError(
                f"missing private key: {key_id}",
                code="missing_key",
            )
        if not pub_path.is_file():
            raise TrustIntegrityError(
                f"missing public key for private key id: {key_id}",
                code="missing_key",
            )
        assert_private_mode(priv_path, what="private key")
        assert_private_mode(key_dir, what="key directory")
        private_raw = _assert_key_material(priv_path.read_bytes(), what="private key")
        public_raw = _assert_key_material(pub_path.read_bytes(), what="public key")
        if public_key_id_from_raw(public_raw) != key_id.strip().lower():
            raise TrustIntegrityError(
                f"public key id mismatch for {key_id}",
                code="key_id_mismatch",
            )
        return PrivateKeyRecord(
            key_id=key_id.strip().lower(),
            algorithm=ALGORITHM,
            private_key_raw=private_raw,
            public_key_raw=public_raw,
        )

    def get_public_key(self, key_id: str) -> PublicKeyRecord:
        key_dir = self._key_dir(key_id)
        assert_not_symlink(key_dir, what="key directory")
        pub_path = key_dir / "public.raw"
        assert_not_symlink(pub_path, what="public key")
        if not pub_path.is_file():
            raise TrustIntegrityError(
                f"missing public key: {key_id}",
                code="missing_key",
            )
        public_raw = _assert_key_material(pub_path.read_bytes(), what="public key")
        actual_id = public_key_id_from_raw(public_raw)
        if actual_id != key_id.strip().lower():
            raise TrustIntegrityError(
                f"public key id mismatch for {key_id}",
                code="key_id_mismatch",
            )
        return PublicKeyRecord(
            key_id=actual_id,
            algorithm=ALGORITHM,
            public_key_raw=public_raw,
        )

    def list_public_key_ids(self) -> list[str]:
        ids: list[str] = []
        if not self.root.is_dir():
            return ids
        assert_not_symlink(self.root, what="trust keys directory")
        for entry in sorted(self.root.iterdir()):
            if entry.is_symlink():
                continue
            if entry.is_dir() and (entry / "public.raw").is_file():
                if not (entry / "public.raw").is_symlink():
                    ids.append(entry.name)
        return ids

    def export_public_key_pem_or_raw(self, key_id: str) -> bytes:
        return self.get_public_key(key_id).public_key_raw

    def install_public_key(self, *, key_id: str, public_key_raw: bytes) -> PublicKeyRecord:
        """Install a public key only (rotation / verify-old-runs). No overwrite."""
        public_key_raw = _assert_key_material(public_key_raw, what="public key")
        actual_id = public_key_id_from_raw(public_key_raw)
        if actual_id != key_id.strip().lower():
            raise TrustIntegrityError(
                "install public key_id does not match key material",
                code="key_id_mismatch",
            )
        key_dir = self._key_dir(actual_id)
        if key_dir.exists():
            assert_not_symlink(key_dir, what="key directory")
        else:
            key_dir.mkdir(parents=True, exist_ok=False)
            os.chmod(key_dir, 0o700)
        pub_path = key_dir / "public.raw"
        if pub_path.exists():
            assert_not_symlink(pub_path, what="public key")
            existing = pub_path.read_bytes()
            if existing != public_key_raw:
                raise TrustIntegrityError(
                    f"refuse overwrite of public key: {actual_id}",
                    code="key_exists",
                )
            return PublicKeyRecord(
                key_id=actual_id,
                algorithm=ALGORITHM,
                public_key_raw=public_key_raw,
            )
        write_bytes_exclusive(pub_path, public_key_raw, mode=0o600)
        meta_path = key_dir / "meta.json"
        if not meta_path.exists():
            write_bytes_exclusive(
                meta_path,
                (
                    json.dumps(
                        {
                            "key_id": actual_id,
                            "algorithm": ALGORITHM,
                            "public_key_sha256": actual_id,
                            "public_only": True,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
                mode=0o600,
            )
        return PublicKeyRecord(
            key_id=actual_id,
            algorithm=ALGORITHM,
            public_key_raw=public_key_raw,
        )
