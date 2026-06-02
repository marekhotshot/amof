from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - optional dependency in some environments.
    TestClient = None

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

if TestClient is not None:
    from amof.api.dependencies import require_step_up_user
    from amof.api.main import app
else:  # pragma: no cover - no fastapi runtime available
    require_step_up_user = None
    app = None


class IntakeDraftApiTests(unittest.TestCase):
    @unittest.skipIf(TestClient is None, "fastapi is not installed")
    def test_control_intake_draft_compiles_packet_text(self) -> None:
        assert TestClient is not None
        assert app is not None
        assert require_step_up_user is not None
        app.dependency_overrides[require_step_up_user] = lambda: {"id": "test-user"}
        try:
            client = TestClient(app)
            response = client.post(
                "/api/v1/control/intake/draft",
                json={
                    "raw_text": (
                        "AMOF-777 urgent: inspect services/operator-console/src/components/amof-assistant-mobile.tsx\n"
                        "blocked by missing AMOF_REMOTE_IAL_API_KEY"
                    )
                },
            )
        finally:
            app.dependency_overrides.pop(require_step_up_user, None)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["classification"], "defer")
        packet = json.loads(payload["packet_text"])
        self.assertEqual(packet["kind"], "bounded_intake_task")
        self.assertEqual(packet["ticket_id"], "AMOF-777")


if __name__ == "__main__":
    unittest.main()
