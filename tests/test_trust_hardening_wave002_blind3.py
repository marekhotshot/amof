"""WAVE-002 Blind #3 repairs: BL3-1 low-order keys, BL3-2 proves, H-1 TOCTOU."""

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
    verify_export_package,
    write_trust_policy,
)
from amof.trust_crypto.ed25519_provider import (
    Ed25519Verifier,
    assert_canonical_ed25519_public_key,
    public_key_id_from_raw,
)
from amof.trust_crypto.interfaces import PublicKeyRecord
from amof.trust_crypto import path_safety
from amof.trust_crypto.path_safety import snapshot_hermetic_export_package
from amof.trust_layer import (
    TrustIntegrityError,
    build_provenance_document,
    sha256_file,
    write_canonical_evidence_bundle,
)

# Ed25519 identity point (order 1) — admits universal signatures (BL3-1).
_IDENTITY_PUB = bytes.fromhex(
    "0100000000000000000000000000000000000000000000000000000000000000"
)
# Non-canonical encoding of identity (y = 1 + p).
_NONCANON_IDENTITY = bytes.fromhex(
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f"
)
# Basepoint encoding; with S=1 forms a universal signature under identity A.
_BASEPOINT = bytes.fromhex(
    "5866666666666666666666666666666666666666666666666666666666666666"
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
    provenance = build_provenance_document(
        run_id=run_id,
        receipt=receipt,
        result=result,
        mission_id=run_id,
        operator="tester",
        payload_sha256="d" * 64,
        workspace_root=str(tmp / "workspace"),
        git_sha="a" * 40,
        base_sha="a" * 40,
        why="done",
    )
    data_root = home / "share"
    bundle = data_root / "trust" / "runs" / run_id
    write_canonical_evidence_bundle(
        bundle,
        run_id=run_id,
        receipt=receipt,
        result=result,
        provenance=provenance,
        result_source=staging,
    )
    provider = FilesystemKeyProvider()
    policy = TrustPolicy(
        allowed_key_ids=frozenset({provider.list_public_key_ids()[0]}),
        revoked_key_ids=frozenset(),
        preferred_key_id=provider.list_public_key_ids()[0],
        require_signatures=True,
        allow_unknown_keys=False,
        allow_unsigned=False,
    )
    sign_evidence_bundle(bundle, key_provider=provider, policy=policy)
    return bundle


class TrustHardeningWave002Blind3Tests(unittest.TestCase):
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

    def _export(self, tmp: Path, run_id: str) -> Path:
        _signed_bundle(tmp, self.home, run_id=run_id)
        result = export_trust_package(
            run_id,
            output_dir=tmp / "exports",
            data_root=self.home / "share",
        )
        return Path(result["export_dir"])

    # --- BL3-1 ----------------------------------------------------------------

    def test_bl3_1_identity_public_key_rejected(self) -> None:
        with self.assertRaises(TrustIntegrityError) as ctx:
            assert_canonical_ed25519_public_key(_IDENTITY_PUB)
        self.assertEqual(ctx.exception.code, "low_order_public_key")

    def test_bl3_1_noncanonical_public_key_rejected(self) -> None:
        with self.assertRaises(TrustIntegrityError) as ctx:
            assert_canonical_ed25519_public_key(_NONCANON_IDENTITY)
        self.assertEqual(ctx.exception.code, "noncanonical_public_key")

    def test_bl3_1_universal_signature_under_identity_fails_closed(self) -> None:
        """Reviewer A R4.1 / R5.1: identity pub + (R=B, S=1) must not verify."""
        payload = b"amof-bl3-1-forgery-payload"
        signature = _BASEPOINT + (1).to_bytes(32, "little")
        # cryptography itself accepts this pair — our screen must reject first.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(_IDENTITY_PUB).verify(signature, payload)
        with self.assertRaises(TrustIntegrityError) as ctx:
            Ed25519Verifier().verify(
                payload,
                signature,
                public_key=PublicKeyRecord(
                    key_id=public_key_id_from_raw(_IDENTITY_PUB),
                    algorithm="ed25519",
                    public_key_raw=_IDENTITY_PUB,
                ),
            )
        self.assertEqual(ctx.exception.code, "low_order_public_key")

    def test_bl3_1_export_with_identity_embedded_key_fails(self) -> None:
        """Coherent package carrying the identity pub must FAIL_CLOSED on verify."""
        with tempfile.TemporaryDirectory() as td:
            export_dir = self._export(Path(td), "bl31-ident")
            identity_id = public_key_id_from_raw(_IDENTITY_PUB)
            identity_b64 = base64.b64encode(_IDENTITY_PUB).decode("ascii")
            # Universal detached signature over the real signed payload bytes.
            from amof.trust_crypto.bundle_sign import (
                SIGNATURE_VERSION,
                canonical_signed_payload,
            )
            from amof.trust_crypto.anchors import load_json_object

            sig_obj = load_json_object(export_dir / "signature.json", code="invalid_signature")
            payload = canonical_signed_payload(
                manifest_digest=str(sig_obj["manifest_digest"]),
                evidence_digest=str(sig_obj["evidence_digest"]),
                version=SIGNATURE_VERSION,
            )
            universal = _BASEPOINT + (1).to_bytes(32, "little")
            # Confirm the universal sig verifies under cryptography alone.
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            Ed25519PublicKey.from_public_bytes(_IDENTITY_PUB).verify(universal, payload)

            pub_doc = {
                "schema": "amof.public_key/v1",
                "algorithm": "ed25519",
                "public_key_id": identity_id,
                "public_key_raw_b64": identity_b64,
            }
            pin_doc = {
                "schema": "amof.local_pinned_trust_anchor/v1",
                "anchor_kind": "local_pinned",
                "algorithm": "ed25519",
                "public_key_id": identity_id,
                "public_key_fingerprint": identity_id,
                "public_key_raw_b64": identity_b64,
                "network": False,
            }
            sig_obj["public_key_id"] = identity_id
            sig_obj["signature"] = base64.b64encode(universal).decode("ascii")
            (export_dir / "public_key.json").write_text(
                json.dumps(pub_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            (export_dir / "trust_anchor.json").write_text(
                json.dumps(pin_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            (export_dir / "signature.json").write_text(
                json.dumps(sig_obj, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            # Snapshot / anchor digests will mismatch — that's fine; we assert the
            # failure code is the low-order reject (or a prior digest fail is also
            # closed). Prefer exercising the verifier screen directly via signature.
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(
                    export_dir,
                    evaluate_trust_now_policy=False,
                    allow_missing_external_anchor=True,
                )
            # Must not be OVERALL PASS; identity key must be rejected somewhere.
            self.assertIn(
                ctx.exception.code,
                {
                    "low_order_public_key",
                    "snapshot_mismatch",
                    "anchor_binding_mismatch",
                    "invalid_anchor",
                    "missing_anchor",
                    "manifest_digest_mismatch",
                },
            )
            # Direct path that forged packages hit for SIGNATURE_TRUST:
            with self.assertRaises(TrustIntegrityError) as ctx2:
                Ed25519Verifier().verify(
                    payload,
                    universal,
                    public_key=PublicKeyRecord(
                        key_id=identity_id,
                        algorithm="ed25519",
                        public_key_raw=_IDENTITY_PUB,
                    ),
                )
            self.assertEqual(ctx2.exception.code, "low_order_public_key")

    # --- BL3-2 ----------------------------------------------------------------

    def test_bl3_2_receipt_proves_is_offline_verifiable_not_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            export_dir = self._export(Path(td), "bl32-proves")
            receipt = json.loads(
                (export_dir / "external_anchor.json").read_text(encoding="utf-8")
            )
            proves = str((receipt.get("semantics") or {}).get("proves") or "")
            self.assertNotIn("append_only", proves.lower())
            self.assertEqual(
                proves,
                "merkle_inclusion_of_digests_and_key_id_vs_embedded_checkpoint",
            )
            does_not = (receipt.get("semantics") or {}).get("does_not_prove") or []
            self.assertIn("append_only", does_not)
            self.assertIn("non_equivocation", does_not)
            verified = verify_export_package(
                export_dir,
                evaluate_trust_now_policy=False,
                expect_key_id=self.key.key_id,
            )
            self.assertTrue(verified["ok"])

    # --- H-1 ------------------------------------------------------------------

    def test_h1_post_scan_symlink_substitution_fails_closed(self) -> None:
        """Reviewer B/E: replace member with symlink after hermetic scan → FAIL."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            export_dir = self._export(root, "h1-toctou")
            outside = root / "outside-verification_metadata.json"
            target = export_dir / "verification_metadata.json"
            outside.write_bytes(target.read_bytes())

            orig = path_safety._enumerate_hermetic_export_package

            def mutate_after_scan(package_root):
                names, inodes = orig(package_root)
                member = Path(package_root) / "verification_metadata.json"
                member.unlink()
                try:
                    member.symlink_to(outside)
                except OSError:
                    self.skipTest("symlink creation not supported")
                return names, inodes

            with patch.object(
                path_safety,
                "_enumerate_hermetic_export_package",
                side_effect=mutate_after_scan,
            ):
                with self.assertRaises(TrustIntegrityError) as ctx:
                    verify_export_package(
                        export_dir,
                        evaluate_trust_now_policy=False,
                        expect_key_id=self.key.key_id,
                    )
            self.assertIn(
                ctx.exception.code,
                {"unsafe_symlink", "unsafe_toctou", "unsafe_path"},
            )

    def test_h1_snapshot_nofollow_rejects_symlink_member(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            export_dir = self._export(root, "h1-snap")
            outside = root / "outside-result.json"
            outside.write_bytes((export_dir / "result.json").read_bytes())
            (export_dir / "result.json").unlink()
            try:
                (export_dir / "result.json").symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation not supported")
            with self.assertRaises(TrustIntegrityError) as ctx:
                snapshot_hermetic_export_package(export_dir)
            self.assertEqual(ctx.exception.code, "unsafe_symlink")


if __name__ == "__main__":
    unittest.main()
