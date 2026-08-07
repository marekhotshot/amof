"""Trust Layer Wave 004 — exportable package + offline verify + transparency."""

from __future__ import annotations

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

from amof.commands.trust_cmd import cmd_trust_export, cmd_trust_verify_export
from amof.trust_crypto import (
    FilesystemKeyProvider,
    TrustPolicy,
    export_trust_package,
    sign_evidence_bundle,
    verify_export_package,
    write_trust_policy,
)
from amof.trust_crypto.policy import revoke_key
from amof.trust_layer import (
    TrustIntegrityError,
    build_provenance_document,
    sha256_file,
    write_canonical_evidence_bundle,
)


def _sample_signed_bundle(tmp: Path, home: Path, *, run_id: str) -> Path:
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
    seal_dir.mkdir()
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
    # Place under data_root/trust/runs for export CLI.
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
    # Prefer key already created in setUp
    from amof.trust_crypto import load_trust_policy

    policy = load_trust_policy()
    sign_evidence_bundle(bundle, key_provider=provider, policy=policy)
    return bundle


class TrustLayerWave004Tests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self._env.stop()
        self._home_td.cleanup()

    def test_export_and_offline_verify_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _sample_signed_bundle(Path(tmp), self.home, run_id="run-export-1")
            out = Path(tmp) / "exports"
            result = export_trust_package(
                "run-export-1",
                output_dir=out,
                data_root=self.home / "share",
            )
            export_dir = Path(result["export_dir"])
            self.assertTrue((export_dir / "signature.json").is_file())
            self.assertTrue((export_dir / "public_key.json").is_file())
            self.assertTrue((export_dir / "trust_snapshot.json").is_file())
            self.assertTrue((export_dir / "external_anchor.json").is_file())
            self.assertFalse(any(export_dir.rglob("private.raw")))

            verified = verify_export_package(export_dir, evaluate_trust_now_policy=False)
            self.assertTrue(verified["ok"])
            self.assertEqual(verified["modes"]["LOCAL_INTEGRITY"]["status"], "PASS")
            self.assertEqual(verified["modes"]["SIGNATURE_TRUST"]["status"], "PASS")
            self.assertEqual(verified["modes"]["EXTERNAL_ANCHOR"]["status"], "PASS")

    def test_verify_after_original_workspace_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_signed_bundle(root, self.home, run_id="run-orphan")
            export_parent = root / "portable"
            result = export_trust_package(
                "run-orphan",
                output_dir=export_parent,
                data_root=self.home / "share",
            )
            export_dir = Path(result["export_dir"])
            # Destroy original runtime material.
            shutil.rmtree(self.home / "share" / "trust" / "runs" / "run-orphan")
            shutil.rmtree(self.home / "config" / "trust" / "keys")
            (self.home / "config" / "trust" / "trust-policy.json").unlink()
            # Also remove tlog private key — offline verify must use embedded log pubkey.
            tlog = self.home / "config" / "trust" / "tlog"
            if tlog.is_dir():
                shutil.rmtree(tlog)

            verified = verify_export_package(export_dir, evaluate_trust_now_policy=False)
            self.assertTrue(verified["ok"])

    def test_public_key_tamper_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _sample_signed_bundle(Path(tmp), self.home, run_id="run-pk")
            result = export_trust_package(
                "run-pk", output_dir=Path(tmp) / "ex", data_root=self.home / "share"
            )
            export_dir = Path(result["export_dir"])
            pub = json.loads((export_dir / "public_key.json").read_text(encoding="utf-8"))
            raw = bytearray(__import__("base64").b64decode(pub["public_key_raw_b64"]))
            raw[0] ^= 0xFF
            pub["public_key_raw_b64"] = __import__("base64").b64encode(bytes(raw)).decode()
            (export_dir / "public_key.json").write_text(json.dumps(pub) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

    def test_trust_snapshot_tamper_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _sample_signed_bundle(Path(tmp), self.home, run_id="run-snap")
            result = export_trust_package(
                "run-snap", output_dir=Path(tmp) / "ex", data_root=self.home / "share"
            )
            export_dir = Path(result["export_dir"])
            snap = json.loads((export_dir / "trust_snapshot.json").read_text(encoding="utf-8"))
            snap["trust_decision"] = "ALLOWED"
            snap["revoked_at_finalization"] = True
            (export_dir / "trust_snapshot.json").write_text(json.dumps(snap) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(export_dir, evaluate_trust_now_policy=False)
            self.assertIn(ctx.exception.code, {"snapshot_untrusted", "snapshot_mismatch"})

    def test_signature_tamper_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _sample_signed_bundle(Path(tmp), self.home, run_id="run-sig")
            result = export_trust_package(
                "run-sig", output_dir=Path(tmp) / "ex", data_root=self.home / "share"
            )
            export_dir = Path(result["export_dir"])
            sig = json.loads((export_dir / "signature.json").read_text(encoding="utf-8"))
            import base64

            raw = bytearray(base64.b64decode(sig["signature"]))
            raw[0] ^= 0xFF
            sig["signature"] = base64.b64encode(bytes(raw)).decode()
            (export_dir / "signature.json").write_text(json.dumps(sig) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

    def test_cross_run_anchor_substitution_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_signed_bundle(root, self.home, run_id="run-a")
            _sample_signed_bundle(root, self.home, run_id="run-b")
            ea = export_trust_package(
                "run-a", output_dir=root / "ex", data_root=self.home / "share"
            )
            eb = export_trust_package(
                "run-b", output_dir=root / "ex", data_root=self.home / "share"
            )
            a_dir = Path(ea["export_dir"])
            b_dir = Path(eb["export_dir"])
            shutil.copy2(a_dir / "external_anchor.json", b_dir / "external_anchor.json")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(b_dir, evaluate_trust_now_policy=False)
            self.assertEqual(ctx.exception.code, "anchor_binding_mismatch")

    def test_artifact_tamper_and_extra_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _sample_signed_bundle(Path(tmp), self.home, run_id="run-art")
            result = export_trust_package(
                "run-art", output_dir=Path(tmp) / "ex", data_root=self.home / "share"
            )
            export_dir = Path(result["export_dir"])
            path = export_dir / "result.json"
            path.write_bytes(path.read_bytes() + b"x")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            # Reset via re-export
            shutil.rmtree(export_dir)
            result = export_trust_package(
                "run-art", output_dir=Path(tmp) / "ex2", data_root=self.home / "share"
            )
            export_dir = Path(result["export_dir"])
            (export_dir / "evil.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(export_dir, evaluate_trust_now_policy=False)
            self.assertEqual(ctx.exception.code, "extra_file")

    def test_revoked_now_vs_trusted_at_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _sample_signed_bundle(Path(tmp), self.home, run_id="run-rev")
            result = export_trust_package(
                "run-rev", output_dir=Path(tmp) / "ex", data_root=self.home / "share"
            )
            export_dir = Path(result["export_dir"])
            # Revoke key in mutable policy AFTER export.
            from amof.trust_crypto import load_trust_policy

            policy = load_trust_policy()
            write_trust_policy(revoke_key(policy, self.key.key_id))
            verified = verify_export_package(export_dir, evaluate_trust_now_policy=True)
            # Offline authenticity/trust-at-finalization still PASS.
            self.assertTrue(verified["ok"])
            self.assertEqual(verified["modes"]["SIGNATURE_TRUST"]["status"], "PASS")
            self.assertEqual(verified["modes"]["TRUST_NOW"]["status"], "REVOKED")

    def test_cli_export_and_verify_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _sample_signed_bundle(Path(tmp), self.home, run_id="cli-run")
            out = Path(tmp) / "cli-exports"
            rc = cmd_trust_export(
                SimpleNamespace(
                    run_id="cli-run",
                    output=str(out),
                    json=False,
                    no_external_anchor=False,
                )
            )
            self.assertEqual(rc, 0)
            export_dir = out / "cli-run"
            rc = cmd_trust_verify_export(
                SimpleNamespace(path=str(export_dir), json=False, offline_only=True)
            )
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
