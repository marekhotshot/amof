"""Adversarial acceptance for Wave 004 export / pin / snapshot / anchor."""

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
from amof.trust_crypto.policy import revoke_key
from amof.trust_layer import (
    TrustIntegrityError,
    build_provenance_document,
    sha256_file,
    write_canonical_evidence_bundle,
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
    seal_dir = tmp / f"seal-{run_id}"
    seal_dir.mkdir()
    (seal_dir / "artifacts").mkdir()
    art = seal_dir / "artifacts" / "result.json"
    art.write_bytes(staging.read_bytes())
    seal_receipt = {
        "schema": "amof.runtime_evidence_seal/v1",
        "receipt_id": f"handoff-seal-{run_id}",
        "sealed_at": "2026-08-07T10:00:02Z",
        "claim_summary": "adv",
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
        operator="adv",
        payload_sha256="d" * 64,
        workspace_root=str(tmp / "workspace"),
        git_sha="a" * 40,
        base_sha="a" * 40,
        why="adv",
    )
    bundle = home / "share" / "trust" / "runs" / run_id
    write_canonical_evidence_bundle(
        bundle,
        run_id=run_id,
        receipt=receipt,
        result=result,
        provenance=provenance,
        result_source=staging,
    )
    from amof.trust_crypto import load_trust_policy

    sign_evidence_bundle(
        bundle, key_provider=FilesystemKeyProvider(), policy=load_trust_policy()
    )
    return bundle


class Wave004AdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name)
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
        self._td.cleanup()

    def _export(self, tmp: Path, run_id: str) -> Path:
        _signed_bundle(tmp, self.home, run_id=run_id)
        result = export_trust_package(
            run_id, output_dir=tmp / "exports", data_root=self.home / "share"
        )
        return Path(result["export_dir"])

    # --- snapshot ---

    def test_snapshot_field_tampers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = self._export(Path(tmp), "snap-1")
            path = export_dir / "trust_snapshot.json"
            base = json.loads(path.read_text(encoding="utf-8"))
            cases = [
                ({"trusted_key_id": "f" * 64}, {"snapshot_mismatch", "anchor_binding_mismatch"}),
                ({"public_key_fingerprint": "e" * 64}, {"snapshot_mismatch", "anchor_binding_mismatch"}),
                # Bound into external_anchor via trust_snapshot_digest.
                ({"policy_digest": "0" * 64}, {"anchor_binding_mismatch"}),
                ({"finalized_at": "1999-01-01T00:00:00Z"}, {"anchor_binding_mismatch"}),
                ({"revoked_at_finalization": True}, {"snapshot_untrusted", "anchor_binding_mismatch"}),
                ({"trust_decision": "UNKNOWN"}, {"snapshot_untrusted", "anchor_binding_mismatch"}),
                ({"manifest_digest": "1" * 64}, {"snapshot_mismatch", "anchor_binding_mismatch"}),
            ]
            for patch_fields, codes in cases:
                obj = dict(base)
                obj.update(patch_fields)
                path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
                with self.assertRaises(TrustIntegrityError) as ctx:
                    verify_export_package(export_dir, evaluate_trust_now_policy=False)
                self.assertIn(ctx.exception.code, codes)
                path.write_text(json.dumps(base) + "\n", encoding="utf-8")

    def test_snapshot_cross_run_and_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = self._export(root, "snap-a")
            b = self._export(root, "snap-b")
            shutil.copy2(a / "trust_snapshot.json", b / "trust_snapshot.json")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(b, evaluate_trust_now_policy=False)
            (b / "trust_snapshot.json").unlink()
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(b, evaluate_trust_now_policy=False)
            self.assertEqual(ctx.exception.code, "missing_file")

    def test_trust_at_finalization_vs_trust_now(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = self._export(Path(tmp), "snap-now")
            from amof.trust_crypto import load_trust_policy

            write_trust_policy(revoke_key(load_trust_policy(), self.key.key_id))
            out = verify_export_package(export_dir, evaluate_trust_now_policy=True)
            self.assertTrue(out["ok"])
            self.assertEqual(out["modes"]["SIGNATURE_TRUST"]["status"], "PASS")
            self.assertEqual(out["modes"]["TRUST_NOW"]["status"], "REVOKED")

    # --- pinning ---

    def test_pin_and_key_attacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = self._export(Path(tmp), "pin-1")
            other = generate_ed25519_keypair()
            # Replace public key only
            pub_path = export_dir / "public_key.json"
            pub = json.loads(pub_path.read_text(encoding="utf-8"))
            pub["public_key_raw_b64"] = base64.b64encode(other.public_key_raw).decode()
            pub_path.write_text(json.dumps(pub) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            # Restore then replace pin only
            export_dir = self._export(Path(tmp), "pin-2")
            pin_path = export_dir / "trust_anchor.json"
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
            pin["public_key_raw_b64"] = base64.b64encode(other.public_key_raw).decode()
            pin_path.write_text(json.dumps(pin) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            # Replace both key and pin consistently with foreign key (signature fails)
            export_dir = self._export(Path(tmp), "pin-3")
            pub = json.loads((export_dir / "public_key.json").read_text(encoding="utf-8"))
            pin = json.loads((export_dir / "trust_anchor.json").read_text(encoding="utf-8"))
            b64 = base64.b64encode(other.public_key_raw).decode()
            pub["public_key_raw_b64"] = b64
            pub["public_key_id"] = other.key_id
            pub["public_key_fingerprint"] = other.key_id
            pin["public_key_raw_b64"] = b64
            pin["public_key_id"] = other.key_id
            pin["public_key_fingerprint"] = other.key_id
            (export_dir / "public_key.json").write_text(json.dumps(pub) + "\n", encoding="utf-8")
            (export_dir / "trust_anchor.json").write_text(json.dumps(pin) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            # Malformed key
            export_dir = self._export(Path(tmp), "pin-4")
            pub = json.loads((export_dir / "public_key.json").read_text(encoding="utf-8"))
            pub["public_key_raw_b64"] = base64.b64encode(b"\x01\x02").decode()
            (export_dir / "public_key.json").write_text(json.dumps(pub) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            # Symlink substitution of public key
            export_dir = self._export(Path(tmp), "pin-5")
            target = Path(tmp) / "evil-pub.json"
            target.write_text((export_dir / "public_key.json").read_text(encoding="utf-8"))
            (export_dir / "public_key.json").unlink()
            (export_dir / "public_key.json").symlink_to(target)
            # Symlink file still readable; not rejected unless path_safety applied.
            # Document: export verify reads JSON content; symlink to equivalent content may PASS.
            # Replace target with evil content:
            evil = json.loads(target.read_text(encoding="utf-8"))
            evil["public_key_raw_b64"] = base64.b64encode(other.public_key_raw).decode()
            target.write_text(json.dumps(evil) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

    # --- external anchor ---

    def test_anchor_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = self._export(root, "anc-a")
            b = self._export(root, "anc-b")
            # Cross-run inclusion/checkpoint copy
            shutil.copy2(a / "external_anchor.json", b / "external_anchor.json")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(b, evaluate_trust_now_policy=False)
            self.assertEqual(ctx.exception.code, "anchor_binding_mismatch")

            export_dir = self._export(root, "anc-c")
            path = export_dir / "external_anchor.json"
            obj = json.loads(path.read_text(encoding="utf-8"))
            # Mutate leaf body digest field
            obj["body"]["manifest_digest"] = "0" * 64
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            export_dir = self._export(root, "anc-d")
            path = export_dir / "external_anchor.json"
            obj = json.loads(path.read_text(encoding="utf-8"))
            obj["inclusion"]["hashes"] = list(reversed(obj["inclusion"]["hashes"] or [])) or [
                "00" * 32
            ]
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            export_dir = self._export(root, "anc-e")
            path = export_dir / "external_anchor.json"
            obj = json.loads(path.read_text(encoding="utf-8"))
            obj["checkpoint"]["root_hash"] = "11" * 32
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            export_dir = self._export(root, "anc-f")
            path = export_dir / "external_anchor.json"
            obj = json.loads(path.read_text(encoding="utf-8"))
            raw = bytearray(base64.b64decode(obj["checkpoint"]["signature_b64"]))
            raw[0] ^= 0xFF
            obj["checkpoint"]["signature_b64"] = base64.b64encode(bytes(raw)).decode()
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            # Replace checkpoint signing key (embedded pubkey) without matching signature
            export_dir = self._export(root, "anc-g")
            path = export_dir / "external_anchor.json"
            obj = json.loads(path.read_text(encoding="utf-8"))
            foreign = generate_ed25519_keypair()
            obj["checkpoint"]["log_public_key_b64"] = base64.b64encode(
                foreign.public_key_raw
            ).decode()
            obj["checkpoint"]["log_key_id"] = foreign.key_id
            path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

    def test_tlog_init_is_explicit_and_no_overwrite(self) -> None:
        with self.assertRaises(TrustIntegrityError) as ctx:
            init_transparency_log()
        self.assertEqual(ctx.exception.code, "tlog_exists")

    # --- package closure ---

    def test_package_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = self._export(Path(tmp), "pkg-1")
            (export_dir / "extra.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(export_dir, evaluate_trust_now_policy=False)
            self.assertEqual(ctx.exception.code, "extra_file")

            export_dir = self._export(Path(tmp), "pkg-2")
            (export_dir / "manifest.json").unlink()
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(export_dir, evaluate_trust_now_policy=False)
            self.assertEqual(ctx.exception.code, "missing_file")

            export_dir = self._export(Path(tmp), "pkg-3")
            (export_dir / "result.json").rename(export_dir / "result-renamed.json")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            export_dir = self._export(Path(tmp), "pkg-4")
            path = export_dir / "result.json"
            path.write_bytes(path.read_bytes()[:-1] + b"Z")
            with self.assertRaises(TrustIntegrityError):
                verify_export_package(export_dir, evaluate_trust_now_policy=False)

            # No private keys in export
            export_dir = self._export(Path(tmp), "pkg-5")
            self.assertFalse(any(export_dir.rglob("*private*")))
            out = verify_export_package(export_dir, evaluate_trust_now_policy=False)
            self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
