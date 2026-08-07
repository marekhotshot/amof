"""Trust Layer Wave 003 — local Ed25519 signatures (FAIL_CLOSED)."""

from __future__ import annotations

import base64
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

from amof.commands.trust_cmd import cmd_trust_keygen, cmd_trust_verify
from amof.trust_crypto import (
    FilesystemKeyProvider,
    TrustPolicy,
    enroll_key,
    revoke_key,
    sign_evidence_bundle,
    verify_bundle_signature,
    write_trust_policy,
)
from amof.trust_crypto.ed25519_provider import generate_ed25519_keypair
from amof.trust_layer import (
    TrustIntegrityError,
    build_provenance_document,
    sha256_file,
    verify_evidence_consistency,
    write_canonical_evidence_bundle,
)


def _sample_bundle(tmp: Path, *, run_id: str = "run-w3-1") -> Path:
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
    staging = tmp / "staging-result.json"
    staging.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    digest = sha256_file(staging)
    receipt["result_sha256"] = digest

    seal_dir = tmp / f"seal-{run_id}"
    seal_dir.mkdir(parents=True, exist_ok=True)
    (seal_dir / "artifacts").mkdir(exist_ok=True)
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


class TrustLayerWave003SignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_td = tempfile.TemporaryDirectory()
        self.home = Path(self._home_td.name)
        self._env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)}, clear=False)
        self._env.start()
        self.provider = FilesystemKeyProvider()
        self.key_a = self.provider.generate_keypair()
        self.key_b = self.provider.generate_keypair()
        policy = TrustPolicy(
            allowed_key_ids=frozenset({self.key_a.key_id, self.key_b.key_id}),
            revoked_key_ids=frozenset(),
            preferred_key_id=self.key_a.key_id,
            require_signatures=True,
            allow_unknown_keys=False,
            allow_unsigned=False,
        )
        write_trust_policy(policy)
        self.policy = policy

    def tearDown(self) -> None:
        self._env.stop()
        self._home_td.cleanup()

    def _sign(self, bundle: Path, *, key_id: str | None = None) -> dict:
        return sign_evidence_bundle(
            bundle,
            key_provider=self.provider,
            policy=self.policy,
            key_id=key_id,
        )

    def test_signed_bundle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            sig = self._sign(bundle)
            self.assertEqual(sig["algorithm"], "ed25519")
            self.assertEqual(sig["public_key_id"], self.key_a.key_id)
            self.assertTrue((bundle / "signature.json").is_file())
            out = verify_evidence_consistency(bundle)
            self.assertTrue(out["ok"])
            self.assertTrue(out["signature"]["signed"])

    def test_tamper_signature_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            self._sign(bundle)
            path = bundle / "signature.json"
            obj = json.loads(path.read_text(encoding="utf-8"))
            raw = base64.b64decode(obj["signature"])
            flipped = bytes([raw[0] ^ 0xFF]) + raw[1:]
            obj["signature"] = base64.b64encode(flipped).decode("ascii")
            path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(bundle)
            self.assertEqual(ctx.exception.code, "signature_invalid")

    def test_tamper_manifest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            self._sign(bundle)
            # Corrupt manifest bytes after signing (digest mismatch vs signature).
            path = bundle / "manifest.json"
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_evidence_consistency(bundle)
            self.assertIn(
                ctx.exception.code,
                {"manifest_digest_mismatch", "digest_mismatch"},
            )

    def test_wrong_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            self._sign(bundle, key_id=self.key_a.key_id)
            # Replace signature with one from key_b over same digests, but claim key_a.
            sig_obj = json.loads((bundle / "signature.json").read_text(encoding="utf-8"))
            from amof.trust_crypto.bundle_sign import canonical_signed_payload
            from amof.trust_crypto.ed25519_provider import Ed25519Signer

            payload = canonical_signed_payload(
                manifest_digest=sig_obj["manifest_digest"],
                evidence_digest=sig_obj["evidence_digest"],
            )
            wrong = Ed25519Signer().sign(payload, private_key=self.key_b)
            sig_obj["signature"] = base64.b64encode(wrong.signature).decode("ascii")
            # Still claims key_a public_key_id
            (bundle / "signature.json").write_text(
                json.dumps(sig_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=self.policy
                )
            self.assertEqual(ctx.exception.code, "signature_invalid")

    def test_rotated_key_old_run_still_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_bundle = _sample_bundle(root, run_id="old-run")
            self._sign(old_bundle, key_id=self.key_a.key_id)
            # Rotate preferred to key_b; keep key_a allowed (no migration).
            rotated = TrustPolicy(
                allowed_key_ids=frozenset({self.key_a.key_id, self.key_b.key_id}),
                revoked_key_ids=frozenset(),
                preferred_key_id=self.key_b.key_id,
                require_signatures=True,
                allow_unknown_keys=False,
                allow_unsigned=False,
            )
            write_trust_policy(rotated)
            self.policy = rotated
            out = verify_evidence_consistency(old_bundle)
            self.assertTrue(out["ok"])
            new_bundle = _sample_bundle(root, run_id="new-run")
            self._sign(new_bundle, key_id=self.key_b.key_id)
            out2 = verify_evidence_consistency(new_bundle)
            self.assertTrue(out2["ok"])
            self.assertEqual(out2["signature"]["public_key_id"], self.key_b.key_id)

    def test_unknown_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            outsider = generate_ed25519_keypair()
            # Install outsider public+private under provider but not in policy.
            self.provider.install_public_key(
                key_id=outsider.key_id, public_key_raw=outsider.public_key_raw
            )
            key_dir = self.provider.root / outsider.key_id
            (key_dir / "private.raw").write_bytes(outsider.private_key_raw)
            os.chmod(key_dir / "private.raw", 0o600)
            # Sign with outsider using a temporary permissive policy for signing only.
            sign_policy = enroll_key(self.policy, outsider.key_id, preferred=True)
            sign_evidence_bundle(
                bundle, key_provider=self.provider, policy=sign_policy, key_id=outsider.key_id
            )
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=self.policy
                )
            self.assertEqual(ctx.exception.code, "unknown_key")

    def test_revoked_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            self._sign(bundle, key_id=self.key_a.key_id)
            revoked = revoke_key(self.policy, self.key_a.key_id)
            # Prefer key_b after revoke.
            revoked = TrustPolicy(
                allowed_key_ids=frozenset({self.key_b.key_id}),
                revoked_key_ids=frozenset({self.key_a.key_id}),
                preferred_key_id=self.key_b.key_id,
                require_signatures=True,
                allow_unknown_keys=False,
                allow_unsigned=False,
            )
            write_trust_policy(revoked)
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=revoked
                )
            self.assertEqual(ctx.exception.code, "revoked_key")

    def test_missing_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            self._sign(bundle, key_id=self.key_a.key_id)
            # Remove public key material while keeping policy allow-list.
            shutil.rmtree(self.provider.root / self.key_a.key_id)
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=self.policy
                )
            self.assertEqual(ctx.exception.code, "missing_key")

    def test_missing_signature_fails_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp))
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=self.policy
                )
            self.assertEqual(ctx.exception.code, "missing_signature")

    def test_unsigned_legacy_allowed_when_policy_permits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), run_id="legacy-unsigned")
            legacy = TrustPolicy(
                allowed_key_ids=frozenset({self.key_a.key_id}),
                revoked_key_ids=frozenset(),
                preferred_key_id=self.key_a.key_id,
                require_signatures=False,
                allow_unknown_keys=False,
                allow_unsigned=True,
            )
            write_trust_policy(legacy)
            out = verify_evidence_consistency(bundle)
            self.assertTrue(out["ok"])
            self.assertFalse(out["signature"]["signed"])

    def test_keygen_cli_enrolls_preferred(self) -> None:
        # Fresh home without keys from setUp — use nested isolation.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                rc = cmd_trust_keygen(
                    SimpleNamespace(
                        json=True, preferred=True, require_signatures=False
                    )
                )
                self.assertEqual(rc, 0)
                provider = FilesystemKeyProvider()
                ids = provider.list_public_key_ids()
                self.assertEqual(len(ids), 1)
                policy_path = home / "config" / "trust" / "trust-policy.json"
                self.assertTrue(policy_path.is_file())
                policy = json.loads(policy_path.read_text(encoding="utf-8"))
                self.assertEqual(policy["preferred_key_id"], ids[0])
                self.assertIn(ids[0], policy["allowed_key_ids"])

    def test_cli_verify_signed_pass_and_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = _sample_bundle(root, run_id="cli-signed")
            self._sign(bundle)
            rc = cmd_trust_verify(SimpleNamespace(run_id=str(bundle), json=False))
            self.assertEqual(rc, 0)
            # Tamper signature → FAIL
            path = bundle / "signature.json"
            obj = json.loads(path.read_text(encoding="utf-8"))
            obj["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            rc = cmd_trust_verify(SimpleNamespace(run_id=str(bundle), json=False))
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
