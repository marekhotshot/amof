import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof.commands.bootstrap import build_bootstrap_contract
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

    def test_installed_cli_external_repo_reports_packaged_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-installed-cli-") as td:
            temp_root = Path(td)
            repo = temp_root / "target-repo"
            runtime_root = temp_root / "site-packages" / "amof"
            runtime_root.mkdir(parents=True, exist_ok=True)
            (runtime_root / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
            _init_git_repo(repo)

            with (
                patch.dict("os.environ", {"AMOF_HOME": str(temp_root / ".amof-home")}, clear=False),
                patch("amof.commands.doctor._runtime_package_root", return_value=runtime_root),
            ):
                report = topology_report(
                    start_path=repo,
                    import_origin=str(runtime_root / "__init__.py"),
                    path_entries=[str(runtime_root.parent)],
                )

        self.assertEqual(report["layout_mode"], "installed_cli")
        self.assertEqual(report["workspace_root"], str(repo))
        self.assertEqual(report["canonical_amof_code_path"], str(runtime_root))
        self.assertEqual(report["contract_support_mode"], "packaged_runtime")
        self.assertTrue(report["runtime_import_is_canonical"])
        self.assertFalse(report["surfaces"]["canonical_amof"]["git"])
        self.assertFalse(any("required contract missing" in item for item in report["failures"]))
        self.assertNotEqual(report["verdict"], "FAIL")

    def test_installed_cli_generic_root_does_not_treat_home_app_data_as_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-installed-root-") as td:
            temp_root = Path(td)
            generic_root = temp_root / "generic-root"
            runtime_root = temp_root / "site-packages" / "amof"
            generic_root.mkdir(parents=True, exist_ok=True)
            runtime_root.mkdir(parents=True, exist_ok=True)
            (runtime_root / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")

            with (
                patch.dict("os.environ", {"AMOF_HOME": str(generic_root / "root" / ".local" / "amof")}, clear=False),
                patch("amof.commands.doctor._runtime_package_root", return_value=runtime_root),
            ):
                report = topology_report(
                    start_path=generic_root,
                    import_origin=str(runtime_root / "__init__.py"),
                    path_entries=[str(runtime_root.parent)],
                )

        self.assertEqual(report["layout_mode"], "installed_cli")
        self.assertEqual(report["source_workspace_roots"], [])
        self.assertFalse(report["app_data"]["roots"]["config_root"]["inside_source_workspace"])
        self.assertFalse(any("source workspace" in item for item in report["failures"]))
        self.assertNotEqual(report["verdict"], "FAIL")

        payload = build_bootstrap_contract(report, output_path=Path(td) / "contract.json")
        self.assertNotEqual(payload["bootstrap_status"], "BLOCKED")
        self.assertTrue(payload["director_prerequisites"]["runtime_roots_outside_source_workspaces"])

    def test_installed_cli_home_directory_does_not_treat_app_data_as_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-installed-home-") as td:
            temp_root = Path(td)
            home_dir = temp_root / "home" / "amof-user"
            runtime_root = temp_root / "site-packages" / "amof"
            home_dir.mkdir(parents=True, exist_ok=True)
            runtime_root.mkdir(parents=True, exist_ok=True)
            (runtime_root / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")

            with (
                patch.dict("os.environ", {"AMOF_HOME": str(home_dir / ".local" / "share" / "amof")}, clear=False),
                patch("amof.commands.doctor._runtime_package_root", return_value=runtime_root),
            ):
                report = topology_report(
                    start_path=home_dir,
                    import_origin=str(runtime_root / "__init__.py"),
                    path_entries=[str(runtime_root.parent)],
                )

        self.assertEqual(report["layout_mode"], "installed_cli")
        self.assertEqual(report["source_workspace_roots"], [])
        self.assertFalse(report["app_data"]["roots"]["data_root"]["inside_source_workspace"])
        self.assertFalse(any("source workspace" in item for item in report["failures"]))
        self.assertNotEqual(report["verdict"], "FAIL")

    def test_installed_cli_external_git_repo_preserves_source_workspace_guard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-installed-git-") as td:
            temp_root = Path(td)
            repo = temp_root / "target-repo"
            runtime_root = temp_root / "site-packages" / "amof"
            runtime_root.mkdir(parents=True, exist_ok=True)
            (runtime_root / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
            _init_git_repo(repo)

            with (
                patch.dict("os.environ", {"AMOF_HOME": str(repo / ".amof-home")}, clear=False),
                patch("amof.commands.doctor._runtime_package_root", return_value=runtime_root),
            ):
                report = topology_report(
                    start_path=repo,
                    import_origin=str(runtime_root / "__init__.py"),
                    path_entries=[str(runtime_root.parent)],
                )

        self.assertEqual(report["layout_mode"], "installed_cli")
        self.assertEqual(report["source_workspace_roots"], [str(repo)])
        self.assertTrue(report["app_data"]["roots"]["config_root"]["inside_source_workspace"])
        self.assertEqual(report["verdict"], "FAIL")

    def test_source_checkout_still_blocks_app_data_inside_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-source-guard-") as td:
            temp_root = Path(td)
            repo = temp_root / "repo"
            (repo / "scripts" / "amof").mkdir(parents=True, exist_ok=True)
            (repo / "scripts" / "amof" / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
            _seed_required_contracts(repo)
            _init_git_repo(repo)

            with patch.dict("os.environ", {"AMOF_HOME": str(repo / ".amof-home")}, clear=False):
                report = topology_report(
                    start_path=repo,
                    import_origin=str(repo / "scripts" / "amof" / "__init__.py"),
                    path_entries=[str(repo / "scripts")],
                )

        self.assertEqual(report["layout_mode"], "standalone_repo")
        self.assertTrue(report["app_data"]["roots"]["config_root"]["inside_source_workspace"])
        self.assertEqual(report["verdict"], "FAIL")

    def test_doctor_reports_canonical_repo_dirt_with_typed_reason(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-canonical-dirty-") as td:
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
            (workspace / "repos" / "amof" / "README.md").write_text("dirty\n", encoding="utf-8")

            with patch.dict("os.environ", {"AMOF_HOME": str(temp_root / ".amof-home")}, clear=False):
                report = topology_report(
                    start_path=workspace / "repos" / "amof",
                    import_origin=str(workspace / "repos" / "amof" / "scripts" / "amof" / "__init__.py"),
                    path_entries=[str(workspace / "repos" / "amof" / "scripts")],
                )

        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(any("CANONICAL_REPO_DIRTY:" in item for item in report["failures"]))

    def test_doctor_reports_nested_worktrees_inside_canonical_repo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-nested-worktrees-") as td:
            temp_root = Path(td)
            workspace = temp_root / "workspace"
            canonical = workspace / "repos" / "amof"
            (canonical / "scripts" / "amof").mkdir(parents=True, exist_ok=True)
            (canonical / "scripts" / "amof" / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
            _seed_required_contracts(canonical)
            _init_git_repo(workspace)
            _init_git_repo(canonical)
            subprocess.run(
                ["git", "worktree", "add", str(canonical / "worktrees" / "public" / "nested"), "HEAD"],
                cwd=canonical,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )

            with patch.dict("os.environ", {"AMOF_HOME": str(temp_root / ".amof-home")}, clear=False):
                report = topology_report(
                    start_path=canonical,
                    import_origin=str(canonical / "scripts" / "amof" / "__init__.py"),
                    path_entries=[str(canonical / "scripts")],
                )

        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(any("CANONICAL_REPO_NESTED_WORKTREES:" in item for item in report["failures"]))

    def test_doctor_reports_artifacts_inside_canonical_repo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-doctor-canonical-artifacts-") as td:
            temp_root = Path(td)
            workspace = temp_root / "workspace"
            canonical = workspace / "repos" / "amof"
            (canonical / "scripts" / "amof").mkdir(parents=True, exist_ok=True)
            (canonical / "scripts" / "amof" / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
            _seed_required_contracts(canonical)
            _init_git_repo(workspace)
            _init_git_repo(canonical)
            (canonical / "receipts").mkdir(parents=True, exist_ok=True)

            with patch.dict("os.environ", {"AMOF_HOME": str(temp_root / ".amof-home")}, clear=False):
                report = topology_report(
                    start_path=canonical,
                    import_origin=str(canonical / "scripts" / "amof" / "__init__.py"),
                    path_entries=[str(canonical / "scripts")],
                )

        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(any("CANONICAL_REPO_ARTIFACTS_PRESENT:" in item for item in report["failures"]))


if __name__ == "__main__":
    unittest.main()
