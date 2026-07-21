from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import runner as runner_cmd
from amof.execution_backends import claude_code, hermes_opensandbox


def _selection(writable_roots: list[str] | None = None) -> hermes_opensandbox.HermesBackendSelection:
    return hermes_opensandbox.HermesBackendSelection(
        runner_id="claude-code-ticket-write",
        capabilities=["read"] if not writable_roots else ["read", "bounded_write"],
        writable_roots=list(writable_roots or []),
        timeout_seconds=30,
        readable_root=None,
    )


class ClaudeCodeDispatchCommandTests(unittest.TestCase):
    def test_read_only_command_excludes_edit_tools_and_permission_mode(self) -> None:
        command = claude_code.claude_dispatch_command(
            model="claude-sonnet-4-5", prompt="probe", writable=False
        )
        self.assertIn("--print", command)
        self.assertIn("--output-format", command)
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        tools = command[command.index("--allowedTools") + 1]
        self.assertNotIn("Edit", tools.split(","))
        self.assertNotIn("Write", tools.split(","))
        self.assertNotIn("--permission-mode", command)
        self.assertEqual(command[-1], "probe")

    def test_bounded_write_command_grants_edit_tools_with_accept_edits(self) -> None:
        command = claude_code.claude_dispatch_command(
            model="claude-sonnet-4-5", prompt="probe", writable=True
        )
        tools = command[command.index("--allowedTools") + 1].split(",")
        self.assertIn("Edit", tools)
        self.assertIn("Write", tools)
        self.assertIn("Bash", tools)
        self.assertEqual(
            command[command.index("--permission-mode") + 1], "acceptEdits"
        )


class ClaudeCodeEnvelopeTests(unittest.TestCase):
    def test_parses_json_envelope(self) -> None:
        envelope, text = claude_code._parse_cli_envelope(
            json.dumps({"type": "result", "result": "findings body", "is_error": False})
        )
        self.assertIsInstance(envelope, dict)
        self.assertEqual(text, "findings body")

    def test_falls_back_to_raw_stdout(self) -> None:
        envelope, text = claude_code._parse_cli_envelope("plain text output")
        self.assertIsNone(envelope)
        self.assertEqual(text, "plain text output")


class ClaudeCodeRunBlockedTests(unittest.TestCase):
    def test_missing_api_key_blocks_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            with (
                patch.object(claude_code, "_api_key", return_value=""),
                patch.object(
                    claude_code,
                    "runtime_health",
                    return_value={
                        "process_identity": {"dispatch_probe": {"status": "ready"}},
                    },
                ),
                patch.object(claude_code, "_run_dir", return_value=Path(tmp) / "run"),
            ):
                (Path(tmp) / "run").mkdir()
                result = claude_code.run(
                    manifest={"repos": [{"path": str(workspace)}]},
                    goal="inspect",
                    request_id="req-1",
                    studio_session_id=None,
                    selection=_selection(),
                )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stop_reason"], "inference_transport_unavailable")
        self.assertEqual(result["backend"], "claude_code")
        self.assertEqual(result["transport"], "anthropic_api")

    def test_cli_unavailable_blocks_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            with (
                patch.object(claude_code, "_api_key", return_value="key"),
                patch.object(
                    claude_code,
                    "runtime_health",
                    return_value={
                        "process_identity": {
                            "dispatch_probe": {"status": "unavailable"}
                        },
                    },
                ),
                patch.object(claude_code, "_run_dir", return_value=Path(tmp) / "run"),
            ):
                (Path(tmp) / "run").mkdir()
                result = claude_code.run(
                    manifest={"repos": [{"path": str(workspace)}]},
                    goal="inspect",
                    request_id="req-2",
                    studio_session_id=None,
                    selection=_selection(),
                )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stop_reason"], "claude_code_dispatch_unavailable")


class ClaudeCodeResultContractTests(unittest.TestCase):
    def test_result_payload_matches_agent_run_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = claude_code._result_payload(
                run_id="claude-test-1",
                status="completed",
                exit_code=0,
                stop_reason="completed",
                final_text="done",
                studio_session_id=None,
                event_log_path=Path(tmp) / "events.jsonl",
                runtime_log_path=Path(tmp) / "runtime.log",
                changed_paths=[],
                selection=_selection(),
                health={"process_identity": {}},
                dispatch_probe={"status": "ready"},
                task_findings="findings",
                cli_envelope={
                    "session_id": "s-1",
                    "num_turns": 3,
                    "duration_ms": 1200,
                    "total_cost_usd": 0.42,
                    "is_error": False,
                },
            )
        for field in (
            "result_kind",
            "contract_version",
            "schema_version",
            "status",
            "session_id",
            "exit_code",
            "stop_reason",
            "final_text",
            "plan_path",
            "checkpoint_path",
            "event_log_path",
            "journal_path",
            "budget_summary",
        ):
            self.assertIn(field, payload)
        self.assertEqual(payload["result_kind"], "agent_run_result")
        self.assertEqual(payload["contract_version"], "agent-run-v1")
        self.assertEqual(payload["backend"], "claude_code")
        self.assertEqual(payload["requested_provider"], "anthropic")
        self.assertEqual(payload["transport"], "anthropic_api")
        self.assertEqual(payload["budget_summary"]["spent"], 0.42)
        self.assertEqual(
            payload["evidence_refs"]["backend_contract_version"],
            "claude-code-cli-v1",
        )
        self.assertEqual(
            payload["evidence_refs"]["cli_envelope_summary"]["session_id"], "s-1"
        )


class ClaudeCodeRunnerRegistryTests(unittest.TestCase):
    def test_template_kind_produces_valid_backend_record(self) -> None:
        payload = runner_cmd._template_payload("claude-code")
        self.assertEqual(payload["runner_id"], "claude-code-ticket-write")
        self.assertEqual(payload["backend"], claude_code.BACKEND_TYPE)
        self.assertEqual(
            payload["backend_contract_version"], claude_code.BACKEND_CONTRACT_VERSION
        )
        # Validation must accept the template as-is (backend + capability rules).
        runner_cmd._validate_backend_payload(
            payload,
            mutation_modes=list(payload["allowed_mutation_modes"]),
            capabilities=list(payload["capabilities"]),
        )

    def test_backend_is_supported(self) -> None:
        self.assertIn(claude_code.BACKEND_TYPE, runner_cmd.SUPPORTED_BACKENDS)
        self.assertIn("claude-code", runner_cmd.SUPPORTED_TEMPLATE_KINDS)

    def test_dangerous_capabilities_rejected(self) -> None:
        payload = runner_cmd._template_payload("claude-code")
        with self.assertRaises(runner_cmd.RunnerCliError):
            runner_cmd._validate_backend_payload(
                payload,
                mutation_modes=["read_only"],
                capabilities=["deploy"],
            )


class ClaudeCodeHandoffRoutingTests(unittest.TestCase):
    def test_dispatch_backend_map_includes_claude_code(self) -> None:
        from amof.commands import handoff as handoff_cmd

        source = Path(handoff_cmd.__file__).read_text(encoding="utf-8")
        self.assertIn("claude_code.BACKEND_TYPE: claude_code", source)
        self.assertIn("_dispatch_backend_handoff", source)


if __name__ == "__main__":
    unittest.main()
