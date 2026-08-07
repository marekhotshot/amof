"""Offline verification of portable trust export packages."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ..trust_layer import (
    BUNDLE_CONTENT_FILES,
    BUNDLE_HASHES_FILE,
    BUNDLE_MANIFEST_FILE,
    BUNDLE_SIGNATURE_FILE,
    TrustIntegrityError,
    sha256_file,
    verify_evidence_consistency,
)
from .anchors import LocalPinnedTrustAnchor, canonical_json_digest, load_json_object
from .bundle_sign import (
    ALGORITHM,
    SIGNATURE_SCHEMA,
    SIGNATURE_VERSION,
    canonical_signed_payload,
    verifier_for_algorithm,
)
from .export_package import (
    PUBLIC_KEY_FILENAME,
    TRUST_ANCHOR_FILENAME,
    TRUST_SNAPSHOT_FILENAME,
    VERIFICATION_METADATA_FILENAME,
)
from .interfaces import PublicKeyRecord
from .path_safety import assert_hermetic_export_package
from .policy import load_trust_policy
from .snapshot import evaluate_trust_now, verify_trust_snapshot
from .transparency import EXTERNAL_ANCHOR_FILENAME, verify_external_anchor


REQUIRED_EXPORT_FILES = (
    *BUNDLE_CONTENT_FILES,
    BUNDLE_HASHES_FILE,
    BUNDLE_MANIFEST_FILE,
    BUNDLE_SIGNATURE_FILE,
    PUBLIC_KEY_FILENAME,
    TRUST_ANCHOR_FILENAME,
    TRUST_SNAPSHOT_FILENAME,
    VERIFICATION_METADATA_FILENAME,
)
EXPORT_METADATA_FILES = frozenset(
    {
        PUBLIC_KEY_FILENAME,
        TRUST_ANCHOR_FILENAME,
        TRUST_SNAPSHOT_FILENAME,
        VERIFICATION_METADATA_FILENAME,
        EXTERNAL_ANCHOR_FILENAME,
    }
)


def _status(ok: bool, *, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "PASS" if ok else "FAIL"}
    if reason:
        out["reason"] = reason
    out.update(extra)
    return out


def verify_signature_with_exported_key(
    export_dir: Path,
    *,
    public_key_raw: bytes,
    public_key_id: str,
) -> dict[str, Any]:
    """Authenticity check using only package public key (no runtime key store)."""
    sig_path = export_dir / BUNDLE_SIGNATURE_FILE
    signature_obj = load_json_object(sig_path, code="invalid_signature")
    if signature_obj.get("schema") != SIGNATURE_SCHEMA:
        raise TrustIntegrityError("unexpected signature schema", code="invalid_signature")
    algorithm = str(signature_obj.get("algorithm") or "").strip().lower()
    key_id = str(signature_obj.get("public_key_id") or "").strip().lower()
    manifest_digest = str(signature_obj.get("manifest_digest") or "").strip().lower()
    evidence_digest = str(signature_obj.get("evidence_digest") or "").strip().lower()
    version = int(signature_obj.get("version") or 0)
    sig_b64 = str(signature_obj.get("signature") or "").strip()
    if algorithm != ALGORITHM or version != SIGNATURE_VERSION or not sig_b64:
        raise TrustIntegrityError("unsupported or incomplete signature", code="invalid_signature")
    if key_id != public_key_id.strip().lower():
        raise TrustIntegrityError(
            "signature public_key_id does not match exported key",
            code="key_id_mismatch",
        )
    if sha256_file(export_dir / BUNDLE_MANIFEST_FILE) != manifest_digest:
        raise TrustIntegrityError(
            "manifest_digest mismatch versus signature.json",
            code="manifest_digest_mismatch",
        )
    if sha256_file(export_dir / "evidence.json") != evidence_digest:
        raise TrustIntegrityError(
            "evidence_digest mismatch versus signature.json",
            code="evidence_digest_mismatch",
        )
    try:
        signature = base64.b64decode(sig_b64, validate=True)
    except Exception as exc:
        raise TrustIntegrityError("signature is not valid base64", code="invalid_signature") from exc
    payload = canonical_signed_payload(
        manifest_digest=manifest_digest,
        evidence_digest=evidence_digest,
        version=version,
    )
    verifier_for_algorithm(algorithm).verify(
        payload,
        signature,
        public_key=PublicKeyRecord(
            key_id=key_id,
            algorithm=algorithm,
            public_key_raw=public_key_raw,
        ),
    )
    return {
        "ok": True,
        "public_key_id": key_id,
        "manifest_digest": manifest_digest,
        "evidence_digest": evidence_digest,
        "signature_digest": sha256_file(sig_path),
    }


def verify_export_package(
    path: Path | str,
    *,
    require_external_anchor: bool | None = None,
    evaluate_trust_now_policy: bool = True,
    expect_key_id: str | None = None,
    allow_missing_external_anchor: bool = False,
) -> dict[str, Any]:
    """Fail-closed package self-consistency verify.

    Does not require producer private keys, DB, or git. Does not establish
    signer authorization unless expect_key_id is provided (verifier trust root).
    Producer absolute seal paths are not required (require_producer_seal=False).
    """
    root = Path(path).resolve()
    if not root.is_dir():
        raise TrustIntegrityError(f"export path missing: {root}", code="missing_export")

    modes: dict[str, Any] = {
        "LOCAL_INTEGRITY": _status(False, reason="not checked"),
        "SIGNATURE_TRUST": _status(False, reason="not checked"),
        "EXTERNAL_ANCHOR": _status(False, reason="not checked"),
        "TRUST_NOW": _status(False, reason="not checked"),
    }

    # Closed export set: required files present; forbid private key materials.
    # BL-4: hermetic enumeration — reject symlinks/hardlinks/traversal before
    # any content-based PASS (Path.is_file() follows symlinks and is unsafe here).
    try:
        actual = assert_hermetic_export_package(root)
    except TrustIntegrityError as exc:
        modes["LOCAL_INTEGRITY"] = _status(False, reason=str(exc), code=exc.code)
        raise
    for name in ("private.raw", "private_key", "private_key.raw"):
        if name in actual:
            raise TrustIntegrityError(
                f"export must not contain private key material: {name}",
                code="private_key_present",
            )
    missing = [n for n in REQUIRED_EXPORT_FILES if n not in actual]
    if missing:
        modes["LOCAL_INTEGRITY"] = _status(False, reason=f"missing files: {missing}")
        raise TrustIntegrityError(
            f"export missing required files: {missing}",
            code="missing_file",
        )
    allowed_names = set(REQUIRED_EXPORT_FILES) | {EXTERNAL_ANCHOR_FILENAME}
    extras = sorted(actual - allowed_names)
    if extras:
        modes["LOCAL_INTEGRITY"] = _status(False, reason=f"extra file: {extras[0]}")
        raise TrustIntegrityError(f"extra file: {extras[0]}", code="extra_file")

    meta = load_json_object(root / VERIFICATION_METADATA_FILENAME, code="invalid_metadata")
    run_id = str(meta.get("run_id") or root.name)

    # LOCAL_INTEGRITY: reuse Wave 002 consistency (hashes + provenance).
    # Skip live signature policy path by using check_signature=False then
    # performing signature with exported key below.
    try:
        verify_evidence_consistency(
            root,
            check_signature=False,
            allowed_extra_files=EXPORT_METADATA_FILES,
            require_producer_seal=False,
        )
        modes["LOCAL_INTEGRITY"] = _status(True)
    except TrustIntegrityError as exc:
        modes["LOCAL_INTEGRITY"] = _status(False, reason=str(exc), code=exc.code)
        raise

    # SIGNATURE_AUTHENTICITY: Ed25519 vs embedded public key + intra-package pin +
    # export-time trust snapshot consistency. This is NOT policy authorization.
    try:
        pub_doc = load_json_object(root / PUBLIC_KEY_FILENAME, code="invalid_public_key")
        public_key_id = str(pub_doc.get("public_key_id") or "").strip().lower()
        expected = str(expect_key_id or "").strip().lower() or None
        if expected and public_key_id != expected:
            raise TrustIntegrityError(
                f"exported public_key_id {public_key_id} does not match expect_key_id",
                code="unexpected_key_id",
            )
        try:
            public_key_raw = base64.b64decode(
                str(pub_doc.get("public_key_raw_b64") or ""), validate=True
            )
        except Exception as exc:
            raise TrustIntegrityError(
                "exported public key is not valid base64",
                code="invalid_public_key",
            ) from exc
        pin = load_json_object(root / TRUST_ANCHOR_FILENAME, code="invalid_trust_anchor")
        LocalPinnedTrustAnchor().verify_package_identity(
            public_key_id=public_key_id,
            public_key_raw=public_key_raw,
            trust_material=pin,
        )
        sig_result = verify_signature_with_exported_key(
            root,
            public_key_raw=public_key_raw,
            public_key_id=public_key_id,
        )
        snapshot = load_json_object(root / TRUST_SNAPSHOT_FILENAME, code="invalid_trust_snapshot")
        verify_trust_snapshot(
            snapshot,
            run_id=run_id,
            public_key_id=public_key_id,
            public_key_fingerprint=public_key_id,
            manifest_digest=sig_result["manifest_digest"],
            evidence_digest=sig_result["evidence_digest"],
            signature_digest=sig_result["signature_digest"],
        )
        modes["SIGNATURE_TRUST"] = _status(
            True,
            public_key_id=public_key_id,
            meaning="package_signature_authenticity_not_authorization",
            expect_key_id_enforced=bool(expected),
            trust_at_export_packaging=snapshot.get("trust_decision"),
        )
    except TrustIntegrityError as exc:
        modes["SIGNATURE_TRUST"] = _status(False, reason=str(exc), code=exc.code)
        raise

    # EXTERNAL_ANCHOR — package-embedded Merkle receipt (not a public transparency log).
    # Unsigned verification_metadata MUST NOT relax the requirement (downgrade attack).
    # Default: require external_anchor.json unless caller explicitly allows missing.
    anchor_path = root / EXTERNAL_ANCHOR_FILENAME
    if require_external_anchor is None:
        require_anchor = not allow_missing_external_anchor
        modes_meta = meta.get("modes") if isinstance(meta.get("modes"), dict) else {}
        # Metadata may only strengthen (require), never relax.
        if str(modes_meta.get("EXTERNAL_ANCHOR") or "") == "required":
            require_anchor = True
    else:
        require_anchor = bool(require_external_anchor)
    if not anchor_path.is_file():
        if require_anchor:
            modes["EXTERNAL_ANCHOR"] = _status(False, reason="missing external_anchor.json")
            raise TrustIntegrityError(
                "missing external_anchor.json",
                code="missing_anchor",
            )
        modes["EXTERNAL_ANCHOR"] = _status(True, detail="SKIPPED", reason="not present")
    else:
        try:
            receipt = load_json_object(anchor_path, code="invalid_anchor")
            snapshot_digest = canonical_json_digest(snapshot)
            verify_external_anchor(
                receipt,
                run_id=run_id,
                manifest_digest=sig_result["manifest_digest"],
                evidence_digest=sig_result["evidence_digest"],
                signature_digest=sig_result["signature_digest"],
                public_key_id=public_key_id,
                trust_snapshot_digest=snapshot_digest,
            )
            modes["EXTERNAL_ANCHOR"] = _status(True, anchor_kind=receipt.get("anchor_kind"))
        except TrustIntegrityError as exc:
            modes["EXTERNAL_ANCHOR"] = _status(False, reason=str(exc), code=exc.code)
            raise

    # TRUST_NOW — informational / optional; does not use package mutability.
    if evaluate_trust_now_policy:
        try:
            policy = load_trust_policy()
        except TrustIntegrityError:
            policy = None
        trust_now = evaluate_trust_now(public_key_id=public_key_id, policy=policy)
        # TRUST_NOW failure (revoked now) does NOT fail offline export verify by default.
        # It is reported separately so operators do not conflate with TRUST_AT_FINALIZATION.
        if trust_now.get("status") == "REVOKED":
            modes["TRUST_NOW"] = {
                "status": "REVOKED",
                "reason": "key revoked in current local policy (does not invalidate TRUST_AT_FINALIZATION)",
                "public_key_id": public_key_id,
            }
        elif trust_now.get("status") == "SKIPPED":
            modes["TRUST_NOW"] = {"status": "SKIPPED", "reason": trust_now.get("reason")}
        else:
            modes["TRUST_NOW"] = {"status": trust_now.get("status"), "public_key_id": public_key_id}
    else:
        modes["TRUST_NOW"] = {"status": "SKIPPED", "reason": "disabled"}

    required_ok = all(
        modes[name]["status"] == "PASS"
        for name in ("LOCAL_INTEGRITY", "SIGNATURE_TRUST", "EXTERNAL_ANCHOR")
        if not (
            name == "EXTERNAL_ANCHOR"
            and modes[name].get("detail") == "SKIPPED"
            and not require_anchor
        )
    )
    # EXTERNAL_ANCHOR SKIPPED when optional counts as pass for overall when not required.
    if modes["EXTERNAL_ANCHOR"].get("detail") == "SKIPPED" and not require_anchor:
        ext_ok = True
    else:
        ext_ok = modes["EXTERNAL_ANCHOR"]["status"] == "PASS"
    overall = (
        modes["LOCAL_INTEGRITY"]["status"] == "PASS"
        and modes["SIGNATURE_TRUST"]["status"] == "PASS"
        and ext_ok
    )
    return {
        "ok": overall,
        "status": "PASS" if overall else "FAIL",
        "run_id": run_id,
        "export_dir": str(root),
        "modes": modes,
        "required_ok": required_ok and overall,
    }


def format_mode_report(result: dict[str, Any]) -> str:
    lines = []
    modes = result.get("modes") or {}
    for name in ("LOCAL_INTEGRITY", "SIGNATURE_TRUST", "EXTERNAL_ANCHOR", "TRUST_NOW"):
        mode = modes.get(name) or {}
        status = mode.get("status") or "UNKNOWN"
        lines.append(f"{name}: {status}")
    lines.append(f"OVERALL: {result.get('status')}")
    return "\n".join(lines)
