"""BL-037/BL-065: bare /workspace aliases resolve to materialized workspace roots."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.orchestrator.tools.base import (
    Guardrails,
    ToolCall,
    ToolRegistry,
    resolve_tool_path,
)
from amof.orchestrator.tools.glob_tool import GlobTool
from amof.orchestrator.tools.ls import LSTool
from amof.orchestrator.tools.read import ReadTool
from amof.orchestrator.tools.write import WriteTool


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

    def test_write_tools_accept_workspace_alias_inside_writable_roots(self) -> None:
        checkout = self.root / "01-amof-private"
        backlog_dir = checkout / "docs" / "product" / "backlogs"
        registry = ToolRegistry(
            guardrails=Guardrails(writable_roots=[backlog_dir], unattended=True),
            workspace_root=checkout,
        )
        registry.register(WriteTool(workspace_root=checkout))

        # Pre-BL-065 this failed writable-root checks on the literal /workspace path.
        written = registry.execute(
            ToolCall(
                id="1",
                name="Write",
                arguments={
                    "path": "/workspace/docs/product/backlogs/dogfood-note.md",
                    "contents": "ok\n",
                },
            )
        )
        self.assertTrue(written.success, written.error)
        self.assertEqual(
            (backlog_dir / "dogfood-note.md").read_text(encoding="utf-8"),
            "ok\n",
        )

    def test_write_alias_outside_writable_roots_still_blocked(self) -> None:
        target = self.root / "01-amof-private" / "docs" / "product" / "backlogs" / "amof.md"
        registry = ToolRegistry(
            guardrails=Guardrails(writable_roots=[target], unattended=True),
            workspace_root=self.root,
        )
        registry.register(WriteTool(workspace_root=self.root))
        blocked = registry.execute(
            ToolCall(
                id="2",
                name="Write",
                arguments={
                    "path": "/workspace/README.md",
                    "contents": "nope\n",
                },
            )
        )
        self.assertFalse(blocked.success)
        self.assertIn("outside writable roots", blocked.error or "")


if __name__ == "__main__":
    unittest.main()
