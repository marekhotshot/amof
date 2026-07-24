"""BL-065: planner structured output accepts per-subtask verification hints."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.orchestrator.agent_models import PlannerOutputModel, SubtaskResult


class PlannerSubtaskVerificationTests(unittest.TestCase):
    def test_plan_subtask_accepts_verification_field(self) -> None:
        plan = PlannerOutputModel.model_validate(
            {
                "analysis": "docs-only backlog note",
                "subtasks": [
                    {
                        "id": "1",
                        "title": "Append note",
                        "description": "Append one line to the backlog.",
                        "verification": (
                            "Check that the line has been added to "
                            "docs/product/backlogs/amof.md."
                        ),
                    }
                ],
                "execution_order": ["1"],
                "risks": [],
                "verification": "git diff --check",
                "questions": [],
            }
        )
        self.assertEqual(plan.subtasks[0].verification.startswith("Check that"), True)

    def test_subtask_result_is_not_polluted_with_plan_fields(self) -> None:
        result = SubtaskResult(success=True, output="done")
        self.assertTrue(result.success)
        self.assertEqual(result.output, "done")
        self.assertIsNone(result.error_logs)
        self.assertEqual(set(SubtaskResult.model_fields), {"success", "output", "error_logs"})


if __name__ == "__main__":
    unittest.main()
