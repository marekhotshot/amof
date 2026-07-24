"""BL-072: git status --short leading space must survive probe parsing."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands.agent_cmd import (  # noqa: E402
    _git_changed_paths,
    _git_probe,
    _git_status_entries,
)


class GitStatusStripChangedPathsTests(unittest.TestCase):
    def test_status_entries_preserve_worktree_modified_paths(self) -> None:
        entries = _git_status_entries(" M docs/product/backlogs/amof.md\n")
        self.assertEqual(entries, [["M", "docs/product/backlogs/amof.md"]])

    def test_leading_space_strip_would_corrupt_path(self) -> None:
        # Document the failure mode that BL-072 fixes.
        corrupted = " M docs/product/backlogs/amof.md".strip()
        self.assertEqual(corrupted, "M docs/product/backlogs/amof.md")
        self.assertEqual(
            _git_status_entries(corrupted),
            [["M", "ocs/product/backlogs/amof.md"]],
        )

    def test_git_probe_does_not_strip_leading_porcelain_space(self) -> None:
        porcelain = " M docs/product/backlogs/amof.md\n"

        def fake_run(args, cwd=None, capture_output=None, text=None, timeout=None):
            del cwd, capture_output, text, timeout
            if args[:3] == ["git", "status", "--short"]:
                return mock.Mock(returncode=0, stdout=porcelain, stderr="")
            if args[:3] == ["git", "diff", "--numstat"]:
                return mock.Mock(
                    returncode=0,
                    stdout="4\t0\tdocs/product/backlogs/amof.md\n",
                    stderr="",
                )
            if args[:3] == ["git", "diff", "--"]:
                return mock.Mock(returncode=0, stdout="+note\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("amof.commands.agent_cmd.subprocess.run", side_effect=fake_run):
                probed = _git_probe(Path(tmp))

        self.assertTrue(probed["status"].startswith(" M "))
        entries = json.loads(probed["status_entries"])
        self.assertEqual(entries, [["M", "docs/product/backlogs/amof.md"]])
        changed = _git_changed_paths(
            {"status_entries": "[]"},
            probed,
        )
        self.assertEqual(changed, ["docs/product/backlogs/amof.md"])


if __name__ == "__main__":
    unittest.main()
