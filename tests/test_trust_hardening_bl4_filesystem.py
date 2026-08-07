"""BL-4 — hermetic export-package filesystem boundary (symlink / traversal FAIL_CLOSED)."""

from __future__ import annotations

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
from amof.trust_crypto.path_safety import (
    assert_hermetic_export_package,
    assert_safe_package_member_name,
    sha256_file_nofollow,
)
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


class TrustHardeningBL4FilesystemTests(unittest.TestCase):
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
        _sample_signed_bundle(tmp, self.home, run_id=run_id)
        result = export_trust_package(
            run_id,
            output_dir=tmp / "exports",
            data_root=self.home / "share",
        )
        return Path(result["export_dir"])

    def test_regular_in_package_files_still_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            export_dir = self._export(Path(td), "bl4-ok")
            verified = verify_export_package(
                export_dir,
                evaluate_trust_now_policy=False,
                expect_key_id=self.key.key_id,
            )
            self.assertTrue(verified["ok"])
            self.assertEqual(verified["status"], "PASS")

    def test_symlink_required_file_outside_package_fails(self) -> None:
        """B-02 reproduction: result.json → out-of-package must FAIL, not PASS."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            export_dir = self._export(root, "bl4-symlink-out")
            outside = root / "outside-result.json"
            outside.write_bytes((export_dir / "result.json").read_bytes())
            (export_dir / "result.json").unlink()
            try:
                (export_dir / "result.json").symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation not supported")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(
                    export_dir,
                    evaluate_trust_now_policy=False,
                    expect_key_id=self.key.key_id,
                )
            self.assertEqual(ctx.exception.code, "unsafe_symlink")

    def test_symlink_nested_and_in_package_targets_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Nested outside target via relative symlink.
            export_dir = self._export(root, "bl4-symlink-nested")
            nested_outside = root / "nested" / "deep" / "result.json"
            nested_outside.parent.mkdir(parents=True)
            nested_outside.write_bytes((export_dir / "result.json").read_bytes())
            (export_dir / "result.json").unlink()
            try:
                (export_dir / "result.json").symlink_to(
                    os.path.relpath(nested_outside, export_dir)
                )
            except OSError:
                self.skipTest("symlink creation not supported")
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(
                    export_dir,
                    evaluate_trust_now_policy=False,
                    expect_key_id=self.key.key_id,
                )
            self.assertEqual(ctx.exception.code, "unsafe_symlink")

            # Symlink to another in-package file still rejected (not hermetic bytes).
            export_dir2 = self._export(root, "bl4-symlink-inpkg")
            (export_dir2 / "result.json").unlink()
            try:
                (export_dir2 / "result.json").symlink_to("receipt.json")
            except OSError:
                self.skipTest("symlink creation not supported")
            with self.assertRaises(TrustIntegrityError) as ctx2:
                verify_export_package(
                    export_dir2,
                    evaluate_trust_now_policy=False,
                    expect_key_id=self.key.key_id,
                )
            self.assertEqual(ctx2.exception.code, "unsafe_symlink")

    def test_path_traversal_and_absolute_member_names_fail(self) -> None:
        for bad in (
            "..",
            "../evil",
            "foo/bar",
            "foo\\bar",
            "/abs",
            "\\abs",
            "C:evil",
            "x\x00y",
            "",
        ):
            with self.assertRaises(TrustIntegrityError) as ctx:
                assert_safe_package_member_name(bad)
            self.assertEqual(ctx.exception.code, "unsafe_path")

        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td) / "pkg"
            pkg.mkdir()
            (pkg / "ok.json").write_text("{}\n", encoding="utf-8")
            # Simulate a nested escape layout the enumerator must reject.
            nested = pkg / "subdir"
            nested.mkdir()
            (nested / "x.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(TrustIntegrityError) as ctx:
                assert_hermetic_export_package(pkg)
            self.assertIn(ctx.exception.code, {"extra_file", "unsafe_path"})

    def test_hardlink_required_file_fails_when_detectable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            export_dir = self._export(root, "bl4-hardlink")
            outside = root / "hardlink-outside-result.json"
            try:
                os.link(export_dir / "result.json", outside)
            except OSError:
                self.skipTest("hardlink creation not supported")
            # nlink on the package member should now be >= 2.
            with self.assertRaises(TrustIntegrityError) as ctx:
                verify_export_package(
                    export_dir,
                    evaluate_trust_now_policy=False,
                    expect_key_id=self.key.key_id,
                )
            self.assertEqual(ctx.exception.code, "unsafe_hardlink")

    def test_sha256_nofollow_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real.json"
            real.write_text('{"a":1}\n', encoding="utf-8")
            link = root / "link.json"
            try:
                link.symlink_to(real)
            except OSError:
                self.skipTest("symlink creation not supported")
            if getattr(os, "O_NOFOLLOW", 0):
                with self.assertRaises(TrustIntegrityError) as ctx:
                    sha256_file_nofollow(link)
                self.assertEqual(ctx.exception.code, "unsafe_symlink")
            digest = sha256_file_nofollow(real)
            self.assertEqual(digest, sha256_file(real))


if __name__ == "__main__":
    unittest.main()
