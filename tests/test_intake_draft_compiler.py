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


if __name__ == "__main__":
    unittest.main()
