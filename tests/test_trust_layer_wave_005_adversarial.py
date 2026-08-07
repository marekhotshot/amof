"""Wave 005 adversarial — ML-DSA FAIL_CLOSED + downgrade resistance."""

from __future__ import annotations

import base64
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
    export_trust_package,
    init_transparency_log,
    sign_evidence_bundle,
    verify_bundle_signature,
    verify_export_package,
    write_trust_policy,
)
from amof.trust_crypto.algorithms import ALGORITHM_ML_DSA_65
from amof.trust_crypto.ed25519_provider import generate_ed25519_keypair
from amof.trust_crypto.ml_dsa_provider import generate_ml_dsa_65_keypair
from amof.trust_layer import TrustIntegrityError
from tests.test_trust_layer_wave_005 import _sample_bundle


class TrustLayerWave005AdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_td = tempfile.TemporaryDirectory()
        self.home = Path(self._home_td.name)
        self._env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)}, clear=False)
        self._env.start()
        self.provider = FilesystemKeyProvider()
        self.key = self.provider.generate_keypair(algorithm="ml-dsa-65")
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

    def _signed(self, tmp: Path, run_id: str = "run-adv") -> Path:
        return _sample_bundle(tmp, self.home, run_id=run_id)

    def test_signature_byte_flip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._signed(Path(tmp))
            sign_evidence_bundle(
                bundle, key_provider=self.provider, policy=TrustPolicy(
                    allowed_key_ids=frozenset({self.key.key_id}),
                    revoked_key_ids=frozenset(),
                    preferred_key_id=self.key.key_id,
                    require_signatures=True,
                    allow_unknown_keys=False,
                    allow_unsigned=False,
                )
            )
            sig_path = bundle / "signature.json"
            obj = json.loads(sig_path.read_text(encoding="utf-8"))
            raw = bytearray(base64.b64decode(obj["signature"]))
            raw[0] ^= 0x01
            obj["signature"] = base64.b64encode(bytes(raw)).decode("ascii")
            sig_path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(bundle, key_provider=self.provider)
            self.assertIn(ctx.exception.code, {"signature_invalid", "invalid_signature"})

    def test_wrong_public_key(self) -> None:
        other = self.provider.generate_keypair(algorithm="ml-dsa-65")
        write_trust_policy(
            TrustPolicy(
                allowed_key_ids=frozenset({self.key.key_id, other.key_id}),
                revoked_key_ids=frozenset(),
                preferred_key_id=self.key.key_id,
                require_signatures=True,
                allow_unknown_keys=False,
                allow_unsigned=False,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._signed(Path(tmp))
            sign_evidence_bundle(bundle, key_provider=self.provider)
            obj = json.loads((bundle / "signature.json").read_text(encoding="utf-8"))
            obj["public_key_id"] = other.key_id
            obj["public_key_fingerprint"] = other.key_id
            (bundle / "signature.json").write_text(
                json.dumps(obj, indent=2, sort_keys=True) + "\n"
            )
            with self.assertRaises(TrustIntegrityError):
                verify_bundle_signature(bundle, key_provider=self.provider)

    def test_malformed_public_key(self) -> None:
        key_dir = self.home / "config" / "trust" / "keys" / self.key.key_id
        (key_dir / "public.raw").write_bytes(b"\x00" * 10)
        with self.assertRaises(TrustIntegrityError) as ctx:
            self.provider.get_public_key(self.key.key_id)
        self.assertEqual(ctx.exception.code, "malformed_key")

    def test_malformed_private_key(self) -> None:
        key_dir = self.home / "config" / "trust" / "keys" / self.key.key_id
        (key_dir / "private.raw").write_bytes(b"\x00" * 8)
        with self.assertRaises(TrustIntegrityError) as ctx:
            self.provider.get_private_key(self.key.key_id)
        self.assertEqual(ctx.exception.code, "malformed_key")

    def test_wrong_algorithm_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._signed(Path(tmp))
            sign_evidence_bundle(bundle, key_provider=self.provider)
            obj = json.loads((bundle / "signature.json").read_text(encoding="utf-8"))
            obj["algorithm"] = "ed25519"
            (bundle / "signature.json").write_text(
                json.dumps(obj, indent=2, sort_keys=True) + "\n"
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(bundle, key_provider=self.provider)
            self.assertEqual(ctx.exception.code, "algorithm_mismatch")

    def test_key_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._signed(Path(tmp))
            sign_evidence_bundle(bundle, key_provider=self.provider)
            obj = json.loads((bundle / "signature.json").read_text(encoding="utf-8"))
            obj["public_key_id"] = "a" * 64
            obj["public_key_fingerprint"] = "a" * 64
            (bundle / "signature.json").write_text(
                json.dumps(obj, indent=2, sort_keys=True) + "\n"
            )
            with self.assertRaises(TrustIntegrityError):
                verify_bundle_signature(bundle, key_provider=self.provider)

    def test_unknown_pq_key(self) -> None:
        foreign = generate_ml_dsa_65_keypair()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._signed(Path(tmp))
            sign_evidence_bundle(bundle, key_provider=self.provider)
            obj = json.loads((bundle / "signature.json").read_text(encoding="utf-8"))
            obj["public_key_id"] = foreign.key_id
            obj["public_key_fingerprint"] = foreign.key_id
            (bundle / "signature.json").write_text(
                json.dumps(obj, indent=2, sort_keys=True) + "\n"
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(bundle, key_provider=self.provider)
            self.assertEqual(ctx.exception.code, "unknown_key")

    def test_downgrade_algorithm_metadata(self) -> None:
        """PQ signature + classical algorithm label must FAIL_CLOSED."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._signed(Path(tmp), run_id="run-down")
            sign_evidence_bundle(bundle, key_provider=self.provider)
            out = Path(tmp) / "ex"
            exported = export_trust_package(
                "run-down", output_dir=out, data_root=self.home / "share"
            )
            export_dir = Path(exported["export_dir"])
            sig = json.loads((export_dir / "signature.json").read_text(encoding="utf-8"))
            sig["algorithm"] = "ed25519"
            (export_dir / "signature.json").write_text(
                json.dumps(sig, indent=2, sort_keys=True) + "\n"
            )
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

    def test_upgrade_spoof_ed25519_as_ml_dsa(self) -> None:
        ed = self.provider.generate_keypair(algorithm="ed25519")
        write_trust_policy(
            TrustPolicy(
                allowed_key_ids=frozenset({self.key.key_id, ed.key_id}),
                revoked_key_ids=frozenset(),
                preferred_key_id=ed.key_id,
                require_signatures=True,
                allow_unknown_keys=False,
                allow_unsigned=False,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._signed(Path(tmp), run_id="run-up")
            sign_evidence_bundle(
                bundle, key_provider=self.provider, key_id=ed.key_id
            )
            obj = json.loads((bundle / "signature.json").read_text(encoding="utf-8"))
            obj["algorithm"] = ALGORITHM_ML_DSA_65
            (bundle / "signature.json").write_text(
                json.dumps(obj, indent=2, sort_keys=True) + "\n"
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(bundle, key_provider=self.provider)
            self.assertEqual(ctx.exception.code, "algorithm_mismatch")

    def test_cross_run_signature_substitution(self) -> None:
        other_key = self.provider.generate_keypair(algorithm="ml-dsa-65")
        write_trust_policy(
            TrustPolicy(
                allowed_key_ids=frozenset({self.key.key_id, other_key.key_id}),
                revoked_key_ids=frozenset(),
                preferred_key_id=self.key.key_id,
                require_signatures=True,
                allow_unknown_keys=False,
                allow_unsigned=False,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            a = self._signed(Path(tmp), run_id="run-sub-a")
            b = self._signed(Path(tmp), run_id="run-sub-b")
            sign_evidence_bundle(a, key_provider=self.provider, key_id=self.key.key_id)
            sign_evidence_bundle(b, key_provider=self.provider, key_id=other_key.key_id)
            sig_b = (b / "signature.json").read_text(encoding="utf-8")
            (a / "signature.json").write_text(sig_b)
            with self.assertRaises(TrustIntegrityError):
                verify_bundle_signature(a, key_provider=self.provider)

    def test_replaced_exported_pq_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._signed(Path(tmp), run_id="run-replace")
            sign_evidence_bundle(bundle, key_provider=self.provider)
            exported = export_trust_package(
                "run-replace",
                output_dir=Path(tmp) / "ex",
                data_root=self.home / "share",
            )
            export_dir = Path(exported["export_dir"])
            foreign = generate_ml_dsa_65_keypair()
            pub = json.loads((export_dir / "public_key.json").read_text(encoding="utf-8"))
            pub["public_key_raw_b64"] = base64.b64encode(foreign.public_key_raw).decode(
                "ascii"
            )
            pub["public_key_id"] = foreign.key_id
            pub["public_key_fingerprint"] = foreign.key_id
            (export_dir / "public_key.json").write_text(
                json.dumps(pub, indent=2, sort_keys=True) + "\n"
            )
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

    def test_signature_copied_with_changed_algorithm_metadata(self) -> None:
        ed = generate_ed25519_keypair()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._signed(Path(tmp), run_id="run-meta")
            sign_evidence_bundle(bundle, key_provider=self.provider)
            obj = json.loads((bundle / "signature.json").read_text(encoding="utf-8"))
            # Keep PQ signature bytes; claim classical + unrelated key id.
            obj["algorithm"] = "ed25519"
            obj["public_key_id"] = ed.key_id
            obj["public_key_fingerprint"] = ed.key_id
            (bundle / "signature.json").write_text(
                json.dumps(obj, indent=2, sort_keys=True) + "\n"
            )
            with self.assertRaises(TrustIntegrityError):
                verify_bundle_signature(bundle, key_provider=self.provider)


if __name__ == "__main__":
    unittest.main()
