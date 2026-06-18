import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof import app_paths, runtime_workspace, worktree_manager
from amof.commands import workspace as workspace_cmd


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
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "test: init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=_commit_env(),
    )


def _init_operator_root(root: Path) -> None:
    (root / "compat").mkdir(parents=True, exist_ok=True)
    (root / "compat" / "public-private.lock.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "worktrees" / "public").mkdir(parents=True, exist_ok=True)
    (root / "worktrees" / "private").mkdir(parents=True, exist_ok=True)
    _init_git_repo(root / "repos" / "amof")
    _init_git_repo(root / "repos" / "amof-private")


@contextmanager
def _cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class CanonicalRepoWriteGuardTests(unittest.TestCase):
    def test_workspace_command_refuses_canonical_public_repo(self) -> None:
        with TemporaryDirectory(prefix="amof-canonical-public-guard-") as td:
            root = Path(td)
            _init_operator_root(root)
            with patch.dict(os.environ, {"AMOF_OPERATOR_WORKSPACE_ROOT": str(root)}, clear=False):
                with _cwd(root / "repos" / "amof"):
                    with self.assertRaisesRegex(RuntimeError, app_paths.CANONICAL_REPO_WRITE_FORBIDDEN) as ctx:
                        workspace_cmd.cmd_workspace({"repos": []})

        self.assertIn("canonical_public_repo", str(ctx.exception))
        self.assertIn("Use a ticket worktree under", str(ctx.exception))

    def test_workspace_command_refuses_canonical_private_repo(self) -> None:
        with TemporaryDirectory(prefix="amof-canonical-private-guard-") as td:
            root = Path(td)
            _init_operator_root(root)
            with patch.dict(os.environ, {"AMOF_OPERATOR_WORKSPACE_ROOT": str(root)}, clear=False):
                with _cwd(root / "repos" / "amof-private"):
                    with self.assertRaisesRegex(RuntimeError, app_paths.CANONICAL_REPO_WRITE_FORBIDDEN) as ctx:
                        workspace_cmd.cmd_workspace({"repos": []})

        self.assertIn("canonical_private_repo", str(ctx.exception))

    def test_workspace_command_allows_public_ticket_worktree(self) -> None:
        with TemporaryDirectory(prefix="amof-public-ticket-worktree-") as td:
            root = Path(td)
            _init_operator_root(root)
            target = root / "worktrees" / "public" / "AMOF-CANONICAL-REPO-WRITE-GUARD-001"
            target.mkdir(parents=True, exist_ok=True)
            with patch.dict(os.environ, {"AMOF_OPERATOR_WORKSPACE_ROOT": str(root)}, clear=False):
                with _cwd(target):
                    code = workspace_cmd.cmd_workspace({"repos": []})

            created = target / "amof.code-workspace"
            created_exists = created.is_file()

        self.assertEqual(code, 0)
        self.assertTrue(created_exists)

    def test_workspace_command_allows_private_ticket_worktree(self) -> None:
        with TemporaryDirectory(prefix="amof-private-ticket-worktree-") as td:
            root = Path(td)
            _init_operator_root(root)
            target = root / "worktrees" / "private" / "AMOF-CANONICAL-REPO-WRITE-GUARD-001"
            target.mkdir(parents=True, exist_ok=True)
            with patch.dict(os.environ, {"AMOF_OPERATOR_WORKSPACE_ROOT": str(root)}, clear=False):
                with _cwd(target):
                    code = workspace_cmd.cmd_workspace({"repos": []})

            created = target / "amof.code-workspace"
            created_exists = created.is_file()

        self.assertEqual(code, 0)
        self.assertTrue(created_exists)

    def test_receipts_root_defaults_outside_canonical_repos(self) -> None:
        with TemporaryDirectory(prefix="amof-receipts-root-") as td:
            root = Path(td)
            _init_operator_root(root)
            with patch.dict(os.environ, {"AMOF_OPERATOR_WORKSPACE_ROOT": str(root)}, clear=False):
                receipts_root = app_paths.operator_receipts_root(root / "repos" / "amof")
                self.assertEqual(receipts_root, (root / "receipts").resolve())
                classified = app_paths.classify_operator_path(receipts_root, base=root)

        self.assertEqual(classified.classification, "operator_receipts")
        self.assertFalse(classified.is_canonical_repo)

    def test_runtime_workspace_refuses_canonical_repo_target_base(self) -> None:
        with TemporaryDirectory(prefix="amof-runtime-target-base-") as td:
            root = Path(td)
            _init_operator_root(root)
            repo = root / "repos" / "amof"
            repo_url = str(repo)
            expected_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            with patch.dict(os.environ, {"AMOF_OPERATOR_WORKSPACE_ROOT": str(root)}, clear=False):
                with self.assertRaisesRegex(RuntimeError, app_paths.CANONICAL_REPO_WRITE_FORBIDDEN) as ctx:
                    runtime_workspace.materialize_run_workspace(
                        repo_name="amof",
                        repo_url=repo_url,
                        expected_sha=expected_sha,
                        run_id="run-1",
                        target_base_dir=repo,
                    )

        self.assertIn("canonical_public_repo", str(ctx.exception))

    def test_worktree_creation_refuses_target_inside_canonical_repo(self) -> None:
        with TemporaryDirectory(prefix="amof-worktree-target-guard-") as td:
            root = Path(td)
            _init_operator_root(root)
            with (
                patch.dict(os.environ, {"AMOF_OPERATOR_WORKSPACE_ROOT": str(root)}, clear=False),
                patch.object(
                    worktree_manager,
                    "get_ticket_repo_worktree_path",
                    lambda workspace_root, ticket_id, repo_name: root / "repos" / "amof" / "worktrees" / ticket_id,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, app_paths.CANONICAL_REPO_WRITE_FORBIDDEN) as ctx:
                    worktree_manager.switch_to_ticket(
                        root / "repos" / "amof",
                        "ticket/AMOF-CANONICAL-REPO-WRITE-GUARD-001",
                        "AMOF-CANONICAL-REPO-WRITE-GUARD-001",
                        "amof",
                        root,
                    )

        self.assertIn("create ticket worktree", str(ctx.exception))

    def test_maintenance_mode_is_narrow_and_not_general_bypass(self) -> None:
        with TemporaryDirectory(prefix="amof-maintenance-guard-") as td:
            root = Path(td)
            _init_operator_root(root)
            target = root / "repos" / "amof" / "receipts"
            with patch.dict(os.environ, {"AMOF_OPERATOR_WORKSPACE_ROOT": str(root)}, clear=False):
                with self.assertRaises(RuntimeError) as missing_ctx:
                    app_paths.ensure_canonical_repo_write_allowed(
                        operation="cleanup canonical artifact",
                        target_path=target,
                        base=root,
                        maintenance_action=True,
                    )

                with patch.dict(os.environ, {app_paths.CANONICAL_REPO_MAINTENANCE_ENV: "1"}, clear=False):
                    allowed = app_paths.ensure_canonical_repo_write_allowed(
                        operation="cleanup canonical artifact",
                        target_path=target,
                        base=root,
                        maintenance_action=True,
                    )
                    with self.assertRaises(RuntimeError) as blocked_ctx:
                        app_paths.ensure_canonical_repo_write_allowed(
                            operation="implementation edit",
                            target_path=target,
                            base=root,
                            maintenance_action=False,
                        )

        self.assertIn(app_paths.CANONICAL_REPO_MAINTENANCE_ENV, str(missing_ctx.exception))
        self.assertEqual(allowed, target.resolve())
        self.assertIn("not a general bypass", str(blocked_ctx.exception))


if __name__ == "__main__":
    unittest.main()
