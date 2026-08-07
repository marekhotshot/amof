"""Canonical portable trust export package (no private keys)."""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path
from typing import Any

from ..trust_layer import (
    BUNDLE_CONTENT_FILES,
    BUNDLE_HASHES_FILE,
    BUNDLE_MANIFEST_FILE,
    BUNDLE_SIGNATURE_FILE,
    TrustIntegrityError,
    evidence_bundle_dir,
    sha256_file,
    utc_now,
    verify_evidence_consistency,
    write_json_exclusive,
)
from .anchors import (
    build_local_pinned_trust_anchor,
    canonical_json_digest,
    write_local_pinned_trust_anchor,
)
from .filesystem_keys import FilesystemKeyProvider
from .policy import load_trust_policy
from .snapshot import build_trust_snapshot, write_trust_snapshot
from .transparency import (
    EXTERNAL_ANCHOR_FILENAME,
    AppendOnlyTransparencyLog,
    write_external_anchor,
)


EXPORT_SCHEMA = "amof.trust_export/v1"
VERIFICATION_METADATA_FILENAME = "verification_metadata.json"
TRUST_ANCHOR_FILENAME = "trust_anchor.json"
TRUST_SNAPSHOT_FILENAME = "trust_snapshot.json"
PUBLIC_KEY_FILENAME = "public_key.json"
BUNDLE_EXPORT_FILES = (
    *BUNDLE_CONTENT_FILES,
    BUNDLE_HASHES_FILE,
    BUNDLE_MANIFEST_FILE,
    BUNDLE_SIGNATURE_FILE,
)


def default_export_root() -> Path:
    from ..app_paths import ensure_app_roots, get_app_paths

    ensure_app_roots()
    root = get_app_paths().data_root / "trust" / "exports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def export_trust_package(
    run_id: str,
    *,
    output_dir: Path | str | None = None,
    data_root: Path | str | None = None,
    include_external_anchor: bool = True,
) -> dict[str, Any]:
    """Copy a finalized run into a portable offline-verifiable package."""
    from ..app_paths import get_app_paths

    rid = str(run_id or "").strip()
    if not rid:
        raise TrustIntegrityError("run_id required", code="invalid_run_id")

    root_data = Path(data_root) if data_root is not None else get_app_paths().data_root
    bundle = evidence_bundle_dir(root_data, rid)
    if not bundle.is_dir():
        # Allow exporting from a directory path passed as run_id.
        candidate = Path(run_id)
        if candidate.is_dir() and (candidate / BUNDLE_MANIFEST_FILE).is_file():
            bundle = candidate.resolve()
            rid = bundle.name
        else:
            raise TrustIntegrityError(
                f"evidence bundle missing for run: {rid}",
                code="missing_bundle",
            )

    # Fail closed: source must already verify in-runtime before export.
    verify_evidence_consistency(bundle)

    sig_path = bundle / BUNDLE_SIGNATURE_FILE
    if not sig_path.is_file():
        raise TrustIntegrityError(
            "export requires signature.json (FINALIZED signed run)",
            code="missing_signature",
        )
    signature_obj = json.loads(sig_path.read_text(encoding="utf-8"))
    key_id = str(signature_obj.get("public_key_id") or "").strip().lower()
    manifest_digest = str(signature_obj.get("manifest_digest") or "").strip().lower()
    evidence_digest = str(signature_obj.get("evidence_digest") or "").strip().lower()
    signature_digest = sha256_file(sig_path)

    provider = FilesystemKeyProvider()
    public = provider.get_public_key(key_id)
    policy = load_trust_policy()
    policy.assert_key_usable(key_id)

    out_parent = Path(output_dir) if output_dir is not None else default_export_root()
    out_parent.mkdir(parents=True, exist_ok=True)
    export_dir = out_parent / rid
    if export_dir.exists():
        raise TrustIntegrityError(
            f"refuse overwrite of existing export: {export_dir}",
            code="export_exists",
        )
    export_dir.mkdir(parents=True, exist_ok=False)

    try:
        for name in BUNDLE_EXPORT_FILES:
            src = bundle / name
            if not src.is_file():
                raise TrustIntegrityError(f"missing bundle file: {name}", code="missing_file")
            shutil.copy2(src, export_dir / name)

        public_key_doc = {
            "schema": "amof.exported_public_key/v1",
            "algorithm": public.algorithm,
            "public_key_id": public.key_id,
            "public_key_fingerprint": public.key_id,
            "public_key_raw_b64": base64.b64encode(public.public_key_raw).decode("ascii"),
        }
        write_json_exclusive(export_dir / PUBLIC_KEY_FILENAME, public_key_doc)

        pin = build_local_pinned_trust_anchor(
            public_key_id=public.key_id,
            public_key_raw=public.public_key_raw,
            algorithm=public.algorithm,
        )
        write_local_pinned_trust_anchor(export_dir / TRUST_ANCHOR_FILENAME, pin)

        snapshot = build_trust_snapshot(
            run_id=rid,
            policy=policy,
            public_key_id=public.key_id,
            public_key_fingerprint=public.key_id,
            manifest_digest=manifest_digest,
            evidence_digest=evidence_digest,
            signature_digest=signature_digest,
        )
        if snapshot["trust_decision"] != "ALLOWED":
            raise TrustIntegrityError(
                "cannot export: key not ALLOWED at finalization snapshot",
                code="snapshot_untrusted",
            )
        write_trust_snapshot(export_dir / TRUST_SNAPSHOT_FILENAME, snapshot)
        snapshot_digest = canonical_json_digest(snapshot)

        external_anchor = None
        if include_external_anchor:
            tlog = AppendOnlyTransparencyLog()
            external_anchor = tlog.append_hashedrekord(
                run_id=rid,
                manifest_digest=manifest_digest,
                evidence_digest=evidence_digest,
                signature_digest=signature_digest,
                public_key_id=public.key_id,
                trust_snapshot_digest=snapshot_digest,
            )
            write_external_anchor(export_dir / EXTERNAL_ANCHOR_FILENAME, external_anchor)

        verification_metadata = {
            "schema": "amof.verification_metadata/v1",
            "export_schema": EXPORT_SCHEMA,
            "run_id": rid,
            "exported_at": utc_now(),
            "source_bundle_relative": "self-contained",
            "files": sorted(p.name for p in export_dir.iterdir() if p.is_file()),
            "modes": {
                "LOCAL_INTEGRITY": "required",
                "SIGNATURE_TRUST": "required",
                "EXTERNAL_ANCHOR": "required" if include_external_anchor else "optional",
            },
            "semantics": {
                "integrity": "content digests / closed artifact set",
                "authenticity": (
                    f"{public.algorithm} signature over digests"
                ),
                "signing_algorithm": public.algorithm,
                "trust_at_finalization": "immutable trust_snapshot.json",
                "trust_now": "optional current policy evaluation (not in package)",
                "transparency": "external_anchor append-only binding (if present)",
            },
            "no_private_keys": True,
            "runtime_local_paths_required": False,
        }
        write_json_exclusive(export_dir / VERIFICATION_METADATA_FILENAME, verification_metadata)
    except Exception:
        shutil.rmtree(export_dir, ignore_errors=True)
        raise

    return {
        "ok": True,
        "run_id": rid,
        "export_dir": str(export_dir),
        "public_key_id": public.key_id,
        "external_anchor": bool(external_anchor),
        "files": sorted(p.name for p in export_dir.iterdir() if p.is_file()),
    }
