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
from amof.commands.bootstrap import build_bootstrap_contract


def _doctor_report(root: Path, *, verdict: str = "WARN") -> dict:
    workspace = root / "workspace"
    canonical_amof = workspace / "repos" / "amof"
    canonical_ui = workspace / "repos" / "amof-ui"
    gmd_app = workspace / "repos" / "gmd-app"
    app_home = root / "appdata"

    def _root(path: Path) -> dict:
        return {
            "path": str(path),
            "exists": True,
            "is_dir": True,
            "writable": True,
            "inside_source_workspace": False,
        }

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
                "workspaces_dir": _root(app_home / "share" / "workspaces"),
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
            "git": {"required": True, "available": True, "error": None, "version": "git version 2.43.0"},
            "python": {"required": True, "available": True, "error": None, "version": "Python 3.12.3"},
            "docker": {"required": False, "available": True, "error": None, "version": "Docker version 29.4.0"},
            "k3d": {"required": False, "available": False, "error": "command not found", "version": None},
            "kubectl": {"required": False, "available": True, "error": None, "version": "v1.35.3"},
            "helm": {"required": False, "available": False, "error": "command not found", "version": None},
        },
        "contracts": {
            "contracts/director-intake-client-contract.md": {"exists": True},
            "contracts/director-intake-execution-contract.schema.json": {"exists": True},
            "contracts/director-plan-result.schema.json": {"exists": True},
            "contracts/workspace-receipt.schema.json": {"exists": True},
            "contracts/execution-handoff-result.schema.json": {"exists": True},
            "contracts/governed-workstation-bootstrap-contract.schema.json": {"exists": True},
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
        "warnings": ["root dirty_count=2", "canonical_amof dirty_count=1"],
        "failures": [] if verdict != "FAIL" else ["runtime roots are misplaced"],
    }


class TestBootstrapContractCli(unittest.TestCase):
    def test_build_bootstrap_contract_preserves_warn_status(self) -> None:
        with TemporaryDirectory(prefix="amof-bootstrap-contract-build-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            report = _doctor_report(tmp_root, verdict="WARN")

            payload = build_bootstrap_contract(report, output_path=tmp_root / "contract.json")

        self.assertEqual(payload["result_kind"], "amof_governed_workstation_bootstrap_contract")
        self.assertEqual(payload["bootstrap_status"], "WARN")
        self.assertEqual(payload["provider_authority"]["provider_profile_ref_count"], 0)
        self.assertEqual(payload["doctor_gates"][1]["name"], "git_dirty_classification")
        self.assertEqual(payload["doctor_gates"][1]["status"], "WARN")

    def test_cli_writes_json_contract_and_prints_json(self) -> None:
        with TemporaryDirectory(prefix="amof-bootstrap-contract-cli-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            output_path = tmp_root / "bootstrap-contract.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            report = _doctor_report(tmp_root, verdict="WARN")

            with patch("amof.commands.bootstrap.topology_report", return_value=report):
                with patch.dict("os.environ", {"SHELL": "/bin/bash"}, clear=False):
                    with patch.object(
                        sys,
                        "argv",
                        ["amof", "bootstrap", "contract", "--json", "--output", str(output_path)],
                    ), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                        with self.assertRaises(SystemExit) as exc:
                            main()

            payload = json.loads(stdout.getvalue())
            written_payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["result_kind"], "amof_governed_workstation_bootstrap_contract")
        self.assertEqual(payload["bootstrap_status"], "WARN")
        self.assertEqual(written_payload["result_kind"], payload["result_kind"])
        self.assertEqual(written_payload["evidence_outputs"]["contract_artifact_path"], str(output_path))

    def test_cli_returns_blocked_exit_code_for_blocked_contract(self) -> None:
        with TemporaryDirectory(prefix="amof-bootstrap-contract-blocked-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            output_path = tmp_root / "bootstrap-contract.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            report = _doctor_report(tmp_root, verdict="FAIL")

            with patch("amof.commands.bootstrap.topology_report", return_value=report):
                with patch.dict("os.environ", {"SHELL": "/bin/bash"}, clear=False):
                    with patch.object(
                        sys,
                        "argv",
                        ["amof", "bootstrap", "contract", "--output", str(output_path)],
                    ), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                        with self.assertRaises(SystemExit) as exc:
                            main()

        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue().strip(), str(output_path))


if __name__ == "__main__":
    unittest.main()
