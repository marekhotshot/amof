from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.orchestrator.llm.base import Usage, estimate_cost_details
from amof.orchestrator.llm.local_openai_compatible import LocalOpenAICompatibleClient
from amof.orchestrator.telemetry import SessionTelemetry


class CostTelemetryTruthTests(unittest.TestCase):
    def test_unknown_usage_serializes_null_total_cost(self) -> None:
        telemetry = SessionTelemetry()

        telemetry.record_from_usage(
            Usage(
                model="local/test/model",
                prompt_tokens=10,
                completion_tokens=5,
                latency_ms=7,
                estimated_cost=0.0,
                cost_status="unknown",
                cost_observed=False,
            ),
            tier="fast",
            provider="local",
        )

        payload = telemetry.to_dict()
        self.assertIsNone(payload["total_cost"])
        self.assertEqual(payload["cost_status"], "unknown")
        self.assertEqual(payload["unknown_cost_calls"], 1)

    def test_unknown_model_pricing_stays_unknown(self) -> None:
        estimated_cost, cost_status, cost_observed = estimate_cost_details(
            "unknown-provider/unknown-model",
            10,
            5,
        )

        self.assertEqual(estimated_cost, 0.0)
        self.assertEqual(cost_status, "unknown")
        self.assertFalse(cost_observed)

    def test_local_openai_compatible_usage_marks_cost_unknown(self) -> None:
        client = LocalOpenAICompatibleClient(
            base_url="http://127.0.0.1:11434/v1",
            model="qwen2.5-coder:7b",
        )
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4),
        )

        usage = client._build_usage(response, latency_ms=9)

        self.assertEqual(usage.estimated_cost, 0.0)
        self.assertEqual(usage.cost_status, "unknown")
        self.assertFalse(usage.cost_observed)


if __name__ == "__main__":
    unittest.main()
