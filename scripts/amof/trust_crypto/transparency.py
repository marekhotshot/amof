"""Append-only transparency anchor (P1 MVP).

Selected over Sigstore/Rekor public network and RFC3161 TSA because:
- Mission forbids certificate PKI / Fulcio-style chains.
- Offline verification must not require network.
- Binding semantics match Rekor hashedrekord (digests + key id) with a
  local append-only log and checkpoint signature.

This is transparency evidence for a binding, not immutable global truth.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..trust_layer import TrustIntegrityError, utc_now, write_json_exclusive
from .anchors import canonical_json_digest, verify_ed25519_detached
from .ed25519_provider import ALGORITHM, generate_ed25519_keypair
from .filesystem_keys import trust_authority_root
from .path_safety import assert_not_symlink, assert_private_mode, write_bytes_exclusive


EXTERNAL_ANCHOR_SCHEMA = "amof.external_anchor/v1"
EXTERNAL_ANCHOR_FILENAME = "external_anchor.json"
ANCHOR_KIND = "append_only_hashedrekord"
LOG_ORIGIN = "amof-local-tlog/v1"


def _leaf_hash(body_digest: str) -> bytes:
    # Domain-separated leaf: 0x00 || sha256(body_digest ascii)
    return hashlib.sha256(b"\x00" + body_digest.encode("ascii")).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _merkle_root(leaves: list[bytes]) -> bytes:
    if not leaves:
        return hashlib.sha256(b"").digest()
    level = list(leaves)
    while len(level) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            if i + 1 == len(level):
                nxt.append(level[i])
            else:
                nxt.append(_node_hash(level[i], level[i + 1]))
        level = nxt
    return level[0]


def _inclusion_proof(leaves: list[bytes], index: int) -> list[str]:
    """Return sibling hashes (hex) from leaf to root."""
    if index < 0 or index >= len(leaves):
        raise TrustIntegrityError("log index out of range", code="invalid_anchor")
    proof: list[str] = []
    level = list(leaves)
    idx = index
    while len(level) > 1:
        if idx % 2 == 0:
            sibling = level[idx + 1] if idx + 1 < len(level) else None
        else:
            sibling = level[idx - 1]
        if sibling is not None:
            proof.append(sibling.hex())
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            if i + 1 == len(level):
                nxt.append(level[i])
            else:
                nxt.append(_node_hash(level[i], level[i + 1]))
        level = nxt
        idx //= 2
    return proof


def _verify_inclusion(
    *,
    leaf: bytes,
    index: int,
    tree_size: int,
    proof_hex: list[str],
    root: bytes,
) -> None:
    if tree_size <= 0 or index < 0 or index >= tree_size:
        raise TrustIntegrityError("invalid inclusion index/tree_size", code="anchor_invalid")
    current = leaf
    idx = index
    # Reconstruct using proof; when odd last node has no sibling, proof omits it.
    # Walk with knowledge of tree_size at each level.
    size = tree_size
    proof_i = 0
    while size > 1:
        if idx % 2 == 1:
            if proof_i >= len(proof_hex):
                raise TrustIntegrityError("inclusion proof too short", code="anchor_invalid")
            sibling = bytes.fromhex(proof_hex[proof_i])
            proof_i += 1
            current = _node_hash(sibling, current)
        else:
            if idx + 1 < size:
                if proof_i >= len(proof_hex):
                    raise TrustIntegrityError("inclusion proof too short", code="anchor_invalid")
                sibling = bytes.fromhex(proof_hex[proof_i])
                proof_i += 1
                current = _node_hash(current, sibling)
            # else: last unpaired node — no sibling
        idx //= 2
        size = (size + 1) // 2
    if proof_i != len(proof_hex):
        raise TrustIntegrityError("inclusion proof too long", code="anchor_invalid")
    if current != root:
        raise TrustIntegrityError(
            "inclusion proof does not match checkpoint root",
            code="anchor_invalid",
        )


def tlog_root() -> Path:
    root = trust_authority_root() / "tlog"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    assert_not_symlink(root, what="tlog root")
    return root


def init_transparency_log(*, root: Path | None = None) -> dict[str, Any]:
    """Explicit authority action: create local tlog checkpoint signing identity.

    Must be run via `amof trust tlog-init` before export can emit EXTERNAL_ANCHOR.
    Never called as a side effect of export/finalize.
    """
    log_root = root if root is not None else tlog_root()
    log_root.mkdir(parents=True, exist_ok=True)
    os.chmod(log_root, 0o700)
    assert_not_symlink(log_root, what="tlog root")
    pub = log_root / "log-public.raw"
    priv = log_root / "log-private.raw"
    meta_path = log_root / "log-meta.json"
    if pub.exists() or priv.exists() or meta_path.exists():
        raise TrustIntegrityError(
            "tlog identity already exists; refuse overwrite",
            code="tlog_exists",
        )
    record = generate_ed25519_keypair()
    write_bytes_exclusive(pub, record.public_key_raw, mode=0o600)
    write_bytes_exclusive(priv, record.private_key_raw, mode=0o600)
    meta = {
        "schema": "amof.local_tlog_meta/v1",
        "origin": LOG_ORIGIN,
        "algorithm": ALGORITHM,
        "log_key_id": record.key_id,
        "selection_rationale": (
            "append_only_hashedrekord chosen over public Sigstore/Rekor "
            "(no Fulcio/PKI) and RFC3161 TSA (network/time authority out of scope)"
        ),
    }
    write_bytes_exclusive(
        meta_path,
        (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o600,
    )
    return {
        "ok": True,
        "origin": LOG_ORIGIN,
        "log_key_id": record.key_id,
        "tlog_root": str(log_root),
    }


class AppendOnlyTransparencyLog:
    """Local append-only log with Ed25519-signed checkpoints."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else tlog_root()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.leaves_path = self.root / "leaves.jsonl"
        self.meta_path = self.root / "log-meta.json"
        self._require_log_identity()

    def _require_log_identity(self) -> None:
        """Fail closed unless an explicit tlog-init has created checkpoint keys."""
        pub = self.root / "log-public.raw"
        priv = self.root / "log-private.raw"
        if pub.is_file() and priv.is_file():
            assert_not_symlink(pub, what="tlog public key")
            assert_not_symlink(priv, what="tlog private key")
            assert_private_mode(priv, what="tlog private key")
            return
        if pub.exists() or priv.exists():
            raise TrustIntegrityError(
                "incomplete tlog key material",
                code="invalid_tlog",
            )
        raise TrustIntegrityError(
            "no tlog checkpoint authority; run `amof trust tlog-init` before export",
            code="missing_tlog_authority",
        )

    def _log_keys(self) -> tuple[bytes, bytes, str]:
        pub = (self.root / "log-public.raw").read_bytes()
        priv = (self.root / "log-private.raw").read_bytes()
        key_id = hashlib.sha256(pub).hexdigest()
        return pub, priv, key_id

    def _read_leaves(self) -> list[dict[str, Any]]:
        if not self.leaves_path.is_file():
            return []
        assert_not_symlink(self.leaves_path, what="tlog leaves")
        rows: list[dict[str, Any]] = []
        for line in self.leaves_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
        return rows

    def append_hashedrekord(
        self,
        *,
        run_id: str,
        manifest_digest: str,
        evidence_digest: str,
        signature_digest: str,
        public_key_id: str,
        trust_snapshot_digest: str,
    ) -> dict[str, Any]:
        body = {
            "kind": "hashedrekord",
            "version": "0.0.1",
            "run_id": run_id,
            "manifest_digest": manifest_digest.strip().lower(),
            "evidence_digest": evidence_digest.strip().lower(),
            "signature_digest": signature_digest.strip().lower(),
            "public_key_id": public_key_id.strip().lower(),
            "trust_snapshot_digest": trust_snapshot_digest.strip().lower(),
        }
        body_digest = canonical_json_digest(body)
        rows = self._read_leaves()
        existing_index = None
        for existing in rows:
            if existing.get("body_digest") == body_digest:
                # Idempotent: same binding already logged; rebuild receipt against
                # the current checkpoint (append-only; no mutation of past leaves).
                existing_index = int(existing["index"])
                leaf = existing
                break
        if existing_index is None:
            index = len(rows)
            leaf = {
                "index": index,
                "body": body,
                "body_digest": body_digest,
                "integrated_time": utc_now(),
            }
            line = json.dumps(leaf, sort_keys=True, separators=(",", ":")) + "\n"
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            fd = os.open(self.leaves_path, flags, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(self.leaves_path, 0o600)
            rows.append(leaf)
        else:
            index = existing_index

        leaf_hashes = [_leaf_hash(str(r["body_digest"])) for r in rows]
        root = _merkle_root(leaf_hashes)
        proof = _inclusion_proof(leaf_hashes, index)
        pub, priv, log_key_id = self._log_keys()
        checkpoint_body = {
            "origin": LOG_ORIGIN,
            "tree_size": len(rows),
            "root_hash": root.hex(),
            "log_key_id": log_key_id,
        }
        checkpoint_payload = (
            json.dumps(checkpoint_body, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        from .ed25519_provider import Ed25519Signer
        from .interfaces import PrivateKeyRecord

        signed = Ed25519Signer().sign(
            checkpoint_payload,
            private_key=PrivateKeyRecord(
                key_id=log_key_id,
                algorithm=ALGORITHM,
                private_key_raw=priv,
                public_key_raw=pub,
            ),
        )
        receipt = {
            "schema": EXTERNAL_ANCHOR_SCHEMA,
            "anchor_kind": ANCHOR_KIND,
            "provider": "amof_local_append_only_tlog",
            "network": False,
            "body": body,
            "body_digest": body_digest,
            "log_index": index,
            "integrated_time": leaf["integrated_time"],
            "inclusion": {
                "leaf_hash": leaf_hashes[index].hex(),
                "root_hash": root.hex(),
                "tree_size": len(rows),
                "hashes": proof,
            },
            "checkpoint": {
                **checkpoint_body,
                "signature_b64": base64.b64encode(signed.signature).decode("ascii"),
                "log_public_key_b64": base64.b64encode(pub).decode("ascii"),
            },
            "semantics": {
                # Offline verify only checks Merkle inclusion vs a checkpoint
                # signed by the package-embedded log key — not append-only /
                # non-equivocation against any external log state (BL3-2).
                "proves": (
                    "merkle_inclusion_of_digests_and_key_id_vs_embedded_checkpoint"
                ),
                "does_not_prove": [
                    "append_only",
                    "non_equivocation",
                    "log_consistency",
                    "global_immutability",
                    "signer_authorization",
                    "future_non_revocation",
                    "wall_clock_timestamp_authority",
                ],
            },
        }
        return receipt


def verify_external_anchor(
    receipt: dict[str, Any],
    *,
    run_id: str,
    manifest_digest: str,
    evidence_digest: str,
    signature_digest: str,
    public_key_id: str,
    trust_snapshot_digest: str,
) -> dict[str, Any]:
    if receipt.get("schema") != EXTERNAL_ANCHOR_SCHEMA:
        raise TrustIntegrityError(
            "unexpected external_anchor schema",
            code="invalid_anchor",
        )
    if receipt.get("anchor_kind") != ANCHOR_KIND:
        raise TrustIntegrityError(
            f"unsupported external anchor kind: {receipt.get('anchor_kind')!r}",
            code="unsupported_anchor",
        )
    body = receipt.get("body")
    if not isinstance(body, dict):
        raise TrustIntegrityError("external_anchor body missing", code="invalid_anchor")
    expected = {
        "kind": "hashedrekord",
        "version": "0.0.1",
        "run_id": run_id,
        "manifest_digest": manifest_digest.strip().lower(),
        "evidence_digest": evidence_digest.strip().lower(),
        "signature_digest": signature_digest.strip().lower(),
        "public_key_id": public_key_id.strip().lower(),
        "trust_snapshot_digest": trust_snapshot_digest.strip().lower(),
    }
    for key, value in expected.items():
        if body.get(key) != value:
            raise TrustIntegrityError(
                f"external_anchor body field mismatch: {key}",
                code="anchor_binding_mismatch",
            )
    body_digest = str(receipt.get("body_digest") or "")
    if body_digest != canonical_json_digest(body):
        raise TrustIntegrityError(
            "external_anchor body_digest mismatch",
            code="anchor_invalid",
        )
    inclusion = receipt.get("inclusion")
    checkpoint = receipt.get("checkpoint")
    if not isinstance(inclusion, dict) or not isinstance(checkpoint, dict):
        raise TrustIntegrityError("external_anchor missing inclusion/checkpoint", code="invalid_anchor")
    leaf = _leaf_hash(body_digest)
    if leaf.hex() != str(inclusion.get("leaf_hash") or ""):
        raise TrustIntegrityError("leaf_hash mismatch", code="anchor_invalid")
    root = bytes.fromhex(str(checkpoint.get("root_hash") or ""))
    if root.hex() != str(inclusion.get("root_hash") or ""):
        raise TrustIntegrityError("checkpoint/inclusion root mismatch", code="anchor_invalid")
    proof = inclusion.get("hashes") or []
    if not isinstance(proof, list):
        raise TrustIntegrityError("inclusion hashes must be a list", code="invalid_anchor")
    _verify_inclusion(
        leaf=leaf,
        index=int(receipt.get("log_index")),
        tree_size=int(inclusion.get("tree_size")),
        proof_hex=[str(x) for x in proof],
        root=root,
    )
    # Verify checkpoint signature with embedded log public key (offline).
    log_pub_b64 = str(checkpoint.get("log_public_key_b64") or "")
    try:
        log_pub = base64.b64decode(log_pub_b64, validate=True)
    except Exception as exc:
        raise TrustIntegrityError(
            "checkpoint log public key invalid",
            code="invalid_anchor",
        ) from exc
    log_key_id = hashlib.sha256(log_pub).hexdigest()
    if str(checkpoint.get("log_key_id") or "") != log_key_id:
        raise TrustIntegrityError("checkpoint log_key_id mismatch", code="anchor_invalid")
    checkpoint_body = {
        "origin": checkpoint.get("origin"),
        "tree_size": checkpoint.get("tree_size"),
        "root_hash": checkpoint.get("root_hash"),
        "log_key_id": checkpoint.get("log_key_id"),
    }
    payload = (
        json.dumps(checkpoint_body, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    verify_ed25519_detached(
        payload=payload,
        signature_b64=str(checkpoint.get("signature_b64") or ""),
        public_key_raw=log_pub,
        public_key_id=log_key_id,
    )
    return {
        "ok": True,
        "anchor_kind": ANCHOR_KIND,
        "log_index": int(receipt.get("log_index")),
        "body_digest": body_digest,
    }


def write_external_anchor(path: Path, receipt: dict[str, Any]) -> Path:
    write_json_exclusive(path, receipt)
    return path
