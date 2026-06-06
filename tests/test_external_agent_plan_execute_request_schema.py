import json
import sys
import unittest
from dataclasses import fields
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands.agent_cmd import (
    AgentPlanExecuteEnvelope,
    AgentPlanExecuteJsonRequest,
    parse_agent_plan_execute_json_request,
)


SCHEMA_PATH = ROOT / "contracts" / "external-agent-plan-execute-request.schema.json"
VALID_EXAMPLE_PATH = ROOT / "contracts" / "examples" / "external-agent-plan-execute-request.example.json"
MINIMAL_EXAMPLE_PATH = ROOT / "contracts" / "examples" / "external-agent-plan-execute-request.minimal.example.json"
INVALID_EXAMPLE_PATH = ROOT / "contracts" / "examples" / "external-agent-plan-execute-request.invalid-unsafe.example.json"
RESULT_ENVELOPE_SCHEMA_PATH = ROOT / "contracts" / "execution-handoff-result.schema.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate(packet: dict) -> None:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        schema = _load(SCHEMA_PATH)
        allowed = set(schema["properties"])
        extra = set(packet) - allowed
        if extra:
            raise ValueError(f"Unknown fields: {sorted(extra)}")
        required = set(schema["required"])
        missing = [key for key in required if key not in packet]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        if packet.get("schema_version") != 1:
            raise ValueError("schema_version must equal 1")
        if packet.get("mode") != "plan-execute":
            raise ValueError("mode must equal plan-execute")
        if packet.get("no_follow_up") is not True:
            raise ValueError("no_follow_up must be true")
        request_id = packet.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        goal = packet.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be a non-empty string")
        for key in ("provider", "model", "planner_model", "resume", "follow_up"):
            value = packet.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{key} must be a non-empty string or null")
        for key in ("budget", "subtask_budget"):
            value = packet.get(key)
            if value is not None and (not isinstance(value, (int, float)) or float(value) <= 0):
                raise ValueError(f"{key} must be > 0 or null")
        if "budget_strict" in packet and not isinstance(packet.get("budget_strict"), bool):
            raise ValueError("budget_strict must be boolean")
        for key in ("approve_capabilities", "approve_tool_packs", "approve_writable_roots"):
            value = packet.get(key)
            if value is None:
                continue
            if not isinstance(value, list):
                raise ValueError(f"{key} must be an array of strings")
            if any((not isinstance(item, str)) or (not item.strip()) for item in value):
                raise ValueError(f"{key} must contain only non-empty strings")
        if isinstance(packet.get("follow_up"), str) and not isinstance(packet.get("resume"), str):
            raise ValueError("follow_up requires resume")
        return
    jsonschema.validate(instance=packet, schema=_load(SCHEMA_PATH))


def _runtime_payload(packet: dict) -> dict:
    supported = {field.name for field in fields(AgentPlanExecuteJsonRequest)}
    return {key: value for key, value in packet.items() if key in supported}


class ExternalAgentPlanExecuteRequestSchemaTests(unittest.TestCase):
    def test_schema_top_level_shape_is_strict_and_versioned(self) -> None:
        schema = _load(SCHEMA_PATH)

        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(schema["properties"]["mode"]["const"], "plan-execute")
        self.assertEqual(schema["properties"]["no_follow_up"]["const"], True)
        self.assertFalse(schema["additionalProperties"])
        for key in ("schema_version", "request_id", "mode", "goal", "no_follow_up"):
            self.assertIn(key, schema["required"])

    def test_valid_bounded_packet_passes(self) -> None:
        _validate(_load(VALID_EXAMPLE_PATH))

    def test_minimal_valid_packet_passes(self) -> None:
        _validate(_load(MINIMAL_EXAMPLE_PATH))

    def test_empty_goal_fails(self) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["goal"] = ""
        with self.assertRaises(Exception):
            _validate(packet)

    def test_unsupported_mode_fails(self) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["mode"] = "execute"
        with self.assertRaises(Exception):
            _validate(packet)

    def test_unknown_fields_fail(self) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["unexpected"] = True
        with self.assertRaises(Exception):
            _validate(packet)

    def test_arbitrary_command_and_shell_fields_fail(self) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["command"] = "echo unsafe"
        packet["shell_command"] = "echo unsafe"
        with self.assertRaises(Exception):
            _validate(packet)

    def test_environment_and_secret_injection_fields_fail(self) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["env"] = {"OPENAI_API_KEY": "secret"}
        packet["auth_header"] = "Bearer secret"
        with self.assertRaises(Exception):
            _validate(packet)

    def test_malformed_approvals_fail(self) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["approve_capabilities"] = ["secret", 3]
        packet["approve_tool_packs"] = "ops-jenkins"
        packet["approve_writable_roots"] = [""]
        with self.assertRaises(Exception):
            _validate(packet)

    def test_invalid_budget_values_fail(self) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["budget"] = 0
        with self.assertRaises(Exception):
            _validate(packet)
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["subtask_budget"] = -1
        with self.assertRaises(Exception):
            _validate(packet)

    def test_resume_follow_up_without_session_id_fails(self) -> None:
        packet = _load(MINIMAL_EXAMPLE_PATH)
        packet["follow_up"] = "Retry only the failed subtask."
        with self.assertRaises(Exception):
            _validate(packet)

    def test_invalid_unsafe_example_fails(self) -> None:
        with self.assertRaises(Exception):
            _validate(_load(INVALID_EXAMPLE_PATH))

    def test_request_maps_only_to_supported_canonical_runtime_options(self) -> None:
        packet = _load(VALID_EXAMPLE_PATH)
        runtime_payload = _runtime_payload(packet)

        self.assertEqual(
            set(runtime_payload),
            {field.name for field in fields(AgentPlanExecuteJsonRequest)},
        )
        request = parse_agent_plan_execute_json_request(runtime_payload)
        self.assertEqual(request.goal, packet["goal"])
        self.assertEqual(request.provider, packet["provider"])
        self.assertEqual(request.model, packet["model"])
        self.assertEqual(request.planner_model, packet["planner_model"])
        self.assertEqual(request.budget, packet["budget"])
        self.assertEqual(request.budget_strict, packet["budget_strict"])
        self.assertEqual(request.subtask_budget, packet["subtask_budget"])
        self.assertEqual(request.resume, packet["resume"])
        self.assertEqual(request.follow_up, packet["follow_up"])
        self.assertEqual(request.approve_capabilities, packet["approve_capabilities"])
        self.assertEqual(request.approve_tool_packs, packet["approve_tool_packs"])
        self.assertEqual(request.approve_writable_roots, packet["approve_writable_roots"])
        self.assertTrue(request.no_follow_up)
        self.assertNotIn("schema_version", runtime_payload)
        self.assertNotIn("request_id", runtime_payload)
        self.assertNotIn("mode", runtime_payload)

    def test_result_envelope_contract_remains_unchanged(self) -> None:
        envelope = AgentPlanExecuteEnvelope(
            schema_version=1,
            status="completed",
            session_id="session-1",
            exit_code=0,
            stop_reason="completed",
            final_text="done",
            plan_path="/tmp/plan.md",
            checkpoint_path=None,
            event_log_path="/tmp/events.jsonl",
            journal_path="/tmp/journal.md",
            budget_summary={"limit": 1.0, "spent": 0.1, "remaining": 0.9},
        )
        self.assertEqual(
            set(envelope.to_dict()),
            {
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
            },
        )
        schema = _load(RESULT_ENVELOPE_SCHEMA_PATH)
        self.assertEqual(schema["$id"], "https://amof.dev/contracts/execution-handoff-result.schema.json")
        self.assertEqual(schema["properties"]["result_kind"]["const"], "workspace_materialization_handoff_result")


if __name__ == "__main__":
    unittest.main()
