"""BL-2: FINALIZED signed evidence bundles are immutable on re-finalize.

Policy:
- Re-finalize must refuse with code=bundle_exists (explicit error).
- Never rmtree a pre-existing bundle (including the bundle_exists path).
- Signing failure after a *new* write by this invocation may clean up only
  that incomplete tree — never a prior FINALIZED signed bundle.
"""

from __future__ import annotations

import hashlib
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

from amof.commands import handoff as handoff_mod
from amof.trust_layer import TrustIntegrityError, evidence_bundle_dir, sha256_file


def _file_tree_digests(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(root))
            digests[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


class _Paths:
    def __init__(self, root: Path) -> None:
        self.data_root = root
        self.config_root = root.parent / "config"
        self.cache_root = root.parent / "cache"
        self.state_root = root.parent / "state"


def _prepare_completed_handoff(data_root: Path, handoff_id: str):
    result_path = data_root / "handoff" / "results" / f"{handoff_id}.json"
    receipt_path = data_root / "handoff" / "receipts" / f"{handoff_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
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
    return receipt, result_path, receipt_path, state


def _enroll_signing_key() -> None:
    from amof.trust_crypto import (
        FilesystemKeyProvider,
        enroll_key,
        load_trust_policy,
        write_trust_policy,
    )

    provider = FilesystemKeyProvider()
    generated = provider.generate_keypair()
    write_trust_policy(enroll_key(load_trust_policy(), generated.key_id, preferred=True))


class TrustHardeningBL2FinalizeImmutableTests(unittest.TestCase):
    def test_re_finalize_keeps_prior_signed_bundle_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "amof-home"
            data_root = home / "data"
            data_root.mkdir(parents=True)
            handoff_id = "handoff-bl2-immutable"
            receipt, result_path, receipt_path, state = _prepare_completed_handoff(
                data_root, handoff_id
            )
            paths = _Paths(data_root)

            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                _enroll_signing_key()
                with patch.object(handoff_mod, "get_app_paths", return_value=paths):
                    with patch.object(handoff_mod, "ensure_app_roots", return_value=None):
                        finalized = handoff_mod._seal_and_finalize_execution(
                            handoff_id=handoff_id,
                            receipt=receipt,
                            result_path=result_path,
                            receipt_path=receipt_path,
                            state=state,
                        )
                self.assertTrue(finalized.finalized)
                bundle_dir = evidence_bundle_dir(data_root, handoff_id)
                self.assertTrue((bundle_dir / "signature.json").is_file())
                before = _file_tree_digests(bundle_dir)

                # Reproduce the BL-2 destroy path: seal cleared/recreatable but
                # signed FINALIZED bundle still present → write raises bundle_exists.
                seal_dir = data_root / "handoff" / "seals" / handoff_id
                shutil.rmtree(seal_dir)

                with patch.object(handoff_mod, "get_app_paths", return_value=paths):
                    with patch.object(handoff_mod, "ensure_app_roots", return_value=None):
                        with self.assertRaises(TrustIntegrityError) as ctx:
                            handoff_mod._seal_and_finalize_execution(
                                handoff_id=handoff_id,
                                receipt=receipt,
                                result_path=result_path,
                                receipt_path=receipt_path,
                                state=state,
                            )
                self.assertEqual(ctx.exception.code, "bundle_exists")
                self.assertTrue(bundle_dir.is_dir())
                self.assertEqual(_file_tree_digests(bundle_dir), before)

                # Preserve wrapper must also refuse explicitly (not silent FINALIZE_FAILED).
                shutil.rmtree(data_root / "handoff" / "seals" / handoff_id, ignore_errors=True)
                with patch.object(handoff_mod, "get_app_paths", return_value=paths):
                    with patch.object(handoff_mod, "ensure_app_roots", return_value=None):
                        with self.assertRaises(TrustIntegrityError) as ctx2:
                            handoff_mod._seal_or_preserve_durable_receipt(
                                handoff_id=handoff_id,
                                receipt=receipt,
                                result_path=result_path,
                                receipt_path=receipt_path,
                                state=state,
                            )
                self.assertEqual(ctx2.exception.code, "bundle_exists")
                self.assertEqual(_file_tree_digests(bundle_dir), before)

    def test_bundle_exists_path_never_rmtrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "amof-home"
            data_root = home / "data"
            data_root.mkdir(parents=True)
            handoff_id = "handoff-bl2-no-rmtree"
            receipt, result_path, receipt_path, state = _prepare_completed_handoff(
                data_root, handoff_id
            )
            paths = _Paths(data_root)
            sentinel = data_root / "trust" / "runs" / handoff_id
            sentinel.mkdir(parents=True)
            marker = sentinel / "KEEP-ME.txt"
            marker.write_text("pre-existing finalized marker\n", encoding="utf-8")
            marker_bytes = marker.read_bytes()

            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                _enroll_signing_key()
                rmtree_calls: list[Path] = []
                real_rmtree = shutil.rmtree

                def _tracking_rmtree(path, *args, **kwargs):
                    rmtree_calls.append(Path(path))
                    return real_rmtree(path, *args, **kwargs)

                with patch.object(handoff_mod, "get_app_paths", return_value=paths):
                    with patch.object(handoff_mod, "ensure_app_roots", return_value=None):
                        with patch("shutil.rmtree", side_effect=_tracking_rmtree):
                            with patch.object(
                                handoff_mod,
                                "_cleanup_incomplete_finalize_bundle",
                                wraps=handoff_mod._cleanup_incomplete_finalize_bundle,
                            ) as cleanup:
                                with self.assertRaises(TrustIntegrityError) as ctx:
                                    handoff_mod._seal_and_finalize_execution(
                                        handoff_id=handoff_id,
                                        receipt=receipt,
                                        result_path=result_path,
                                        receipt_path=receipt_path,
                                        state=state,
                                    )
                self.assertEqual(ctx.exception.code, "bundle_exists")
                self.assertTrue(marker.is_file())
                self.assertEqual(marker.read_bytes(), marker_bytes)
                # Early refuse: cleanup helper must not run; no rmtree of prior bundle.
                cleanup.assert_not_called()
                self.assertEqual(rmtree_calls, [])

                # Helper policy: created_by_this_invocation=False never deletes.
                handoff_mod._cleanup_incomplete_finalize_bundle(
                    sentinel, created_by_this_invocation=False
                )
                self.assertTrue(marker.is_file())
                self.assertEqual(marker.read_bytes(), marker_bytes)

    def test_signing_failure_cleans_only_this_invocation_incomplete_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "amof-home"
            data_root = home / "data"
            data_root.mkdir(parents=True)
            prior_id = "handoff-bl2-prior-finalized"
            new_id = "handoff-bl2-sign-fail"

            prior_receipt, prior_result, prior_receipt_path, prior_state = (
                _prepare_completed_handoff(data_root, prior_id)
            )
            new_receipt, new_result, new_receipt_path, new_state = (
                _prepare_completed_handoff(data_root, new_id)
            )
            paths = _Paths(data_root)

            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                _enroll_signing_key()
                with patch.object(handoff_mod, "get_app_paths", return_value=paths):
                    with patch.object(handoff_mod, "ensure_app_roots", return_value=None):
                        prior = handoff_mod._seal_and_finalize_execution(
                            handoff_id=prior_id,
                            receipt=prior_receipt,
                            result_path=prior_result,
                            receipt_path=prior_receipt_path,
                            state=prior_state,
                        )
                self.assertTrue(prior.finalized)
                prior_bundle = evidence_bundle_dir(data_root, prior_id)
                prior_before = _file_tree_digests(prior_bundle)

                with patch.object(handoff_mod, "get_app_paths", return_value=paths):
                    with patch.object(handoff_mod, "ensure_app_roots", return_value=None):
                        with patch(
                            "amof.trust_crypto.sign_evidence_bundle",
                            side_effect=TrustIntegrityError(
                                "forced signing failure",
                                code="sign_failed",
                            ),
                        ):
                            with self.assertRaises(TrustIntegrityError) as ctx:
                                handoff_mod._seal_and_finalize_execution(
                                    handoff_id=new_id,
                                    receipt=new_receipt,
                                    result_path=new_result,
                                    receipt_path=new_receipt_path,
                                    state=new_state,
                                )
                self.assertEqual(ctx.exception.code, "sign_failed")
                new_bundle = evidence_bundle_dir(data_root, new_id)
                self.assertFalse(
                    new_bundle.exists(),
                    "incomplete bundle created by this invocation must be cleaned up",
                )
                self.assertTrue(prior_bundle.is_dir())
                self.assertEqual(
                    _file_tree_digests(prior_bundle),
                    prior_before,
                    "prior FINALIZED signed bundle must remain untouched",
                )


if __name__ == "__main__":
    unittest.main()
