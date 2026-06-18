from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof.commands import promote_main as promote_main_module
from amof.commands.promote_main import (
    PromoteMainInput,
    execute_promote_main_push,
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


def _write_private_policy(workspace_root: Path, *, remote_url: str) -> Path:
    policy_path = workspace_root / ".amof-local" / "promotion-targets.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        "version: 1\n"
        "targets:\n"
        "  amof-private:\n"
        "    path: repos/amof-private\n"
        f"    remote: {remote_url}\n"
        "    branch: main\n",
        encoding="utf-8",
    )
    return policy_path


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

    def _prepare_private_workspace(self) -> tuple[Path, Path, str, str, str, Path]:
        temp_root = Path(tempfile.mkdtemp(prefix="amof-promote-private-linkage-"))
        remote = temp_root / "remote-private.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)

        seed = temp_root / "seed-private"
        subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True, text=True)
        (seed / "README.md").write_text("# private base\n", encoding="utf-8")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "private base")
        _git(seed, "remote", "add", "origin", str(remote))
        _git(seed, "push", "-u", "origin", "main")

        repo = temp_root / "repos" / "amof-private"
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True, text=True)
        _git(repo, "fetch", "origin", "main")
        expected_main_sha = _git(repo, "rev-parse", "origin/main")

        branch = "ticket/AMOF-PRIVATE-IAL-INTAKE-CONTRACT-RECOVERY-001-private-ial-intake-contract-recovery"
        _git(repo, "checkout", "-b", branch)
        (repo / "README.md").write_text("# private changed\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "private change for promote-main")
        source_sha = _git(repo, "rev-parse", "HEAD")
        policy_path = _write_private_policy(temp_root, remote_url=str(remote))

        return temp_root, repo, branch, source_sha, expected_main_sha, policy_path

    def _prepare_public_private_compat_workspace(
        self,
    ) -> tuple[Path, str, str, str, str, str]:
        temp_root = Path(tempfile.mkdtemp(prefix="amof-promote-compat-lock-"))

        public_remote = temp_root / "public.git"
        subprocess.run(["git", "init", "--bare", str(public_remote)], check=True, capture_output=True, text=True)
        public_seed = temp_root / "public-seed"
        subprocess.run(["git", "init", "-b", "main", str(public_seed)], check=True, capture_output=True, text=True)
        (public_seed / "README.md").write_text("# public base\n", encoding="utf-8")
        _git(public_seed, "add", "README.md")
        _git(public_seed, "commit", "-m", "public base")
        _git(public_seed, "remote", "add", "origin", str(public_remote))
        _git(public_seed, "push", "-u", "origin", "main")

        public_repo = temp_root / "repos" / "amof"
        public_repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", str(public_remote), str(public_repo)], check=True, capture_output=True, text=True)
        _git(public_repo, "fetch", "origin", "main")
        expected_public_main = _git(public_repo, "rev-parse", "origin/main")
        branch = "ticket/AMOF-PROMOTE-MAIN-COMPAT-LOCK-RECONCILE-001-promote-main-compat-lock-reconcile"
        _git(public_repo, "checkout", "-b", branch)
        (public_repo / "README.md").write_text("# public promoted\n", encoding="utf-8")
        _git(public_repo, "add", "README.md")
        _git(public_repo, "commit", "-m", "public candidate")
        source_sha = _git(public_repo, "rev-parse", "HEAD")

        private_remote = temp_root / "private.git"
        subprocess.run(["git", "init", "--bare", str(private_remote)], check=True, capture_output=True, text=True)
        private_seed = temp_root / "private-seed"
        subprocess.run(["git", "init", "-b", "main", str(private_seed)], check=True, capture_output=True, text=True)
        (private_seed / "README.md").write_text("# private base\n", encoding="utf-8")
        _git(private_seed, "add", "README.md")
        _git(private_seed, "commit", "-m", "private base")
        _git(private_seed, "remote", "add", "origin", str(private_remote))
        _git(private_seed, "push", "-u", "origin", "main")

        private_repo = temp_root / "repos" / "amof-private"
        subprocess.run(["git", "clone", str(private_remote), str(private_repo)], check=True, capture_output=True, text=True)
        _git(private_repo, "fetch", "origin", "main")
        private_origin_main = _git(private_repo, "rev-parse", "origin/main")
        (private_repo / "LOCAL_ONLY.md").write_text("local divergent head\n", encoding="utf-8")
        _git(private_repo, "add", "LOCAL_ONLY.md")
        _git(private_repo, "commit", "-m", "local private change not on origin")
        private_local_head = _git(private_repo, "rev-parse", "HEAD")

        lock_path = temp_root / "compat" / "public-private.lock.yaml"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            "public:\n"
            "  repo_url: \"https://github.com/marekhotshot/amof.git\"\n"
            "  main_sha: \"0000000000000000000000000000000000000000\"\n"
            "private:\n"
            "  repo_url: \"https://github.com/marekhotshot/amof-private.git\"\n"
            "  current_main_sha: '1111111111111111111111111111111111111111'\n",
            encoding="utf-8",
        )

        return (
            temp_root,
            branch,
            source_sha,
            expected_public_main,
            private_origin_main,
            private_local_head,
        )

    def _prepare_public_candidate_worktree_workspace(self) -> tuple[Path, Path, Path, str, str]:
        temp_root = Path(tempfile.mkdtemp(prefix="amof-promote-closeout-"))
        remote = temp_root / "public.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)

        seed = temp_root / "seed"
        subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True, text=True)
        (seed / "README.md").write_text("# public base\n", encoding="utf-8")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "public base")
        _git(seed, "remote", "add", "origin", str(remote))
        _git(seed, "push", "-u", "origin", "main")

        repo = temp_root / "repos" / "amof"
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True, text=True)
        _git(repo, "fetch", "origin", "main")
        expected_public_main = _git(repo, "rev-parse", "origin/main")

        candidate_branch = "ticket/AMOF-PROMOTE-MAIN-WORKTREE-CLOSEOUT-001"
        candidate_worktree = temp_root / "candidate-worktree"
        _git(repo, "worktree", "add", "-b", candidate_branch, str(candidate_worktree), "origin/main")
        (candidate_worktree / "README.md").write_text("# candidate from worktree\n", encoding="utf-8")
        _git(candidate_worktree, "add", "README.md")
        _git(candidate_worktree, "commit", "-m", "candidate worktree change")
        source_sha = _git(candidate_worktree, "rev-parse", "HEAD")
        return temp_root, repo, candidate_worktree, candidate_branch, source_sha

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

    def test_plan_promote_main_private_dry_run_succeeds_and_audit_records_policy_path(self) -> None:
        temp_root, _repo, branch, source_sha, expected_main_sha, policy_path = self._prepare_private_workspace()
        try:
            bundle = PromoteMainInput(
                repo="amof-private",
                ticket_id="AMOF-PRIVATE-IAL-INTAKE-CONTRACT-RECOVERY-001",
                candidate_branch=branch,
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test private dry-run with deterministic policy",
                dry_run=True,
            )
            plan = plan_promote_main_dry_run(
                {"repos": []},
                bundle,
                ecosystem=None,
                workspace_root=temp_root,
            )
            self.assertTrue(plan.ok, plan.rejection_reason)
            self.assertEqual(plan.promotion_target_policy_path, str(policy_path.resolve(strict=False)))
            audit_path = temp_root / plan.audit_record_path
            audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit_payload["repo"], "amof-private")
            self.assertEqual(audit_payload["promotion_target_policy_path"], str(policy_path.resolve(strict=False)))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_successful_public_promotion_reconciles_operator_compat_lock(self) -> None:
        (
            temp_root,
            branch,
            source_sha,
            expected_public_main,
            private_origin_main,
            private_local_head,
        ) = self._prepare_public_private_compat_workspace()
        try:
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-PROMOTE-MAIN-COMPAT-LOCK-RECONCILE-001",
                candidate_branch=branch,
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_public_main,
                promotion_reason="test compat lock reconciliation",
                dry_run=False,
            )
            with mock.patch.object(
                promote_main_module,
                "_run_post_reconciliation_doctor",
                return_value={"status": "failed", "exit_code": 7},
            ) as doctor:
                plan = execute_promote_main_push(
                    {"repos": [{"name": "amof", "path": str(temp_root / "repos" / "amof")}]},
                    bundle,
                    ecosystem=None,
                    workspace_root=temp_root,
                )

            doctor.assert_called_once()
            self.assertTrue(plan.ok, plan.failure_reason)
            self.assertEqual(plan.status, "promoted")
            self.assertEqual(plan.push_succeeded, True)
            self.assertEqual(plan.compat_lock_reconciliation["attempted"], True)
            self.assertEqual(plan.compat_lock_reconciliation["status"], "warning")
            self.assertEqual(plan.compat_lock_reconciliation["doctor_status"], "warning")
            self.assertEqual(plan.compat_lock_reconciliation["failure_reason"], "doctor_failed")
            self.assertTrue(Path(plan.compat_lock_reconciliation["backup_path"]).is_file())
            self.assertEqual(plan.compat_lock_reconciliation["public_origin_main"], plan.result_main_sha)
            self.assertEqual(plan.compat_lock_reconciliation["private_origin_main"], private_origin_main)
            self.assertNotEqual(private_local_head, private_origin_main)

            lock_text = (temp_root / "compat" / "public-private.lock.yaml").read_text(encoding="utf-8")
            self.assertIn(f'main_sha: "{plan.result_main_sha}"', lock_text)
            self.assertIn(f"current_main_sha: '{private_origin_main}'", lock_text)
            self.assertNotIn(private_local_head, lock_text)

            audit_payload = json.loads((temp_root / plan.audit_record_path).read_text(encoding="utf-8"))
            reconciliation = audit_payload["compat_lock_reconciliation"]
            self.assertEqual(
                set(reconciliation),
                {
                    "attempted",
                    "status",
                    "public_origin_main",
                    "private_origin_main",
                    "lock_path",
                    "backup_path",
                    "doctor_status",
                    "failure_reason",
                },
            )
            self.assertEqual(reconciliation["status"], "warning")
            self.assertEqual(reconciliation["doctor_status"], "warning")
            self.assertEqual(reconciliation["private_origin_main"], private_origin_main)
            self.assertNotIn("stdout", json.dumps(reconciliation))
            self.assertNotIn("stderr", json.dumps(reconciliation))
            self.assertNotIn("LOCAL_ONLY", json.dumps(reconciliation))
            self.assertNotIn(private_local_head, json.dumps(reconciliation))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_push_failure_does_not_run_compat_lock_reconciliation(self) -> None:
        (
            temp_root,
            branch,
            source_sha,
            expected_public_main,
            _private_origin_main,
            _private_local_head,
        ) = self._prepare_public_private_compat_workspace()
        try:
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-PROMOTE-MAIN-COMPAT-LOCK-RECONCILE-001",
                candidate_branch=branch,
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_public_main,
                promotion_reason="test push failure skips compat reconciliation",
                dry_run=False,
            )
            with mock.patch.object(
                promote_main_module,
                "_push_synthetic_commit",
                return_value=(False, "simulated push failure"),
            ):
                with mock.patch.object(
                    promote_main_module,
                    "_reconcile_operator_compat_lock_after_promotion",
                ) as reconcile:
                    plan = execute_promote_main_push(
                        {"repos": [{"name": "amof", "path": str(temp_root / "repos" / "amof")}]},
                        bundle,
                        ecosystem=None,
                        workspace_root=temp_root,
                    )

            self.assertFalse(plan.ok)
            self.assertEqual(plan.status, "failed")
            self.assertEqual(plan.failure_stage, "push_rejected")
            self.assertIsNone(plan.compat_lock_reconciliation)
            reconcile.assert_not_called()
            audit_payload = json.loads((temp_root / plan.audit_record_path).read_text(encoding="utf-8"))
            self.assertIsNone(audit_payload["compat_lock_reconciliation"])
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_successful_public_promotion_writes_candidate_worktree_closeout_receipt(self) -> None:
        temp_root, repo, candidate_worktree, candidate_branch, source_sha = self._prepare_public_candidate_worktree_workspace()
        try:
            expected_public_main = _git(repo, "rev-parse", "origin/main")
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-PROMOTE-MAIN-WORKTREE-CLOSEOUT-001",
                candidate_branch=candidate_branch,
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_public_main,
                promotion_reason="test candidate worktree closeout receipt",
                dry_run=False,
            )
            with mock.patch.object(
                promote_main_module,
                "_reconcile_operator_compat_lock_after_promotion",
                return_value=None,
            ):
                plan = execute_promote_main_push(
                    {"repos": [{"name": "amof", "path": str(repo)}]},
                    bundle,
                    ecosystem=None,
                    workspace_root=temp_root,
                )

            self.assertTrue(plan.ok, plan.failure_reason)
            self.assertEqual(plan.status, "promoted")
            closeout = plan.candidate_worktree_closeout
            self.assertIsNotNone(closeout)
            assert closeout is not None
            self.assertEqual(closeout["candidate_branch"], candidate_branch)
            self.assertEqual(closeout["candidate_worktree_path"], str(candidate_worktree.resolve(strict=False)))
            self.assertEqual(closeout["source_sha"], source_sha)
            self.assertEqual(closeout["promoted_result_sha"], plan.result_main_sha)
            self.assertEqual(closeout["worktree_clean"], True)
            self.assertEqual(closeout["auto_removal_allowed"], True)
            self.assertIn(str(repo.resolve(strict=False)), closeout["safe_remove_command"])
            self.assertIn(str(candidate_worktree.resolve(strict=False)), closeout["safe_remove_command"])
            receipt_path = temp_root / closeout["closeout_receipt_path"]
            self.assertTrue(receipt_path.is_file())
            receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt_payload["candidate_branch"], candidate_branch)
            self.assertEqual(receipt_payload["candidate_worktree_path"], str(candidate_worktree.resolve(strict=False)))
            self.assertEqual(receipt_payload["safe_remove_command"], closeout["safe_remove_command"])
            audit_payload = json.loads((temp_root / plan.audit_record_path).read_text(encoding="utf-8"))
            self.assertEqual(audit_payload["candidate_worktree_closeout"]["closeout_receipt_path"], closeout["closeout_receipt_path"])
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

    def test_plan_promote_main_private_rejects_invalid_expected_main_sha(self) -> None:
        temp_root, repo, branch, source_sha, _expected_main_sha, _policy_path = self._prepare_private_workspace()
        try:
            _git(repo, "checkout", "main")
            _git(repo, "checkout", "-b", "wrong-expected-main")
            (repo / "WRONG.md").write_text("wrong expected main\n", encoding="utf-8")
            _git(repo, "add", "WRONG.md")
            _git(repo, "commit", "-m", "wrong expected main")
            wrong_expected_main_sha = _git(repo, "rev-parse", "HEAD")
            _git(repo, "checkout", branch)

            bundle = PromoteMainInput(
                repo="amof-private",
                ticket_id="AMOF-PRIVATE-IAL-INTAKE-CONTRACT-RECOVERY-001",
                candidate_branch=branch,
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=wrong_expected_main_sha,
                promotion_reason="test private invalid expected-main",
                dry_run=True,
            )
            plan = plan_promote_main_dry_run(
                {"repos": []},
                bundle,
                ecosystem=None,
                workspace_root=temp_root,
            )
            self.assertFalse(plan.ok)
            self.assertIn("origin/main drifted", plan.rejection_reason or "")
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_plan_promote_main_private_rejects_unreachable_source_sha(self) -> None:
        temp_root, repo, branch, _source_sha, expected_main_sha, _policy_path = self._prepare_private_workspace()
        try:
            _git(repo, "checkout", "main")
            _git(repo, "checkout", "-b", "other-private-work")
            (repo / "OTHER.md").write_text("other\n", encoding="utf-8")
            _git(repo, "add", "OTHER.md")
            _git(repo, "commit", "-m", "other private work")
            unreachable_source_sha = _git(repo, "rev-parse", "HEAD")
            _git(repo, "checkout", branch)

            bundle = PromoteMainInput(
                repo="amof-private",
                ticket_id="AMOF-PRIVATE-IAL-INTAKE-CONTRACT-RECOVERY-001",
                candidate_branch=branch,
                source_sha=unreachable_source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test private unreachable source",
                dry_run=True,
            )
            plan = plan_promote_main_dry_run(
                {"repos": []},
                bundle,
                ecosystem=None,
                workspace_root=temp_root,
            )
            self.assertFalse(plan.ok)
            self.assertIn("source_sha is not reachable from candidate_branch", plan.rejection_reason or "")
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_plan_promote_main_private_rejects_invalid_evidence(self) -> None:
        temp_root, _repo, branch, source_sha, expected_main_sha, _policy_path = self._prepare_private_workspace()
        try:
            evidence_path = temp_root / ".amof-local" / "evidence" / "run-summary.json"
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(
                json.dumps(
                    {
                        "final_status": "failed",
                        "lifecycle_state": "not_ready",
                        "expected_sha": source_sha,
                        "actual_sha": source_sha,
                        "failure_message": "boom",
                    }
                ),
                encoding="utf-8",
            )
            bundle = PromoteMainInput(
                repo="amof-private",
                ticket_id="AMOF-PRIVATE-IAL-INTAKE-CONTRACT-RECOVERY-001",
                candidate_branch=branch,
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test private invalid evidence",
                dry_run=True,
                require_run_summary=str(evidence_path),
            )
            plan = plan_promote_main_dry_run(
                {"repos": []},
                bundle,
                ecosystem=None,
                workspace_root=temp_root,
            )
            self.assertFalse(plan.ok)
            self.assertIn("run_summary.final_status must be 'executed'", plan.rejection_reason or "")
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

    def test_plan_promote_main_repo_checkout_uses_detected_operator_receipts_root(self) -> None:
        temp_root, repo, source_sha, expected_main_sha = self._prepare_workspace()
        try:
            lock_path = temp_root / "compat" / "public-private.lock.yaml"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                "public:\n"
                "  repo_url: \"https://github.com/marekhotshot/amof.git\"\n"
                "  main_sha: \"0000000000000000000000000000000000000000\"\n",
                encoding="utf-8",
            )
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-RUNTIME-CONTEXT-SWITCHING-001",
                candidate_branch="ticket/AMOF-RUNTIME-CONTEXT-SWITCHING-001-runtime-context-switching",
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test operator receipts root detection",
                dry_run=True,
            )
            plan = plan_promote_main_dry_run(
                {"repos": []},
                bundle,
                ecosystem=None,
                workspace_root=repo,
            )
            audit_path = temp_root / plan.audit_record_path
            self.assertTrue(audit_path.exists())
            self.assertIn("receipts/promote-main/AMOF-RUNTIME-CONTEXT-SWITCHING-001/audit", str(audit_path))
            self.assertFalse((repo / "receipts").exists())
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_plan_promote_main_repo_checkout_falls_back_to_amof_home_receipts(self) -> None:
        temp_root, repo, source_sha, expected_main_sha = self._prepare_workspace()
        try:
            amof_home = temp_root / "amof-home"
            bundle = PromoteMainInput(
                repo="amof",
                ticket_id="AMOF-RUNTIME-CONTEXT-SWITCHING-001",
                candidate_branch="ticket/AMOF-RUNTIME-CONTEXT-SWITCHING-001-runtime-context-switching",
                source_sha=source_sha,
                gitops_commit_sha=None,
                expected_main_sha=expected_main_sha,
                promotion_reason="test AMOF_HOME receipts fallback",
                dry_run=True,
            )
            with mock.patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                plan = plan_promote_main_dry_run(
                    {"repos": []},
                    bundle,
                    ecosystem=None,
                    workspace_root=repo,
                )
            audit_path = Path(plan.audit_record_path)
            self.assertTrue(audit_path.exists())
            self.assertTrue(
                str(audit_path).startswith(
                    str(
                        amof_home / "share" / "receipts" / "promote-main" / "AMOF-RUNTIME-CONTEXT-SWITCHING-001"
                    )
                )
            )
            self.assertFalse((repo / "receipts").exists())
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
