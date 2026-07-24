"""BL-070: ToolProposal normalizes absolute/workspace-alias allowed_paths."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.orchestrator.tools.tool_proposal import (  # noqa: E402
    ToolProposalTool,
    _normalize_allowed_paths,
)


class ToolProposalAllowedPathsNormalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name) / "ws-bl070"
        backlog = self.root / "docs" / "product" / "backlogs"
        backlog.mkdir(parents=True)
        self.target = backlog / "amof.md"
        self.target.write_text("# backlog\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_normalize_workspace_alias_and_absolute_approved_root(self) -> None:
        alias = _normalize_allowed_paths(
            ["/workspace/docs/product/backlogs/amof.md"],
            workspace_root=self.root,
        )
        absolute = _normalize_allowed_paths(
            [str(self.target.resolve())],
            workspace_root=self.root,
        )
        self.assertEqual(alias, ["docs/product/backlogs/amof.md"])
        self.assertEqual(absolute, ["docs/product/backlogs/amof.md"])

    def test_docs_only_absolute_approved_root_proposal_executes(self) -> None:
        tool = ToolProposalTool(workspace_root=self.root)
        result = tool.execute(
            purpose="Scan the bounded docs-only backlog note.",
            mutation_intent=False,
            allowed_paths=["/workspace/docs/product/backlogs/amof.md"],
            allow_network=False,
            timeout_seconds=10,
            inputs=["/workspace/docs/product/backlogs/amof.md"],
            outputs=["heading presence"],
            rollback="None needed; read-only scan.",
            script=(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "p = Path('/workspace/docs/product/backlogs/amof.md')\n"
                "print(p.read_text())\n"
            ),
        )
        self.assertTrue(result.success, result.error or result.output)
        self.assertEqual(
            result.metadata["allowed_paths"],
            ["docs/product/backlogs/amof.md"],
        )
        self.assertIn("# backlog", result.output)

    def test_workspace_escaping_and_broad_paths_still_hard_fail(self) -> None:
        escape = _normalize_allowed_paths(
            [str(Path(self._tmpdir.name) / "outside.txt")],
            workspace_root=self.root,
        )
        self.assertIsInstance(escape, str)
        self.assertIn("must stay within the target workspace", escape)

        for broad in (".", "/", "*", "**", "~"):
            err = _normalize_allowed_paths([broad], workspace_root=self.root)
            self.assertIsInstance(err, str)
            self.assertIn("broad or absolute allowed_paths are not allowed", err)

        tool = ToolProposalTool(workspace_root=self.root)
        blocked = tool.execute(
            purpose="escape",
            mutation_intent=False,
            allowed_paths=["/etc/passwd"],
            allow_network=False,
            timeout_seconds=10,
            inputs=["/etc/passwd"],
            outputs=["secret"],
            rollback="none",
            script="#!/usr/bin/env python3\nprint('nope')\n",
        )
        self.assertFalse(blocked.success)
        self.assertIn("must stay within the target workspace", blocked.error or "")


if __name__ == "__main__":
    unittest.main()
