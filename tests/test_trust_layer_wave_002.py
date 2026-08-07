"""Trust Layer Wave 002 — canonical evidence bundle + provenance consistency."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from amof.commands.trust_cmd import cmd_trust_verify
from amof.trust_layer import (
    TrustIntegrityError,
    build_provenance_document,
    evidence_bundle_dir,
    sha256_file,
    verify_evidence_bundle,
    verify_evidence_consistency,
    write_canonical_evidence_bundle,
)


def _sample_bundle(tmp: Path, *, run_id: str = "run-w2-1") -> Path:
    receipt = {
        "schema_version": 1,
        "handoff_id": run_id,
        "request_id": run_id,
        "status": "completed",
        "exit_code": 0,
        "stop_reason": "done",
        "session_id": "sess-1",
        "result_path": f"/tmp/{run_id}-result.json",
        "result_sha256": "",  # filled after result bytes known
        "evidence": {
            "evidence_seal_path": "",
            "finalization": "FINALIZED",
        },
        "receipt_path": f"/tmp/{run_id}-receipt.json",
        "started_at": "2026-08-07T10:00:00Z",
        "completed_at": "2026-08-07T10:00:01Z",
        "finalized": True,
        "workspace_root": str(tmp / "workspace"),
        "base_sha": "a" * 40,
    }
    result = {
        "status": "completed",
        "exit_code": 0,
        "stop_reason": "done",
        "session_id": "sess-1",
        "backend": "amof_native",
        "runner_id": "runner-1",
        "effective_model": "test-model",
        "started_at": "2026-08-07T10:00:00Z",
        "completed_at": "2026-08-07T10:00:01Z",
        "write_scope_binding_id": "wsb-" + ("b" * 24),
        "write_scope_approval_id": "wsa-" + ("c" * 24),
    }
    # Temporary result file to compute digest for receipt.
    staging = tmp / "staging-result.json"
    staging.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    digest = sha256_file(staging)
    receipt["result_sha256"] = digest

    # Minimal seal so consistency can optionally verify when path set.
    seal_dir = tmp / "seal"
    seal_dir.mkdir()
    (seal_dir / "artifacts").mkdir()
    art = seal_dir / "artifacts" / "result.json"
    art.write_bytes(staging.read_bytes())
    seal_receipt = {
        "schema": "amof.runtime_evidence_seal/v1",
        "receipt_id": f"handoff-seal-{run_id}",
        "sealed_at": "2026-08-07T10:00:02Z",
        "claim_summary": "test",
        "artifacts": [
            {
                "name": "result.json",
                "sha256": sha256_file(art),
                "bytes": art.stat().st_size,
                "storage": "copied",
                "sealed_path": "artifacts/result.json",
                "source_path": str(staging),
                "digest_kind": "content_sha256",
            }
        ],
        "producer": {"role": "runtime", "model": "test"},
    }
    (seal_dir / "receipt.json").write_text(
        json.dumps(seal_receipt, indent=2) + "\n", encoding="utf-8"
    )
    receipt["evidence"]["evidence_seal_path"] = str(seal_dir / "receipt.json")

    provenance = build_provenance_document(
        run_id=run_id,
        receipt=receipt,
        result=result,
        seal_receipt=seal_receipt,
        mission_id=run_id,
        operator="tester",
        payload_sha256="d" * 64,
        workspace_root=str(tmp / "workspace"),
        git_sha="a" * 40,
        base_sha="a" * 40,
        why="done",
    )
    bundle_dir = tmp / "trust" / "runs" / run_id
    write_canonical_evidence_bundle(
        bundle_dir,
        run_id=run_id,
        receipt=receipt,
        result=result,
        provenance=provenance,
        result_source=staging,
    )
    return bundle_dir


class TrustLayerWave002BundleTests(unittest.TestCase):
    def test_valid_bundle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            out = verify_evidence_consistency(bundle)
            self.assertTrue(out["ok"])
            for name in (
                "receipt.json",
                "result.json",
                "evidence.json",
                "hashes.json",
                "manifest.json",
            ):
                self.assertTrue((bundle / name).is_file())

    def test_remove_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            (bundle / "evidence.json").unlink()
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_bundle(bundle)
            self.assertEqual(ctx.exception.code, "missing_file")

    def test_rename_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            (bundle / "evidence.json").rename(bundle / "evidence-renamed.json")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_bundle(bundle)
            self.assertIn(ctx.exception.code, {"missing_file", "extra_file"})

    def test_duplicate_extra_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            shutil.copy2(bundle / "result.json", bundle / "result.copy.json")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_bundle(bundle)
            self.assertEqual(ctx.exception.code, "extra_file")

    def test_change_one_byte_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            path = bundle / "result.json"
            raw = path.read_bytes()
            path.write_bytes(raw[:-2] + b"X\n")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_bundle(bundle)
            self.assertEqual(ctx.exception.code, "digest_mismatch")

    def test_change_receipt_only_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
            receipt["stop_reason"] = "tampered"
            (bundle / "receipt.json").write_text(
                json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_bundle(bundle)
            self.assertEqual(ctx.exception.code, "digest_mismatch")

    def test_change_manifest_only_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            # Flip one digest so file content no longer matches manifest.
            manifest["files"][0]["sha256"] = "0" * 64
            (bundle / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_bundle(bundle)
            self.assertEqual(ctx.exception.code, "digest_mismatch")

    def test_change_timestamps_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = _sample_bundle(root)
            # Rewrite bundle with matching hashes but inconsistent timestamps:
            # mutate evidence.when after rewriting hashes+manifest carefully via API-level check.
            evidence = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))
            evidence["when"]["completed_at"] = "2099-01-01T00:00:00Z"
            (bundle / "evidence.json").write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            # Recompute hashes/manifest so only consistency timestamp check fires.
            from amof.trust_layer import BUNDLE_CONTENT_FILES, BUNDLE_HASHES_FILE, BUNDLE_HASHES_SCHEMA, BUNDLE_MANIFEST_SCHEMA, utc_now

            content_hashes = {
                name: {
                    "sha256": sha256_file(bundle / name),
                    "bytes": (bundle / name).stat().st_size,
                }
                for name in BUNDLE_CONTENT_FILES
            }
            hashes_payload = {
                "schema": BUNDLE_HASHES_SCHEMA,
                "run_id": "run-w2-1",
                "hash_algorithm": "sha256",
                "generated_at": utc_now(),
                "artifacts": content_hashes,
            }
            (bundle / BUNDLE_HASHES_FILE).write_text(
                json.dumps(hashes_payload, indent=2) + "\n", encoding="utf-8"
            )
            files = []
            for name in (*BUNDLE_CONTENT_FILES, BUNDLE_HASHES_FILE):
                files.append(
                    {
                        "name": name,
                        "sha256": sha256_file(bundle / name),
                        "bytes": (bundle / name).stat().st_size,
                    }
                )
            manifest = {
                "schema": BUNDLE_MANIFEST_SCHEMA,
                "run_id": "run-w2-1",
                "hash_algorithm": "sha256",
                "generated_at": utc_now(),
                "files": files,
                "excluded": ["manifest.json"],
                "file_count": len(files),
            }
            (bundle / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(bundle)
            self.assertEqual(ctx.exception.code, "timestamp_mismatch")

    def test_change_workspace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            evidence = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))
            evidence["where"]["workspace_root"] = "/totally/different/workspace"
            (bundle / "evidence.json").write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            self._rehash_bundle(bundle, run_id="run-w2-1")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(bundle)
            self.assertEqual(ctx.exception.code, "workspace_mismatch")

    def test_change_git_sha_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            evidence = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))
            evidence["git"]["workspace_head"] = "f" * 40
            (bundle / "evidence.json").write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            self._rehash_bundle(bundle, run_id="run-w2-1")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(bundle)
            self.assertEqual(ctx.exception.code, "git_sha_mismatch")

    def test_cli_pass_and_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = _sample_bundle(root, run_id="cli-run-1")
            data_root = root
            # Place bundle under canonical data_root/trust/runs/<id>
            canonical = evidence_bundle_dir(data_root, "cli-run-1")
            if bundle != canonical:
                canonical.parent.mkdir(parents=True, exist_ok=True)
                if canonical.exists():
                    shutil.rmtree(canonical)
                shutil.copytree(bundle, canonical)

            class _Paths:
                def __init__(self, dr: Path) -> None:
                    self.data_root = dr

            with patch("amof.commands.trust_cmd.get_app_paths", return_value=_Paths(data_root)):
                rc = cmd_trust_verify(SimpleNamespace(run_id="cli-run-1", json=False))
                self.assertEqual(rc, 0)
                # Tamper and ensure FAIL
                (canonical / "result.json").write_text("{}\n", encoding="utf-8")
                rc = cmd_trust_verify(SimpleNamespace(run_id="cli-run-1", json=False))
                self.assertEqual(rc, 1)

    def _rehash_bundle(self, bundle: Path, *, run_id: str) -> None:
        from amof.trust_layer import (
            BUNDLE_CONTENT_FILES,
            BUNDLE_HASHES_FILE,
            BUNDLE_HASHES_SCHEMA,
            BUNDLE_MANIFEST_SCHEMA,
            utc_now,
        )

        content_hashes = {
            name: {
                "sha256": sha256_file(bundle / name),
                "bytes": (bundle / name).stat().st_size,
            }
            for name in BUNDLE_CONTENT_FILES
        }
        hashes_payload = {
            "schema": BUNDLE_HASHES_SCHEMA,
            "run_id": run_id,
            "hash_algorithm": "sha256",
            "generated_at": utc_now(),
            "artifacts": content_hashes,
        }
        (bundle / BUNDLE_HASHES_FILE).write_text(
            json.dumps(hashes_payload, indent=2) + "\n", encoding="utf-8"
        )
        files = []
        for name in (*BUNDLE_CONTENT_FILES, BUNDLE_HASHES_FILE):
            files.append(
                {
                    "name": name,
                    "sha256": sha256_file(bundle / name),
                    "bytes": (bundle / name).stat().st_size,
                }
            )
        manifest = {
            "schema": BUNDLE_MANIFEST_SCHEMA,
            "run_id": run_id,
            "hash_algorithm": "sha256",
            "generated_at": utc_now(),
            "files": files,
            "excluded": ["manifest.json"],
            "file_count": len(files),
        }
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
