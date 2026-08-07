"""Trust Layer — fail-closed SHA-256 integrity + provenance bundles.

Wave 001: verify-on-consume + evidence seals.
Wave 002: canonical evidence bundles + provenance graph + consistency verify.

Pure content hashing. No signatures, PQC, Merkle redesign, or external anchors.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


TRUST_SEAL_SCHEMA = "amof.runtime_evidence_seal/v1"
BOOTSTRAP_MANIFEST_KIND = "amof_bootstrap_sha256_manifest"
PROVENANCE_SCHEMA = "amof.runtime_provenance/v1"
BUNDLE_HASHES_SCHEMA = "amof.evidence_bundle_hashes/v1"
BUNDLE_MANIFEST_SCHEMA = "amof.evidence_bundle_manifest/v1"
SHA256_HEX_RE_LEN = 64

# Canonical evidence bundle layout (flat directory, exactly these names).
BUNDLE_CONTENT_FILES = ("receipt.json", "result.json", "evidence.json")
BUNDLE_HASHES_FILE = "hashes.json"
BUNDLE_MANIFEST_FILE = "manifest.json"
BUNDLE_SIGNATURE_FILE = "signature.json"
BUNDLE_REQUIRED_FILES = BUNDLE_CONTENT_FILES + (BUNDLE_HASHES_FILE, BUNDLE_MANIFEST_FILE)
# signature.json authenticates digests; excluded from manifest self-set.
BUNDLE_OPTIONAL_FILES = (BUNDLE_SIGNATURE_FILE,)


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


# ── Wave 002: canonical evidence bundle + provenance graph ─────────────


def evidence_bundle_dir(data_root: Path | str, run_id: str) -> Path:
    """Canonical path: <data_root>/trust/runs/<run_id>/."""
    rid = str(run_id or "").strip()
    if not rid:
        raise TrustIntegrityError("run_id is required for evidence bundle", code="missing_run_id")
    return Path(data_root) / "trust" / "runs" / rid


def _read_json_object(path: Path, *, code: str) -> dict[str, Any]:
    if not path.is_file():
        raise TrustIntegrityError(f"missing file: {path.name}", code=code)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustIntegrityError(
            f"invalid JSON: {path.name}",
            code=code,
        ) from exc
    if not isinstance(payload, dict):
        raise TrustIntegrityError(f"{path.name} must be a JSON object", code=code)
    return payload


def _list_bundle_files(bundle_dir: Path) -> set[str]:
    names: set[str] = set()
    for entry in bundle_dir.iterdir():
        if entry.is_file():
            names.add(entry.name)
        elif entry.is_dir():
            raise TrustIntegrityError(
                f"extra directory not allowed in evidence bundle: {entry.name}",
                code="extra_file",
            )
        else:
            raise TrustIntegrityError(
                f"unexpected entry in evidence bundle: {entry.name}",
                code="extra_file",
            )
    return names


def build_provenance_document(
    *,
    run_id: str,
    receipt: Mapping[str, Any],
    result: Mapping[str, Any],
    seal_receipt: Mapping[str, Any] | None = None,
    mission_id: str | None = None,
    operator: str | None = None,
    payload_sha256: str | None = None,
    workspace_root: str | None = None,
    git_sha: str | None = None,
    base_sha: str | None = None,
    why: str | None = None,
) -> dict[str, Any]:
    """Canonical provenance object answering WHO/WHAT/WHEN/WHERE/WHY/HOW/FROM/TO/AUTHORITY."""
    rid = str(run_id or "").strip()
    if not rid:
        raise TrustIntegrityError("provenance requires run_id", code="missing_run_id")

    result_binding = str(result.get("write_scope_binding_id") or "").strip() or None
    result_approval = str(result.get("write_scope_approval_id") or "").strip() or None
    mutation_receipt = result.get("mutation_receipt")
    mutation_id = None
    if isinstance(mutation_receipt, dict):
        mutation_id = str(mutation_receipt.get("receipt_id") or "").strip() or None

    effective_base_sha = (
        str(base_sha or "").strip().lower()
        or str(receipt.get("base_sha") or "").strip().lower()
        or (
            str(mutation_receipt.get("base_sha") or "").strip().lower()
            if isinstance(mutation_receipt, dict)
            else ""
        )
        or None
    )
    effective_workspace = (
        str(workspace_root or "").strip()
        or str(receipt.get("workspace_root") or "").strip()
        or (
            str(mutation_receipt.get("workspace_root") or "").strip()
            if isinstance(mutation_receipt, dict)
            else ""
        )
        or None
    )
    effective_git = str(git_sha or "").strip().lower() or effective_base_sha

    seal_meta = None
    if isinstance(seal_receipt, Mapping):
        seal_meta = {
            "seal_receipt_id": str(seal_receipt.get("receipt_id") or "") or None,
            "seal_schema": str(seal_receipt.get("schema") or "") or None,
            "sealed_at": str(seal_receipt.get("sealed_at") or "") or None,
        }

    return {
        "schema": PROVENANCE_SCHEMA,
        "run_id": rid,
        "mission": {
            "mission_id": str(mission_id or "").strip() or None,
            "request_id": str(receipt.get("request_id") or rid),
            "handoff_id": str(receipt.get("handoff_id") or rid),
        },
        "who": {
            "operator": str(operator or "").strip() or None,
            "backend": str(result.get("backend") or "").strip() or None,
            "runner_id": str(result.get("runner_id") or "").strip() or None,
            "model": str(result.get("effective_model") or result.get("requested_model") or "").strip()
            or None,
            "producer_role": "runtime",
        },
        "what": {
            "receipt_status": str(receipt.get("status") or "").strip() or None,
            "result_status": str(result.get("status") or "").strip() or None,
            "stop_reason": str(receipt.get("stop_reason") or result.get("stop_reason") or "").strip()
            or None,
            "exit_code": receipt.get("exit_code", result.get("exit_code")),
            "finalized": bool(receipt.get("finalized")),
        },
        "when": {
            "started_at": str(receipt.get("started_at") or result.get("started_at") or "") or None,
            "completed_at": str(receipt.get("completed_at") or result.get("completed_at") or "")
            or None,
            "finalized_at": utc_now(),
        },
        "where": {
            "workspace_root": effective_workspace,
            "studio_session_id": str(
                receipt.get("studio_session_id") or result.get("studio_session_id") or ""
            ).strip()
            or None,
        },
        "why": {
            "summary": str(why or "").strip() or None,
            "failure_classification": str(result.get("failure_classification") or "").strip()
            or None,
        },
        "how": {
            "backend": str(result.get("backend") or "").strip() or None,
            "transport": str(result.get("transport") or "").strip() or None,
            "session_id": str(receipt.get("session_id") or result.get("session_id") or "").strip()
            or None,
        },
        "from_input": {
            "payload_sha256": str(payload_sha256 or "").strip().lower() or None,
            "base_sha": effective_base_sha,
        },
        "to_output": {
            "result_sha256": str(receipt.get("result_sha256") or "").strip().lower() or None,
        },
        "authority": {
            "write_scope_binding_id": result_binding,
            "write_scope_approval_id": result_approval,
            "mutation_receipt_id": mutation_id,
        },
        "git": {
            "workspace_head": effective_git,
            "base_sha": effective_base_sha,
        },
        "seal": seal_meta,
        "artifact_hash_refs": list(BUNDLE_CONTENT_FILES),
    }


def _write_bytes_exclusive(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def write_canonical_evidence_bundle(
    bundle_dir: Path | str,
    *,
    run_id: str,
    receipt: Mapping[str, Any],
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
    result_source: Path | str | None = None,
    receipt_source: Path | str | None = None,
) -> dict[str, Any]:
    """Write exactly one immutable canonical evidence bundle directory.

    Prefer byte-exact copies of result/receipt sources when provided so
    ``result_sha256`` remains valid. Otherwise JSON is rewritten and digests
    are synchronized to the written bytes.
    """
    root = Path(bundle_dir)
    rid = str(run_id or "").strip()
    if root.exists():
        raise TrustIntegrityError(
            f"refuse overwrite of existing evidence bundle: {root}",
            code="bundle_exists",
        )
    if provenance.get("schema") != PROVENANCE_SCHEMA:
        raise TrustIntegrityError(
            f"unexpected provenance schema: {provenance.get('schema')!r}",
            code="invalid_provenance",
        )
    if str(provenance.get("run_id") or "") != rid:
        raise TrustIntegrityError(
            "provenance.run_id does not match bundle run_id",
            code="run_id_mismatch",
        )

    root.mkdir(parents=True, exist_ok=False)
    try:
        result_path = root / "result.json"
        if result_source is not None:
            src = Path(result_source)
            if not src.is_file():
                raise TrustIntegrityError(
                    f"result_source missing: {src}",
                    code="missing_artifact",
                )
            _write_bytes_exclusive(result_path, src.read_bytes())
        else:
            write_json_exclusive(result_path, dict(result))

        result_digest = sha256_file(result_path)
        receipt_payload = dict(receipt)
        receipt_payload["result_sha256"] = result_digest

        provenance_payload = dict(provenance)
        to_output = dict(provenance_payload.get("to_output") or {})
        to_output["result_sha256"] = result_digest
        provenance_payload["to_output"] = to_output

        receipt_path = root / "receipt.json"
        if receipt_source is not None and result_source is not None:
            # Only byte-copy receipt when result was also copied (digest already aligned).
            src_receipt = Path(receipt_source)
            if not src_receipt.is_file():
                raise TrustIntegrityError(
                    f"receipt_source missing: {src_receipt}",
                    code="missing_artifact",
                )
            # Still rewrite receipt so result_sha256 and finalized fields stay authoritative.
            write_json_exclusive(receipt_path, receipt_payload)
        else:
            write_json_exclusive(receipt_path, receipt_payload)

        write_json_exclusive(root / "evidence.json", provenance_payload)

        content_hashes = {
            name: {
                "sha256": sha256_file(root / name),
                "bytes": (root / name).stat().st_size,
            }
            for name in BUNDLE_CONTENT_FILES
        }
        hashes_payload = {
            "schema": BUNDLE_HASHES_SCHEMA,
            "run_id": rid,
            "hash_algorithm": "sha256",
            "generated_at": utc_now(),
            "artifacts": content_hashes,
        }
        write_json_exclusive(root / BUNDLE_HASHES_FILE, hashes_payload)

        manifest_files = []
        for name in (*BUNDLE_CONTENT_FILES, BUNDLE_HASHES_FILE):
            path = root / name
            manifest_files.append(
                {
                    "name": name,
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
            )
        manifest_payload = {
            "schema": BUNDLE_MANIFEST_SCHEMA,
            "run_id": rid,
            "hash_algorithm": "sha256",
            "generated_at": utc_now(),
            "files": manifest_files,
            "excluded": [BUNDLE_MANIFEST_FILE],
            "file_count": len(manifest_files),
        }
        write_json_exclusive(root / BUNDLE_MANIFEST_FILE, manifest_payload)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise

    verify_evidence_bundle(root)
    # Signature is written after the canonical files exist (Wave 003 finalize).
    verify_evidence_consistency(root, check_signature=False)
    return manifest_payload


def verify_evidence_bundle(
    bundle_dir: Path | str,
    *,
    allowed_extra_files: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Fail-closed manifest verification: missing / extra / modified files."""
    root = Path(bundle_dir)
    if not root.is_dir():
        raise TrustIntegrityError(
            f"evidence bundle missing: {root}",
            code="missing_bundle",
        )

    actual = _list_bundle_files(root)
    expected = set(BUNDLE_REQUIRED_FILES)
    allowed = expected | set(BUNDLE_OPTIONAL_FILES) | set(allowed_extra_files or ())
    missing = sorted(expected - actual)
    extra = sorted(actual - allowed)
    if missing:
        raise TrustIntegrityError(
            f"missing file: {missing[0]}",
            code="missing_file",
        )
    if extra:
        raise TrustIntegrityError(
            f"extra file: {extra[0]}",
            code="extra_file",
        )

    manifest = _read_json_object(root / BUNDLE_MANIFEST_FILE, code="invalid_manifest")
    if manifest.get("schema") != BUNDLE_MANIFEST_SCHEMA:
        raise TrustIntegrityError(
            f"unexpected manifest schema: {manifest.get('schema')!r}",
            code="invalid_manifest",
        )
    if manifest.get("hash_algorithm") != "sha256":
        raise TrustIntegrityError(
            f"unsupported manifest hash_algorithm: {manifest.get('hash_algorithm')!r}",
            code="invalid_manifest",
        )
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise TrustIntegrityError("manifest.files must be a non-empty array", code="invalid_manifest")

    listed = {str(item.get("name") or "") for item in files if isinstance(item, dict)}
    expected_listed = set(BUNDLE_CONTENT_FILES) | {BUNDLE_HASHES_FILE}
    if listed != expected_listed:
        raise TrustIntegrityError(
            f"manifest file set mismatch: expected={sorted(expected_listed)} actual={sorted(listed)}",
            code="manifest_set_mismatch",
        )

    for item in files:
        if not isinstance(item, dict):
            raise TrustIntegrityError("manifest file entry must be an object", code="invalid_manifest")
        name = str(item.get("name") or "")
        path = root / name
        verify_file_sha256(path, str(item.get("sha256") or ""), field=f"manifest.{name}")
        expected_bytes = item.get("bytes")
        if expected_bytes is not None and path.stat().st_size != int(expected_bytes):
            raise TrustIntegrityError(
                f"modified file: {name} (size mismatch)",
                code="modified_file",
            )
    return manifest


def verify_evidence_consistency(
    bundle_dir: Path | str,
    *,
    check_signature: bool = True,
    allowed_extra_files: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Cross-check receipt/result/evidence/hashes/seal/workspace/git/base_sha.

    When check_signature=False, skip Wave 003 signature verify (used while writing
    the unsigned canonical files before sign_evidence_bundle).
    """
    root = Path(bundle_dir)
    manifest = verify_evidence_bundle(root, allowed_extra_files=allowed_extra_files)

    receipt = _read_json_object(root / "receipt.json", code="invalid_receipt")
    result = _read_json_object(root / "result.json", code="invalid_result")
    evidence = _read_json_object(root / "evidence.json", code="invalid_provenance")
    hashes = _read_json_object(root / BUNDLE_HASHES_FILE, code="invalid_hashes")

    run_id = str(manifest.get("run_id") or "").strip()
    if not run_id:
        raise TrustIntegrityError("manifest missing run_id", code="missing_run_id")
    for label, payload in (
        ("receipt", receipt),
        ("hashes", hashes),
        ("evidence", evidence),
    ):
        candidate = str(
            payload.get("run_id")
            or payload.get("handoff_id")
            or (payload.get("mission") or {}).get("handoff_id")
            or ""
        ).strip()
        if label == "receipt":
            candidate = str(payload.get("handoff_id") or payload.get("request_id") or "").strip()
        if label == "evidence":
            candidate = str(payload.get("run_id") or "").strip()
        if candidate and candidate != run_id:
            raise TrustIntegrityError(
                f"run_id mismatch in {label}: expected={run_id} actual={candidate}",
                code="run_id_mismatch",
            )

    if evidence.get("schema") != PROVENANCE_SCHEMA:
        raise TrustIntegrityError(
            f"unexpected evidence/provenance schema: {evidence.get('schema')!r}",
            code="invalid_provenance",
        )

    # receipt → result
    verify_result_sha256(
        result_path=root / "result.json",
        result_sha256=str(receipt.get("result_sha256") or "") or None,
    )
    to_output = evidence.get("to_output") if isinstance(evidence.get("to_output"), dict) else {}
    expected_result_digest = str(to_output.get("result_sha256") or "").strip().lower()
    actual_result_digest = str(receipt.get("result_sha256") or "").strip().lower()
    if expected_result_digest and expected_result_digest != actual_result_digest:
        raise TrustIntegrityError(
            "provenance to_output.result_sha256 does not match receipt.result_sha256",
            code="result_hash_mismatch",
        )

    # hashes.json ↔ files
    if hashes.get("schema") != BUNDLE_HASHES_SCHEMA:
        raise TrustIntegrityError(
            f"unexpected hashes schema: {hashes.get('schema')!r}",
            code="invalid_hashes",
        )
    artifacts = hashes.get("artifacts")
    if not isinstance(artifacts, dict):
        raise TrustIntegrityError("hashes.artifacts must be an object", code="invalid_hashes")
    for name in BUNDLE_CONTENT_FILES:
        meta = artifacts.get(name)
        if not isinstance(meta, dict):
            raise TrustIntegrityError(f"hashes missing artifact {name}", code="missing_hash")
        verify_file_sha256(
            root / name,
            str(meta.get("sha256") or ""),
            field=f"hashes.{name}",
        )

    # timestamps must match between provenance and receipt
    when = evidence.get("when") if isinstance(evidence.get("when"), dict) else {}
    for field in ("started_at", "completed_at"):
        left = str(when.get(field) or "").strip()
        right = str(receipt.get(field) or "").strip()
        if left and right and left != right:
            raise TrustIntegrityError(
                f"timestamp mismatch for {field}: provenance={left} receipt={right}",
                code="timestamp_mismatch",
            )

    # workspace / git / base_sha consistency
    where = evidence.get("where") if isinstance(evidence.get("where"), dict) else {}
    git = evidence.get("git") if isinstance(evidence.get("git"), dict) else {}
    from_input = evidence.get("from_input") if isinstance(evidence.get("from_input"), dict) else {}

    prov_workspace = str(where.get("workspace_root") or "").strip()
    receipt_workspace = str(receipt.get("workspace_root") or "").strip()
    if prov_workspace and receipt_workspace and prov_workspace != receipt_workspace:
        raise TrustIntegrityError(
            "workspace mismatch between provenance and receipt",
            code="workspace_mismatch",
        )

    prov_base = str(from_input.get("base_sha") or git.get("base_sha") or "").strip().lower()
    receipt_base = str(receipt.get("base_sha") or "").strip().lower()
    mutation = result.get("mutation_receipt")
    mutation_base = ""
    if isinstance(mutation, dict):
        mutation_base = str(mutation.get("base_sha") or "").strip().lower()
    for label, value in (("receipt", receipt_base), ("mutation_receipt", mutation_base)):
        if prov_base and value and prov_base != value:
            raise TrustIntegrityError(
                f"base_sha mismatch between provenance and {label}",
                code="base_sha_mismatch",
            )

    prov_git = str(git.get("workspace_head") or "").strip().lower()
    if prov_git and prov_base and prov_git != prov_base:
        raise TrustIntegrityError(
            "git sha mismatch between provenance.workspace_head and provenance.base_sha",
            code="git_sha_mismatch",
        )
    if prov_git and receipt_base and prov_git != receipt_base:
        raise TrustIntegrityError(
            "git sha mismatch between provenance.workspace_head and receipt.base_sha",
            code="git_sha_mismatch",
        )

    # Optional seal binding: if provenance names a seal id, Wave-001 seal dir must verify when present
    # beside the bundle (../seals/<run_id>) or via absolute evidence path on receipt.
    receipt_evidence = receipt.get("evidence") if isinstance(receipt.get("evidence"), dict) else {}
    seal_path = str(receipt_evidence.get("evidence_seal_path") or "").strip()
    if seal_path:
        seal_receipt_path = Path(seal_path)
        if seal_receipt_path.name == "receipt.json":
            verify_evidence_seal(seal_receipt_path.parent)
        elif seal_receipt_path.is_dir():
            verify_evidence_seal(seal_receipt_path)
        else:
            raise TrustIntegrityError(
                f"seal path not verifiable: {seal_path}",
                code="invalid_seal",
            )
        seal_meta = evidence.get("seal") if isinstance(evidence.get("seal"), dict) else {}
        sealed = _read_json_object(
            seal_receipt_path if seal_receipt_path.is_file() else seal_receipt_path / "receipt.json",
            code="invalid_seal",
        )
        if seal_meta.get("seal_receipt_id") and str(seal_meta.get("seal_receipt_id")) != str(
            sealed.get("receipt_id") or ""
        ):
            raise TrustIntegrityError(
                "seal receipt id mismatch between provenance and seal",
                code="seal_mismatch",
            )

    # Wave 003: cryptographic signature over manifest + evidence digests.
    signature_result: dict[str, Any] | None = None
    if check_signature:
        from .trust_crypto.bundle_sign import verify_bundle_signature

        signature_result = verify_bundle_signature(root)

    return {
        "run_id": run_id,
        "bundle_dir": str(root),
        "manifest": manifest,
        "signature": signature_result,
        "ok": True,
    }


def verify_run_evidence(data_root: Path | str, run_id: str) -> dict[str, Any]:
    """Resolve canonical bundle for RUN and fail-closed verify it."""
    bundle = evidence_bundle_dir(data_root, run_id)
    return verify_evidence_consistency(bundle)


__all__ = [
    "BUNDLE_HASHES_FILE",
    "BUNDLE_MANIFEST_FILE",
    "BUNDLE_OPTIONAL_FILES",
    "BUNDLE_REQUIRED_FILES",
    "BUNDLE_SIGNATURE_FILE",
    "BUNDLE_HASHES_SCHEMA",
    "BUNDLE_MANIFEST_SCHEMA",
    "PROVENANCE_SCHEMA",
    "TRUST_SEAL_SCHEMA",
    "TrustIntegrityError",
    "build_provenance_document",
    "evidence_bundle_dir",
    "load_verified_bootstrap_summary",
    "seal_evidence_artifacts",
    "sha256_file",
    "utc_now",
    "verify_bootstrap_bundle",
    "verify_bootstrap_sha256_manifest",
    "verify_evidence_bundle",
    "verify_evidence_consistency",
    "verify_evidence_seal",
    "verify_execution_result_integrity",
    "verify_file_sha256",
    "verify_result_sha256",
    "verify_run_evidence",
    "write_canonical_evidence_bundle",
    "write_json_exclusive",
]
