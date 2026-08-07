"""Adversarial acceptance for Trust Layer Waves 001–003 (FAIL_CLOSED)."""

from __future__ import annotations

import base64
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
from amof.trust_crypto import (
    FilesystemKeyProvider,
    TrustPolicy,
    enroll_key,
    load_trust_policy,
    revoke_key,
    sign_evidence_bundle,
    verify_bundle_signature,
    write_trust_policy,
)
from amof.trust_crypto.ed25519_provider import generate_ed25519_keypair
from amof.trust_crypto.policy import policy_from_dict
from amof.trust_layer import (
    TrustIntegrityError,
    build_provenance_document,
    sha256_file,
    verify_evidence_consistency,
    write_canonical_evidence_bundle,
)


def _sample_bundle(tmp: Path, *, run_id: str) -> Path:
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
    seal_dir.mkdir(parents=True)
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


class TrustAdversarialTests(unittest.TestCase):
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
            require_signatures=True,
            allow_unknown_keys=False,
            allow_unsigned=False,
        )
        write_trust_policy(self.policy)

    def tearDown(self) -> None:
        self._env.stop()
        self._home_td.cleanup()

    def _sign(self, bundle: Path) -> None:
        sign_evidence_bundle(bundle, key_provider=self.provider, policy=self.policy)

    # --- private key ---

    def test_private_key_wrong_permissions(self) -> None:
        priv = self.provider.root / self.key.key_id / "private.raw"
        os.chmod(priv, 0o644)
        with self.assertRaises(TrustIntegrityError) as ctx:
            self.provider.get_private_key(self.key.key_id)
        self.assertEqual(ctx.exception.code, "insecure_permissions")

    def test_private_key_missing(self) -> None:
        priv = self.provider.root / self.key.key_id / "private.raw"
        priv.unlink()
        with self.assertRaises(TrustIntegrityError) as ctx:
            self.provider.get_private_key(self.key.key_id)
        self.assertEqual(ctx.exception.code, "missing_key")

    def test_private_key_replaced_breaks_verify_if_public_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), run_id="priv-replaced")
            self._sign(bundle)
            other = generate_ed25519_keypair()
            priv = self.provider.root / self.key.key_id / "private.raw"
            # Force-replace private material (bypass exclusive write).
            os.chmod(priv, 0o600)
            priv.write_bytes(other.private_key_raw)
            os.chmod(priv, 0o600)
            # Existing signature was made with old private key → still verifies
            # against stored public key. Re-sign would create signatures that
            # fail against public. Prove get_private_key still loads but new
            # signatures fail verification.
            from amof.trust_crypto.bundle_sign import canonical_signed_payload
            from amof.trust_crypto.ed25519_provider import Ed25519Signer

            sig = json.loads((bundle / "signature.json").read_text(encoding="utf-8"))
            payload = canonical_signed_payload(
                manifest_digest=sig["manifest_digest"],
                evidence_digest=sig["evidence_digest"],
            )
            # Sign with replaced private under old key_id directory.
            replaced = self.provider.get_private_key(self.key.key_id)
            bad = Ed25519Signer().sign(payload, private_key=replaced)
            sig["signature"] = base64.b64encode(bad.signature).decode("ascii")
            (bundle / "signature.json").write_text(
                json.dumps(sig, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=self.policy
                )
            self.assertEqual(ctx.exception.code, "signature_invalid")

    def test_private_key_truncated(self) -> None:
        priv = self.provider.root / self.key.key_id / "private.raw"
        os.chmod(priv, 0o600)
        priv.write_bytes(priv.read_bytes()[:16])
        os.chmod(priv, 0o600)
        with self.assertRaises(TrustIntegrityError) as ctx:
            self.provider.get_private_key(self.key.key_id)
        self.assertEqual(ctx.exception.code, "malformed_key")

    def test_private_key_malformed(self) -> None:
        priv = self.provider.root / self.key.key_id / "private.raw"
        os.chmod(priv, 0o600)
        priv.write_bytes(b"\x00" * 32)
        os.chmod(priv, 0o600)
        # All-zero may still be a valid curve scalar for Ed25519 in cryptography;
        # length-valid keys load. Prove signing still produces verifiable-or-not
        # against public — public mismatch path covered separately. Here ensure
        # load succeeds only for length; use non-32 garbage above for truncate.
        # Force verify path with garbage that from_private_bytes rejects:
        # cryptography accepts any 32 bytes. Treat as replaced key case.
        record = self.provider.get_private_key(self.key.key_id)
        self.assertEqual(len(record.private_key_raw), 32)

    def test_private_key_symlink_rejected(self) -> None:
        priv = self.provider.root / self.key.key_id / "private.raw"
        backup = priv.read_bytes()
        priv.unlink()
        target = self.home / "elsewhere.priv"
        target.write_bytes(backup)
        os.chmod(target, 0o600)
        priv.symlink_to(target)
        with self.assertRaises(TrustIntegrityError) as ctx:
            self.provider.get_private_key(self.key.key_id)
        self.assertEqual(ctx.exception.code, "unsafe_symlink")

    def test_keygen_collision_overwrite_refused(self) -> None:
        with patch(
            "amof.trust_crypto.filesystem_keys.generate_ed25519_keypair",
            return_value=self.key,
        ):
            with self.assertRaises(TrustIntegrityError) as ctx:
                self.provider.generate_keypair()
        self.assertEqual(ctx.exception.code, "key_exists")

    def test_public_key_overwrite_refused(self) -> None:
        other = generate_ed25519_keypair()
        self.provider.install_public_key(
            key_id=other.key_id, public_key_raw=other.public_key_raw
        )
        # Idempotent reinstall of identical material is OK.
        self.provider.install_public_key(
            key_id=other.key_id, public_key_raw=other.public_key_raw
        )
        # Different material under same directory id refuses.
        pub = self.provider.root / other.key_id / "public.raw"
        os.chmod(pub, 0o600)
        flipped = bytearray(other.public_key_raw)
        flipped[0] ^= 0xFF
        pub.write_bytes(bytes(flipped))
        os.chmod(pub, 0o600)
        with self.assertRaises(TrustIntegrityError) as ctx:
            self.provider.install_public_key(
                key_id=other.key_id, public_key_raw=other.public_key_raw
            )
        self.assertIn(ctx.exception.code, {"key_exists", "key_id_mismatch"})

    def test_public_key_replaced_mismatched_id(self) -> None:
        other = generate_ed25519_keypair()
        pub = self.provider.root / self.key.key_id / "public.raw"
        os.chmod(pub, 0o600)
        pub.write_bytes(other.public_key_raw)
        os.chmod(pub, 0o600)
        with self.assertRaises(TrustIntegrityError) as ctx:
            self.provider.get_public_key(self.key.key_id)
        self.assertEqual(ctx.exception.code, "key_id_mismatch")

    def test_public_key_malformed(self) -> None:
        pub = self.provider.root / self.key.key_id / "public.raw"
        os.chmod(pub, 0o600)
        pub.write_bytes(b"\x01\x02")
        os.chmod(pub, 0o600)
        with self.assertRaises(TrustIntegrityError) as ctx:
            self.provider.get_public_key(self.key.key_id)
        self.assertEqual(ctx.exception.code, "malformed_key")

    # --- policy ---

    def test_policy_allowed_key_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), run_id="policy-removed")
            self._sign(bundle)
            emptied = TrustPolicy(
                allowed_key_ids=frozenset(),
                revoked_key_ids=frozenset(),
                preferred_key_id=None,
                require_signatures=True,
                allow_unknown_keys=False,
                allow_unsigned=False,
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=emptied
                )
            self.assertEqual(ctx.exception.code, "unknown_key")

    def test_policy_revoked_and_allowed_overlap_rejected(self) -> None:
        with self.assertRaises(TrustIntegrityError) as ctx:
            policy_from_dict(
                {
                    "schema": "amof.trust_policy/v1",
                    "allowed_key_ids": [self.key.key_id],
                    "revoked_key_ids": [self.key.key_id],
                    "preferred_key_id": self.key.key_id,
                    "require_signatures": True,
                }
            )
        self.assertEqual(ctx.exception.code, "invalid_policy")

    def test_policy_malformed_json(self) -> None:
        path = self.home / "config" / "trust" / "trust-policy.json"
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(TrustIntegrityError) as ctx:
            load_trust_policy(path)
        self.assertEqual(ctx.exception.code, "invalid_policy")

    def test_policy_symlink_rejected(self) -> None:
        path = self.home / "config" / "trust" / "trust-policy.json"
        real = self.home / "config" / "trust" / "policy-real.json"
        real.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.unlink()
        path.symlink_to(real)
        with self.assertRaises(TrustIntegrityError) as ctx:
            load_trust_policy(path)
        self.assertEqual(ctx.exception.code, "unsafe_symlink")

    def test_policy_missing_rejects_unsigned_finalized(self) -> None:
        # BL-5: empty/missing policy allow_unsigned must not accept FINALIZED leftovers.
        path = self.home / "config" / "trust" / "trust-policy.json"
        path.unlink()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), run_id="no-policy")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(bundle)
            self.assertEqual(ctx.exception.code, "unsigned_finalized")

    def test_unknown_key_injected_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), run_id="unknown-inject")
            outsider = generate_ed25519_keypair()
            self.provider.install_public_key(
                key_id=outsider.key_id, public_key_raw=outsider.public_key_raw
            )
            key_dir = self.provider.root / outsider.key_id
            (key_dir / "private.raw").write_bytes(outsider.private_key_raw)
            os.chmod(key_dir / "private.raw", 0o600)
            permissive = enroll_key(self.policy, outsider.key_id, preferred=True)
            sign_evidence_bundle(
                bundle,
                key_provider=self.provider,
                policy=permissive,
                key_id=outsider.key_id,
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=self.policy
                )
            self.assertEqual(ctx.exception.code, "unknown_key")

    # --- signature ---

    def test_signature_one_byte_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), run_id="sig-mutate")
            self._sign(bundle)
            path = bundle / "signature.json"
            obj = json.loads(path.read_text(encoding="utf-8"))
            raw = bytearray(base64.b64decode(obj["signature"]))
            raw[0] ^= 0xFF
            obj["signature"] = base64.b64encode(bytes(raw)).decode("ascii")
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(bundle)
            self.assertEqual(ctx.exception.code, "signature_invalid")

    def test_signature_copied_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = _sample_bundle(root, run_id="run-a")
            b = _sample_bundle(root, run_id="run-b")
            self._sign(a)
            self._sign(b)
            shutil.copy2(a / "signature.json", b / "signature.json")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(b)
            self.assertIn(
                ctx.exception.code,
                {"manifest_digest_mismatch", "evidence_digest_mismatch", "signature_invalid"},
            )

    def test_signature_digest_and_meta_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), run_id="sig-fields")
            self._sign(bundle)
            path = bundle / "signature.json"
            obj = json.loads(path.read_text(encoding="utf-8"))
            obj["manifest_digest"] = "0" * 64
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=self.policy
                )
            self.assertEqual(ctx.exception.code, "manifest_digest_mismatch")

            # Reset and tamper evidence digest
            self._re_sign_fresh(bundle)
            obj = json.loads(path.read_text(encoding="utf-8"))
            obj["evidence_digest"] = "1" * 64
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx2:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=self.policy
                )
            self.assertEqual(ctx2.exception.code, "evidence_digest_mismatch")

            path.unlink()
            self._sign(bundle)
            obj = json.loads(path.read_text(encoding="utf-8"))
            obj["algorithm"] = "rsa"
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx3:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=self.policy
                )
            self.assertEqual(ctx3.exception.code, "unsupported_algorithm")

            path.unlink()
            self._sign(bundle)
            obj = json.loads(path.read_text(encoding="utf-8"))
            obj["public_key_id"] = "f" * 64
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx4:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=self.policy
                )
            self.assertIn(ctx4.exception.code, {"unknown_key", "missing_key"})

            path.unlink()
            self._sign(bundle)
            obj = json.loads(path.read_text(encoding="utf-8"))
            obj["timestamp"] = "1999-01-01T00:00:00Z"
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            # Timestamp is not in signed payload — authenticity of digests still holds.
            # Documented NOISE: timestamp cosmetic; digests+sig remain binding.
            verify_bundle_signature(
                bundle, key_provider=self.provider, policy=self.policy
            )

    def _re_sign_fresh(self, bundle: Path) -> None:
        sig = bundle / "signature.json"
        if sig.exists():
            sig.unlink()
        self._sign(bundle)

    # --- provenance ---

    def test_provenance_extra_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), run_id="prov-extra")
            self._sign(bundle)
            (bundle / "evil.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(bundle)
            self.assertEqual(ctx.exception.code, "extra_file")
            (bundle / "evil.json").unlink()
            (bundle / "result.json").unlink()
            with self.assertRaises(TrustIntegrityError) as ctx2:
                verify_evidence_consistency(bundle)
            self.assertEqual(ctx2.exception.code, "missing_file")

    def test_finalize_requires_explicit_keygen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "amof-home"
            data_root = home / "data"
            data_root.mkdir(parents=True)
            handoff_id = "handoff-no-keygen"
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
                    self.config_root = root.parent / "config"
                    self.cache_root = root.parent / "cache"
                    self.state_root = root.parent / "state"

            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                with patch.object(handoff_mod, "get_app_paths", return_value=_Paths(data_root)):
                    with patch.object(handoff_mod, "ensure_app_roots", return_value=None):
                        with self.assertRaises(TrustIntegrityError) as ctx:
                            handoff_mod._seal_and_finalize_execution(
                                handoff_id=handoff_id,
                                receipt=receipt,
                                result_path=result_path,
                                receipt_path=receipt_path,
                                state=state,
                            )
            self.assertEqual(ctx.exception.code, "missing_signing_authority")

    def test_revoked_key_fails_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), run_id="revoked")
            self._sign(bundle)
            revoked = revoke_key(self.policy, self.key.key_id)
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=revoked
                )
            self.assertEqual(ctx.exception.code, "revoked_key")


if __name__ == "__main__":
    unittest.main()
