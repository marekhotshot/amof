from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands.chat import PlanPacket, _maybe_apply_critic_pass
from amof.critic import (
    CriticRiskSignals,
    decide_critic_gate,
    merge_critic_fields,
)


def _sample_packet(**overrides: object) -> PlanPacket:
    payload = {
        "result_kind": "plan_bundle",
        "contract_version": "plan-bundle-v1",
        "ticket_id": "AMOF-CRITIC-RISK-GATED-001",
        "proposed_ticket_id": None,
        "objective": "Critique gate fixture",
        "repo_scope": "fixture scope",
        "files_to_inspect": ["README.md"],
        "proposed_steps": ["Inspect README", "Draft proposal"],
        "risks": ["Bounded fixture only"],
        "validation_plan": ["Unit test the gate"],
        "execution_prompt_for_director": (
            "Proposal only. Do not execute until the user explicitly approves this PlanBundle."
        ),
        "requires_user_approval": True,
        "execution_allowed": False,
        "confidence": 0.8,
    }
    payload.update(overrides)
    return PlanPacket.from_dict(payload)


class CriticGateTests(unittest.TestCase):
    def test_high_mutation_runs_critique(self) -> None:
        decision = decide_critic_gate(
            CriticRiskSignals(mutation_ceiling="runtime_mutation")
        )
        self.assertTrue(decision.run_critique)
        self.assertIn("high_mutation_ceiling:runtime_mutation", decision.reason_codes)

    def test_read_only_explore_skips(self) -> None:
        decision = decide_critic_gate(
            CriticRiskSignals(mutation_ceiling="read_only", explore_readonly=True)
        )
        self.assertFalse(decision.run_critique)
        self.assertEqual(decision.reason_codes, ("read_only_explore",))

    def test_budget_exhausted_skips_and_degrades(self) -> None:
        decision = decide_critic_gate(CriticRiskSignals(budget_exhausted=True))
        self.assertFalse(decision.run_critique)
        self.assertTrue(decision.degrade_supervised)
        self.assertEqual(decision.reason_codes, ("budget_exhausted",))

    def test_low_confidence_runs(self) -> None:
        decision = decide_critic_gate(CriticRiskSignals(planner_confidence=0.2))
        self.assertTrue(decision.run_critique)
        self.assertIn("low_planner_confidence", decision.reason_codes)

    def test_insufficient_signals_never_always_on(self) -> None:
        decision = decide_critic_gate(CriticRiskSignals())
        self.assertFalse(decision.run_critique)
        self.assertEqual(decision.reason_codes, ("insufficient_risk_signals",))

    def test_merge_skip_evidences_interpretation_without_dissent(self) -> None:
        packet = _sample_packet()
        decision = decide_critic_gate(CriticRiskSignals(explore_readonly=True))
        merged = merge_critic_fields(
            packet_dict=packet.to_dict(),
            decision=decision,
            critic_payload=None,
        )
        self.assertTrue(
            any(
                item.get("text", "").startswith("critique_skipped:")
                for item in merged.get("interpretations") or []
            )
        )
        self.assertNotIn("dissent", merged)

    def test_merge_run_appends_dissent_once(self) -> None:
        packet = _sample_packet()
        decision = decide_critic_gate(
            CriticRiskSignals(mutation_ceiling="bounded_worktree")
        )
        merged = merge_critic_fields(
            packet_dict=packet.to_dict(),
            decision=decision,
            critic_payload={
                "interpretations": [{"text": "Scope may be broad.", "role": "critic"}],
                "dissent": [{"text": "Write roots look oversized.", "severity": "medium"}],
            },
        )
        ran = [
            item
            for item in merged["interpretations"]
            if str(item.get("text", "")).startswith("critique_ran:")
        ]
        self.assertEqual(len(ran), 1)
        self.assertEqual(len(merged["dissent"]), 1)

    def test_maybe_apply_critic_pass_skips_without_second_ial_call(self) -> None:
        packet = _sample_packet(confidence=0.9)
        client = MagicMock()
        events = MagicMock()
        updated, decision = _maybe_apply_critic_pass(
            packet=packet,
            client=client,
            context_prompt="context",
            planning_receipt_payload={"files_to_inspect": ["README.md"]},
            events=events,
            risk_signals={"mutation_ceiling": "read_only", "explore_readonly": True},
        )
        client.chat.assert_not_called()
        self.assertFalse(decision["run_critique"])
        self.assertTrue(
            any(
                str(item.get("text", "")).startswith("critique_skipped:")
                for item in updated.interpretations or []
            )
        )

    def test_maybe_apply_critic_pass_runs_once_for_high_risk(self) -> None:
        packet = _sample_packet(confidence=0.9)
        client = MagicMock()
        events = MagicMock()
        critic_payload = {
            "interpretations": [{"text": "Check write scope.", "role": "critic"}],
            "dissent": [{"text": "Prod path risk.", "severity": "high"}],
        }
        attribution = MagicMock()
        with unittest.mock.patch(
            "amof.commands.chat._call_remote_ial_json",
            return_value=(critic_payload, attribution),
        ) as mocked_call:
            updated, decision = _maybe_apply_critic_pass(
                packet=packet,
                client=client,
                context_prompt="context",
                planning_receipt_payload={"files_to_inspect": ["security/auth.ts"]},
                events=events,
                risk_signals={
                    "mutation_ceiling": "runtime_mutation",
                    "prod_touching": True,
                },
            )
        self.assertTrue(decision["run_critique"])
        self.assertEqual(mocked_call.call_count, 1)
        self.assertTrue(updated.dissent)
        self.assertEqual(updated.dissent[0]["severity"], "high")


if __name__ == "__main__":
    unittest.main()
