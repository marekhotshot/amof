"""BL-1 — signed execution identity must not diverge from unsigned envelope."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from amof.trust_crypto import (
    FilesystemKeyProvider,
    TrustPolicy,
    export_trust_package,
    init_transparency_log,
    sign_evidence_bundle,
    verify_export_package,
    write_trust_policy,
)
from amof.trust_layer import (
    TrustIntegrityError,
    build_provenance_document,
    sha256_file,
    write_canonical_evidence_bundle,
)


def _signed_bundle(tmp: Path, home: Path, *, run_id: str) -> Path:
    receipt = {
        "schema_version": 1,
        "handoff_id": run_id,
        "request_id": run_id,
        "status": "completed",
        "exit_code": 0,
        "stop_reason": "done",
        "session_id": "sess-1",
        "result_path": f"/tmp/{run_id}-result.json",
        "result_sha256": "",
        "evidence": {"evidence_seal_path": "", "finalization": "FINALIZED"},
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
    staging = tmp / f"staging-{run_id}.json"
    staging.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    receipt["result_sha256"] = sha256_file(staging)
    seal_dir = tmp / f"seal-{run_id}"
    seal_dir.mkdir()
    (seal_dir / "artifacts").mkdir()
    art = seal_dir / "artifacts" / "result.json"
    art.write_bytes(staging.read_bytes())
    seal_receipt = {
        "schema": "amof.runtime_evidence_seal/v1",
        "receipt_id": f"handoff-seal-{run_id}",
        "sealed_at": "2026-08-07T10:00:02Z",
        "claim_summary": "bl1",
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
        operator="bl1",
        payload_sha256="d" * 64,
        workspace_root=str(tmp / "workspace"),
        git_sha="a" * 40,
        base_sha="a" * 40,
        why="bl1",
    )
    bundle = home / "share" / "trust" / "runs" / run_id
    write_canonical_evidence_bundle(
        bundle,
        run_id=run_id,
        receipt=receipt,
        result=result,
        provenance=provenance,
        result_source=staging,
    )
    from amof.trust_crypto import load_trust_policy

    sign_evidence_bundle(
        bundle, key_provider=FilesystemKeyProvider(), policy=load_trust_policy()
    )
    return bundle


class TrustHardeningBL1IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name)
        self._env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)}, clear=False)
        self._env.start()
        self.provider = FilesystemKeyProvider()
        self.key = self.provider.generate_keypair()
        write_trust_policy(
            TrustPolicy(
                allowed_key_ids=frozenset({self.key.key_id}),
                revoked_key_ids=frozenset(),
                preferred_key_id=self.key.key_id,
                require_signatures=True,
                allow_unknown_keys=False,
                allow_unsigned=False,
            )
        )
        init_transparency_log()

    def tearDown(self) -> None:
        self._env.stop()
        self._td.cleanup()

    def test_laundered_directory_rename_export_refused(self) -> None:
        """Copy signed bundle under a new directory name → export must refuse."""
        with tempfile.TemporaryDirectory() as tmp:
            true_id = "run-TRUE-IDENTITY"
            false_id = "run-LAUNDERED-IDENTITY"
            true_bundle = _signed_bundle(Path(tmp), self.home, run_id=true_id)
            laundered = self.home / "share" / "trust" / "runs" / false_id
            shutil.copytree(true_bundle, laundered)

            with self.assertRaises(TrustIntegrityError) as ctx:
                export_trust_package(
                    false_id,
                    output_dir=Path(tmp) / "exports",
                    data_root=self.home / "share",
                )
            self.assertEqual(ctx.exception.code, "run_id_mismatch")

            # Path-addressed export of the laundered directory also refuses.
            with self.assertRaises(TrustIntegrityError) as ctx2:
                export_trust_package(
                    str(laundered),
                    output_dir=Path(tmp) / "exports-path",
                    data_root=self.home / "share",
                )
            self.assertEqual(ctx2.exception.code, "run_id_mismatch")

    def test_tamper_metadata_snapshot_run_id_fail_closed(self) -> None:
        """Unsigned envelope rewrite must not PASS under allow-missing-anchor."""
        with tempfile.TemporaryDirectory() as tmp:
            genuine = "run-genuine-0001"
            attacker = "run-ATTACKER-CHOSEN"
            _signed_bundle(Path(tmp), self.home, run_id=genuine)
            exported = export_trust_package(
                genuine,
                output_dir=Path(tmp) / "exports",
                data_root=self.home / "share",
                include_external_anchor=False,
            )
            export_dir = Path(exported["export_dir"])
            # Drop any accidental anchor; verify with allow-missing.
            anchor = export_dir / "external_anchor.json"
            if anchor.is_file():
                anchor.unlink()

            meta_path = export_dir / "verification_metadata.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["run_id"] = attacker
            meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

            snap_path = export_dir / "trust_snapshot.json"
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            snap["run_id"] = attacker
            snap_path.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")

            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(
                    export_dir,
                    expect_key_id=self.key.key_id,
                    allow_missing_external_anchor=True,
                    evaluate_trust_now_policy=False,
                )
            self.assertEqual(ctx.exception.code, "run_id_mismatch")

    def test_reported_run_id_equals_signed_manifest_on_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_id = "run-canonical-pass"
            _signed_bundle(Path(tmp), self.home, run_id=run_id)
            exported = export_trust_package(
                run_id,
                output_dir=Path(tmp) / "exports",
                data_root=self.home / "share",
            )
            export_dir = Path(exported["export_dir"])
            manifest = json.loads(
                (export_dir / "manifest.json").read_text(encoding="utf-8")
            )
            signed_run_id = manifest["run_id"]
            self.assertEqual(signed_run_id, run_id)

            result = verify_export_package(
                export_dir,
                expect_key_id=self.key.key_id,
                evaluate_trust_now_policy=False,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["run_id"], signed_run_id)

            # Renaming the export directory must not change reported identity.
            renamed = Path(tmp) / "renamed-export-dir"
            export_dir.rename(renamed)
            result2 = verify_export_package(
                renamed,
                expect_key_id=self.key.key_id,
                evaluate_trust_now_policy=False,
            )
            self.assertTrue(result2["ok"])
            self.assertEqual(result2["run_id"], signed_run_id)


if __name__ == "__main__":
    unittest.main()
