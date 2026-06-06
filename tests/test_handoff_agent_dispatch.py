from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import amof.entrypoint as entrypoint
from amof.commands import agent_cmd, handoff


def _execute_args(**overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "command": "handoff",
        "handoff_cmd": "execute-agent",
        "ecosystem": "demo-repo",
        "handoff_id": "handoff-test-001",
        "preview": True,
        "confirm": False,
        "provider": None,
        "model": None,
        "planner_model": None,
        "budget": None,
        "budget_strict": False,
        "subtask_budget": None,
        "approve_capabilities": None,
        "approve_tool_packs": None,
        "approve_writable_roots": None,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _run_execute(args: SimpleNamespace, amof_home: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = handoff.cmd_handoff_execute_agent(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _write_packet(
    amof_home: Path,
    *,
    handoff_id: str = "handoff-test-001",
    target: str = "amof-agent",
    text: str = "Execute this bounded goal.",
) -> Path:
    payload = handoff.PreparedPayload(
        text=text,
        character_count=len(text),
        utf8_byte_count=len(text.encode("utf-8")),
        sha256=handoff.hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    packet = handoff.PreparedHandoffPacket(
        schema_version=1,
        handoff_id=handoff_id,
        source="chatgpt",
        target=target,
        payload_kind="selected_text",
        payload=payload,
        state="prepared",
    )
    with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
        return handoff._write_packet(packet)


def _correlation_envelope(
    *,
    request_id: str = "handoff-test-001",
    status: str = "completed",
    exit_code: int = 0,
    stop_reason: str = "completed",
    session_id: str = "session-1",
    plan_path: str | None = "/tmp/plan.md",
    checkpoint_path: str | None = None,
    event_log_path: str | None = "/tmp/events.jsonl",
    journal_path: str | None = "/tmp/journal.md",
) -> agent_cmd.AgentPlanExecuteCorrelationEnvelope:
    return agent_cmd.AgentPlanExecuteCorrelationEnvelope(
        schema_version=1,
        request_id=request_id,
        result={
            "schema_version": 1,
            "status": status,
            "session_id": session_id,
            "exit_code": exit_code,
            "stop_reason": stop_reason,
            "final_text": f"{status} result",
            "plan_path": plan_path,
            "checkpoint_path": checkpoint_path,
            "event_log_path": event_log_path,
            "journal_path": journal_path,
            "budget_summary": {"limit": 1.0, "spent": 0.0, "remaining": 1.0},
        },
    )


class HandoffAgentDispatchTests(unittest.TestCase):
    def test_valid_prepared_amof_agent_packet_executes_and_maps_goal_and_request_id(
        self,
    ) -> None:
        captured: dict[str, object] = {}

        def _fake_runtime(manifest: dict[str, object], payload: dict[str, object]):
            captured["manifest"] = manifest
            captured["payload"] = payload
            return _correlation_envelope()

        with TemporaryDirectory(prefix="amof-handoff-dispatch-success-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with (
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    side_effect=_fake_runtime,
                ),
            ):
                code, stdout, stderr = _run_execute(
                    _execute_args(confirm=True), amof_home
                )

            receipt = json.loads(stdout)
            payload = captured["payload"]
            assert isinstance(payload, dict)
            self.assertEqual(code, 0)
            self.assertEqual(payload["goal"], "Execute this bounded goal.")
            self.assertEqual(payload["request_id"], "handoff-test-001")
            self.assertEqual(payload["mode"], "plan-execute")
            self.assertTrue(payload["no_follow_up"])
            self.assertIn("[handoff] Execute-agent preview", stderr)
            self.assertEqual(receipt["handoff_id"], "handoff-test-001")
            self.assertEqual(receipt["request_id"], "handoff-test-001")
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["session_id"], "session-1")

    def test_preview_without_confirmation_invokes_nothing(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-preview-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with patch(
                "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope"
            ) as runtime_mock:
                code, stdout, stderr = _run_execute(
                    _execute_args(confirm=False), amof_home
                )

            self.assertEqual(code, 0)
            self.assertEqual(stdout, "")
            runtime_mock.assert_not_called()
            self.assertIn("Execute-agent preview", stderr)
            self.assertIn("no agent execution occurred", stderr)
            self.assertFalse((amof_home / "share" / "handoff" / "results").exists())

    def test_wrong_target_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-target-") as td:
            amof_home = Path(td)
            _write_packet(amof_home, target="zed")
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), amof_home)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("amof-agent", stderr)
        self.assertIn("zed", stderr)

    def test_missing_packet_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-missing-") as td:
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), Path(td))

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("not found", stderr)

    def test_malformed_packet_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-malformed-") as td:
            amof_home = Path(td)
            outbox = amof_home / "share" / "handoff" / "outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            (outbox / "handoff-test-001.json").write_text(
                '{"schema_version":1,"handoff_id":"handoff-test-001","source":"chatgpt","target":"amof-agent","payload_kind":"selected_text","state":"prepared"}\n',
                encoding="utf-8",
            )
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), amof_home)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("payload", stderr)

    def test_oversized_and_nul_payloads_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-oversized-") as td:
            amof_home = Path(td)
            huge = "a" * 40001
            _write_packet(amof_home, text=huge)
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), amof_home)
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("40000", stderr)

        with TemporaryDirectory(prefix="amof-handoff-dispatch-nul-") as td:
            amof_home = Path(td)
            outbox = _write_packet(amof_home)
            packet = json.loads(outbox.read_text(encoding="utf-8"))
            packet["payload"]["text"] = "abc\u0000def"
            outbox.write_text(json.dumps(packet) + "\n", encoding="utf-8")
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), amof_home)
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("NUL", stderr)

    def test_already_executed_packet_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-reexec-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                handoff._write_execution_state(
                    handoff.HandoffExecutionState(
                        schema_version=1,
                        handoff_id="handoff-test-001",
                        status="completed",
                        request_id="handoff-test-001",
                        updated_at="2026-06-07T00:00:00Z",
                        started_at="2026-06-07T00:00:00Z",
                        completed_at="2026-06-07T00:01:00Z",
                    )
                )
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), amof_home)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("re-execution", stderr)

    def test_optional_canonical_execution_fields_map_correctly(self) -> None:
        captured: dict[str, object] = {}

        def _fake_runtime(manifest: dict[str, object], payload: dict[str, object]):
            captured["payload"] = payload
            return _correlation_envelope()

        args = _execute_args(
            confirm=True,
            provider="openrouter",
            model="openai/gpt-4o-mini",
            planner_model="anthropic/claude-sonnet-4.5",
            budget=1.25,
            budget_strict=True,
            subtask_budget=0.5,
            approve_capabilities=["secret"],
            approve_tool_packs=["ops-jenkins"],
            approve_writable_roots=["/tmp/out"],
        )
        with TemporaryDirectory(prefix="amof-handoff-dispatch-options-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with (
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    side_effect=_fake_runtime,
                ),
            ):
                code, stdout, stderr = _run_execute(args, amof_home)

        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(code, 0)
        self.assertEqual(payload["provider"], "openrouter")
        self.assertEqual(payload["model"], "openai/gpt-4o-mini")
        self.assertEqual(payload["planner_model"], "anthropic/claude-sonnet-4.5")
        self.assertEqual(payload["budget"], 1.25)
        self.assertTrue(payload["budget_strict"])
        self.assertEqual(payload["subtask_budget"], 0.5)
        self.assertEqual(payload["approve_capabilities"], ["secret"])
        self.assertEqual(payload["approve_tool_packs"], ["ops-jenkins"])
        self.assertEqual(payload["approve_writable_roots"], ["/tmp/out"])
        self.assertIn("selected_execution_configuration", stderr)
        self.assertIn("openrouter", stderr)
        self.assertNotIn("Execute this bounded goal.", stdout)
        self.assertEqual(json.loads(stdout)["status"], "completed")

    def test_budget_strict_and_readiness_blocks_and_failed_subtasks_are_truthful(
        self,
    ) -> None:
        scenarios = [
            ("blocked", 1, "budget_preflight_blocked"),
            ("blocked", 1, "missing_required_secret_access"),
            ("failed", 1, "max_iterations"),
            ("completed", 0, "completed"),
        ]
        for status, exit_code, stop_reason in scenarios:
            with self.subTest(status=status, stop_reason=stop_reason):
                with TemporaryDirectory(
                    prefix=f"amof-handoff-dispatch-{status}-"
                ) as td:
                    amof_home = Path(td)
                    _write_packet(amof_home)
                    with (
                        patch(
                            "amof.commands.handoff._load_execution_manifest",
                            return_value={"ecosystem": "demo-repo", "repos": []},
                        ),
                        patch(
                            "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                            return_value=_correlation_envelope(
                                status=status,
                                exit_code=exit_code,
                                stop_reason=stop_reason,
                            ),
                        ),
                    ):
                        code, stdout, _stderr = _run_execute(
                            _execute_args(confirm=True), amof_home
                        )

                    receipt = json.loads(stdout)
                    self.assertEqual(code, exit_code)
                    self.assertEqual(receipt["status"], status)
                    self.assertEqual(receipt["exit_code"], exit_code)
                    self.assertEqual(receipt["stop_reason"], stop_reason)

    def test_canonical_session_and_evidence_references_are_preserved(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-evidence-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with (
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    return_value=_correlation_envelope(
                        session_id="session-42",
                        plan_path="/tmp/plan.md",
                        checkpoint_path="/tmp/checkpoint.json",
                        event_log_path="/tmp/events.jsonl",
                        journal_path="/tmp/journal.md",
                    ),
                ),
            ):
                code, stdout, _stderr = _run_execute(
                    _execute_args(confirm=True), amof_home
                )

            receipt = json.loads(stdout)
            self.assertEqual(code, 0)
            self.assertEqual(receipt["session_id"], "session-42")
            self.assertEqual(receipt["evidence"]["plan_path"], "/tmp/plan.md")
            self.assertEqual(
                receipt["evidence"]["checkpoint_path"], "/tmp/checkpoint.json"
            )
            self.assertEqual(receipt["evidence"]["event_log_path"], "/tmp/events.jsonl")
            self.assertEqual(receipt["evidence"]["journal_path"], "/tmp/journal.md")

    def test_result_file_permissions_and_stdout_receipt_shape(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-result-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with (
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    return_value=_correlation_envelope(),
                ),
            ):
                code, stdout, stderr = _run_execute(
                    _execute_args(confirm=True), amof_home
                )

            receipt = json.loads(stdout)
            result_path = Path(receipt["result_path"])
            self.assertEqual(code, 0)
            self.assertEqual(
                stdout.strip(),
                json.dumps(
                    receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ),
            )
            self.assertTrue(result_path.is_file())
            self.assertEqual(stat.S_IMODE(os.stat(result_path.parent).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(result_path).st_mode), 0o600)
            self.assertIn("Execute-agent preview", stderr)
            self.assertNotIn("Execute this bounded goal.", stdout)

    def test_no_network_subprocess_or_external_app_integration_is_introduced(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-no-external-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with (
                patch(
                    "subprocess.run", side_effect=AssertionError("subprocess forbidden")
                ),
                patch(
                    "socket.create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    return_value=_correlation_envelope(),
                ),
            ):
                code, stdout, _stderr = _run_execute(
                    _execute_args(confirm=True), amof_home
                )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "completed")


class HandoffDispatchEntrypointTests(unittest.TestCase):
    def test_handoff_dispatch_uses_no_ecosystem_entrypoint_branch(self) -> None:
        args = _execute_args(confirm=True)
        with (
            patch("amof.entrypoint.parse_args", return_value=args),
            patch("amof.entrypoint.cmd_handoff", return_value=4) as handoff_mock,
        ):
            with self.assertRaises(SystemExit) as exc:
                entrypoint.main()

        self.assertEqual(exc.exception.code, 4)
        handoff_mock.assert_called_once_with(args)

    def test_existing_agent_help_surface_remains_available(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "amof.py"), "agent", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(SCRIPTS_ROOT)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--request-json", result.stdout)
        self.assertIn("--json", result.stdout)


if __name__ == "__main__":
    unittest.main()
