import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof.commands.promote_main import (
    _forbidden_code_delta_files,
    _name_status_diff,
    _stale_base_overlap_details,
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


class PromoteMainPolicyTests(unittest.TestCase):
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
