"""BL-037: bare /workspace aliases resolve to materialized workspace roots."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.orchestrator.tools.base import resolve_tool_path
from amof.orchestrator.tools.glob_tool import GlobTool
from amof.orchestrator.tools.ls import LSTool
from amof.orchestrator.tools.read import ReadTool


class WorkspacePathAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name) / "ws-20260724"
        private = self.root / "01-amof-private" / "docs" / "product" / "backlogs"
        private.mkdir(parents=True)
        (private / "amof.md").write_text("# backlog\n", encoding="utf-8")
        (self.root / "README.md").write_text("ok\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_resolve_tool_path_rewrites_bare_workspace_alias(self) -> None:
        self.assertEqual(
            resolve_tool_path("/workspace", workspace_root=self.root),
            self.root.resolve(),
        )
        self.assertEqual(
            resolve_tool_path("/workspace/", workspace_root=self.root),
            self.root.resolve(),
        )
        self.assertEqual(
            resolve_tool_path("/workspace/README.md", workspace_root=self.root),
            (self.root / "README.md").resolve(),
        )
        other = Path(self._tmpdir.name) / "other"
        other.mkdir()
        self.assertEqual(
            resolve_tool_path(str(other), workspace_root=self.root),
            other,
        )

    def test_ls_tool_does_not_fail_on_bare_workspace_alias(self) -> None:
        result = LSTool(workspace_root=self.root).execute(target_directory="/workspace")
        self.assertTrue(result.success, result.error)
        self.assertIn("01-amof-private/", result.output)
        self.assertNotIn("Directory not found: /workspace", result.error or "")

    def test_glob_and_read_accept_workspace_alias(self) -> None:
        glob_result = GlobTool(workspace_root=self.root).execute(
            glob_pattern="amof.md",
            target_directory="/workspace",
        )
        self.assertTrue(glob_result.success, glob_result.error)
        self.assertIn("amof.md", glob_result.output)

        read_result = ReadTool(workspace_root=self.root).execute(
            path="/workspace/01-amof-private/docs/product/backlogs/amof.md"
        )
        self.assertTrue(read_result.success, read_result.error)
        self.assertIn("# backlog", read_result.output)


if __name__ == "__main__":
    unittest.main()
