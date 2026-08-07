"""Trust Layer Wave 001 — fail-closed SHA-256 verify + seal binding tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from amof.commands import handoff as handoff_mod
from amof.commands.bootstrap import build_sha256_manifest, cmd_bootstrap
from amof.generated_build.candidate import promote_candidate
from amof.trust_layer import (
    TrustIntegrityError,
    load_verified_bootstrap_summary,
    seal_evidence_artifacts,
    sha256_file,
    verify_bootstrap_sha256_manifest,
    verify_evidence_seal,
    verify_execution_result_integrity,
)


class TrustLayerResultSha256Tests(unittest.TestCase):
    def test_tampered_result_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            result.write_text('{"status":"completed","exit_code":0}\n', encoding="utf-8")
            digest = sha256_file(result)
            receipt = {
                "result_path": str(result),
                "result_sha256": digest,
            }
            verify_execution_result_integrity(result_path=result, receipt=receipt)
            result.write_text('{"status":"completed","exit_code":0,"tampered":true}\n', encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_execution_result_integrity(result_path=result, receipt=receipt)
            self.assertEqual(ctx.exception.code, "digest_mismatch")

    def test_missing_receipt_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            result.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_execution_result_integrity(result_path=result, receipt=None)
            self.assertEqual(ctx.exception.code, "missing_receipt")


class TrustLayerBootstrapTests(unittest.TestCase):
    def test_valid_manifest_verifies_and_tamper_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.json"
            b = root / "b.json"
            a.write_text('{"a":1}\n', encoding="utf-8")
            b.write_text('{"b":2}\n', encoding="utf-8")
            manifest = build_sha256_manifest(
                bundle_directory=root,
                artifact_paths={
                    "a": str(a),
                    "b": str(b),
                    "sha256_manifest_path": str(root / "bootstrap-sha256-manifest.json"),
                },
                excluded_labels=["sha256_manifest_path"],
            )
            manifest_path = root / "bootstrap-sha256-manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            verify_bootstrap_sha256_manifest(manifest_path)
            a.write_text('{"a":999}\n', encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bootstrap_sha256_manifest(manifest_path)
            self.assertEqual(ctx.exception.code, "digest_mismatch")

    def test_load_verified_summary_rejects_missing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "up10-bootstrap-summary.json"
            summary.write_text(
                json.dumps(
                    {
                        "result_kind": "amof_up10_bootstrap_summary",
                        "artifact_paths": {"sha256_manifest_path": ""},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                load_verified_bootstrap_summary(summary)
            self.assertEqual(ctx.exception.code, "missing_manifest")

    def test_bundle_emit_then_verify_cli_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = {
                "result_kind": "amof_doctor_result",
                "contract_version": "2026-05-15",
                "verdict": "PASS",
                "layout_mode": "split_workspace",
                "workspace_root": str(root / "workspace"),
                "canonical_amof_code_path": str(root / "workspace" / "repos" / "amof"),
                "canonical_ui_path": str(root / "workspace" / "repos" / "amof-ui"),
                "runtime_import_source": str(
                    root / "workspace" / "repos" / "amof" / "scripts" / "amof" / "__init__.py"
                ),
                "runtime_import_is_canonical": True,
                "surfaces": {},
                "app_data": {"roots": {}},
                "toolchain": {},
                "contracts": {
                    "contracts/governed-workstation-bootstrap-contract.schema.json": {
                        "exists": True
                    },
                    "contracts/bootstrap-sha256-manifest.schema.json": {"exists": True},
                    "contracts/up10-bootstrap-summary.schema.json": {"exists": True},
                    "contracts/bootstrap-source-checkout-receipt.schema.json": {
                        "exists": True
                    },
                    "contracts/bootstrap-toolchain-receipt.schema.json": {"exists": True},
                    "contracts/bootstrap-provider-configuration-receipt.schema.json": {
                        "exists": True
                    },
                    "contracts/bootstrap-failure-receipt.schema.json": {"exists": True},
                },
                "contexts": {
                    "available_contexts": ["local"],
                    "current": {
                        "current_context": "local",
                        "controlplane_mode": "local-cli",
                        "execution_backend": "local",
                    },
                },
                "warnings": [],
                "failures": [],
            }
            for path in (
                root / "workspace",
                root / "workspace" / "repos" / "amof",
                root / "workspace" / "repos" / "amof-ui",
            ):
                path.mkdir(parents=True, exist_ok=True)
            bundle_dir = root / "bundle"
            args = type(
                "Args",
                (),
                {
                    "bootstrap_cmd": "bundle",
                    "json": True,
                    "output_dir": str(bundle_dir),
                },
            )()
            with patch("amof.commands.bootstrap.topology_report", return_value=report):
                rc = cmd_bootstrap(args)
            self.assertIn(rc, {0, 2})
            manifest_path = bundle_dir / "bootstrap-sha256-manifest.json"
            self.assertTrue(manifest_path.is_file())
            verify_bootstrap_sha256_manifest(manifest_path)
            # Tamper one artifact and ensure verify-on-consume fails.
            doctor = bundle_dir / "doctor.json"
            payload = json.loads(doctor.read_text(encoding="utf-8"))
            payload["tampered"] = True
            doctor.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_bootstrap_sha256_manifest(manifest_path)


class TrustLayerSealFinalizeTests(unittest.TestCase):
    def test_seal_verify_ok_and_tamper_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            art = root / "evidence.json"
            art.write_text('{"ok":true}\n', encoding="utf-8")
            seal_dir = root / "seal"
            seal_evidence_artifacts(
                seal_dir=seal_dir,
                receipt_id="seal-1",
                claim_summary="test",
                artifacts=(("evidence.json", art),),
            )
            verify_evidence_seal(seal_dir)
            sealed = seal_dir / "artifacts" / "evidence.json"
            sealed.write_text('{"ok":false}\n', encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_seal(seal_dir)
            self.assertEqual(ctx.exception.code, "digest_mismatch")

    def test_handoff_finalize_requires_valid_seal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            data_root.mkdir()
            handoff_id = "handoff-trust-layer-1"
            result_path = data_root / "handoff" / "results" / f"{handoff_id}.json"
            receipt_path = data_root / "handoff" / "receipts" / f"{handoff_id}.json"
            result_path.parent.mkdir(parents=True)
            receipt_path.parent.mkdir(parents=True)
            result_payload = {
                "status": "completed",
                "exit_code": 0,
                "stop_reason": "done",
                "session_id": "s1",
            }
            result_path.write_text(json.dumps(result_payload) + "\n", encoding="utf-8")
            digest = sha256_file(result_path)
            receipt = handoff_mod.HandoffExecutionReceipt(
                schema_version=1,
                handoff_id=handoff_id,
                request_id=handoff_id,
                status="completed",
                exit_code=0,
                stop_reason="done",
                session_id="s1",
                studio_session_id=None,
                result_path=str(result_path),
                result_sha256=digest,
                evidence={},
                receipt_path=str(receipt_path),
                started_at="2026-08-07T00:00:00Z",
                completed_at="2026-08-07T00:00:01Z",
            )
            receipt_path.write_text(
                json.dumps(receipt.to_dict(), indent=2) + "\n", encoding="utf-8"
            )
            state = handoff_mod.HandoffExecutionState(
                schema_version=1,
                handoff_id=handoff_id,
                status="completed",
                request_id=handoff_id,
                updated_at="2026-08-07T00:00:01Z",
                started_at="2026-08-07T00:00:00Z",
                completed_at="2026-08-07T00:00:01Z",
                receipt_path=str(receipt_path),
                result_path=str(result_path),
            )

            class _Paths:
                def __init__(self, root: Path) -> None:
                    self.data_root = root

            with patch.object(handoff_mod, "get_app_paths", return_value=_Paths(data_root)):
                with patch.object(handoff_mod, "ensure_app_roots", return_value=None):
                    finalized = handoff_mod._seal_and_finalize_execution(
                        handoff_id=handoff_id,
                        receipt=receipt,
                        result_path=result_path,
                        receipt_path=receipt_path,
                        state=state,
                    )
            self.assertTrue(finalized.finalized)
            self.assertEqual(finalized.status, "completed")
            self.assertTrue((data_root / "handoff" / "seals" / handoff_id / "receipt.json").is_file())

            # Seal failure must prevent treating run as finalized on consume.
            sealed_result = (
                data_root / "handoff" / "seals" / handoff_id / "artifacts" / "result.json"
            )
            sealed_result.write_text('{"status":"completed","tampered":1}\n', encoding="utf-8")
            live_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            with patch.object(handoff_mod, "get_app_paths", return_value=_Paths(data_root)):
                with self.assertRaises(TrustIntegrityError):
                    handoff_mod._verify_consumed_execution_result(
                        handoff_id,
                        result=result_payload,
                        receipt=live_receipt,
                        state=handoff_mod.HandoffExecutionState(
                            schema_version=1,
                            handoff_id=handoff_id,
                            status="finalized",
                            request_id=handoff_id,
                            updated_at="2026-08-07T00:00:02Z",
                            started_at="2026-08-07T00:00:00Z",
                            completed_at="2026-08-07T00:00:01Z",
                            receipt_path=str(receipt_path),
                            result_path=str(result_path),
                        ),
                    )

    def test_legacy_completed_without_seal_still_verifies_result_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            result.write_text('{"status":"completed"}\n', encoding="utf-8")
            digest = sha256_file(result)
            receipt = {
                "status": "completed",
                "finalized": False,
                "result_path": str(result),
                "result_sha256": digest,
                "evidence": {},
            }
            # No seal required for legacy completed receipts.
            verify_execution_result_integrity(result_path=result, receipt=receipt)


class TrustLayerAuditReceiptPathTests(unittest.TestCase):
    def test_audit_receipt_path_is_placeholder_on_public_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("AMOF_GENERATED_BUILDS_ROOT")
            os.environ["AMOF_GENERATED_BUILDS_ROOT"] = tmp
            try:
                result = promote_candidate(
                    {
                        "artifact_ref": {
                            "artifact_path": "/tmp/a.json",
                            "repo_path": "/tmp/repo",
                            "service": "web",
                            "image_digest": "sha256:" + ("c" * 64),
                        },
                        "target_ecosystem": "example",
                        "target_service": "web",
                    }
                )
            finally:
                if old is None:
                    os.environ.pop("AMOF_GENERATED_BUILDS_ROOT", None)
                else:
                    os.environ["AMOF_GENERATED_BUILDS_ROOT"] = old
        self.assertEqual(result["result"], "refused")
        self.assertEqual(result["audit_receipt_path"], "")


if __name__ == "__main__":
    unittest.main()
