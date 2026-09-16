"""15-minute Runtime Authority demo — authority lifecycle, no bypass."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from amof.capability import CAPABILITY_REFERENCE_ACTION
from amof.commands.demo import cmd_demo
from amof.demo_runtime import MENU_ORDER, run_scenario, validate_demo_receipt
from amof.reference_capability import (
    ACCEPTANCE_PASS,
    RecordingReferenceTransport,
    ReferenceCapabilityStore,
    ReferenceExecutor,
    ReferenceOperation,
    approve_proposal,
    execute_reference_capability,
    propose_reference_capability,
)


def _home() -> str:
    return tempfile.mkdtemp(prefix="amof-demo-test-")


class ReferenceCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = _home()
        self.env = patch.dict(os.environ, {"AMOF_HOME": self.home}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = ReferenceCapabilityStore.from_env()
        self.transport = RecordingReferenceTransport(
            {"customer-batch-001": {"location": "legacy-system-a", "generation": 1}}
        )
        self.executor = ReferenceExecutor(self.transport)

    def _grant(self):
        proposal = propose_reference_capability(
            run_id="ref-propose",
            body={
                "capability": CAPABILITY_REFERENCE_ACTION,
                "system": "local-migration",
                "objects": ["customer-batch-001"],
                "actions": ["migrate"],
                "constraints": {"target": "cloud-native-target-b"},
                "reason": "test",
            },
            requested_by="worker:demo",
            store=self.store,
        )
        return approve_proposal(
            proposal["proposal_id"],
            ttl="30m",
            approved_by="operator:demo",
            store=self.store,
        )

    def test_blocked_action_does_not_invoke_transport(self) -> None:
        approval = self._grant()
        outcome = execute_reference_capability(
            operation=ReferenceOperation(
                system="local-migration",
                object="customer-batch-101",
                action="migrate",
                params={"target": "cloud-native-target-b"},
                mission_id="t",
                requested_by="worker:demo",
                run_id="blocked",
            ),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "wrong_object")
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(outcome.receipt["executed"])

    def test_pass_requires_verification(self) -> None:
        approval = self._grant()
        outcome = execute_reference_capability(
            operation=ReferenceOperation(
                system="local-migration",
                object="customer-batch-001",
                action="migrate",
                params={"target": "cloud-native-target-b"},
                mission_id="t",
                requested_by="worker:demo",
                run_id="ok",
            ),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertTrue(outcome.ok)
        receipt = outcome.receipt
        self.assertEqual(receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertTrue(receipt["verification"]["verified"])
        self.assertTrue(receipt["within_scope"])
        self.assertTrue(receipt["secrets_omitted"])


class DemoScenarioTests(unittest.TestCase):
    def test_every_scenario_allows_and_blocks(self) -> None:
        for name in MENU_ORDER:
            with self.subTest(scenario=name):
                result = run_scenario(name, home=_home())
                self.assertTrue(result.ok, f"{name} failed: {result.steps}")
                blocked = [step for step in result.steps if step.name.startswith("BLOCKED")]
                allowed = [step for step in result.steps if step.name == "ALLOWED ACTION"]
                verified = [step for step in result.steps if step.name == "VERIFICATION"]
                self.assertTrue(blocked)
                self.assertTrue(all(step.ok for step in blocked))
                self.assertTrue(all(step.invoked_transport is False for step in blocked))
                self.assertTrue(allowed and allowed[0].ok)
                self.assertTrue(verified and verified[0].ok)
                validate_demo_receipt(result.receipt or {})
                if name in {"migration", "security", "insurance", "banking", "healthcare"}:
                    self.assertEqual(result.mode, "LOCAL REFERENCE SYSTEM")
                if name == "git":
                    self.assertEqual(result.mode, "REAL")
                    self.assertEqual((result.receipt or {}).get("kind"), "mutation_receipt")
                if name == "kubernetes":
                    self.assertEqual(result.mode, "LOCAL REFERENCE SYSTEM")
                    self.assertEqual((result.receipt or {}).get("kind"), "kubernetes_capability_receipt")

    def test_cli_non_interactive_migration(self) -> None:
        home = _home()
        args = type(
            "Args",
            (),
            {
                "scenario": "migration",
                "non_interactive": True,
                "live": False,
                "show_receipt": False,
                "json": True,
            },
        )()
        with patch.dict(os.environ, {"AMOF_HOME": home}, clear=False):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = cmd_demo(args)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["scenario"], "migration")
        self.assertTrue(Path(payload["receipt_path"]).is_file())

    def test_demo_uses_canonical_propose_approve(self) -> None:
        home = _home()
        with patch(
            "amof.demo_runtime.propose_reference_capability",
            wraps=propose_reference_capability,
        ) as propose:
            result = run_scenario("migration", home=home)
        self.assertTrue(result.ok)
        self.assertTrue(propose.called)


if __name__ == "__main__":
    unittest.main()
