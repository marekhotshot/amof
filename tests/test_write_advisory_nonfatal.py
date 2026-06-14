"""Advisory guardrail redirects must not count as fatal tool failures.

Regression coverage for AMOF-EXECUTOR-WRITE-ADVISORY-NONFATAL-001.

The Write overwrite guard ("use StrReplace instead") is an advisory redirect,
not a genuine execution failure. When an executor receives it, adapts, and still
reaches `completed`, the redirect must not fail the subtask. These tests pin the
four layers that implement that behaviour:

1. base.ToolRegistry.execute marks the advisory block via metadata.
2. SessionTelemetry.record_tool_call buckets it as advisory, not a failure.
3. analyze_tool_call_events excludes it from the failure analysis.
4. Genuine (non-advisory) write failures still count as failures.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.orchestrator.telemetry import SessionTelemetry
from amof.orchestrator.tool_failure_semantics import analyze_tool_call_events
from amof.orchestrator.tools.base import (
    ADVISORY_GUARDRAIL_MESSAGES,
    WRITE_OVERWRITE_ADVISORY,
    ToolCall,
    ToolRegistry,
)
from amof.orchestrator.tools.write import WriteTool


class WriteOverwriteAdvisoryGuardTests(unittest.TestCase):
    def test_overwrite_guard_returns_advisory_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "pkg" / "__init__.py"
            existing.parent.mkdir(parents=True, exist_ok=True)
            existing.write_text("", encoding="utf-8")

            registry = ToolRegistry()
            registry.register(WriteTool())
            result = registry.execute(
                ToolCall(
                    id="t1",
                    name="Write",
                    arguments={"path": str(existing), "contents": "# pkg"},
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error, WRITE_OVERWRITE_ADVISORY)
        self.assertTrue(result.metadata.get("advisory"))

    def test_overwrite_message_is_registered_advisory(self):
        self.assertIn(WRITE_OVERWRITE_ADVISORY, ADVISORY_GUARDRAIL_MESSAGES)


class TelemetryAdvisoryAccountingTests(unittest.TestCase):
    def test_advisory_block_not_counted_as_failure(self):
        tel = SessionTelemetry()
        tel.record_tool_call("Write", success=False, duration_ms=5, metadata={"advisory": True})

        tm = tel.tool_metrics["Write"]
        self.assertEqual(tm.calls, 1)
        self.assertEqual(tm.failures, 0)
        self.assertEqual(tm.advisory_blocks, 1)

    def test_genuine_failure_still_counted(self):
        tel = SessionTelemetry()
        tel.record_tool_call("Write", success=False, duration_ms=5, metadata={})

        tm = tel.tool_metrics["Write"]
        self.assertEqual(tm.failures, 1)
        self.assertEqual(tm.advisory_blocks, 0)

    def test_failed_tool_calls_rollup_excludes_advisory(self):
        tel = SessionTelemetry()
        tel.record_tool_call("Write", success=True, duration_ms=3)
        tel.record_tool_call("Write", success=False, duration_ms=3, metadata={"advisory": True})

        failed_tool_calls = sum(m.failures for m in tel.tool_metrics.values())
        self.assertEqual(failed_tool_calls, 0)


class AnalyzeToolCallEventsAdvisoryTests(unittest.TestCase):
    def test_advisory_event_excluded_from_failures(self):
        events = [
            {
                "tool": "Write",
                "args": {"path": "pkg/__init__.py", "contents": "# pkg"},
                "success": False,
                "error": WRITE_OVERWRITE_ADVISORY,
                "metadata": {"advisory": True},
            }
        ]
        analysis = analyze_tool_call_events(events, task_text="implement a contract module")
        self.assertEqual(analysis["failures"], [])
        self.assertEqual(analysis["fatal_failures"], [])

    def test_genuine_write_failure_still_analyzed(self):
        events = [
            {
                "tool": "Write",
                "args": {"path": "pkg/x.py", "contents": "x = 1"},
                "success": False,
                "error": "Permission denied: pkg/x.py",
            }
        ]
        analysis = analyze_tool_call_events(events, task_text="implement a contract module")
        self.assertEqual(len(analysis["failures"]), 1)


if __name__ == "__main__":
    unittest.main()
