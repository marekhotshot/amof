from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands.agent_cmd import AgentPlanExecuteEnvelope
from amof.commands.chat import PlanPacket
from amof.contracts_runtime import AgentRunResult, PlanBundle


PLAN_BUNDLE_SCHEMA_PATH = ROOT / "contracts" / "plan-bundle.schema.json"
PLAN_BUNDLE_EXAMPLE_PATH = ROOT / "contracts" / "examples" / "plan-bundle.example.json"
AGENT_RUN_SCHEMA_PATH = ROOT / "contracts" / "agent-run-result.schema.json"
AGENT_RUN_EXAMPLE_PATH = ROOT / "contracts" / "examples" / "agent-run-result.example.json"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class PlanBundleAgentRunContractTests(unittest.TestCase):
    def test_plan_bundle_round_trips_from_example(self) -> None:
        payload = _load(PLAN_BUNDLE_EXAMPLE_PATH)

        bundle = PlanBundle.from_dict(payload)

        self.assertEqual(bundle.result_kind, "plan_bundle")
        self.assertEqual(bundle.contract_version, "plan-bundle-v1")
        self.assertEqual(bundle.to_dict(), payload)

    def test_plan_packet_remains_a_compatibility_alias(self) -> None:
        packet = PlanPacket.from_dict(_load(PLAN_BUNDLE_EXAMPLE_PATH))

        self.assertIsInstance(packet, PlanBundle)
        self.assertEqual(packet.result_kind, "plan_bundle")
        self.assertEqual(packet.contract_version, "plan-bundle-v1")

    def test_agent_run_result_round_trips_from_example(self) -> None:
        payload = _load(AGENT_RUN_EXAMPLE_PATH)

        result = AgentRunResult(**payload)

        self.assertEqual(result.result_kind, "agent_run_result")
        self.assertEqual(result.contract_version, "agent-run-v1")
        self.assertEqual(result.to_dict(), payload)

    def test_agent_plan_execute_envelope_remains_a_compatibility_alias(self) -> None:
        envelope = AgentPlanExecuteEnvelope(**_load(AGENT_RUN_EXAMPLE_PATH))

        self.assertIsInstance(envelope, AgentRunResult)
        self.assertEqual(envelope.result_kind, "agent_run_result")
        self.assertEqual(envelope.contract_version, "agent-run-v1")

    def test_agent_run_result_optionally_carries_studio_session_id(self) -> None:
        payload = _load(AGENT_RUN_EXAMPLE_PATH)
        payload["studio_session_id"] = "studio-20260608-004150"

        result = AgentRunResult(**payload)

        self.assertEqual(result.studio_session_id, "studio-20260608-004150")
        self.assertEqual(result.to_dict()["studio_session_id"], "studio-20260608-004150")

    def test_agent_run_result_round_trips_transport_fields(self) -> None:
        payload = _load(AGENT_RUN_EXAMPLE_PATH)
        payload["failure_classification"] = None

        result = AgentRunResult(**payload)

        self.assertEqual(result.transport, "remote_ial")
        self.assertEqual(result.to_dict()["requested_provider"], "remote-ial")
        self.assertEqual(result.to_dict()["result_path"], "/tmp/amof/result.json")

    def test_agent_run_result_allows_unknown_exit_code_for_recovery_boundary(self) -> None:
        payload = _load(AGENT_RUN_EXAMPLE_PATH)
        payload["exit_code"] = "unknown"
        payload["failure_classification"] = "result_missing"

        result = AgentRunResult(**payload)

        self.assertEqual(result.exit_code, "unknown")
        self.assertEqual(result.to_dict()["failure_classification"], "result_missing")

    def test_contract_schemas_match_example_kinds(self) -> None:
        plan_schema = _load(PLAN_BUNDLE_SCHEMA_PATH)
        plan_example = _load(PLAN_BUNDLE_EXAMPLE_PATH)
        run_schema = _load(AGENT_RUN_SCHEMA_PATH)
        run_example = _load(AGENT_RUN_EXAMPLE_PATH)

        self.assertEqual(plan_schema["properties"]["result_kind"]["const"], plan_example["result_kind"])
        self.assertEqual(
            plan_schema["properties"]["contract_version"]["const"],
            plan_example["contract_version"],
        )
        self.assertEqual(run_schema["properties"]["result_kind"]["const"], run_example["result_kind"])
        self.assertEqual(
            run_schema["properties"]["contract_version"]["const"],
            run_example["contract_version"],
        )


if __name__ == "__main__":
    unittest.main()
