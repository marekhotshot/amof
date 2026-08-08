"""Regression: relative writable roots must bind to readable_root, not process CWD."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.execution_backends import hermes_opensandbox


class HermesResolveRootsRelativeTests(unittest.TestCase):
    def test_relative_root_joins_readable_workspace_when_cwd_is_slash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "00-amof-private"
            target = repo / "docs" / "operations" / "remote-ial-operator-cheatsheet.md"
            target.parent.mkdir(parents=True)
            target.write_text("# cheatsheet\n", encoding="utf-8")

            previous = os.getcwd()
            try:
                os.chdir("/")
                resolved = hermes_opensandbox._resolve_roots(
                    ["docs/operations/remote-ial-operator-cheatsheet.md"],
                    readable_root=str(repo),
                )
            finally:
                os.chdir(previous)

            self.assertEqual(resolved, [target.resolve()])

    def test_relative_root_without_readable_workspace_fails_closed(self) -> None:
        with self.assertRaises(hermes_opensandbox.HermesBackendError) as ctx:
            hermes_opensandbox._resolve_roots(
                ["docs/operations/remote-ial-operator-cheatsheet.md"],
                readable_root=None,
            )
        self.assertIn("requires readable workspace", str(ctx.exception))

    def test_absolute_outside_readable_workspace_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            outside = Path(tmp) / "outside.md"
            outside.write_text("x\n", encoding="utf-8")
            with self.assertRaises(hermes_opensandbox.HermesBackendError) as ctx:
                hermes_opensandbox._resolve_roots(
                    [str(outside)],
                    readable_root=str(repo),
                )
            self.assertIn("outside the readable workspace", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
