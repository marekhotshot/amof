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

        self.assertEqual(packet["task_kind"], "adoption")
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
        self.assertEqual(packet["task_kind"], "adoption")

    def test_adoption_packet_remains_validate_ready(self) -> None:
        raw_text = "Adopt repository igormraz.com into the hotshot.sk ecosystem."
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)
        validated = _validate_packet(packet)
        self.assertEqual(validated.kind, "bounded_intake_task")
        self.assertEqual(validated.mutations_allowed, [])

    def test_bare_domain_does_not_pollute_paths(self) -> None:
        raw_text = "AMOF-777 today: inspect amof.dev availability and fix services/operator-console/src/app/page.tsx"
        draft = compile_intake_draft(raw_text)
        packet = json.loads(draft.packet_text)
        self.assertNotIn("amof.dev", packet["paths_to_inspect"])
        self.assertIn("services/operator-console/src/app/page.tsx", packet["paths_to_inspect"])


if __name__ == "__main__":
    unittest.main()
