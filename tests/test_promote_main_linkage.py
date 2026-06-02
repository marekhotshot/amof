from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof.commands.promote_main import (
    PromoteMainInput,
    _is_legacy_numeric_ticket_id,
    plan_promote_main_dry_run,
)
from amof.intake.build_write import infer_ticket_id


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


class PromoteMainLinkageTests(unittest.TestCase):
    def _prepare_workspace(self) -> tuple[Path, Path, str, str]:
        temp_root = Path(tempfile.mkdtemp(prefix="amof-promote-linkage-"))
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

        branch = "ticket/AMOF-RUNTIME-CONTEXT-SWITCHING-001-runtime-context-switching"
        _git(repo, "checkout", "-b", branch)
        (repo / "README.md").write_text("# changed\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "change for descriptive ticket linkage")
        source_sha = _git(repo, "rev-parse", "HEAD")

        return temp_root, repo, source_sha, expected_main_sha

    def _prepare_stale_base_workspace(self, *, overlapping: bool) -> tuple[Path, str, str, str]:
        temp_root = Path(tempfile.mkdtemp(prefix="amof-promote-linkage-stale-"))
        remote = temp_root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)

        seed = temp_root / "seed"
        subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True, text=True)
        (seed / "README.md").write_text("# base\n", encoding="utf-8")
        (seed / "scripts" / "amof" / "commands").mkdir(parents=True, exist_ok=True)
        (seed / "tests").mkdir(parents=True, exist_ok=True)
        (seed / "scripts" / "amof" / "commands" / "promote_main.py").write_text("base promote\n", encoding="utf-8")
        (seed / "tests" / "test_promote_main_cli.py").write_text("base cli\n", encoding="utf-8")
        (seed / "tests" / "test_promote_main_linkage.py").write_text("base linkage\n", encoding="utf-8")
        (seed / "docs").mkdir(parents=True, exist_ok=True)
        (seed / "docs" / "status.md").write_text("base status\n", encoding="utf-8")
        _git(seed, "add", ".")
        _git(seed, "commit", "-m", "base")
        _git(seed, "remote", "add", "origin", str(remote))
        _git(seed, "push", "-u", "origin", "main")

        repo = temp_root / "repos" / "amof"
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True, text=True)
        _git(repo, "fetch", "origin", "main")

        branch = "ticket/AMOF-PROMOTE-MAIN-STALE-BASE-GUARD-001-promote-main-stale-base-guard"
        _git(repo, "checkout", "-b", branch)
        if overlapping:
            (repo / "scripts" / "amof" / "commands" / "promote_main.py").write_text(
                "candidate promote\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_promote_main_cli.py").write_text("candidate cli\n", encoding="utf-8")
            (repo / "tests" / "test_promote_main_linkage.py").write_text(
                "candidate linkage\n",
                encoding="utf-8",
            )
            _git(
                repo,
                "add",
                "scripts/amof/commands/promote_main.py",
                "tests/test_promote_main_cli.py",
                "tests/test_promote_main_linkage.py",
            )
        else:
            (repo / "README.md").write_text("# candidate only\n", encoding="utf-8")
            _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "candidate branch changes")
        source_sha = _git(repo, "rev-parse", "HEAD")

        _git(repo, "checkout", "main")
        if overlapping:
            (repo / "scripts" / "amof" / "commands" / "promote_main.py").write_text(
                "main promote\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_promote_main_cli.py").write_text("main cli\n", encoding="utf-8")
            (repo / "tests" / "test_promote_main_linkage.py").write_text(
                "main linkage\n",
                encoding="utf-8",
            )
            _git(
                repo,
                "add",
                "scripts/amof/commands/promote_main.py",
                "tests/test_promote_main_cli.py",
                "tests/test_promote_main_linkage.py",
            )
        else:
            (repo / "docs" / "status.md").write_text("main only\n", encoding="utf-8")
            _git(repo, "add", "docs/status.md")
        _git(repo, "commit", "-m", "advance main")
        _git(repo, "push", "origin", "main")
        _git(repo, "fetch", "origin", "main")
        expected_main_sha = _git(repo, "rev-parse", "origin/main")
        _git(repo, "checkout", branch)

        return temp_root, branch, source_sha, expected_main_sha

    def test_infer_ticket_id_supports_descriptive_ids(self) -> None:
        branch = "ticket/AMOF-RUNTIME-CONTEXT-SWITCHING-001-runtime-context-switching"
        self.assertEqual(infer_ticket_id(branch), "AMOF-RUNTIME-CONTEXT-SWITCHING-001")

    def test_plan_promote_main_accepts_descriptive_ticket_and_resolves_origin_main(self) -> None:
        temp_root, _repo, source_sha, expected_main_sha = self._prepare_workspace()
        try:
            manifest = {
                "repos": [
                    {
                        "name": "amof",
                        "path": str(temp_root / "repos" / "amof"),
                    }
                ]
            }
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-RUNTIME-CONTEXT-SWITCHING-001",
                candidate_branch="ticket/AMOF-RUNTIME-CONTEXT-SWITCHING-001-runtime-context-switching",
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test descriptive linkage",
                dry_run=True,
            )
            plan = plan_promote_main_dry_run(
                manifest,
                bundle,
                ecosystem="amof-dev",
                workspace_root=temp_root,
            )
            self.assertTrue(plan.validation_checks["ticket_linkage_consistent"])
            self.assertTrue(plan.validation_checks["origin_main_matches_expected_main_sha"])
            self.assertEqual(plan.current_origin_main_sha, expected_main_sha)
            self.assertFalse(plan.legacy_numeric_fallback_used)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_plan_promote_main_rejects_ticket_mismatch_with_clear_details(self) -> None:
        temp_root, _repo, source_sha, expected_main_sha = self._prepare_workspace()
        try:
            manifest = {
                "repos": [
                    {
                        "name": "amof",
                        "path": str(temp_root / "repos" / "amof"),
                    }
                ]
            }
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-INTAKE-CONTRACT-001",
                candidate_branch="ticket/AMOF-RUNTIME-CONTEXT-SWITCHING-001-runtime-context-switching",
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test mismatch",
                dry_run=True,
            )
            plan = plan_promote_main_dry_run(
                manifest,
                bundle,
                ecosystem="amof-dev",
                workspace_root=temp_root,
            )
            self.assertFalse(plan.validation_checks["ticket_linkage_consistent"])
            self.assertIn("branch_ticket_id=", plan.rejection_reason or "")
            self.assertIn("input_ticket_id=AMOF-INTAKE-CONTRACT-001", plan.rejection_reason or "")
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_plan_promote_main_resolves_repo_without_ecosystem_when_manifest_is_empty(self) -> None:
        temp_root, repo, source_sha, expected_main_sha = self._prepare_workspace()
        try:
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-RUNTIME-CONTEXT-SWITCHING-001",
                candidate_branch="ticket/AMOF-RUNTIME-CONTEXT-SWITCHING-001-runtime-context-switching",
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test no ecosystem repo resolution",
                dry_run=True,
            )
            plan = plan_promote_main_dry_run(
                {"repos": []},
                bundle,
                ecosystem=None,
                workspace_root=temp_root,
            )
            self.assertTrue(plan.ok)
            self.assertEqual(Path(plan.repo_path), repo)
            self.assertTrue(plan.validation_checks["origin_main_matches_expected_main_sha"])
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_plan_promote_main_rejects_stale_base_overlap_with_clear_replay_guidance(self) -> None:
        temp_root, branch, source_sha, expected_main_sha = self._prepare_stale_base_workspace(overlapping=True)
        try:
            manifest = {
                "repos": [
                    {
                        "name": "amof",
                        "path": str(temp_root / "repos" / "amof"),
                    }
                ]
            }
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-PROMOTE-MAIN-STALE-BASE-GUARD-001",
                candidate_branch=branch,
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test stale overlap rejection",
                dry_run=True,
            )
            plan = plan_promote_main_dry_run(
                manifest,
                bundle,
                ecosystem="amof-dev",
                workspace_root=temp_root,
            )
            self.assertFalse(plan.ok)
            self.assertFalse(plan.validation_checks["stale_base_overlap_free"])
            self.assertIn("stale-base overlap detected", plan.rejection_reason or "")
            self.assertIn("scripts/amof/commands/promote_main.py", plan.rejection_reason or "")
            self.assertIn("tests/test_promote_main_cli.py", plan.rejection_reason or "")
            self.assertIn("tests/test_promote_main_linkage.py", plan.rejection_reason or "")
            self.assertIn("Replay or rebase", plan.rejection_reason or "")
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_plan_promote_main_allows_stale_base_without_overlap(self) -> None:
        temp_root, branch, source_sha, expected_main_sha = self._prepare_stale_base_workspace(overlapping=False)
        try:
            manifest = {
                "repos": [
                    {
                        "name": "amof",
                        "path": str(temp_root / "repos" / "amof"),
                    }
                ]
            }
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-PROMOTE-MAIN-STALE-BASE-GUARD-001",
                candidate_branch=branch,
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test stale non-overlap",
                dry_run=True,
            )
            plan = plan_promote_main_dry_run(
                manifest,
                bundle,
                ecosystem="amof-dev",
                workspace_root=temp_root,
            )
            self.assertTrue(plan.ok, plan.rejection_reason)
            self.assertTrue(plan.validation_checks["stale_base_overlap_free"])
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_plan_promote_main_uses_workspace_receipts_audit_path_when_ecosystem_missing(self) -> None:
        temp_root, _repo, source_sha, expected_main_sha = self._prepare_workspace()
        try:
            receipts_root = temp_root / "receipts" / "promote-main" / "AMOF-RUNTIME-CONTEXT-SWITCHING-001"
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-RUNTIME-CONTEXT-SWITCHING-001",
                candidate_branch="ticket/AMOF-RUNTIME-CONTEXT-SWITCHING-001-runtime-context-switching",
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test no ecosystem audit path",
                dry_run=True,
            )
            plan = plan_promote_main_dry_run(
                {"repos": []},
                bundle,
                ecosystem=None,
                workspace_root=receipts_root,
            )
            self.assertTrue(plan.audit_record_path.startswith("audit/"))
            audit_path = receipts_root / plan.audit_record_path
            self.assertTrue(audit_path.exists())
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_plan_promote_main_missing_repo_fails_with_clear_resolution_error(self) -> None:
        temp_root = Path(tempfile.mkdtemp(prefix="amof-promote-linkage-missing-repo-"))
        try:
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-RUNTIME-CONTEXT-SWITCHING-001",
                candidate_branch="ticket/AMOF-RUNTIME-CONTEXT-SWITCHING-001-runtime-context-switching",
                source_sha="e497ee272ac4c8569539b860c2b622c4d95c8432",
                gitops_commit_sha=None,
                expected_main_sha="e497ee272ac4c8569539b860c2b622c4d95c8432",
                promotion_reason="test missing repo error",
                dry_run=True,
            )
            with self.assertRaisesRegex(RuntimeError, "could not be resolved from manifest or workspace layout"):
                plan_promote_main_dry_run(
                    {"repos": []},
                    bundle,
                    ecosystem=None,
                    workspace_root=temp_root,
                )
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_legacy_numeric_fallback_detection_is_explicit(self) -> None:
        self.assertTrue(_is_legacy_numeric_ticket_id("AMOF-221"))
        self.assertFalse(_is_legacy_numeric_ticket_id("AMOF-RUNTIME-CONTEXT-SWITCHING-001"))


if __name__ == "__main__":
    unittest.main()
