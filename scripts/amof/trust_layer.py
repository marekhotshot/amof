"""Trust Layer Wave 001 — fail-closed SHA-256 integrity helpers.

Pure content hashing. No signatures, PQC, Merkle redesign, or external anchors.
Preserves existing receipt producers; adds verify-on-consume and evidence seals.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


TRUST_SEAL_SCHEMA = "amof.runtime_evidence_seal/v1"
BOOTSTRAP_MANIFEST_KIND = "amof_bootstrap_sha256_manifest"
SHA256_HEX_RE_LEN = 64


class TrustIntegrityError(ValueError):
    """Fail-closed integrity error for Trust Layer verification."""

    def __init__(self, message: str, *, code: str = "integrity_error") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256_hex(value: str, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != SHA256_HEX_RE_LEN or any(
        ch not in "0123456789abcdef" for ch in normalized
    ):
        raise TrustIntegrityError(
            f"{field} must be a 64-character lowercase hex SHA-256 digest",
            code="invalid_digest",
        )
    return normalized


def verify_file_sha256(
    path: Path,
    expected_sha256: str,
    *,
    field: str = "sha256",
) -> str:
    """Re-hash a file and fail closed on mismatch or missing file."""
    target = Path(path)
    if not target.is_file():
        raise TrustIntegrityError(
            f"missing artifact for {field}: {target}",
            code="missing_artifact",
        )
    expected = _require_sha256_hex(expected_sha256, field=field)
    actual = sha256_file(target)
    if actual != expected:
        raise TrustIntegrityError(
            f"{field} mismatch for {target}: expected={expected} actual={actual}",
            code="digest_mismatch",
        )
    return actual


def verify_result_sha256(
    *,
    result_path: Path | str | None,
    result_sha256: str | None,
) -> str:
    """Verify a handoff execution result file against its receipt digest."""
    if result_path is None or not str(result_path).strip():
        raise TrustIntegrityError(
            "execution receipt is missing result_path",
            code="missing_result_path",
        )
    if result_sha256 is None or not str(result_sha256).strip():
        raise TrustIntegrityError(
            "execution receipt is missing result_sha256",
            code="missing_result_sha256",
        )
    return verify_file_sha256(
        Path(result_path),
        str(result_sha256),
        field="result_sha256",
    )


def verify_execution_result_integrity(
    *,
    result_path: Path | str | None,
    receipt: Mapping[str, Any] | None,
) -> str:
    """Fail-closed verify-on-consume for handoff result + receipt."""
    if receipt is None:
        raise TrustIntegrityError(
            "execution result present but execution receipt is missing",
            code="missing_receipt",
        )
    receipt_result_path = str(receipt.get("result_path") or "").strip() or None
    effective_path = str(result_path or "").strip() or receipt_result_path
    return verify_result_sha256(
        result_path=effective_path,
        result_sha256=str(receipt.get("result_sha256") or "") or None,
    )


def write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def seal_evidence_artifacts(
    *,
    seal_dir: Path,
    receipt_id: str,
    claim_summary: str,
    artifacts: Sequence[tuple[str, Path]],
    producer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy+hash artifacts into a write-once runtime evidence seal."""
    seal_root = Path(seal_dir)
    receipt_path = seal_root / "receipt.json"
    if receipt_path.exists():
        raise TrustIntegrityError(
            f"refuse overwrite of existing seal: {receipt_path}",
            code="seal_exists",
        )

    artifacts_dir = seal_root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for name, source in artifacts:
        src = Path(source)
        if not src.is_file():
            raise TrustIntegrityError(
                f"cannot seal missing artifact {name}: {src}",
                code="missing_artifact",
            )
        dest = artifacts_dir / name
        if dest.exists():
            raise TrustIntegrityError(
                f"seal artifact name collision: {dest}",
                code="seal_collision",
            )
        dest.write_bytes(src.read_bytes())
        os.chmod(dest, 0o600)
        digest = sha256_file(dest)
        entries.append(
            {
                "name": name,
                "sha256": digest,
                "bytes": dest.stat().st_size,
                "storage": "copied",
                "sealed_path": f"artifacts/{name}",
                "source_path": str(src.resolve()),
                "digest_kind": "content_sha256",
            }
        )

    sealed_at = utc_now()
    receipt = {
        "schema": TRUST_SEAL_SCHEMA,
        "receipt_id": receipt_id,
        "sealed_at": sealed_at,
        "claim_summary": claim_summary,
        "artifacts": entries,
        "producer": dict(producer or {"role": "runtime", "model": "amof-trust-layer"}),
    }
    write_json_exclusive(receipt_path, receipt)
    return receipt


def verify_evidence_seal(seal_dir: Path | str) -> dict[str, Any]:
    """Re-hash sealed artifacts; fail closed on any mismatch."""
    root = Path(seal_dir)
    receipt_path = root / "receipt.json"
    if not receipt_path.is_file():
        raise TrustIntegrityError(
            f"missing seal receipt: {receipt_path}",
            code="missing_seal",
        )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustIntegrityError(
            f"seal receipt is not valid JSON: {receipt_path}",
            code="invalid_seal",
        ) from exc
    if not isinstance(receipt, dict):
        raise TrustIntegrityError(
            "seal receipt must be a JSON object",
            code="invalid_seal",
        )
    if receipt.get("schema") != TRUST_SEAL_SCHEMA:
        raise TrustIntegrityError(
            f"unexpected seal schema: {receipt.get('schema')!r}",
            code="invalid_seal",
        )
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise TrustIntegrityError(
            "seal receipt has no artifacts",
            code="invalid_seal",
        )
    for art in artifacts:
        if not isinstance(art, dict):
            raise TrustIntegrityError(
                "seal artifact entry must be an object",
                code="invalid_seal",
            )
        name = str(art.get("name") or "")
        storage = str(art.get("storage") or "")
        if storage == "copied":
            target = root / str(art.get("sealed_path") or "")
        elif storage == "path_bound":
            target = Path(str(art.get("source_path") or ""))
        else:
            raise TrustIntegrityError(
                f"unknown seal storage for {name}: {storage!r}",
                code="invalid_seal",
            )
        actual = verify_file_sha256(
            target,
            str(art.get("sha256") or ""),
            field=f"seal.artifact.{name}",
        )
        expected_bytes = art.get("bytes")
        if expected_bytes is not None and target.stat().st_size != int(expected_bytes):
            raise TrustIntegrityError(
                f"seal size mismatch for {name}: expected={expected_bytes} "
                f"actual={target.stat().st_size}",
                code="size_mismatch",
            )
        if actual != str(art.get("sha256") or "").strip().lower():
            raise TrustIntegrityError(
                f"seal digest mismatch for {name}",
                code="digest_mismatch",
            )
    return receipt


def verify_bootstrap_sha256_manifest(manifest_path: Path | str) -> dict[str, Any]:
    """Verify-on-consume for bootstrap SHA-256 manifests. No regeneration."""
    path = Path(manifest_path)
    if not path.is_file():
        raise TrustIntegrityError(
            f"bootstrap sha256 manifest missing: {path}",
            code="missing_manifest",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustIntegrityError(
            f"bootstrap sha256 manifest is not valid JSON: {path}",
            code="invalid_manifest",
        ) from exc
    if not isinstance(payload, dict):
        raise TrustIntegrityError(
            "bootstrap sha256 manifest must be a JSON object",
            code="invalid_manifest",
        )
    if payload.get("result_kind") != BOOTSTRAP_MANIFEST_KIND:
        raise TrustIntegrityError(
            f"unexpected bootstrap manifest kind: {payload.get('result_kind')!r}",
            code="invalid_manifest",
        )
    if payload.get("hash_algorithm") != "sha256":
        raise TrustIntegrityError(
            f"unsupported bootstrap hash_algorithm: {payload.get('hash_algorithm')!r}",
            code="invalid_manifest",
        )
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise TrustIntegrityError(
            "bootstrap manifest artifacts must be an array",
            code="invalid_manifest",
        )
    for art in artifacts:
        if not isinstance(art, dict):
            raise TrustIntegrityError(
                "bootstrap manifest artifact must be an object",
                code="invalid_manifest",
            )
        label = str(art.get("label") or "")
        art_path = Path(str(art.get("path") or ""))
        if not art_path.is_file():
            raise TrustIntegrityError(
                f"bootstrap artifact missing for {label}: {art_path}",
                code="missing_artifact",
            )
        verify_file_sha256(
            art_path,
            str(art.get("sha256") or ""),
            field=f"bootstrap.artifact.{label}",
        )
        expected_bytes = art.get("bytes")
        if expected_bytes is not None and art_path.stat().st_size != int(expected_bytes):
            raise TrustIntegrityError(
                f"bootstrap artifact size mismatch for {label}: "
                f"expected={expected_bytes} actual={art_path.stat().st_size}",
                code="size_mismatch",
            )
    return payload


def verify_bootstrap_bundle(bundle_directory: Path | str) -> dict[str, Any]:
    """Locate and verify the sha256 manifest for a bootstrap evidence bundle."""
    root = Path(bundle_directory)
    if not root.is_dir():
        raise TrustIntegrityError(
            f"bootstrap bundle directory missing: {root}",
            code="missing_bundle",
        )
    manifest_path = root / "bootstrap-sha256-manifest.json"
    return verify_bootstrap_sha256_manifest(manifest_path)


def load_verified_bootstrap_summary(summary_path: Path | str) -> dict[str, Any]:
    """Consume a UP10 summary only after its bundle manifest verifies."""
    path = Path(summary_path)
    if not path.is_file():
        raise TrustIntegrityError(
            f"bootstrap summary missing: {path}",
            code="missing_summary",
        )
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustIntegrityError(
            f"bootstrap summary is not valid JSON: {path}",
            code="invalid_summary",
        ) from exc
    if not isinstance(summary, dict):
        raise TrustIntegrityError(
            "bootstrap summary must be a JSON object",
            code="invalid_summary",
        )
    artifact_paths = summary.get("artifact_paths")
    if not isinstance(artifact_paths, dict):
        raise TrustIntegrityError(
            "bootstrap summary missing artifact_paths",
            code="invalid_summary",
        )
    manifest_path = str(artifact_paths.get("sha256_manifest_path") or "").strip()
    if not manifest_path:
        raise TrustIntegrityError(
            "bootstrap summary missing sha256_manifest_path",
            code="missing_manifest",
        )
    verify_bootstrap_sha256_manifest(manifest_path)
    return summary


__all__ = [
    "TRUST_SEAL_SCHEMA",
    "TrustIntegrityError",
    "load_verified_bootstrap_summary",
    "seal_evidence_artifacts",
    "sha256_file",
    "utc_now",
    "verify_bootstrap_bundle",
    "verify_bootstrap_sha256_manifest",
    "verify_evidence_seal",
    "verify_execution_result_integrity",
    "verify_file_sha256",
    "verify_result_sha256",
]
