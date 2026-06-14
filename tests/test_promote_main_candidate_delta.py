"""Regression tests for AMOF-PROMOTE-MAIN-CANDIDATE-DELTA-SAFETY-001.

These tests lock in the candidate-delta promotion contract: a governed
promotion must apply only the candidate's own ticket delta
(diff(merge_base(candidate, current_main) .. candidate_source)) onto the
current canonical main, never reverting main's advancement, and must fail
closed when the candidate delta overlaps files changed on main since the
merge base.

The defect being guarded against: synthesizing the promoted tree from
diff(expected_main .. candidate_source) silently carries the absence of
prior, disjoint promotions as reverts when multiple ticket branches share an
older base.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof.commands.promote_main import (  # noqa: E402
    PromoteMainInput,
    execute_promote_main_push,
    plan_promote_main_dry_run,
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


class _PrivateWorkspace:
    """A private repo cloned from a bare remote, plus a deterministic policy."""

    def __init__(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="amof-promote-delta-"))
        self.remote = self.temp_root / "remote-private.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True, text=True)

        seed = self.temp_root / "seed"
        subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True, text=True)
        # Base commit B carries three disjoint files plus a file a candidate may delete/rename.
        for name in ("fileA.txt", "fileB.txt", "fileC.txt", "fileD.txt", "fileR.txt"):
            (seed / name).write_text("base\n", encoding="utf-8")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "base B")
        _git(seed, "remote", "add", "origin", str(self.remote))
        _git(seed, "push", "-u", "origin", "main")

        self.repo = self.temp_root / "repos" / "amof-private"
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", str(self.remote), str(self.repo)], check=True, capture_output=True, text=True)
        _git(self.repo, "fetch", "origin", "main")
        self.base_sha = _git(self.repo, "rev-parse", "origin/main")

        policy_path = self.temp_root / ".amof-local" / "promotion-targets.yaml"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(
            "version: 1\n"
            "targets:\n"
            "  amof-private:\n"
            "    path: repos/amof-private\n"
            f"    remote: {self.remote}\n"
            "    branch: main\n",
            encoding="utf-8",
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def origin_main(self) -> str:
        _git(self.repo, "fetch", "origin", "main")
        return _git(self.repo, "rev-parse", "origin/main")

    def make_candidate(self, ticket: str, slug: str, mutate: list[tuple[str, str | None]], *, base: str | None = None) -> tuple[str, str]:
        """Create a candidate branch from `base` (default base B) applying mutations.

        Each mutation is (path, content) where content=None means delete the file.
        Returns (branch_name, source_sha).
        """
        branch = f"ticket/{ticket}-{slug}"
        _git(self.repo, "checkout", "-q", "-B", branch, base or self.base_sha)
        for path, content in mutate:
            full = self.repo / path
            if content is None:
                _git(self.repo, "rm", "-q", path)
            else:
                full.write_text(content, encoding="utf-8")
                _git(self.repo, "add", path)
        _git(self.repo, "commit", "-m", f"{ticket}: candidate change")
        source = _git(self.repo, "rev-parse", "HEAD")
        # Leave the repo on a detached-safe ref so later checkouts are clean.
        _git(self.repo, "checkout", "-q", "--detach", "origin/main")
        return branch, source

    def promote(self, ticket: str, branch: str, source: str, *, dry_run: bool):
        bundle = PromoteMainInput(
            repo="amof-private",
            ticket_id=ticket,
            candidate_branch=branch,
            source_sha=source,
            gitops_commit_sha=None,
            expected_main_sha=self.origin_main(),
            promotion_reason="candidate-delta regression",
            dry_run=dry_run,
        )
        if dry_run:
            return plan_promote_main_dry_run({"repos": []}, bundle, ecosystem=None, workspace_root=self.temp_root)
        return execute_promote_main_push({"repos": []}, bundle, ecosystem=None, workspace_root=self.temp_root)

    def main_content(self, path: str) -> str:
        _git(self.repo, "fetch", "origin", "main")
        return _git(self.repo, "show", f"origin/main:{path}")

    def main_has(self, path: str) -> bool:
        _git(self.repo, "fetch", "origin", "main")
        result = subprocess.run(
            ["git", "cat-file", "-e", f"origin/main:{path}"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=_commit_env(),
        )
        return result.returncode == 0


class CandidateDeltaPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = _PrivateWorkspace()
        self.addCleanup(self.ws.cleanup)

    # --- Requirements 1-5, 9: disjoint sequential promotion is additive ---
    def test_three_disjoint_candidates_promote_sequentially_without_revert(self) -> None:
        a_branch, a_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-A-001", "delta-a", [("fileA.txt", "A-change\n")])
        b_branch, b_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-B-001", "delta-b", [("fileB.txt", "B-change\n")])
        c_branch, c_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-C-001", "delta-c", [("fileC.txt", "C-change\n")])

        plan_a = self.ws.promote("AMOF-DELTA-SAFETY-A-001", a_branch, a_src, dry_run=False)
        self.assertTrue(plan_a.ok, plan_a.failure_reason or plan_a.rejection_reason)
        self.assertEqual(plan_a.status, "promoted")
        self.assertEqual(self.ws.main_content("fileA.txt"), "A-change")

        # Candidate B is promoted from its original stale shared base.
        plan_b = self.ws.promote("AMOF-DELTA-SAFETY-B-001", b_branch, b_src, dry_run=False)
        self.assertTrue(plan_b.ok, plan_b.failure_reason or plan_b.rejection_reason)
        self.assertEqual(plan_b.status, "promoted")
        # Requirement 3: promotion B must not revert A. (This content assertion is the
        # direct defect guard: the unfixed diff(expected_main..source) synthesis leaves
        # fileA.txt == "base" here.)
        self.assertEqual(self.ws.main_content("fileA.txt"), "A-change")
        self.assertEqual(self.ws.main_content("fileB.txt"), "B-change")
        self.assertTrue(plan_b.stale_base, "candidate B should be detected as stale")

        # Candidate C is promoted from its original stale shared base.
        plan_c = self.ws.promote("AMOF-DELTA-SAFETY-C-001", c_branch, c_src, dry_run=False)
        self.assertTrue(plan_c.ok, plan_c.failure_reason or plan_c.rejection_reason)
        self.assertEqual(plan_c.status, "promoted")
        # Requirements 2, 4, 5: final main contains A + B + C; C did not revert A or B.
        self.assertEqual(self.ws.main_content("fileA.txt"), "A-change")
        self.assertEqual(self.ws.main_content("fileB.txt"), "B-change")
        self.assertEqual(self.ws.main_content("fileC.txt"), "C-change")
        # Requirement 9: untouched base files remain present (no spurious deletions).
        self.assertTrue(self.ws.main_has("fileD.txt"))

        # Requirement 15: audit receipt records merge-base and candidate-delta evidence.
        audit_path = self.ws.temp_root / plan_c.audit_record_path
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit["merge_base_sha"], self.ws.base_sha)
        self.assertEqual(audit["candidate_delta_paths"], ["fileC.txt"])
        self.assertIn("fileA.txt", audit["current_main_advanced_paths"])
        self.assertIn("fileB.txt", audit["current_main_advanced_paths"])
        self.assertEqual(audit["overlap_paths"], [])
        self.assertTrue(audit["stale_base"])

    # --- Requirements 6, 7, 14: overlapping stale candidate fails closed ---
    def test_stale_candidate_overlapping_current_main_fails_closed(self) -> None:
        a_branch, a_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-A-001", "delta-a", [("fileA.txt", "A-change\n")])
        # Candidate X (stale, shared base) also edits fileA -> overlaps A's main advancement.
        x_branch, x_src = self.ws.make_candidate(
            "AMOF-DELTA-SAFETY-X-001", "delta-x", [("fileA.txt", "X-change\n"), ("fileB.txt", "X-b\n")]
        )

        plan_a = self.ws.promote("AMOF-DELTA-SAFETY-A-001", a_branch, a_src, dry_run=False)
        self.assertTrue(plan_a.ok, plan_a.failure_reason or plan_a.rejection_reason)

        plan_x = self.ws.promote("AMOF-DELTA-SAFETY-X-001", x_branch, x_src, dry_run=True)
        self.assertFalse(plan_x.ok)
        self.assertFalse(plan_x.validation_checks["stale_base_overlap_free"])
        # Requirement 7: failure reports the exact overlapping path.
        self.assertIn("fileA.txt", plan_x.rejection_reason or "")
        self.assertEqual(plan_x.overlap_paths, ["fileA.txt"])
        # Requirement 14: no synthetic tree materialized after a failed overlap check.
        self.assertIsNone(plan_x.synthetic_tree_sha)

    # --- Requirement 8: candidate deletion of a file it owns remains representable ---
    def test_stale_candidate_can_delete_its_own_file_without_reverting_main(self) -> None:
        a_branch, a_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-A-001", "delta-a", [("fileA.txt", "A-change\n")])
        # Candidate D (stale) deletes fileD which it owns; disjoint from fileA.
        d_branch, d_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-D-001", "delta-d", [("fileD.txt", None)])

        self.ws.promote("AMOF-DELTA-SAFETY-A-001", a_branch, a_src, dry_run=False)
        plan_d = self.ws.promote("AMOF-DELTA-SAFETY-D-001", d_branch, d_src, dry_run=False)
        self.assertTrue(plan_d.ok, plan_d.failure_reason or plan_d.rejection_reason)
        # The candidate's owned deletion is honoured...
        self.assertFalse(self.ws.main_has("fileD.txt"))
        # ...while main's prior advancement (fileA) is preserved, not reverted.
        self.assertEqual(self.ws.main_content("fileA.txt"), "A-change")

    # --- Requirement 10: rename within a candidate is representable (no-rename diff) ---
    def test_stale_candidate_rename_of_owned_file_is_representable(self) -> None:
        a_branch, a_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-A-001", "delta-a", [("fileA.txt", "A-change\n")])
        # Candidate R (stale) renames fileR -> fileR2 (delete + add of owned paths).
        r_branch, r_src = self.ws.make_candidate(
            "AMOF-DELTA-SAFETY-R-001", "delta-r", [("fileR.txt", None), ("fileR2.txt", "renamed\n")]
        )

        self.ws.promote("AMOF-DELTA-SAFETY-A-001", a_branch, a_src, dry_run=False)
        plan_r = self.ws.promote("AMOF-DELTA-SAFETY-R-001", r_branch, r_src, dry_run=False)
        self.assertTrue(plan_r.ok, plan_r.failure_reason or plan_r.rejection_reason)
        self.assertFalse(self.ws.main_has("fileR.txt"))
        self.assertEqual(self.ws.main_content("fileR2.txt"), "renamed")
        self.assertEqual(self.ws.main_content("fileA.txt"), "A-change")

    # --- Requirement 11: non-stale direct candidate behaviour remains green ---
    def test_non_stale_direct_candidate_promotes_unchanged(self) -> None:
        # Branch directly from current origin/main (== base): merge_base == expected_main.
        a_branch, a_src = self.ws.make_candidate(
            "AMOF-DELTA-SAFETY-A-001", "delta-a", [("fileA.txt", "A-change\n")], base="origin/main"
        )
        plan = self.ws.promote("AMOF-DELTA-SAFETY-A-001", a_branch, a_src, dry_run=False)
        self.assertTrue(plan.ok, plan.failure_reason or plan.rejection_reason)
        self.assertEqual(plan.status, "promoted")
        self.assertFalse(plan.stale_base)
        self.assertEqual(plan.merge_base_sha, plan.expected_main_sha)
        self.assertEqual(self.ws.main_content("fileA.txt"), "A-change")

    # --- Requirement 13: dry-run and push use identical candidate-delta semantics ---
    def test_dry_run_and_push_synthetic_tree_match_for_stale_candidate(self) -> None:
        a_branch, a_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-A-001", "delta-a", [("fileA.txt", "A-change\n")])
        b_branch, b_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-B-001", "delta-b", [("fileB.txt", "B-change\n")])

        self.ws.promote("AMOF-DELTA-SAFETY-A-001", a_branch, a_src, dry_run=False)

        dry = self.ws.promote("AMOF-DELTA-SAFETY-B-001", b_branch, b_src, dry_run=True)
        self.assertTrue(dry.ok, dry.rejection_reason)
        self.assertIsNotNone(dry.synthetic_tree_sha)
        push = self.ws.promote("AMOF-DELTA-SAFETY-B-001", b_branch, b_src, dry_run=False)
        self.assertTrue(push.ok, push.failure_reason or push.rejection_reason)
        # If semantics differed, the push would fail with synthetic_tree_mismatch.
        self.assertEqual(push.synthetic_tree_sha, dry.synthetic_tree_sha)

    # --- Requirement 12: existing stale-base overlap guard helper stays correct ---
    def test_stale_base_overlap_details_helper_distinguishes_disjoint_and_overlap(self) -> None:
        _a_branch, a_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-A-001", "delta-a", [("fileA.txt", "A-change\n")])
        b_branch, b_src = self.ws.make_candidate("AMOF-DELTA-SAFETY-B-001", "delta-b", [("fileB.txt", "B-change\n")])
        overlap_branch, overlap_src = self.ws.make_candidate(
            "AMOF-DELTA-SAFETY-Y-001", "delta-y", [("fileA.txt", "Y-change\n")]
        )
        # Advance main to include A.
        self.ws.promote("AMOF-DELTA-SAFETY-A-001", _a_branch, a_src, dry_run=False)
        current_main = self.ws.origin_main()

        mb_disjoint, overlap_disjoint = _stale_base_overlap_details(
            self.ws.repo, source_sha=b_src, current_origin_main_sha=current_main
        )
        self.assertEqual(mb_disjoint, self.ws.base_sha)
        self.assertEqual(overlap_disjoint, [])

        mb_overlap, overlap_files = _stale_base_overlap_details(
            self.ws.repo, source_sha=overlap_src, current_origin_main_sha=current_main
        )
        self.assertEqual(mb_overlap, self.ws.base_sha)
        self.assertEqual(overlap_files, ["fileA.txt"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
