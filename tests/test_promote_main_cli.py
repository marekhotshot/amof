from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"


def _commit_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "AMOF Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "AMOF Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    if extra:
        env.update(extra)
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


class PromoteMainCliTests(unittest.TestCase):
    def _prepare_workspace(self) -> tuple[Path, Path, str, str, str]:
        temp_root = Path(tempfile.mkdtemp(prefix="amof-promote-cli-"))
        remote = temp_root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)

        seed = temp_root / "seed"
        subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True, text=True)
        (seed / "README.md").write_text("# base\n", encoding="utf-8")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "base")
        _git(seed, "remote", "add", "origin", str(remote))
        _git(seed, "push", "-u", "origin", "main")

        repo = temp_root / "repos" / "amof"
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True, text=True)
        _git(repo, "fetch", "origin", "main")
        expected_main_sha = _git(repo, "rev-parse", "origin/main")

        branch = "ticket/AMOF-PROMOTE-MAIN-ECOSYSTEM-SEMANTICS-001-promote-main-ecosystem-semantics"
        _git(repo, "checkout", "-b", branch)
        (repo / "README.md").write_text("# changed\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "change for promote-main no ecosystem CLI test")
        source_sha = _git(repo, "rev-parse", "HEAD")

        return temp_root, repo, branch, source_sha, expected_main_sha

    def test_promote_main_dry_run_without_ecosystem_reaches_planner(self) -> None:
        temp_root, _repo, branch, source_sha, expected_main_sha = self._prepare_workspace()
        try:
            receipts_root = temp_root / "receipts" / "promote-main" / "AMOF-PROMOTE-MAIN-ECOSYSTEM-SEMANTICS-001"
            receipts_root.mkdir(parents=True, exist_ok=True)
            amof_home = temp_root / "amof-home"
            env = _commit_env(
                {
                    "PYTHONPATH": str(SCRIPTS_ROOT),
                    "AMOF_CWD": str(receipts_root),
                    "AMOF_HOME": str(amof_home),
                }
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "amof",
                    "promote-main",
                    "--repo",
                    "amof",
                    "--ticket-id",
                    "AMOF-PROMOTE-MAIN-ECOSYSTEM-SEMANTICS-001",
                    "--candidate-branch",
                    branch,
                    "--source-sha",
                    source_sha,
                    "--expected-main-sha",
                    expected_main_sha,
                    "--promotion-reason",
                    "test no ecosystem dry-run",
                    "--dry-run",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[promote-main] Promotion result", result.stdout)
            self.assertNotIn("Ecosystem could not be resolved", result.stderr)
            audit_dir = receipts_root / "audit"
            self.assertTrue(audit_dir.exists())
            self.assertTrue(any(audit_dir.iterdir()))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
