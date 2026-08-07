"""Trust Model v1 recovery — seal portability, expect-key-id, anchor downgrade."""

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
from amof.trust_crypto.ed25519_provider import generate_ed25519_keypair
from amof.trust_layer import TrustIntegrityError
from tests.test_trust_layer_wave_004 import _sample_signed_bundle


class TrustModelV1RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_td = tempfile.TemporaryDirectory()
        self.home = Path(self._home_td.name)
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
        self._home_td.cleanup()

    def test_clean_room_without_producer_seal_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _sample_signed_bundle(Path(tmp), self.home, run_id="run-cr-seal")
            exported = export_trust_package(
                "run-cr-seal",
                output_dir=Path(tmp) / "ex",
                data_root=self.home / "share",
            )
            pkg = Path(exported["export_dir"])
            iso = Path(tmp) / "iso"
            shutil.copytree(pkg, iso)
            # Destroy producer home including any seal dirs under AMOF_HOME.
            shutil.rmtree(self.home)
            home2 = Path(tmp) / "empty"
            home2.mkdir()
            with patch.dict(os.environ, {"AMOF_HOME": str(home2)}):
                out = verify_export_package(iso, evaluate_trust_now_policy=False)
            self.assertTrue(out["ok"], msg=out)
            self.assertEqual(out["modes"]["LOCAL_INTEGRITY"]["status"], "PASS")

    def test_expect_key_id_rejects_foreign_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _sample_signed_bundle(Path(tmp), self.home, run_id="run-expect")
            exported = export_trust_package(
                "run-expect",
                output_dir=Path(tmp) / "ex",
                data_root=self.home / "share",
            )
            export_dir = Path(exported["export_dir"])
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(
                    export_dir,
                    evaluate_trust_now_policy=False,
                    expect_key_id="a" * 64,
                )
            self.assertEqual(ctx.exception.code, "unexpected_key_id")
            ok = verify_export_package(
                export_dir,
                evaluate_trust_now_policy=False,
                expect_key_id=self.key.key_id,
            )
            self.assertTrue(ok["ok"])
            self.assertTrue(ok["modes"]["SIGNATURE_TRUST"].get("expect_key_id_enforced"))

    def test_metadata_cannot_relax_missing_external_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _sample_signed_bundle(Path(tmp), self.home, run_id="run-anchor-down")
            exported = export_trust_package(
                "run-anchor-down",
                output_dir=Path(tmp) / "ex",
                data_root=self.home / "share",
            )
            export_dir = Path(exported["export_dir"])
            (export_dir / "external_anchor.json").unlink()
            meta_path = export_dir / "verification_metadata.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["modes"]["EXTERNAL_ANCHOR"] = "optional"
            meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(export_dir, evaluate_trust_now_policy=False)
            self.assertEqual(ctx.exception.code, "missing_anchor")
            # Explicit opt-out still works.
            out = verify_export_package(
                export_dir,
                evaluate_trust_now_policy=False,
                allow_missing_external_anchor=True,
            )
            self.assertTrue(out["ok"])

    def test_coherent_forgery_fails_expect_key_id(self) -> None:
        """Package self-consistency may PASS; expect_key_id must FAIL forgery."""
        with tempfile.TemporaryDirectory() as tmp:
            _sample_signed_bundle(Path(tmp), self.home, run_id="run-forge")
            exported = export_trust_package(
                "run-forge",
                output_dir=Path(tmp) / "ex",
                data_root=self.home / "share",
            )
            export_dir = Path(exported["export_dir"])
            # Without expect_key_id, legitimate package passes (self-consistency).
            self.assertTrue(
                verify_export_package(export_dir, evaluate_trust_now_policy=False)["ok"]
            )
            # Operator key id is the verifier root for this test.
            self.assertTrue(
                verify_export_package(
                    export_dir,
                    evaluate_trust_now_policy=False,
                    expect_key_id=self.key.key_id,
                )["ok"]
            )
            foreign = generate_ed25519_keypair()
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(
                    export_dir,
                    evaluate_trust_now_policy=False,
                    expect_key_id=foreign.key_id,
                )


if __name__ == "__main__":
    unittest.main()
