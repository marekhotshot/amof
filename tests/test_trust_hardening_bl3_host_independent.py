"""BL-3 — export verify LOCAL_INTEGRITY is host-independent (package-bytes-only).

Export / offline verify (require_producer_seal=False) must never open or verify
absolute evidence_seal_path on the verifier host. PASS/FAIL depends only on
package bytes — not producer filesystem seals, absolute seal paths, or producer
runtime layout.
"""

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
    verify_export_package,
    write_trust_policy,
)
from amof.trust_layer import TrustIntegrityError, verify_evidence_consistency
from tests.test_trust_layer_wave_004 import _sample_signed_bundle


def _receipt_seal_path(export_dir: Path) -> str:
    receipt = json.loads((export_dir / "receipt.json").read_text(encoding="utf-8"))
    evidence = receipt.get("evidence") if isinstance(receipt.get("evidence"), dict) else {}
    path = str(evidence.get("evidence_seal_path") or "").strip()
    assert path, "fixture receipt must advertise absolute evidence_seal_path"
    return path


def _plant_bogus_seal(seal_receipt_path: str) -> Path:
    """Create a seal at the absolute path that would fail verify_evidence_seal."""
    target = Path(seal_receipt_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    bogus = {
        "schema": "amof.runtime_evidence_seal/v1",
        "receipt_id": "bogus-verifier-host-seal",
        "sealed_at": "2099-01-01T00:00:00Z",
        "claim_summary": "planted to poison host-dependent verify",
        "artifacts": [
            {
                "name": "result.json",
                "sha256": "0" * 64,
                "bytes": 1,
                "storage": "copied",
                "sealed_path": "artifacts/result.json",
                "source_path": "/nonexistent",
                "digest_kind": "content_sha256",
            }
        ],
        "producer": {"role": "attacker", "model": "none"},
    }
    if target.name == "receipt.json":
        (target.parent / "artifacts").mkdir(parents=True, exist_ok=True)
        (target.parent / "artifacts" / "result.json").write_bytes(b"X")
        target.write_text(json.dumps(bogus, indent=2) + "\n", encoding="utf-8")
    else:
        target.mkdir(parents=True, exist_ok=True)
        (target / "artifacts").mkdir(parents=True, exist_ok=True)
        (target / "artifacts" / "result.json").write_bytes(b"X")
        (target / "receipt.json").write_text(
            json.dumps(bogus, indent=2) + "\n", encoding="utf-8"
        )
    return target


class TrustHardeningBL3HostIndependentTests(unittest.TestCase):
    """Export LOCAL_INTEGRITY must be package-bytes-only (no producer host seal)."""

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

    def _export_isolated_package(self, tmp: Path, run_id: str) -> Path:
        _sample_signed_bundle(tmp, self.home, run_id=run_id)
        exported = export_trust_package(
            run_id,
            output_dir=tmp / "ex",
            data_root=self.home / "share",
        )
        pkg = Path(exported["export_dir"])
        iso = tmp / "clean-room" / run_id
        shutil.copytree(pkg, iso)
        return iso

    def test_1_clean_room_package_passes(self) -> None:
        """Package PASS in clean room (no producer host state)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = self._export_isolated_package(root, "run-bl3-clean")
            seal_path = _receipt_seal_path(iso)
            # Destroy producer home + any seal layout under the advertised path.
            shutil.rmtree(self.home)
            if Path(seal_path).exists():
                parent = Path(seal_path)
                if parent.name == "receipt.json":
                    shutil.rmtree(parent.parent, ignore_errors=True)
                elif parent.is_dir():
                    shutil.rmtree(parent, ignore_errors=True)
            home2 = root / "empty-home"
            home2.mkdir()
            with patch.dict(os.environ, {"AMOF_HOME": str(home2)}):
                out = verify_export_package(iso, evaluate_trust_now_policy=False)
            self.assertTrue(out["ok"], msg=out)
            self.assertEqual(out["modes"]["LOCAL_INTEGRITY"]["status"], "PASS")

    def test_2_bogus_host_seal_at_absolute_path_ignored(self) -> None:
        """Same package PASS when bogus file planted at receipt absolute seal path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = self._export_isolated_package(root, "run-bl3-poison")
            seal_path = _receipt_seal_path(iso)

            # Baseline: require_producer_seal=True would fail on planted bogus seal.
            _plant_bogus_seal(seal_path)
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(
                    iso,
                    check_signature=False,
                    allowed_extra_files=frozenset(
                        {
                            "signature.json",
                            "public_key.json",
                            "trust_anchor.json",
                            "trust_snapshot.json",
                            "verification_metadata.json",
                            "external_anchor.json",
                        }
                    ),
                    require_producer_seal=True,
                )
            self.assertIn(
                ctx.exception.code,
                {"digest_mismatch", "seal_mismatch", "invalid_seal", "missing_artifact"},
            )

            # Export path: must ignore host-local seal entirely → PASS.
            out = verify_export_package(iso, evaluate_trust_now_policy=False)
            self.assertTrue(out["ok"], msg=out)
            self.assertEqual(out["modes"]["LOCAL_INTEGRITY"]["status"], "PASS")

            # Direct consistency API with require_producer_seal=False must also PASS.
            consistency = verify_evidence_consistency(
                iso,
                check_signature=False,
                allowed_extra_files=frozenset(
                    {
                        "signature.json",
                        "public_key.json",
                        "trust_anchor.json",
                        "trust_snapshot.json",
                        "verification_metadata.json",
                        "external_anchor.json",
                    }
                ),
                require_producer_seal=False,
            )
            self.assertTrue(consistency["ok"])

    def test_3_producer_seal_deleted_or_relocated_still_passes(self) -> None:
        """Same package PASS after producer seal path deleted / different layout."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = self._export_isolated_package(root, "run-bl3-reloc")
            seal_path = _receipt_seal_path(iso)
            seal = Path(seal_path)

            # Remove original producer seal if still present under tmp/home.
            if seal.exists():
                if seal.name == "receipt.json":
                    shutil.rmtree(seal.parent, ignore_errors=True)
                elif seal.is_dir():
                    shutil.rmtree(seal, ignore_errors=True)
            self.assertFalse(seal.exists())

            out_missing = verify_export_package(iso, evaluate_trust_now_policy=False)
            self.assertTrue(out_missing["ok"], msg=out_missing)
            self.assertEqual(out_missing["modes"]["LOCAL_INTEGRITY"]["status"], "PASS")

            # Different path layout: create an unrelated directory tree where the
            # absolute seal path's parent used to live; must not affect verify.
            if seal.name == "receipt.json":
                seal.parent.mkdir(parents=True, exist_ok=True)
                (seal.parent / "unrelated.txt").write_text("not-a-seal\n", encoding="utf-8")
            else:
                seal.mkdir(parents=True, exist_ok=True)
                (seal / "unrelated.txt").write_text("not-a-seal\n", encoding="utf-8")

            out_reloc = verify_export_package(iso, evaluate_trust_now_policy=False)
            self.assertTrue(out_reloc["ok"], msg=out_reloc)
            self.assertEqual(out_reloc["modes"]["LOCAL_INTEGRITY"]["status"], "PASS")

    def test_4_package_bytes_only_documented_contract(self) -> None:
        """Document: export LOCAL_INTEGRITY is package-bytes-only (no host seal I/O)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = self._export_isolated_package(root, "run-bl3-bytes")
            seal_path = _receipt_seal_path(iso)
            _plant_bogus_seal(seal_path)

            opened: list[str] = []
            real_open = Path.open

            def tracking_open(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
                opened.append(str(self))
                return real_open(self, *args, **kwargs)

            with patch.object(Path, "open", tracking_open):
                out = verify_export_package(iso, evaluate_trust_now_policy=False)
            self.assertTrue(out["ok"], msg=out)
            self.assertEqual(out["modes"]["LOCAL_INTEGRITY"]["status"], "PASS")

            # No I/O into the absolute producer seal path (or its seal artifacts).
            seal_root = str(Path(seal_path).parent if Path(seal_path).name == "receipt.json" else Path(seal_path))
            for path in opened:
                self.assertFalse(
                    path == seal_path or path.startswith(seal_root + os.sep),
                    msg=f"export verify opened producer seal path: {path}",
                )


if __name__ == "__main__":
    unittest.main()
