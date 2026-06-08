from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands.studio import cmd_studio


def _run_studio_cmd(**kwargs: object) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    args = SimpleNamespace(**kwargs)
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_studio(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _write_run_events(events_path: Path, run_id: str, session_id: str, *, planning_mode: str | None = None) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "event_id": f"{run_id}:0001",
            "run_id": run_id,
            "session_id": session_id,
            "timestamp": "2026-06-08T00:00:00+00:00",
            "event_type": "run_created",
            "severity": "info",
            "actor": "amof.agent",
            "planning_mode": planning_mode,
        },
        {
            "event_id": f"{run_id}:0002",
            "run_id": run_id,
            "session_id": session_id,
            "timestamp": "2026-06-08T00:00:01+00:00",
            "event_type": "run_finished",
            "severity": "info",
            "actor": "amof.agent",
            "planning_mode": planning_mode,
            "cost_status": "unknown",
            "estimated_cost": None,
        },
    ]
    with events_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


class StudioSessionCoreTests(unittest.TestCase):
    def test_create_checkpoint_attach_show_and_end_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-studio-core-") as td:
            amof_home = Path(td) / "amof-home"
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                code, stdout, stderr = _run_studio_cmd(studio_cmd="create", json=True)
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                created = json.loads(stdout)
                studio_session_id = created["manifest"]["studio_session_id"]
                session_dir = Path(created["paths"]["session_dir"])
                self.assertTrue((session_dir / "session.json").exists())
                self.assertTrue((session_dir / "events.jsonl").exists())
                self.assertTrue((session_dir / "runs.json").exists())
                self.assertTrue((session_dir / "checkpoints.jsonl").exists())

                run_events = amof_home / "share" / "runs" / "run-123" / "events.jsonl"
                _write_run_events(run_events, "run-123", "run-123", planning_mode="execute")

                code, stdout, stderr = _run_studio_cmd(
                    studio_cmd="attach-run",
                    studio_session_id=studio_session_id,
                    run_id="run-123",
                    json=True,
                )
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                attached = json.loads(stdout)
                self.assertEqual(attached["run_id"], "run-123")
                self.assertEqual(attached["surface"], "agent")

                code, stdout, stderr = _run_studio_cmd(
                    studio_cmd="checkpoint",
                    studio_checkpoint_cmd="add",
                    studio_session_id=studio_session_id,
                    summary="Planner reviewed and ready for execution.",
                    json=True,
                )
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                checkpoint = json.loads(stdout)
                self.assertEqual(checkpoint["studio_session_id"], studio_session_id)

                code, stdout, stderr = _run_studio_cmd(
                    studio_cmd="show",
                    studio_session_id=studio_session_id,
                    json=True,
                )
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                shown = json.loads(stdout)
                self.assertEqual(shown["summary"]["attached_runs_count"], 1)
                self.assertEqual(shown["summary"]["checkpoints_count"], 1)
                self.assertEqual(shown["manifest"]["status"], "active")

                code, stdout, stderr = _run_studio_cmd(
                    studio_cmd="end",
                    studio_session_id=studio_session_id,
                    json=True,
                )
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                ended = json.loads(stdout)
                self.assertEqual(ended["manifest"]["status"], "ended")
                self.assertIsNotNone(ended["manifest"]["ended_at"])

                events = (session_dir / "events.jsonl").read_text(encoding="utf-8")
                self.assertIn('"studio_session_created"', events)
                self.assertIn('"run.attached"', events)
                self.assertIn('"studio_checkpoint_added"', events)
                self.assertIn('"studio_session_ended"', events)

    def test_attach_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-studio-core-idempotent-") as td:
            amof_home = Path(td) / "amof-home"
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                code, stdout, _stderr = _run_studio_cmd(studio_cmd="create", json=True)
                self.assertEqual(code, 0)
                studio_session_id = json.loads(stdout)["manifest"]["studio_session_id"]

                run_events = amof_home / "share" / "runs" / "chat-plans" / "chat-456" / "events.jsonl"
                _write_run_events(run_events, "chat-456", "chat-456", planning_mode="minimal_context")

                for _ in range(2):
                    code, _stdout, stderr = _run_studio_cmd(
                        studio_cmd="attach-run",
                        studio_session_id=studio_session_id,
                        run_id="chat-456",
                        json=False,
                    )
                    self.assertEqual(code, 0)
                    self.assertEqual(stderr, "")

                code, stdout, stderr = _run_studio_cmd(
                    studio_cmd="show",
                    studio_session_id=studio_session_id,
                    json=True,
                )
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                shown = json.loads(stdout)
                self.assertEqual(shown["summary"]["attached_runs_count"], 1)
                self.assertEqual(shown["attached_runs"][0]["surface"], "chat")

    def test_attach_unknown_run_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-studio-core-missing-run-") as td:
            amof_home = Path(td) / "amof-home"
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                code, stdout, _stderr = _run_studio_cmd(studio_cmd="create", json=True)
                self.assertEqual(code, 0)
                studio_session_id = json.loads(stdout)["manifest"]["studio_session_id"]

                code, stdout, stderr = _run_studio_cmd(
                    studio_cmd="attach-run",
                    studio_session_id=studio_session_id,
                    run_id="does-not-exist",
                    json=False,
                )
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("[studio] run not found: does-not-exist", stderr)


if __name__ == "__main__":
    unittest.main()
