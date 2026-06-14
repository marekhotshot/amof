"""New-file (untracked) creation must register as a real target mutation.

Regression coverage for AMOF-PLAN-EXECUTE-NEWFILE-DIFF-VERIFICATION-001.

`git diff` reports only tracked changes, so a plan-execute slice whose deliverable
is *new files* previously verified as "produced no target repository diff" even
though valid files were written. These tests pin that `_git_probe` now surfaces
untracked files through numstat, that mutation detection sees them, and that the
diff guard and py_compile verification operate on newly-created files.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands.agent_cmd import (
    _evaluate_diff_guard,
    _git_probe,
    _untracked_numstat,
    _verify_changed_python_files,
)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class NewFileDiffVerificationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _git(["init", "-q"], self.root)
        _git(["config", "user.email", "t@t"], self.root)
        _git(["config", "user.name", "t"], self.root)
        (self.root / "base.txt").write_text("seed\n", encoding="utf-8")
        _git(["add", "."], self.root)
        _git(["commit", "-q", "-m", "base"], self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_untracked_file_appears_in_probe_numstat(self):
        before = _git_probe(self.root)
        self.assertEqual(before.get("numstat", ""), "")

        (self.root / "contracts").mkdir()
        (self.root / "contracts" / "ticket_status.py").write_text(
            "x = 1\ny = 2\n", encoding="utf-8"
        )
        after = _git_probe(self.root)

        self.assertIn("contracts/ticket_status.py", after.get("numstat", ""))
        # Mutation detection signal used by the verifier.
        self.assertNotEqual(before.get("numstat"), after.get("numstat"))
        self.assertTrue(bool(after.get("numstat") or after.get("diff")))

    def test_untracked_numstat_counts_lines(self):
        (self.root / "new.py").write_text("a\nb\nc\n", encoding="utf-8")
        status = _git_probe(self.root)["status"]
        line = _untracked_numstat(self.root, status)
        self.assertEqual(line, "3\t0\tnew.py")

    def test_diff_guard_sees_new_file_for_requested_path(self):
        (self.root / "contracts").mkdir()
        (self.root / "contracts" / "ticket_status.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
        after = _git_probe(self.root)
        guard = _evaluate_diff_guard(
            "Add exactly ONE new module contracts/ticket_status.py", self.root, after
        )
        self.assertIn("contracts/ticket_status.py", guard["changed_files"])
        self.assertTrue(guard["requested_paths_observed"])
        self.assertEqual(guard["status"], "pass")

    def test_py_compile_runs_on_new_python_file(self):
        (self.root / "good.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        after = _git_probe(self.root)
        result = _verify_changed_python_files(self.root, after)
        self.assertEqual(result["status"], "pass", result)
        self.assertIn("good.py", result["files"])

    def test_py_compile_detects_broken_new_python_file(self):
        (self.root / "bad.py").write_text("def f(:\n", encoding="utf-8")
        after = _git_probe(self.root)
        result = _verify_changed_python_files(self.root, after)
        self.assertEqual(result["status"], "fail", result)


if __name__ == "__main__":
    unittest.main()
