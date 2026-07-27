"""BL-047: native plan-execute envelopes carry provider/model/transport."""

from __future__ import annotations

import unittest

from amof.commands import agent_cmd


class NativeResultTelemetryTests(unittest.TestCase):
    def test_with_request_provenance_fills_provider_model_transport(self) -> None:
        bare = agent_cmd.AgentPlanExecuteEnvelope(
            schema_version=1,
            status="completed",
            session_id="20260727-100152",
            exit_code=0,
            stop_reason="completed",
            final_text="ok",
            plan_path=None,
            checkpoint_path=None,
            event_log_path=None,
            journal_path=None,
            budget_summary={"limit": None, "spent": 0.0, "remaining": None},
        )
        request = agent_cmd.AgentPlanExecuteJsonRequest(
            goal="docs-only note",
            provider="remote-ial",
            model="openai/gpt-5.5",
        )
        filled = agent_cmd._with_request_provenance(bare, request)
        self.assertEqual(filled.exit_code, 0)
        self.assertEqual(filled.requested_provider, "remote-ial")
        self.assertEqual(filled.effective_provider, "remote-ial")
        self.assertEqual(filled.requested_model, "openai/gpt-5.5")
        self.assertEqual(filled.effective_model, "openai/gpt-5.5")
        self.assertEqual(filled.transport, "remote_ial")
        self.assertEqual(filled.runner_id, "amof-built-in-code")
        self.assertEqual(filled.backend, "amof_builtin_code")

    def test_with_request_provenance_does_not_clobber_existing(self) -> None:
        stamped = agent_cmd.AgentPlanExecuteEnvelope(
            schema_version=1,
            status="completed",
            session_id="s1",
            exit_code=0,
            stop_reason="completed",
            final_text="ok",
            plan_path=None,
            checkpoint_path=None,
            event_log_path=None,
            journal_path=None,
            budget_summary={"limit": None, "spent": 0.0, "remaining": None},
            requested_provider="anthropic",
            effective_provider="anthropic",
            requested_model="claude-sonnet-4-5",
            effective_model="claude-sonnet-4-5",
            transport="claude_code",
            runner_id="claude-code-ticket-write",
            backend="claude_code",
        )
        request = agent_cmd.AgentPlanExecuteJsonRequest(
            goal="x",
            provider="remote-ial",
            model="openai/gpt-5.5",
        )
        filled = agent_cmd._with_request_provenance(stamped, request)
        self.assertEqual(filled.effective_provider, "anthropic")
        self.assertEqual(filled.effective_model, "claude-sonnet-4-5")
        self.assertEqual(filled.transport, "claude_code")
        self.assertEqual(filled.runner_id, "claude-code-ticket-write")


if __name__ == "__main__":
    unittest.main()
