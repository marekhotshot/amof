"""Trust Layer Wave 005 — ML-DSA-65 provider + algorithm agility."""

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

from amof.commands.trust_cmd import cmd_trust_keygen
from amof.trust_crypto import (
    FilesystemKeyProvider,
    TrustPolicy,
    export_trust_package,
    init_transparency_log,
    load_trust_policy,
    sign_evidence_bundle,
    verify_bundle_signature,
    verify_export_package,
    write_trust_policy,
)
from amof.trust_crypto.algorithms import ALGORITHM_ML_DSA_65, algorithm_class
from amof.trust_layer import (
    TrustIntegrityError,
    build_provenance_document,
    sha256_file,
    write_canonical_evidence_bundle,
)


def _sample_bundle(tmp: Path, home: Path, *, run_id: str) -> Path:
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
        json.dumps(seal_receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
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
    return bundle


class TrustLayerWave005Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._home_td = tempfile.TemporaryDirectory()
        self.home = Path(self._home_td.name)
        self._env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)}, clear=False)
        self._env.start()
        self.provider = FilesystemKeyProvider()
        init_transparency_log()

    def tearDown(self) -> None:
        self._env.stop()
        self._home_td.cleanup()

    def _enroll(self, key) -> TrustPolicy:
        policy = TrustPolicy(
            allowed_key_ids=frozenset({key.key_id}),
            revoked_key_ids=frozenset(),
            preferred_key_id=key.key_id,
            require_signatures=True,
            allow_unknown_keys=False,
            allow_unsigned=False,
        )
        write_trust_policy(policy)
        return policy

    def test_ml_dsa_keygen_sign_verify(self) -> None:
        key = self.provider.generate_keypair(algorithm="ml-dsa")
        self.assertEqual(key.algorithm, ALGORITHM_ML_DSA_65)
        self.assertEqual(algorithm_class(key.algorithm), "PQC")
        policy = self._enroll(key)
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), self.home, run_id="run-pq-1")
            sig = sign_evidence_bundle(bundle, key_provider=self.provider, policy=policy)
            self.assertEqual(sig["algorithm"], ALGORITHM_ML_DSA_65)
            self.assertEqual(sig["public_key_fingerprint"], sig["public_key_id"])
            verified = verify_bundle_signature(
                bundle, key_provider=self.provider, policy=policy
            )
            self.assertTrue(verified["ok"])
            self.assertEqual(verified["algorithm"], ALGORITHM_ML_DSA_65)

    def test_keygen_cli_algorithm_ml_dsa(self) -> None:
        rc = cmd_trust_keygen(
            SimpleNamespace(
                algorithm="ml-dsa",
                preferred=True,
                require_signatures=True,
                json=False,
            )
        )
        self.assertEqual(rc, 0)
        policy = load_trust_policy()
        self.assertIsNotNone(policy.preferred_key_id)
        pub = self.provider.get_public_key(policy.preferred_key_id)
        self.assertEqual(pub.algorithm, ALGORITHM_ML_DSA_65)

    def test_no_auto_keygen_on_sign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), self.home, run_id="run-no-key")
            with self.assertRaises(TrustIntegrityError) as ctx:
                sign_evidence_bundle(
                    bundle,
                    key_provider=self.provider,
                    policy=TrustPolicy(
                        allowed_key_ids=frozenset(),
                        revoked_key_ids=frozenset(),
                        preferred_key_id=None,
                        require_signatures=True,
                        allow_unknown_keys=False,
                        allow_unsigned=False,
                    ),
                )
            self.assertEqual(ctx.exception.code, "missing_key")

    def test_algorithm_agility_same_flow(self) -> None:
        """Run A Ed25519 + Run B ML-DSA share identical trust flow."""
        ed = self.provider.generate_keypair(algorithm="ed25519")
        pq = self.provider.generate_keypair(algorithm="ml-dsa-65")
        write_trust_policy(
            TrustPolicy(
                allowed_key_ids=frozenset({ed.key_id, pq.key_id}),
                revoked_key_ids=frozenset(),
                preferred_key_id=ed.key_id,
                require_signatures=True,
                allow_unknown_keys=False,
                allow_unsigned=False,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_a = _sample_bundle(tmp_path, self.home, run_id="run-A-ed25519")
            run_b = _sample_bundle(tmp_path, self.home, run_id="run-B-ml-dsa")
            policy = load_trust_policy()
            sig_a = sign_evidence_bundle(
                run_a, key_provider=self.provider, policy=policy, key_id=ed.key_id
            )
            # Prefer PQ for B without separate runtime path.
            write_trust_policy(
                TrustPolicy(
                    allowed_key_ids=frozenset({ed.key_id, pq.key_id}),
                    revoked_key_ids=frozenset(),
                    preferred_key_id=pq.key_id,
                    require_signatures=True,
                    allow_unknown_keys=False,
                    allow_unsigned=False,
                )
            )
            policy_b = load_trust_policy()
            sig_b = sign_evidence_bundle(
                run_b, key_provider=self.provider, policy=policy_b, key_id=pq.key_id
            )
            self.assertEqual(sig_a["algorithm"], "ed25519")
            self.assertEqual(sig_b["algorithm"], ALGORITHM_ML_DSA_65)
            # Same schema / digest fields — only algorithm/material differ.
            self.assertEqual(sig_a["schema"], sig_b["schema"])
            self.assertEqual(set(sig_a.keys()), set(sig_b.keys()))

            out = tmp_path / "exports"
            exp_a = export_trust_package(
                "run-A-ed25519", output_dir=out, data_root=self.home / "share"
            )
            exp_b = export_trust_package(
                "run-B-ml-dsa", output_dir=out, data_root=self.home / "share"
            )
            # Clean-room-ish: wipe private keys before verify-export.
            shutil.rmtree(self.home / "config" / "trust" / "keys")
            for path in (exp_a["export_dir"], exp_b["export_dir"]):
                result = verify_export_package(path, evaluate_trust_now_policy=False)
                self.assertTrue(result["ok"], msg=result)
                self.assertEqual(result["modes"]["LOCAL_INTEGRITY"]["status"], "PASS")
                self.assertEqual(result["modes"]["SIGNATURE_TRUST"]["status"], "PASS")
                self.assertEqual(result["modes"]["EXTERNAL_ANCHOR"]["status"], "PASS")

    def test_mixed_historical_verification(self) -> None:
        ed = self.provider.generate_keypair(algorithm="ed25519")
        pq = self.provider.generate_keypair(algorithm="ml-dsa")
        write_trust_policy(
            TrustPolicy(
                allowed_key_ids=frozenset({ed.key_id, pq.key_id}),
                revoked_key_ids=frozenset(),
                preferred_key_id=pq.key_id,
                require_signatures=True,
                allow_unknown_keys=False,
                allow_unsigned=False,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            old = _sample_bundle(Path(tmp), self.home, run_id="run-old-ed")
            new = _sample_bundle(Path(tmp), self.home, run_id="run-new-pq")
            policy = load_trust_policy()
            sign_evidence_bundle(
                old, key_provider=self.provider, policy=policy, key_id=ed.key_id
            )
            sign_evidence_bundle(
                new, key_provider=self.provider, policy=policy, key_id=pq.key_id
            )
            self.assertTrue(
                verify_bundle_signature(
                    old, key_provider=self.provider, policy=policy
                )["ok"]
            )
            self.assertTrue(
                verify_bundle_signature(
                    new, key_provider=self.provider, policy=policy
                )["ok"]
            )

    def test_revoked_pq_key_fail_closed(self) -> None:
        from amof.trust_crypto import revoke_key

        key = self.provider.generate_keypair(algorithm="ml-dsa-65")
        policy = self._enroll(key)
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_bundle(Path(tmp), self.home, run_id="run-rev")
            sign_evidence_bundle(bundle, key_provider=self.provider, policy=policy)
            revoked = revoke_key(policy, key.key_id)
            write_trust_policy(revoked)
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_bundle_signature(
                    bundle, key_provider=self.provider, policy=revoked
                )
            self.assertEqual(ctx.exception.code, "revoked_key")


if __name__ == "__main__":
    unittest.main()
