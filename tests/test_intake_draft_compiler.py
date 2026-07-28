from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands.intake import _validate_packet
from amof.intake.draft_compiler import compile_intake_draft


class IntakeDraftCompilerTests(unittest.TestCase):
    def test_compile_returns_expected_fields(self) -> None:
        raw_text = (
            "AMOF-424 urgent: fix failing intake route in services/operator-console/src/app/api/intake/route.ts\n"
            "blocked by missing AMOF_OPERATOR_CONSOLE_IAL_TOKEN in cloud-dev\n"
            "collect receipts and logs before submit"
        )
        draft = compile_intake_draft(raw_text)
        payload = draft.to_dict()

        self.assertEqual(payload["classification"], "defer")
        self.assertEqual(payload["replay_lane"], "defer")
        self.assertTrue(payload["title"].startswith("AMOF-424 urgent"))
        self.assertTrue(any("blocked by" in line.lower() for line in payload["blockers"]))
        self.assertIn("services/operator-console/src/app/api/intake/route.ts", payload["bounded_scope"])
        self.assertTrue(payload["packet_text"])

    def test_packet_text_is_validate_ready(self) -> None:
        raw_text = "AMOF-999 now: validate canonical intake draft compiler path for services/operator-console/src/components/amof-assistant-mobile.tsx"
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)
        validated = _validate_packet(packet)
        self.assertEqual(validated.ticket_id, "AMOF-999")
        self.assertEqual(validated.kind, "bounded_intake_task")
        self.assertEqual(validated.mutations_allowed, [])
        self.assertIn("deploy", validated.mutations_forbidden)

    # AMOF-INTAKE-ADOPTION-SEMANTIC-CLASSIFICATION-001 regression coverage

    def test_adoption_draft_is_classified_as_adoption_not_discard(self) -> None:
        raw_text = (
            "Adopt the IgorMraz.com website repository under AMOF governance.\n"
            "Ignore the legacy theme folder during analysis.\n"
            "Map the runtime facts and propose adoption tickets."
        )
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)

        self.assertEqual(packet["task_kind"], "repo_runtime_adoption")
        self.assertNotEqual(draft.classification, "kill")
        self.assertIn("IgorMraz.com", packet["extracted_repositories"])
        self.assertNotIn("IgorMraz.com", packet["repo_scope"])
        self.assertTrue(packet["uc_classification"]["adoption"])

    def test_negated_kill_verbs_do_not_classify_kill(self) -> None:
        raw_text = "AMOF-555 now: review intake flow. Do not discard any drafts and never cancel running sessions."
        draft = compile_intake_draft(raw_text)
        self.assertNotEqual(draft.classification, "kill")
        self.assertEqual(json.loads(draft.packet_text)["task_kind"], "other")

    def test_kill_still_works_when_targeting_the_intake_itself(self) -> None:
        raw_text = "Discard this ticket: duplicate of AMOF-101."
        draft = compile_intake_draft(raw_text)
        self.assertEqual(draft.classification, "kill")
        self.assertEqual(json.loads(draft.packet_text)["task_kind"], "discard")

    def test_runtime_extraction_fidelity(self) -> None:
        raw_text = (
            "Adopt the hotshot-operator-host-01 runtime and the amof-cloud-runtime worker. "
            "Repository: https://github.com/marekhotshot/amof.git"
        )
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)
        self.assertIn("hotshot-operator-host-01", packet["extracted_runtimes"])
        self.assertIn("amof-cloud-runtime", packet["extracted_runtimes"])
        self.assertIn("https://github.com/marekhotshot/amof.git", packet["extracted_repositories"])
        self.assertEqual(packet["task_kind"], "repo_runtime_adoption")

    def test_adoption_packet_remains_validate_ready(self) -> None:
        raw_text = "Adopt repository igormraz.com into the hotshot.sk ecosystem."
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)
        validated = _validate_packet(packet)
        self.assertEqual(validated.kind, "bounded_intake_task")
        self.assertEqual(validated.mutations_allowed, [])

    def test_adoption_expected_shape_repo_runtime_adoption_replay_now_read_only(self) -> None:
        raw_text = "Adopt the IgorMraz.com repository and the hotshot runtime under AMOF governance."
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)
        self.assertEqual(packet["task_kind"], "repo_runtime_adoption")
        self.assertEqual(packet["uc_classification"]["replay_lane"], "replay_now")
        self.assertEqual(packet["mutations"]["allowed"], [], "adoption packets stay read-only")

    def test_hypothetical_language_does_not_classify_kill(self) -> None:
        raw_text = (
            "AMOF-808 now: review session handling. "
            "If we were to cancel a stale session, the queue should terminalize it. "
            "Decide whether to drop expired leases automatically."
        )
        draft = compile_intake_draft(raw_text)
        self.assertNotEqual(draft.classification, "kill")

    def test_noun_phrase_runtime_extraction(self) -> None:
        raw_text = "Adopt the IgorMraz.com repository and the hotshot runtime under AMOF governance."
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)
        self.assertIn("hotshot", packet["extracted_runtimes"])
        # Generic qualifiers never become runtime identities.
        generic = compile_intake_draft("Keep the cloud runtime healthy.")
        generic_packet = json.loads(generic.packet_text)
        self.assertEqual(generic_packet["extracted_runtimes"], [])

    def test_bare_domain_does_not_pollute_paths(self) -> None:
        raw_text = "AMOF-777 today: inspect amof.dev availability and fix services/operator-console/src/app/page.tsx"
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)
        self.assertNotIn("amof.dev", packet["paths_to_inspect"])
        self.assertIn("services/operator-console/src/app/page.tsx", packet["paths_to_inspect"])

    # AMOF-BL-109: verification-style classification with project-memory inject

    _WAVE_D_MISSION = (
        "Develop-AMOF backlog verification mission under project amof: in\n"
        "marekhotshot/amof-private, the only file in scope is\n"
        "docs/product/backlogs/amof.md. Docs-only mission.\n"
        "\n"
        "This is a verification-only bounded write for AMOF-BL-108 / BL-091 dogfood\n"
        "on bl077-107-108. The expected and correct outcome is ZERO file changes.\n"
        "Do not invent an edit to appear productive.\n"
        "\n"
        "Task: verify that the delivered status marker for AMOF-BL-108 in\n"
        "docs/product/backlogs/amof.md is intact and mentions allow_no_change.\n"
        "Inspect the marker lines only.\n"
        "\n"
        "If the marker already contains the required references (expected), make NO\n"
        "changes at all and state clearly in your final findings which lines you\n"
        "inspected and that you intentionally made zero changes.\n"
        "\n"
        "Do not touch any other line. An empty diff is success.\n"
        "\n"
        "Final requirement: include\n"
        '"End-of-mission sentinel AMOF-EOM-BL108-DOGFOOD-20260728 observed." in findings.'
    )

    _WAVE_D_MEMORY_PREFIX = (
        "## Project memory\n"
        "- AMOF develops AMOF via Predator Golden Flow: select project/targets → compile\n"
        "mission → preflight → prepare packet → dispatch → review evidence → publish/\n"
        "promote only with operator decision. Models are workers; runtime owns\n"
        "capability, policy, audit, and writes. UI is projection + operator surface,\n"
        "not execution authority. Conversation/planning ≠ approval ≠ execution.\n"
        "Evidence must report observation (events, logs, provenance), not configured\n"
        "labels. Fail closed on missing write_scope_proposals[]; stop honestly when\n"
        "blocked or no-delta. Public installable surface (marekhotshot/amof) is\n"
        "separate from private operator/runtime leverage (marekhotshot/amof-private).\n"
        "Historical Arena agent-society / UI-owned writes are rejected design history. "
        "[charter, seed, bl-064]\n"
        "- Recent closed trains (names only): DELIVERY-TAIL-001 (BL-047/074/075/076),\n"
        "WORKFORCE-LADDERS-001/002 (BL-054/055 + qualification calibrate),\n"
        "BACKLOG-CLEANUP-001 (status reconcile + BL-033/061/062). [mission-history, seed, bl-064]\n"
        "\n"
    )

    def test_bl109_wave_d_mission_with_memory_is_verification(self) -> None:
        """Exact wave D dogfood shape: memory prepend must not yield task_kind=blocked."""
        raw_text = self._WAVE_D_MEMORY_PREFIX + self._WAVE_D_MISSION
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)
        self.assertEqual(packet["task_kind"], "verification")
        self.assertNotEqual(packet["task_kind"], "blocked")
        self.assertTrue(packet["rough_intent"].startswith("## Project memory\n"))
        self.assertIn("docs/product/backlogs/amof.md", packet["paths_to_inspect"])
        self.assertTrue(draft.title.startswith("Develop-AMOF backlog verification"))

    def test_bl109_wave_d_mission_without_memory_is_verification(self) -> None:
        draft = compile_intake_draft(self._WAVE_D_MISSION)
        packet = json.loads(draft.packet_text)
        self.assertEqual(packet["task_kind"], "verification")

    def test_bl109_mutation_with_memory_stays_implementation(self) -> None:
        mission = (
            "AMOF-BL-109 today: implement a docs fix in docs/product/backlogs/amof.md.\n"
            "Update the marker line to mention allow_no_change and edit the file.\n"
            "Apply a patch; empty diff is not success."
        )
        raw_text = self._WAVE_D_MEMORY_PREFIX + mission
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)
        self.assertEqual(packet["task_kind"], "implementation")
        self.assertNotEqual(packet["task_kind"], "blocked")
        self.assertNotEqual(packet["task_kind"], "verification")

    def test_bl109_ambiguous_verification_and_mutation_is_fail_visible(self) -> None:
        mission = (
            "AMOF-BL-109 verification-only mission for docs/product/backlogs/amof.md.\n"
            "Zero file changes expected. Also update the marker line and edit the file "
            "to implement the missing note."
        )
        draft = compile_intake_draft(mission)
        packet = json.loads(draft.packet_text)
        self.assertEqual(packet["task_kind"], "classification_ambiguous")
        self.assertEqual(draft.classification, "ambiguous")
        self.assertTrue(
            any("classification_ambiguous" in item for item in packet["uc_classification"]["blockers"])
        )

    def test_bl109_incidental_blocked_in_principles_does_not_defer(self) -> None:
        raw_text = (
            "AMOF-900 today: inspect docs/product/backlogs/amof.md marker lines.\n"
            "Principle reminder: stop honestly when blocked or no-delta.\n"
            "This mission is actionable now."
        )
        draft = compile_intake_draft(raw_text)
        self.assertNotEqual(draft.classification, "defer")
        self.assertNotEqual(json.loads(draft.packet_text)["task_kind"], "blocked")


if __name__ == "__main__":
    unittest.main()
