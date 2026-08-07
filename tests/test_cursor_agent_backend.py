from __future__ import annotations

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
from amof.execution_backends import cursor_agent, hermes_opensandbox


def _selection(writable_roots: list[str] | None = None) -> hermes_opensandbox.HermesBackendSelection:
    return hermes_opensandbox.HermesBackendSelection(
        runner_id="cursor-agent-ticket-write",
        capabilities=["read"] if not writable_roots else ["read", "bounded_write"],
        writable_roots=list(writable_roots or []),
        timeout_seconds=30,
        readable_root=None,
    )


class CursorAgentContractTests(unittest.TestCase):
    def test_backend_constants(self) -> None:
        self.assertEqual(cursor_agent.BACKEND_TYPE, "cursor_agent")
        self.assertEqual(cursor_agent.DEFAULT_SETTING_SOURCES, ())
        self.assertEqual(cursor_agent.TRANSPORT, "cursor_agent")
        self.assertEqual(cursor_agent.PROVIDER, "cursor")

    def test_result_payload_maps_substrate_refs_not_amof_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = cursor_agent._result_payload(
                run_id="cursor-amof-1",
                status="completed",
                exit_code=0,
                stop_reason="completed",
                final_text="done",
                studio_session_id=None,
                event_log_path=Path(tmp) / "events.jsonl",
                runtime_log_path=Path(tmp) / "runtime.log",
                changed_paths=[],
                selection=_selection(),
                health={"process_identity": {"setting_sources": []}},
                dispatch_probe={"status": "ready"},
                substrate_agent_id="agent-abc",
                substrate_run_id="run-xyz",
                sdk_envelope={
                    "status": "finished",
                    "substrate_agent_id": "agent-abc",
                    "substrate_run_id": "run-xyz",
                },
            )
        self.assertEqual(payload["result_kind"], "agent_run_result")
        self.assertEqual(payload["contract_version"], "agent-run-v1")
        self.assertEqual(payload["session_id"], "cursor-amof-1")
        self.assertEqual(payload["backend"], "cursor_agent")
        self.assertEqual(payload["evidence_refs"]["amof_run_id"], "cursor-amof-1")
        self.assertEqual(payload["evidence_refs"]["substrate_agent_id"], "agent-abc")
        self.assertNotEqual(payload["session_id"], "agent-abc")


class CursorAgentRunBlockedTests(unittest.TestCase):
    def test_missing_api_key_blocks_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            with (
                patch.object(cursor_agent, "_api_key", return_value=""),
                patch.object(
                    cursor_agent,
                    "runtime_health",
                    return_value={
                        "feature_flag_enabled": True,
                        "process_identity": {"dispatch_probe": {"status": "ready"}},
                    },
                ),
                patch.object(cursor_agent, "_run_dir", return_value=Path(tmp) / "run"),
            ):
                (Path(tmp) / "run").mkdir()
                result = cursor_agent.run(
                    manifest={"repos": [{"path": str(workspace)}]},
                    goal="inspect",
                    request_id="req-1",
                    studio_session_id=None,
                    selection=_selection(),
                )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stop_reason"], "inference_transport_unavailable")
        self.assertEqual(result["backend"], "cursor_agent")

    def test_mocked_invoke_normalizes_agent_run_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            invoke = cursor_agent.CursorAgentInvokeResult(
                status="finished",
                result_text="findings from mocked cursor",
                substrate_agent_id="agent-mock-1",
                substrate_run_id="run-mock-9",
            )
            with (
                patch.object(cursor_agent, "_api_key", return_value="key"),
                patch.object(
                    cursor_agent,
                    "runtime_health",
                    return_value={
                        "feature_flag_enabled": True,
                        "process_identity": {
                            "dispatch_probe": {"status": "ready"},
                            "setting_sources": [],
                        },
                    },
                ),
                patch.object(cursor_agent, "_run_dir", return_value=Path(tmp) / "run"),
                patch.object(cursor_agent, "invoke_cursor_local", return_value=invoke),
                patch.object(cursor_agent._shared, "_changed_paths", return_value=[]),
                patch.object(cursor_agent._shared, "_changed_paths_delta", return_value=[]),
            ):
                (Path(tmp) / "run").mkdir()
                result = cursor_agent.run(
                    manifest={"repos": [{"path": str(workspace)}]},
                    goal="inspect workspace",
                    request_id="req-ok",
                    studio_session_id=None,
                    selection=_selection(),
                )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["evidence_refs"]["substrate_agent_id"], "agent-mock-1")
        self.assertNotEqual(result["session_id"], "agent-mock-1")

    def test_cancelled_maps_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            invoke = cursor_agent.CursorAgentInvokeResult(status="cancelled", result_text="")
            with (
                patch.object(cursor_agent, "_api_key", return_value="key"),
                patch.object(
                    cursor_agent,
                    "runtime_health",
                    return_value={
                        "feature_flag_enabled": True,
                        "process_identity": {"dispatch_probe": {"status": "ready"}},
                    },
                ),
                patch.object(cursor_agent, "_run_dir", return_value=Path(tmp) / "run"),
                patch.object(cursor_agent, "invoke_cursor_local", return_value=invoke),
                patch.object(cursor_agent._shared, "_changed_paths", return_value=[]),
                patch.object(cursor_agent._shared, "_changed_paths_delta", return_value=[]),
            ):
                (Path(tmp) / "run").mkdir()
                result = cursor_agent.run(
                    manifest={"repos": [{"path": str(workspace)}]},
                    goal="inspect",
                    request_id="req-cancel",
                    studio_session_id=None,
                    selection=_selection(),
                )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "cancelled")


class CursorAgentRunnerRegistryTests(unittest.TestCase):
    def test_template_kind_produces_valid_backend_record(self) -> None:
        payload = runner_cmd._template_payload("cursor-agent")
        self.assertEqual(payload["runner_id"], "cursor-agent-ticket-write")
        self.assertEqual(payload["backend"], cursor_agent.BACKEND_TYPE)
        runner_cmd._validate_backend_payload(
            payload,
            mutation_modes=list(payload["allowed_mutation_modes"]),
            capabilities=list(payload["capabilities"]),
        )

    def test_backend_is_supported(self) -> None:
        self.assertIn(cursor_agent.BACKEND_TYPE, runner_cmd.SUPPORTED_BACKENDS)
        self.assertIn("cursor-agent", runner_cmd.SUPPORTED_TEMPLATE_KINDS)

    def test_handoff_dispatch_map_includes_cursor_agent(self) -> None:
        from amof.commands import handoff as handoff_cmd

        source = Path(handoff_cmd.__file__).read_text(encoding="utf-8")
        self.assertIn("cursor_agent.BACKEND_TYPE: cursor_agent", source)

    def test_lazy_sdk_import_only_in_invoke(self) -> None:
        source = Path(cursor_agent.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(
            source,
            r"(?m)^(from cursor_sdk import|import cursor_sdk)\b",
        )
        self.assertIn("from cursor_sdk import", source)


if __name__ == "__main__":
    unittest.main()
