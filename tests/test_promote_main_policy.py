import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof.commands.promote_main import (
    PromoteMainInput,
    _forbidden_code_delta_files,
    _name_status_diff,
    _load_private_promotion_target,
    _private_promotion_policy_path,
    _stale_base_overlap_details,
    plan_promote_main_dry_run,
)


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


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
        env=_commit_env(),
    )
    return completed.stdout.strip()


def _write(repo: Path, relative_path: str, content: str) -> None:
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _write_private_policy(workspace_root: Path, content: str) -> Path:
    policy_path = workspace_root / ".amof-local" / "promotion-targets.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(content, encoding="utf-8")
    return policy_path


class PromoteMainPolicyTests(unittest.TestCase):
    def test_private_policy_path_is_exact_workspace_location(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-path-") as td:
            workspace_root = Path(td) / "workspace"
            workspace_root.mkdir(parents=True, exist_ok=True)
            expected = (workspace_root / ".amof-local" / "promotion-targets.yaml").resolve(strict=False)

            self.assertEqual(_private_promotion_policy_path(workspace_root), expected)

    def test_private_policy_does_not_use_ancestor_discovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-ancestor-") as td:
            root = Path(td)
            child_workspace = root / "receipts" / "promote-main" / "TICKET"
            child_workspace.mkdir(parents=True, exist_ok=True)
            parent_policy = root / ".amof-local" / "promotion-targets.yaml"
            parent_policy.parent.mkdir(parents=True, exist_ok=True)
            parent_policy.write_text(
                "version: 1\n"
                "targets:\n"
                "  amof-private:\n"
                "    path: repos/amof-private\n"
                "    remote: https://example.test/amof-private.git\n"
                "    branch: main\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, str(child_workspace / ".amof-local" / "promotion-targets.yaml")):
                _load_private_promotion_target(child_workspace)

    def test_private_policy_rejects_arbitrary_target_names_even_when_present(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-extra-targets-") as td:
            workspace_root = Path(td)
            _write_private_policy(
                workspace_root,
                "version: 1\n"
                "targets:\n"
                "  amof-private:\n"
                "    path: repos/amof-private\n"
                "    remote: https://example.test/amof-private.git\n"
                "    branch: main\n"
                "  arbitrary-repo:\n"
                "    path: repos/arbitrary-repo\n"
                "    remote: https://example.test/arbitrary-repo.git\n"
                "    branch: main\n",
            )

            with self.assertRaisesRegex(RuntimeError, "unexpected targets: arbitrary-repo"):
                _load_private_promotion_target(workspace_root)

    def test_private_policy_rejects_lexical_path_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-escape-") as td:
            workspace_root = Path(td) / "workspace"
            workspace_root.mkdir(parents=True, exist_ok=True)
            _write_private_policy(
                workspace_root,
                "version: 1\n"
                "targets:\n"
                "  amof-private:\n"
                "    path: ../outside/amof-private\n"
                "    remote: https://example.test/amof-private.git\n"
                "    branch: main\n",
            )

            with self.assertRaisesRegex(RuntimeError, "escapes the resolved workspace root"):
                _load_private_promotion_target(workspace_root)

    def test_private_policy_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-symlink-") as td:
            root = Path(td)
            workspace_root = root / "workspace"
            workspace_root.mkdir(parents=True, exist_ok=True)
            outside_repo = root / "outside" / "amof-private"
            outside_repo.mkdir(parents=True, exist_ok=True)
            symlink_path = workspace_root / "linked-private"
            symlink_path.symlink_to(outside_repo, target_is_directory=True)
            _write_private_policy(
                workspace_root,
                "version: 1\n"
                "targets:\n"
                "  amof-private:\n"
                "    path: linked-private\n"
                "    remote: https://example.test/amof-private.git\n"
                "    branch: main\n",
            )

            with self.assertRaisesRegex(RuntimeError, "escapes the resolved workspace root"):
                _load_private_promotion_target(workspace_root)

    def test_plan_promote_main_does_not_fetch_when_private_policy_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-no-fetch-") as td:
            workspace_root = Path(td)
            _write_private_policy(
                workspace_root,
                "version: 1\n"
                "targets:\n"
                "  amof-private:\n"
                "    path: ../outside/amof-private\n"
                "    remote: https://example.test/amof-private.git\n"
                "    branch: main\n",
            )
            bundle = PromoteMainInput(
                repo="amof-private",
                ticket_id="AMOF-PROMOTE-MAIN-PRIVATE-REPO-001",
                candidate_branch="ticket/AMOF-PROMOTE-MAIN-PRIVATE-REPO-001-promote-main-private-repo",
                source_sha="1111111111111111111111111111111111111111",
                gitops_commit_sha=None,
                expected_main_sha="2222222222222222222222222222222222222222",
                promotion_reason="policy validation should fail before fetch",
                dry_run=True,
            )

            with mock.patch("amof.commands.promote_main._fetch_origin_main") as fetch_origin_main:
                with self.assertRaisesRegex(RuntimeError, "escapes the resolved workspace root"):
                    plan_promote_main_dry_run(
                        {"repos": []},
                        bundle,
                        ecosystem=None,
                        workspace_root=workspace_root,
                    )
                fetch_origin_main.assert_not_called()

    def test_deleting_forbidden_tgz_path_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-delete-") as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _write(repo, "README.md", "base\n")
            _write(repo, "charts/amof-platform/charts/test-0.1.0.tgz", "placeholder\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "base")
            base_sha = _git(repo, "rev-parse", "HEAD")

            (repo / "charts/amof-platform/charts/test-0.1.0.tgz").unlink()
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "delete forbidden artifact")
            head_sha = _git(repo, "rev-parse", "HEAD")

            entries = _name_status_diff(repo, base_sha, head_sha)

        self.assertEqual(entries, [("D", "charts/amof-platform/charts/test-0.1.0.tgz")])
        self.assertEqual(_forbidden_code_delta_files(entries), [])

    def test_adding_forbidden_tgz_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-add-") as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _write(repo, "README.md", "base\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "base")
            base_sha = _git(repo, "rev-parse", "HEAD")

            _write(repo, "charts/amof-platform/charts/test-0.1.0.tgz", "placeholder\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "add forbidden artifact")
            head_sha = _git(repo, "rev-parse", "HEAD")

            entries = _name_status_diff(repo, base_sha, head_sha)

        self.assertEqual(entries, [("A", "charts/amof-platform/charts/test-0.1.0.tgz")])
        self.assertEqual(_forbidden_code_delta_files(entries), ["charts/amof-platform/charts/test-0.1.0.tgz"])

    def test_modifying_forbidden_audit_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-modify-") as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _write(repo, "README.md", "base\n")
            _write(repo, "ecosystems/demo/audit/record.json", "{\n  \"status\": \"base\"\n}\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "base")
            base_sha = _git(repo, "rev-parse", "HEAD")

            _write(repo, "ecosystems/demo/audit/record.json", "{\n  \"status\": \"changed\"\n}\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "modify forbidden audit artifact")
            head_sha = _git(repo, "rev-parse", "HEAD")

            entries = _name_status_diff(repo, base_sha, head_sha)

        self.assertEqual(entries, [("M", "ecosystems/demo/audit/record.json")])
        self.assertEqual(_forbidden_code_delta_files(entries), ["ecosystems/demo/audit/record.json"])

    def test_stale_base_overlap_details_capture_exact_regression_shape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-stale-overlap-") as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _write(repo, "README.md", "base\n")
            _write(repo, "scripts/amof/commands/promote_main.py", "base promote\n")
            _write(repo, "tests/test_promote_main_cli.py", "base cli\n")
            _write(repo, "tests/test_promote_main_linkage.py", "base linkage\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "base")
            merge_base_sha = _git(repo, "rev-parse", "HEAD")

            _git(repo, "checkout", "-b", "ticket/AMOF-PROMOTE-MAIN-STALE-BASE-GUARD-001")
            _write(repo, "scripts/amof/commands/promote_main.py", "candidate promote\n")
            _write(repo, "tests/test_promote_main_cli.py", "candidate cli\n")
            _write(repo, "tests/test_promote_main_linkage.py", "candidate linkage\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "candidate changes")
            source_sha = _git(repo, "rev-parse", "HEAD")

            _git(repo, "checkout", "main")
            _write(repo, "scripts/amof/commands/promote_main.py", "main promote\n")
            _write(repo, "tests/test_promote_main_cli.py", "main cli\n")
            _write(repo, "tests/test_promote_main_linkage.py", "main linkage\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "main changes")
            current_origin_main_sha = _git(repo, "rev-parse", "HEAD")

            detected_merge_base, overlapping_files = _stale_base_overlap_details(
                repo,
                source_sha=source_sha,
                current_origin_main_sha=current_origin_main_sha,
            )

        self.assertEqual(detected_merge_base, merge_base_sha)
        self.assertEqual(
            overlapping_files,
            [
                "scripts/amof/commands/promote_main.py",
                "tests/test_promote_main_cli.py",
                "tests/test_promote_main_linkage.py",
            ],
        )

    def test_stale_base_overlap_details_allow_non_overlapping_stale_candidate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-promote-policy-stale-safe-") as td:
            repo = Path(td)
            _git(repo, "init", "-b", "main")
            _write(repo, "README.md", "base\n")
            _write(repo, "docs/guide.md", "base guide\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "base")
            merge_base_sha = _git(repo, "rev-parse", "HEAD")

            _git(repo, "checkout", "-b", "ticket/AMOF-PROMOTE-MAIN-STALE-BASE-GUARD-001")
            _write(repo, "README.md", "candidate update\n")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "candidate readme")
            source_sha = _git(repo, "rev-parse", "HEAD")

            _git(repo, "checkout", "main")
            _write(repo, "docs/guide.md", "main update\n")
            _git(repo, "add", "docs/guide.md")
            _git(repo, "commit", "-m", "main guide")
            current_origin_main_sha = _git(repo, "rev-parse", "HEAD")

            detected_merge_base, overlapping_files = _stale_base_overlap_details(
                repo,
                source_sha=source_sha,
                current_origin_main_sha=current_origin_main_sha,
            )

        self.assertEqual(detected_merge_base, merge_base_sha)
        self.assertEqual(overlapping_files, [])


if __name__ == "__main__":
    unittest.main()
