import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof.commands.doctor import topology_report


def _commit_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "AMOF Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "AMOF Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return env


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True, text=True)
    if not (path / "README.md").exists():
        (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "test: init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=_commit_env(),
    )


def _seed_required_contracts(root: Path) -> None:
    contracts_dir = root / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    for relative_path in (
        "director-intake-client-contract.md",
        "director-intake-execution-contract.schema.json",
        "director-plan-result.schema.json",
        "workspace-receipt.schema.json",
        "execution-handoff-result.schema.json",
        "governed-workstation-bootstrap-contract.schema.json",
        "bootstrap-source-checkout-receipt.schema.json",
        "bootstrap-toolchain-receipt.schema.json",
        "bootstrap-provider-configuration-receipt.schema.json",
        "bootstrap-failure-receipt.schema.json",
        "up10-bootstrap-summary.schema.json",
        "bootstrap-sha256-manifest.schema.json",
    ):
        target = contracts_dir / relative_path
        target.write_text("{}\n", encoding="utf-8")


class DoctorLayoutTests(unittest.TestCase):
    def test_standalone_clone_layout_passes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-standalone-") as td:
            temp_root = Path(td)
            repo = temp_root / "repo"
            (repo / "scripts" / "amof").mkdir(parents=True, exist_ok=True)
            (repo / "scripts" / "amof" / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
            _seed_required_contracts(repo)
            _init_git_repo(repo)

            with patch.dict("os.environ", {"AMOF_HOME": str(temp_root / ".amof-home")}, clear=False):
                report = topology_report(
                    start_path=repo,
                    import_origin=str(repo / "scripts" / "amof" / "__init__.py"),
                    path_entries=[str(repo / "scripts")],
                )

        self.assertEqual(report["layout_mode"], "standalone_repo")
        self.assertNotEqual(report["verdict"], "FAIL")
        self.assertEqual(report["workspace_root"], str(repo))
        self.assertEqual(report["canonical_amof_code_path"], str(repo))
        self.assertTrue(report["runtime_import_is_canonical"])
        self.assertEqual(report["result_kind"], "amof_doctor_result")
        self.assertEqual(report["contexts"]["current"]["current_context"], "local")
        self.assertFalse(report["app_data"]["roots"]["evidence_dir"]["inside_source_workspace"])

    def test_split_workspace_layout_passes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-split-") as td:
            temp_root = Path(td)
            workspace = temp_root / "workspace"
            (workspace / "repos" / "amof" / "scripts" / "amof").mkdir(parents=True, exist_ok=True)
            (workspace / "repos" / "amof" / "scripts" / "amof" / "__init__.py").write_text(
                "__version__ = 'test'\n",
                encoding="utf-8",
            )
            _seed_required_contracts(workspace / "repos" / "amof")
            _init_git_repo(workspace)
            _init_git_repo(workspace / "repos" / "amof")

            with patch.dict("os.environ", {"AMOF_HOME": str(temp_root / ".amof-home")}, clear=False):
                report = topology_report(
                    start_path=workspace / "repos" / "amof",
                    import_origin=str(workspace / "repos" / "amof" / "scripts" / "amof" / "__init__.py"),
                    path_entries=[str(workspace / "repos" / "amof" / "scripts")],
                )

        self.assertEqual(report["layout_mode"], "split_workspace")
        self.assertNotEqual(report["verdict"], "FAIL")
        self.assertEqual(report["workspace_root"], str(workspace))
        self.assertEqual(report["canonical_amof_code_path"], str(workspace / "repos" / "amof"))
        self.assertTrue(report["runtime_import_is_canonical"])
        self.assertFalse(report["app_data"]["roots"]["workspaces_dir"]["inside_source_workspace"])

    def test_import_outside_canonical_path_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-invalid-import-") as td:
            temp_root = Path(td)
            repo = temp_root / "repo"
            (repo / "scripts" / "amof").mkdir(parents=True, exist_ok=True)
            (repo / "scripts" / "amof" / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
            _seed_required_contracts(repo)
            _init_git_repo(repo)

            with patch.dict("os.environ", {"AMOF_HOME": str(temp_root / ".amof-home")}, clear=False):
                report = topology_report(
                    start_path=repo,
                    import_origin="/tmp/not-canonical/amof/__init__.py",
                    path_entries=[str(repo / "scripts")],
                )

        self.assertEqual(report["layout_mode"], "standalone_repo")
        self.assertEqual(report["verdict"], "FAIL")
        self.assertFalse(report["runtime_import_is_canonical"])
        self.assertTrue(any("outside canonical" in item for item in report["failures"]))


if __name__ == "__main__":
    unittest.main()
