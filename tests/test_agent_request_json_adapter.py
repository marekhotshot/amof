from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import amof.entrypoint as entrypoint
from amof.commands import agent_cmd

MINIMAL_EXAMPLE_PATH = (
    ROOT
    / "contracts"
    / "examples"
    / "external-agent-plan-execute-request.minimal.example.json"
)
FULL_EXAMPLE_PATH = (
    ROOT / "contracts" / "examples" / "external-agent-plan-execute-request.example.json"
)
UNSAFE_EXAMPLE_PATH = (
    ROOT
    / "contracts"
    / "examples"
    / "external-agent-plan-execute-request.invalid-unsafe.example.json"
)


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest() -> dict[str, object]:
    return {"ecosystem": "demo-repo", "repos": []}


def _request_json_args(**overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "command": "agent",
        "ecosystem": "demo-repo",
        "request_json": "-",
        "json": True,
        "plan_execute": True,
        "goal": None,
        "provider": None,
        "plan": None,
        "model": None,
        "verbose": None,
        "max_cost": None,
        "budget": None,
        "cost_limit": None,
        "subtask_budget": None,
        "add_budget": None,
        "require_budget_approval": None,
        "budget_strict": None,
        "budget_status": None,
        "model_ladder": None,
        "fast_model": None,
        "strong_model": None,
        "planner_model": None,
        "index": None,
        "resume": None,
        "follow_up": None,
        "follow_up_file": None,
        "plan_file": None,
        "no_follow_up": None,
        "approve_plan": None,
        "continue_budget": None,
        "approve_capabilities": None,
        "approve_tool_packs": None,
        "approve_writable_roots": None,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _envelope(
    *, status: str = "completed", exit_code: int = 0, stop_reason: str = "completed"
) -> agent_cmd.AgentPlanExecuteEnvelope:
    return agent_cmd.AgentPlanExecuteEnvelope(
        schema_version=1,
        status=status,
        session_id="session-1",
        exit_code=exit_code,
        stop_reason=stop_reason,
        final_text=f"{status} result",
        plan_path=None,
        checkpoint_path=None,
        event_log_path=None,
        journal_path=None,
        budget_summary={"limit": 1.0, "spent": 0.0, "remaining": 1.0},
    )


def _run_request_json_cmd(
    args: SimpleNamespace, stdin_text: str
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", io.StringIO(stdin_text)):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = agent_cmd.cmd_agent_request_json(_manifest(), args)
    return code, stdout.getvalue(), stderr.getvalue()


class ExternalAgentRequestAdapterTests(unittest.TestCase):
    def test_minimal_valid_packet_parses(self) -> None:
        request = agent_cmd.parse_external_agent_plan_execute_request(
            _load(MINIMAL_EXAMPLE_PATH)
        )

        self.assertEqual(request.schema_version, 1)
        self.assertEqual(request.request_id, "external-request-minimal-001")
        self.assertEqual(request.mode, "plan-execute")
        self.assertEqual(request.request.goal, "One bounded execution goal.")
        self.assertTrue(request.request.no_follow_up)

    def test_full_valid_packet_maps_only_canonical_runtime_fields(self) -> None:
        request = agent_cmd.parse_external_agent_plan_execute_request(
            _load(FULL_EXAMPLE_PATH)
        )

        self.assertEqual(request.request_id, "external-request-001")
        self.assertEqual(
            request.request.goal,
            "Inspect this repo and summarize the release blockers.",
        )
        self.assertEqual(request.request.provider, "openrouter")
        self.assertEqual(request.request.model, "openai/gpt-4o-mini")
        self.assertEqual(request.request.planner_model, "anthropic/claude-sonnet-4.5")
        self.assertEqual(request.request.budget, 1.0)
        self.assertTrue(request.request.budget_strict)
        self.assertEqual(request.request.subtask_budget, 0.25)
        self.assertIsNone(request.request.resume)
        self.assertIsNone(request.request.follow_up)
        self.assertEqual(request.request.approve_capabilities, [])
        self.assertEqual(request.request.approve_tool_packs, [])
        self.assertEqual(request.request.approve_writable_roots, [])
        self.assertTrue(request.request.no_follow_up)

    def test_deterministic_field_mapping_and_approvals_reach_existing_runtime(
        self,
    ) -> None:
        captured: dict[str, object] = {}

        def _fake_run(
            manifest: dict[str, object],
            request: agent_cmd.AgentPlanExecuteJsonRequest,
            *,
            studio_session_id: str | None = None,
        ) -> agent_cmd.AgentPlanExecuteEnvelope:
            captured["manifest"] = manifest
            captured["request"] = request
            captured["studio_session_id"] = studio_session_id
            return _envelope()

        with patch(
            "amof.commands.agent_cmd._run_agent_plan_execute_request",
            side_effect=_fake_run,
        ):
            response = agent_cmd.run_external_agent_plan_execute_envelope(
                _manifest(), _load(FULL_EXAMPLE_PATH)
            )

        request = cast(agent_cmd.AgentPlanExecuteJsonRequest, captured["request"])
        self.assertEqual(captured["manifest"], _manifest())
        self.assertEqual(
            request.goal, "Inspect this repo and summarize the release blockers."
        )
        self.assertEqual(request.provider, "openrouter")
        self.assertEqual(request.model, "openai/gpt-4o-mini")
        self.assertEqual(request.planner_model, "anthropic/claude-sonnet-4.5")
        self.assertEqual(request.budget, 1.0)
        self.assertTrue(request.budget_strict)
        self.assertEqual(request.subtask_budget, 0.25)
        self.assertEqual(request.approve_capabilities, [])
        self.assertEqual(request.approve_tool_packs, [])
        self.assertEqual(request.approve_writable_roots, [])
        self.assertTrue(request.no_follow_up)
        self.assertIsNone(cast(object, captured["studio_session_id"]))
        self.assertEqual(response.request_id, "external-request-001")
        self.assertEqual(response.result["status"], "completed")

    def test_stdin_invocation_emits_one_parseable_json_document_with_correlation(
        self,
    ) -> None:
        with patch(
            "amof.commands.agent_cmd._run_agent_plan_execute_request",
            return_value=_envelope(),
        ):
            code, stdout, stderr = _run_request_json_cmd(
                _request_json_args(),
                MINIMAL_EXAMPLE_PATH.read_text(encoding="utf-8"),
            )

        payload = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["request_id"], "external-request-minimal-001")
        self.assertEqual(payload["result"]["schema_version"], 1)
        self.assertEqual(payload["result"]["status"], "completed")
        self.assertEqual(stdout.strip(), json.dumps(payload))

    def test_provider_configuration_failure_returns_wrapped_envelope_without_fallback(
        self,
    ) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["request_id"] = "external-request-provider-config-001"
        packet.pop("provider", None)

        with tempfile.TemporaryDirectory(
            prefix="amof-request-json-provider-config-"
        ) as td:
            env = {"AMOF_HOME": str(Path(td) / "amof-home")}
            repo = Path(td) / "demo-repo"
            repo.mkdir(parents=True, exist_ok=True)
            with patch.dict(os.environ, env, clear=False):
                with patch("amof.commands.agent_cmd.Path.cwd", return_value=repo):
                    with patch(
                        "amof.commands.agent_cmd._active_provider_profile",
                        return_value={
                            "name": "cloud-dev",
                            "provider": "remote-ial",
                            "model": "remote-ial/default",
                        },
                    ):
                        code, stdout, stderr = _run_request_json_cmd(
                            _request_json_args(),
                            json.dumps(packet),
                        )

        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["request_id"], "external-request-provider-config-001")
        self.assertEqual(payload["result"]["status"], "failed")
        self.assertEqual(
            payload["result"]["stop_reason"], "provider_configuration_failed"
        )
        self.assertIn("base_url or default_base_url", payload["result"]["final_text"])
        self.assertNotEqual(
            payload["result"]["stop_reason"], "invalid_json_mode_result"
        )
        self.assertEqual(stdout.strip(), json.dumps(payload))

    def test_parseable_invalid_packet_preserves_request_id_in_failure_wrapper(
        self,
    ) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["unexpected"] = True

        code, stdout, _stderr = _run_request_json_cmd(
            _request_json_args(),
            json.dumps(packet),
        )

        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertEqual(payload["request_id"], "external-request-minimal-001")
        self.assertEqual(payload["result"]["status"], "failed")
        self.assertIn("Unknown fields", payload["result"]["final_text"])

    def test_empty_input_is_rejected(self) -> None:
        code, stdout, stderr = _run_request_json_cmd(_request_json_args(), "  \n\t")

        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIsNone(payload["request_id"])
        self.assertEqual(payload["result"]["stop_reason"], "invalid_request")
        self.assertIn("empty", payload["result"]["final_text"])

    def test_malformed_json_is_rejected(self) -> None:
        code, stdout, _stderr = _run_request_json_cmd(_request_json_args(), "{not json")

        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertIsNone(payload["request_id"])
        self.assertIn("Malformed request-json input", payload["result"]["final_text"])

    def test_multiple_json_documents_are_rejected(self) -> None:
        code, stdout, _stderr = _run_request_json_cmd(
            _request_json_args(),
            '{"schema_version":1} {"schema_version":1}',
        )

        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertIn(
            "exactly one JSON document",
            payload["result"]["final_text"],
        )

    def test_unknown_field_is_rejected(self) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["unexpected"] = True

        code, stdout, _stderr = _run_request_json_cmd(
            _request_json_args(), json.dumps(packet)
        )

        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertIn("Unknown fields", payload["result"]["final_text"])

    def test_unsafe_example_is_rejected(self) -> None:
        code, stdout, _stderr = _run_request_json_cmd(
            _request_json_args(),
            UNSAFE_EXAMPLE_PATH.read_text(encoding="utf-8"),
        )

        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertEqual(payload["request_id"], "external-request-unsafe-001")
        self.assertEqual(payload["result"]["status"], "failed")

    def test_unsupported_mode_is_rejected(self) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["mode"] = "execute"

        code, stdout, _stderr = _run_request_json_cmd(
            _request_json_args(), json.dumps(packet)
        )

        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertEqual(payload["request_id"], "external-request-minimal-001")
        self.assertIn("plan-execute", payload["result"]["final_text"])

    def test_positional_goal_text_is_rejected_in_request_json_mode(self) -> None:
        args = _request_json_args(goal="do not allow")
        with patch(
            "amof.commands.agent_cmd._run_agent_plan_execute_request"
        ) as run_mock:
            code, stdout, _stderr = _run_request_json_cmd(
                args,
                MINIMAL_EXAMPLE_PATH.read_text(encoding="utf-8"),
            )

        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        run_mock.assert_not_called()
        self.assertIn("rejects positional goal text", payload["result"]["final_text"])

    def test_cli_execution_options_cannot_override_packet_values(self) -> None:
        args = _request_json_args(provider="openrouter")
        with patch(
            "amof.commands.agent_cmd._run_agent_plan_execute_request"
        ) as run_mock:
            code, stdout, _stderr = _run_request_json_cmd(
                args,
                MINIMAL_EXAMPLE_PATH.read_text(encoding="utf-8"),
            )

        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        run_mock.assert_not_called()
        self.assertIn(
            "rejects separate CLI execution overrides",
            payload["result"]["final_text"],
        )

    def test_request_json_requires_plan_execute_and_json(self) -> None:
        code, stdout, _stderr = _run_request_json_cmd(
            _request_json_args(plan_execute=False),
            MINIMAL_EXAMPLE_PATH.read_text(encoding="utf-8"),
        )
        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertIn("requires --plan-execute", payload["result"]["final_text"])

        code, stdout, _stderr = _run_request_json_cmd(
            _request_json_args(json=False),
            MINIMAL_EXAMPLE_PATH.read_text(encoding="utf-8"),
        )
        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertIn("requires --json", payload["result"]["final_text"])

    def test_blocked_and_failed_runs_return_structured_wrapped_results(self) -> None:
        for status, exit_code, stop_reason in (
            ("blocked", 1, "budget_preflight_blocked"),
            ("failed", 1, "max_iterations"),
        ):
            with self.subTest(status=status):
                with patch(
                    "amof.commands.agent_cmd._run_agent_plan_execute_request",
                    return_value=_envelope(
                        status=status,
                        exit_code=exit_code,
                        stop_reason=stop_reason,
                    ),
                ):
                    code, stdout, stderr = _run_request_json_cmd(
                        _request_json_args(),
                        MINIMAL_EXAMPLE_PATH.read_text(encoding="utf-8"),
                    )

                payload = json.loads(stdout)
                self.assertEqual(code, 1)
                self.assertEqual(stderr, "")
                self.assertEqual(payload["request_id"], "external-request-minimal-001")
                self.assertEqual(payload["result"]["status"], status)
                self.assertEqual(payload["result"]["exit_code"], exit_code)
                self.assertEqual(payload["result"]["stop_reason"], stop_reason)


class AgentRequestJsonEntrypointRegressionTests(unittest.TestCase):
    def test_request_json_branch_dispatches_to_adapter(self) -> None:
        args = _request_json_args()
        with (
            patch("amof.entrypoint.parse_args", return_value=args),
            patch("amof.entrypoint.load_manifest", return_value=_manifest()),
            patch(
                "amof.commands.agent_cmd.cmd_agent_request_json", return_value=4
            ) as request_json_mock,
        ):
            with self.assertRaises(SystemExit) as exc:
                entrypoint.main()

        self.assertEqual(exc.exception.code, 4)
        request_json_mock.assert_called_once_with(_manifest(), args)

    def test_existing_agent_json_branch_remains_unchanged_without_request_json(
        self,
    ) -> None:
        args = SimpleNamespace(
            command="agent",
            ecosystem="demo-repo",
            goal=None,
            json=True,
            request_json=None,
        )
        with (
            patch("amof.entrypoint.parse_args", return_value=args),
            patch("amof.entrypoint.load_manifest", return_value=_manifest()),
            patch(
                "amof.commands.agent_cmd.cmd_agent_json", return_value=7
            ) as agent_json_mock,
            patch("sys.stdin", io.StringIO("{}")),
        ):
            with self.assertRaises(SystemExit) as exc:
                entrypoint.main()

        self.assertEqual(exc.exception.code, 7)
        agent_json_mock.assert_called_once_with(_manifest(), {})


if __name__ == "__main__":
    unittest.main()
