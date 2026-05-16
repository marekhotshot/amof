import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof.entrypoint import main


def _doctor_report(root: Path, *, verdict: str = "WARN") -> dict:
    workspace = root / "workspace"
    canonical_amof = workspace / "repos" / "amof"
    canonical_ui = workspace / "repos" / "amof-ui"
    gmd_app = workspace / "repos" / "gmd-app"
    app_home = root / "appdata"
    for path in (workspace, canonical_amof, canonical_ui, gmd_app):
        path.mkdir(parents=True, exist_ok=True)

    def _root(path: Path) -> dict:
        return {
            "path": str(path),
            "exists": True,
            "is_dir": True,
            "writable": True,
            "inside_source_workspace": False,
        }

    failures = [] if verdict != "FAIL" else ["runtime roots are misplaced"]
    warnings = ["root dirty_count=2", "canonical_amof dirty_count=1"] if verdict != "PASS" else []
    failing_root = _root(app_home / "share" / "workspaces")
    if verdict == "FAIL":
        failing_root["inside_source_workspace"] = True
    return {
        "result_kind": "amof_doctor_result",
        "contract_version": "2026-05-15",
        "verdict": verdict,
        "layout_mode": "split_workspace",
        "workspace_root": str(workspace),
        "canonical_amof_code_path": str(canonical_amof),
        "canonical_ui_path": str(canonical_ui),
        "runtime_import_source": str(canonical_amof / "scripts" / "amof" / "__init__.py"),
        "runtime_import_is_canonical": True,
        "surfaces": {
            "root": {"path": str(workspace), "exists": True, "branch": "feature/up10", "head": "abc1234", "dirty_count": 2},
            "canonical_amof": {"path": str(canonical_amof), "exists": True, "branch": "feature/up10", "head": "def5678", "dirty_count": 1},
            "canonical_ui": {"path": str(canonical_ui), "exists": True, "branch": "main", "head": "9876543", "dirty_count": 0},
            "gmd_app": {"path": str(gmd_app), "exists": True, "branch": "main", "head": "7654321", "dirty_count": 0},
        },
        "app_data": {
            "roots": {
                "config_root": _root(app_home / "config"),
                "data_root": _root(app_home / "share"),
                "cache_root": _root(app_home / "cache"),
                "state_root": _root(app_home / "state"),
                "evidence_dir": _root(app_home / "share" / "evidence"),
                "runs_dir": _root(app_home / "share" / "runs"),
                "workspaces_dir": failing_root,
                "materialized_runs_dir": _root(app_home / "share" / "workspaces" / "materialized-runs"),
                "receipts_dir": _root(app_home / "share" / "receipts"),
                "logs_dir": _root(app_home / "state" / "logs"),
                "locks_dir": _root(app_home / "state" / "locks"),
                "queue_dir": _root(app_home / "state" / "queue"),
                "tmp_dir": _root(app_home / "cache" / "tmp"),
                "provider_profiles_dir": _root(app_home / "config" / "provider-profiles"),
            }
        },
        "toolchain": {
            "git": {"required": True, "available": True, "error": None, "version": "git version 2.43.0", "resolved_path": "/usr/bin/git"},
            "python": {"required": True, "available": True, "error": None, "version": "Python 3.12.3", "resolved_path": "/usr/bin/python3"},
            "docker": {"required": False, "available": True, "error": None, "version": "Docker version 29.4.0", "resolved_path": "/usr/bin/docker"},
            "k3d": {"required": False, "available": False, "error": "command not found", "version": None, "resolved_path": None},
            "kubectl": {"required": False, "available": True, "error": None, "version": "v1.35.3", "resolved_path": "/usr/bin/kubectl"},
            "helm": {"required": False, "available": False, "error": "command not found", "version": None, "resolved_path": None},
        },
        "contracts": {
            "contracts/director-intake-client-contract.md": {"exists": True},
            "contracts/director-intake-execution-contract.schema.json": {"exists": True},
            "contracts/director-plan-result.schema.json": {"exists": True},
            "contracts/workspace-receipt.schema.json": {"exists": True},
            "contracts/execution-handoff-result.schema.json": {"exists": True},
            "contracts/governed-workstation-bootstrap-contract.schema.json": {"exists": True},
            "contracts/bootstrap-source-checkout-receipt.schema.json": {"exists": True},
            "contracts/bootstrap-toolchain-receipt.schema.json": {"exists": True},
            "contracts/bootstrap-provider-configuration-receipt.schema.json": {"exists": True},
            "contracts/bootstrap-failure-receipt.schema.json": {"exists": True},
            "contracts/up10-bootstrap-summary.schema.json": {"exists": True},
            "contracts/bootstrap-sha256-manifest.schema.json": {"exists": True},
        },
        "contexts": {
            "available_contexts": ["local"],
            "current": {
                "current_context": "local",
                "controlplane_mode": "local-cli",
                "execution_backend": "local",
                "workspace_backend": "local-appdata",
                "evidence_backend": "local-appdata",
                "provider_profile_refs": [],
                "provider_profile_ref_count": 0,
                "provider_health_status": "unconfigured",
                "kubeconfig_ref": None,
                "kubeconfig_ref_exists": None,
            },
        },
        "secret_exposure": {"finding_count": 0},
        "warnings": warnings,
        "failures": failures,
    }


class TestBootstrapBundleCli(unittest.TestCase):
    def test_bundle_writes_receipts_summary_and_manifest(self) -> None:
        with TemporaryDirectory(prefix="amof-bootstrap-bundle-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            output_dir = tmp_root / "bundle"
            stdout = io.StringIO()
            stderr = io.StringIO()
            report = _doctor_report(tmp_root, verdict="WARN")

            with patch("amof.commands.bootstrap.topology_report", return_value=report):
                with patch.dict("os.environ", {"SHELL": "/bin/bash"}, clear=False):
                    with patch.object(
                        sys,
                        "argv",
                        ["amof", "bootstrap", "bundle", "--json", "--output-dir", str(output_dir)],
                    ), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                        with self.assertRaises(SystemExit) as exc:
                            main()

            summary_payload = json.loads(stdout.getvalue())
            self.assertEqual(exc.exception.code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(summary_payload["result_kind"], "amof_up10_bootstrap_summary")
            self.assertEqual(summary_payload["bootstrap_status"], "WARN")

            expected_files = {
                "contract_artifact_path",
                "doctor_artifact_path",
                "source_checkout_receipt_path",
                "toolchain_receipt_path",
                "provider_configuration_receipt_path",
                "summary_artifact_path",
                "sha256_manifest_path",
            }
            for key in expected_files:
                self.assertTrue(Path(summary_payload["artifact_paths"][key]).exists(), msg=key)
            self.assertIsNone(summary_payload["artifact_paths"]["failure_receipt_path"])

            manifest_payload = json.loads(
                Path(summary_payload["artifact_paths"]["sha256_manifest_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest_payload["result_kind"], "amof_bootstrap_sha256_manifest")
            self.assertEqual(manifest_payload["artifact_count"], len(manifest_payload["artifacts"]))
            self.assertIn("sha256_manifest_path", manifest_payload["excluded_artifacts"])

    def test_bundle_emits_failure_receipt_and_exit_code_when_blocked(self) -> None:
        with TemporaryDirectory(prefix="amof-bootstrap-bundle-blocked-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            output_dir = tmp_root / "bundle"
            stdout = io.StringIO()
            stderr = io.StringIO()
            report = _doctor_report(tmp_root, verdict="FAIL")

            with patch("amof.commands.bootstrap.topology_report", return_value=report):
                with patch.dict("os.environ", {"SHELL": "/bin/bash"}, clear=False):
                    with patch.object(
                        sys,
                        "argv",
                        ["amof", "bootstrap", "bundle", "--json", "--output-dir", str(output_dir)],
                    ), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                        with self.assertRaises(SystemExit) as exc:
                            main()

            summary_payload = json.loads(stdout.getvalue())
            self.assertEqual(exc.exception.code, 2)
            self.assertEqual(summary_payload["bootstrap_status"], "BLOCKED")
            failure_path = summary_payload["artifact_paths"]["failure_receipt_path"]
            self.assertIsNotNone(failure_path)
            failure_payload = json.loads(Path(str(failure_path)).read_text(encoding="utf-8"))
            self.assertEqual(failure_payload["result_kind"], "amof_bootstrap_failure_receipt")
            self.assertTrue(failure_payload["blocked_gate_names"])

    def test_bundle_artifacts_validate_against_schemas_when_jsonschema_available(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed in this environment")

        with TemporaryDirectory(prefix="amof-bootstrap-bundle-schema-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            output_dir = tmp_root / "bundle"
            stdout = io.StringIO()
            report = _doctor_report(tmp_root, verdict="WARN")

            with patch("amof.commands.bootstrap.topology_report", return_value=report):
                with patch.dict("os.environ", {"SHELL": "/bin/bash"}, clear=False):
                    with patch.object(
                        sys,
                        "argv",
                        ["amof", "bootstrap", "bundle", "--json", "--output-dir", str(output_dir)],
                    ), patch("sys.stdout", stdout), patch("sys.stderr", io.StringIO()):
                        with self.assertRaises(SystemExit):
                            main()

            summary_payload = json.loads(stdout.getvalue())
            schema_map = {
                "contract_artifact_path": ROOT / "contracts" / "governed-workstation-bootstrap-contract.schema.json",
                "source_checkout_receipt_path": ROOT / "contracts" / "bootstrap-source-checkout-receipt.schema.json",
                "toolchain_receipt_path": ROOT / "contracts" / "bootstrap-toolchain-receipt.schema.json",
                "provider_configuration_receipt_path": ROOT / "contracts" / "bootstrap-provider-configuration-receipt.schema.json",
                "summary_artifact_path": ROOT / "contracts" / "up10-bootstrap-summary.schema.json",
                "sha256_manifest_path": ROOT / "contracts" / "bootstrap-sha256-manifest.schema.json",
            }
            for key, schema_path in schema_map.items():
                payload = json.loads(Path(summary_payload["artifact_paths"][key]).read_text(encoding="utf-8"))
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                jsonschema.validate(instance=payload, schema=schema)


if __name__ == "__main__":
    unittest.main()
