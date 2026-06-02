from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.intake.authority_ledger import (
    ContextTrustClass,
    IntakeDecisionClass,
    ToolPolicyMetadata,
    evaluate_intake_authority,
)


class AuthorityLedgerTests(unittest.TestCase):
    def test_privileged_action_blocked_without_approval(self) -> None:
        artifact = evaluate_intake_authority(
            requested_decision_class=IntakeDecisionClass.PRIVILEGED_ACTION,
            present_context_classes=[ContextTrustClass.OPERATOR_ASSERTED],
            requested_tools=["Shell"],
            approval_granted=False,
        ).to_dict()

        self.assertEqual(artifact["decision_class"], "escalate")
        self.assertEqual(artifact["eligible_tools"], [])
        self.assertEqual(artifact["ineligible_tools"][0]["tool_name"], "Shell")
        self.assertIn("explicit approval", artifact["blockers"][0])
        self.assertEqual(artifact["expected_evidence"], ["approval_ref"])

    def test_untrusted_context_cannot_upgrade_authority(self) -> None:
        artifact = evaluate_intake_authority(
            requested_decision_class=IntakeDecisionClass.BOUNDED_ACTION,
            present_context_classes=[
                ContextTrustClass.EXTERNAL_UNTRUSTED,
                ContextTrustClass.TRANSCRIPT_UNTRUSTED,
            ],
            requested_tools=["Read"],
        ).to_dict()

        self.assertEqual(artifact["decision_class"], "refuse")
        self.assertEqual(artifact["eligible_tools"], [])
        self.assertIn("untrusted context cannot upgrade authority", artifact["rationale"])
        self.assertEqual(artifact["present_context_classes"], ["external_untrusted", "transcript_untrusted"])

    def test_bounded_action_requires_matching_tool_policy(self) -> None:
        artifact = evaluate_intake_authority(
            requested_decision_class="bounded_action",
            present_context_classes=["operator_asserted"],
            requested_tools=["NoSuchTool"],
        ).to_dict()

        self.assertEqual(artifact["decision_class"], "refuse")
        self.assertEqual(artifact["eligible_tools"], [])
        self.assertEqual(artifact["ineligible_tools"][0]["reason"], "missing tool policy metadata")
        self.assertIn("bounded_action requires", artifact["blockers"][0])

    def test_refusal_emits_machine_readable_reason(self) -> None:
        artifact = evaluate_intake_authority(
            requested_decision_class=IntakeDecisionClass.REFUSE,
            present_context_classes=[ContextTrustClass.OPERATOR_ASSERTED],
            requested_tools=["Read"],
            rationale="operator request targets out-of-scope surface",
        ).to_dict()

        self.assertEqual(artifact["decision_class"], "refuse")
        self.assertEqual(artifact["rationale"], "operator request targets out-of-scope surface")
        self.assertEqual(artifact["blockers"], ["operator request targets out-of-scope surface"])
        self.assertEqual(artifact["ineligible_tools"][0]["reason"], "operator request targets out-of-scope surface")

    def test_answer_only_path_avoids_execution_tool_selection(self) -> None:
        artifact = evaluate_intake_authority(
            requested_decision_class=IntakeDecisionClass.ANSWER_ONLY,
            present_context_classes=[ContextTrustClass.OPERATOR_ASSERTED],
            requested_tools=["Read", "Shell"],
            rationale="question can be answered from supplied operator context",
        ).to_dict()

        self.assertEqual(artifact["decision_class"], "answer_only")
        self.assertEqual(artifact["eligible_tools"], [])
        self.assertEqual([item["tool_name"] for item in artifact["ineligible_tools"]], ["Read", "Shell"])
        self.assertIn("avoids execution tool selection", artifact["ineligible_tools"][1]["reason"])

    def test_bounded_action_emits_eligible_policy_and_evidence(self) -> None:
        policies = {
            "Read": ToolPolicyMetadata(
                risk_class="read_only",
                requires_approval=False,
                allowed_context_classes=(ContextTrustClass.OPERATOR_ASSERTED,),
                minimum_evidence=("operator_intent", "path_scope"),
                refusal_reason_template="{tool_name} is outside bounded read policy.",
            )
        }

        artifact = evaluate_intake_authority(
            requested_decision_class=IntakeDecisionClass.BOUNDED_ACTION,
            present_context_classes=[ContextTrustClass.OPERATOR_ASSERTED],
            requested_tools=["Read"],
            tool_policies=policies,
            emitted_evidence_refs=["intake://amof-authority-ledger-001"],
        ).to_dict()

        self.assertEqual(artifact["decision_class"], "bounded_action")
        self.assertEqual(artifact["eligible_tools"][0]["tool_name"], "Read")
        self.assertEqual(artifact["eligible_tools"][0]["policy"]["risk_class"], "read_only")
        self.assertEqual(artifact["expected_evidence"], ["operator_intent", "path_scope"])
        self.assertEqual(artifact["emitted_evidence_refs"], ["intake://amof-authority-ledger-001"])


if __name__ == "__main__":
    unittest.main()

