"""BL-5 — Durability & crash consistency for FINALIZED evidence bundles.

Atomic protocol: staging write → sign → publish/rename → then handoff receipt.
FINALIZED xor NOT FINALIZED; unsigned FINALIZED-claiming leftovers fail-closed.
"""

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

from amof.trust_crypto import (
    FilesystemKeyProvider,
    TrustPolicy,
    sign_evidence_bundle,
    write_trust_policy,
)
from amof.trust_layer import (
    TrustIntegrityError,
    abandon_evidence_bundle_staging,
    build_provenance_document,
    finalize_signed_evidence_bundle,
    make_evidence_bundle_staging_dir,
    publish_evidence_bundle_dir,
    sha256_file,
    verify_evidence_consistency,
    write_canonical_evidence_bundle,
)


def _seal_and_receipt(tmp: Path, *, run_id: str, finalized: bool = True) -> tuple[dict, dict, dict, Path]:
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
        "evidence": {
            "evidence_seal_path": "",
            "finalization": "FINALIZED" if finalized else "COMPLETE",
        },
        "receipt_path": f"/tmp/{run_id}-receipt.json",
        "started_at": "2026-08-07T10:00:00Z",
        "completed_at": "2026-08-07T10:00:01Z",
        "finalized": finalized,
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
    staging_result = tmp / "staging-result.json"
    staging_result.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    digest = sha256_file(staging_result)
    receipt["result_sha256"] = digest

    seal_dir = tmp / f"seal-{run_id}"
    seal_dir.mkdir(parents=True, exist_ok=True)
    (seal_dir / "artifacts").mkdir(exist_ok=True)
    art = seal_dir / "artifacts" / "result.json"
    art.write_bytes(staging_result.read_bytes())
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
                "source_path": str(staging_result),
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
    return receipt, result, provenance, staging_result


class TrustHardeningBL5DurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_td = tempfile.TemporaryDirectory()
        self.home = Path(self._home_td.name)
        self._env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)}, clear=False)
        self._env.start()
        self.provider = FilesystemKeyProvider()
        self.key = self.provider.generate_keypair()
        self.policy = TrustPolicy(
            allowed_key_ids=frozenset({self.key.key_id}),
            revoked_key_ids=frozenset(),
            preferred_key_id=self.key.key_id,
            require_signatures=False,
            allow_unknown_keys=False,
            allow_unsigned=True,  # empty-policy style; FINALIZED still requires sig
        )
        write_trust_policy(self.policy)

    def tearDown(self) -> None:
        self._env.stop()
        self._home_td.cleanup()

    def _signer(self, staging: Path) -> None:
        sign_evidence_bundle(
            staging, key_provider=self.provider, policy=self.policy
        )

    def test_crash_after_unsigned_write_before_sign_rejects_finalized(self) -> None:
        """Simulate crash after unsigned bundle write before sign."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt, result, provenance, src = _seal_and_receipt(root, run_id="crash-pre-sign")
            final = root / "trust" / "runs" / "crash-pre-sign"
            staging = make_evidence_bundle_staging_dir(final)
            write_canonical_evidence_bundle(
                staging,
                run_id="crash-pre-sign",
                receipt=receipt,
                result=result,
                provenance=provenance,
                result_source=src,
            )
            # Crash: staging left unsigned; final never published.
            self.assertTrue(staging.is_dir())
            self.assertFalse(final.exists())
            self.assertFalse((staging / "signature.json").is_file())

            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(staging)
            self.assertEqual(ctx.exception.code, "unsigned_finalized")

            # Publish must also refuse unsigned FINALIZED.
            with self.assertRaises(TrustIntegrityError) as ctx2:
                publish_evidence_bundle_dir(staging, final)
            self.assertEqual(ctx2.exception.code, "unsigned_finalized")
            self.assertFalse(final.exists())

    def test_successful_path_finalized_and_signed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt, result, provenance, src = _seal_and_receipt(root, run_id="ok-final")
            final = root / "trust" / "runs" / "ok-final"
            finalize_signed_evidence_bundle(
                final,
                run_id="ok-final",
                receipt=receipt,
                result=result,
                provenance=provenance,
                result_source=src,
                signer=self._signer,
            )
            self.assertTrue(final.is_dir())
            self.assertTrue((final / "signature.json").is_file())
            # No staging leftovers after success.
            leftovers = list((final.parent).glob(".staging-*"))
            self.assertEqual(leftovers, [])
            out = verify_evidence_consistency(final)
            self.assertTrue(out["ok"])
            self.assertTrue(out["signature"]["signed"])
            receipt_obj = json.loads((final / "receipt.json").read_text(encoding="utf-8"))
            self.assertTrue(receipt_obj["finalized"])
            self.assertEqual(receipt_obj["evidence"]["finalization"], "FINALIZED")

    def test_power_failure_staging_may_remain_final_absent_or_complete(self) -> None:
        """Staging may remain; final is either complete signed or absent."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt, result, provenance, src = _seal_and_receipt(root, run_id="pfail")
            final = root / "trust" / "runs" / "pfail"
            staging = make_evidence_bundle_staging_dir(final)
            write_canonical_evidence_bundle(
                staging,
                run_id="pfail",
                receipt=receipt,
                result=result,
                provenance=provenance,
                result_source=src,
            )
            # Simulate cleanup failure leaving staging (power-failure style).
            self.assertTrue(staging.is_dir())
            self.assertFalse(final.exists())

            # Failed finalize path: abandon may leave staging if rmtree fails;
            # final must still be non-FINALIZED / absent.
            def boom(_path: Path) -> None:
                raise TrustIntegrityError("simulated sign crash", code="sign_crash")

            receipt2, result2, provenance2, src2 = _seal_and_receipt(
                root, run_id="pfail-2"
            )
            final2 = root / "trust" / "runs" / "pfail-2"
            with self.assertRaises(TrustIntegrityError) as ctx:
                finalize_signed_evidence_bundle(
                    final2,
                    run_id="pfail-2",
                    receipt=receipt2,
                    result=result2,
                    provenance=provenance2,
                    result_source=src2,
                    signer=boom,
                )
            self.assertEqual(ctx.exception.code, "sign_crash")
            # finalize uses its own staging under final2.parent; final2 absent.
            self.assertFalse(final2.exists())

            # Original staging leftover still not publishable unsigned.
            with self.assertRaises(TrustIntegrityError):
                publish_evidence_bundle_dir(staging, final)
            self.assertFalse(final.exists())

    def test_retry_does_not_delete_preexisting_signed_finalized(self) -> None:
        """BL-2 coexistence: re-finalize must not rmtree existing signed FINALIZED."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt, result, provenance, src = _seal_and_receipt(root, run_id="immutable")
            final = root / "trust" / "runs" / "immutable"
            finalize_signed_evidence_bundle(
                final,
                run_id="immutable",
                receipt=receipt,
                result=result,
                provenance=provenance,
                result_source=src,
                signer=self._signer,
            )
            sig_before = (final / "signature.json").read_bytes()
            manifest_before = (final / "manifest.json").read_bytes()

            with self.assertRaises(TrustIntegrityError) as ctx:
                finalize_signed_evidence_bundle(
                    final,
                    run_id="immutable",
                    receipt=receipt,
                    result=result,
                    provenance=provenance,
                    result_source=src,
                    signer=self._signer,
                )
            self.assertEqual(ctx.exception.code, "bundle_exists")
            self.assertTrue(final.is_dir())
            self.assertEqual((final / "signature.json").read_bytes(), sig_before)
            self.assertEqual((final / "manifest.json").read_bytes(), manifest_before)
            out = verify_evidence_consistency(final)
            self.assertTrue(out["ok"])

    def test_allow_unsigned_policy_still_rejects_unsigned_finalized(self) -> None:
        """Default allow_unsigned=True must not accept FINALIZED leftovers."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt, result, provenance, src = _seal_and_receipt(
                root, run_id="leftover-final"
            )
            # Legacy in-place unsigned write (pre-protocol leftover shape).
            bundle = root / "trust" / "runs" / "leftover-final"
            write_canonical_evidence_bundle(
                bundle,
                run_id="leftover-final",
                receipt=receipt,
                result=result,
                provenance=provenance,
                result_source=src,
            )
            self.assertFalse((bundle / "signature.json").is_file())
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(bundle)
            self.assertEqual(ctx.exception.code, "unsigned_finalized")

    def test_unsigned_non_finalized_still_allowed_when_policy_permits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt, result, provenance, src = _seal_and_receipt(
                root, run_id="legacy-ok", finalized=False
            )
            bundle = root / "trust" / "runs" / "legacy-ok"
            write_canonical_evidence_bundle(
                bundle,
                run_id="legacy-ok",
                receipt=receipt,
                result=result,
                provenance=provenance,
                result_source=src,
            )
            out = verify_evidence_consistency(bundle)
            self.assertTrue(out["ok"])
            self.assertFalse(out["signature"]["signed"])

    def test_abandon_staging_does_not_touch_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt, result, provenance, src = _seal_and_receipt(root, run_id="keep-final")
            final = root / "trust" / "runs" / "keep-final"
            finalize_signed_evidence_bundle(
                final,
                run_id="keep-final",
                receipt=receipt,
                result=result,
                provenance=provenance,
                result_source=src,
                signer=self._signer,
            )
            staging = make_evidence_bundle_staging_dir(
                root / "trust" / "runs" / "other-run"
            )
            staging.mkdir()
            (staging / "junk").write_text("x\n", encoding="utf-8")
            abandon_evidence_bundle_staging(staging)
            self.assertFalse(staging.exists())
            self.assertTrue(final.is_dir())
            self.assertTrue((final / "signature.json").is_file())


if __name__ == "__main__":
    unittest.main()
