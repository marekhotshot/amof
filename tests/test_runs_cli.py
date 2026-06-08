from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands.runs import cmd_runs
import amof.entrypoint as entrypoint


def _write_events(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def _base_args(runs_cmd: str, **overrides):
    payload = {"runs_cmd": runs_cmd, "json": False}
    payload.update(overrides)
    return SimpleNamespace(**payload)


class RunsCliTests(unittest.TestCase):
    def test_runs_is_no_ecosystem_command(self) -> None:
        self.assertIn("runs", entrypoint.NO_ECOSYSTEM_COMMANDS)

    def test_list_discovers_runs(self) -> None:
        with TemporaryDirectory(prefix="amof-runs-list-") as td:
            run_home = Path(td)
            events_path = run_home / "share" / "runs" / "chat-plans" / "run-a" / "events.jsonl"
            _write_events(
                events_path,
                [
                    {
                        "event_id": "run-a:0001",
                        "run_id": "run-a",
                        "session_id": "run-a",
                        "timestamp": "2026-05-30T10:00:00+00:00",
                        "event_type": "run_created",
                        "severity": "info",
                        "actor": "amof.chat",
                        "ticket_id": "AMOF-RUNS-CLI-001",
                        "planning_mode": "minimal_context",
                    },
                    {
                        "event_id": "run-a:0002",
                        "run_id": "run-a",
                        "session_id": "run-a",
                        "timestamp": "2026-05-30T10:00:01+00:00",
                        "event_type": "run_finished",
                        "severity": "info",
                        "actor": "amof.chat",
                        "ticket_id": "AMOF-RUNS-CLI-001",
                        "planning_mode": "minimal_context",
                        "cost_status": "observed",
                        "estimated_cost": 0.000321,
                    },
                ],
            )

            with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                with patch.dict(os.environ, {"AMOF_HOME": str(run_home)}, clear=False):
                    code = cmd_runs(_base_args("list"))

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            output = stdout.getvalue()
            self.assertIn("run_id", output)
            self.assertIn("run-a", output)
            self.assertIn("observed/0.000321", output)

    def test_show_resolves_by_run_id(self) -> None:
        with TemporaryDirectory(prefix="amof-runs-show-") as td:
            run_home = Path(td)
            session_dir = run_home / "share" / "runs" / "chat-plans" / "run-b"
            events_path = session_dir / "events.jsonl"
            _write_events(
                events_path,
                [
                    {
                        "event_id": "run-b:0001",
                        "run_id": "run-b",
                        "session_id": "run-b",
                        "timestamp": "2026-05-30T10:00:00+00:00",
                        "event_type": "run_created",
                        "severity": "info",
                        "actor": "amof.chat",
                        "ticket_id": "AMOF-RUNS-CLI-001",
                        "planning_mode": "minimal_context",
                    },
                    {
                        "event_id": "run-b:0002",
                        "run_id": "run-b",
                        "session_id": "run-b",
                        "studio_session_id": "studio-20260608-004150",
                        "timestamp": "2026-05-30T10:00:01+00:00",
                        "event_type": "planning_context_receipt_written",
                        "severity": "info",
                        "actor": "amof.chat",
                        "receipt_ref": str(session_dir / "planning-context-receipt.json"),
                    },
                    {
                        "event_id": "run-b:0003",
                        "run_id": "run-b",
                        "session_id": "run-b",
                        "timestamp": "2026-05-30T10:00:02+00:00",
                        "event_type": "run_finished",
                        "severity": "info",
                        "actor": "amof.chat",
                        "cost_status": "unknown",
                        "estimated_cost": None,
                    },
                ],
            )
            (session_dir / "plan-result.json").write_text('{"ok": true}\n', encoding="utf-8")

            with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                with patch.dict(os.environ, {"AMOF_HOME": str(run_home)}, clear=False):
                    code = cmd_runs(_base_args("show", run_id="run-b"))

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            output = stdout.getvalue()
            self.assertIn("run_id: run-b", output)
            self.assertIn("studio_session_id: studio-20260608-004150", output)
            self.assertIn("cost_status: unknown", output)
            self.assertIn("estimated_cost: -", output)
            self.assertIn("output_path:", output)

    def test_logs_prints_ordered_events(self) -> None:
        with TemporaryDirectory(prefix="amof-runs-logs-") as td:
            run_home = Path(td)
            events_path = run_home / "share" / "runs" / "chat-plans" / "run-c" / "events.jsonl"
            _write_events(
                events_path,
                [
                    {
                        "event_id": "run-c:0001",
                        "run_id": "run-c",
                        "session_id": "run-c",
                        "timestamp": "2026-05-30T10:00:00+00:00",
                        "event_type": "run_created",
                        "severity": "info",
                        "actor": "amof.chat",
                    },
                    {
                        "event_id": "run-c:0002",
                        "run_id": "run-c",
                        "session_id": "run-c",
                        "timestamp": "2026-05-30T10:00:01+00:00",
                        "event_type": "ial_request_finished",
                        "severity": "info",
                        "actor": "amof.chat",
                        "cost_status": "observed",
                        "estimated_cost": 0.001234,
                    },
                    {
                        "event_id": "run-c:0003",
                        "run_id": "run-c",
                        "session_id": "run-c",
                        "timestamp": "2026-05-30T10:00:02+00:00",
                        "event_type": "run_finished",
                        "severity": "info",
                        "actor": "amof.chat",
                    },
                ],
            )

            with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                with patch.dict(os.environ, {"AMOF_HOME": str(run_home)}, clear=False):
                    code = cmd_runs(_base_args("logs", run_id="run-c", limit=0))

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
            self.assertEqual(len(lines), 3)
            self.assertIn("run_created", lines[0])
            self.assertIn("ial_request_finished", lines[1])
            self.assertIn("run_finished", lines[2])

    def test_missing_run_returns_clean_error(self) -> None:
        with TemporaryDirectory(prefix="amof-runs-missing-") as td:
            run_home = Path(td)
            with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                with patch.dict(os.environ, {"AMOF_HOME": str(run_home)}, clear=False):
                    code = cmd_runs(_base_args("show", run_id="does-not-exist"))

            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("[runs] run not found: does-not-exist", stderr.getvalue())

    def test_cost_status_unknown_does_not_render_fake_zero(self) -> None:
        with TemporaryDirectory(prefix="amof-runs-unknown-cost-") as td:
            run_home = Path(td)
            events_path = run_home / "share" / "runs" / "chat-plans" / "run-d" / "events.jsonl"
            _write_events(
                events_path,
                [
                    {
                        "event_id": "run-d:0001",
                        "run_id": "run-d",
                        "session_id": "run-d",
                        "timestamp": "2026-05-30T10:00:00+00:00",
                        "event_type": "run_created",
                        "severity": "info",
                        "actor": "amof.chat",
                    },
                    {
                        "event_id": "run-d:0002",
                        "run_id": "run-d",
                        "session_id": "run-d",
                        "timestamp": "2026-05-30T10:00:01+00:00",
                        "event_type": "ial_request_finished",
                        "severity": "info",
                        "actor": "amof.chat",
                        "cost_status": "unknown",
                        "estimated_cost": None,
                    },
                ],
            )

            with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                with patch.dict(os.environ, {"AMOF_HOME": str(run_home)}, clear=False):
                    code = cmd_runs(_base_args("list"))

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            output = stdout.getvalue()
            self.assertIn("unknown/-", output)
            self.assertNotIn("unknown/0.000000", output)

    def test_raw_provider_generation_id_is_not_printed(self) -> None:
        with TemporaryDirectory(prefix="amof-runs-redact-provider-id-") as td:
            run_home = Path(td)
            events_path = run_home / "share" / "runs" / "chat-plans" / "run-e" / "events.jsonl"
            _write_events(
                events_path,
                [
                    {
                        "event_id": "run-e:0001",
                        "run_id": "run-e",
                        "session_id": "run-e",
                        "timestamp": "2026-05-30T10:00:00+00:00",
                        "event_type": "ial_request_finished",
                        "severity": "info",
                        "actor": "amof.chat",
                        "cost_status": "observed",
                        "estimated_cost": 0.000121,
                        "provider_generation_id": "raw-provider-id",
                    }
                ],
            )

            with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                with patch.dict(os.environ, {"AMOF_HOME": str(run_home)}, clear=False):
                    code = cmd_runs(_base_args("logs", run_id="run-e", limit=0))

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn("provider_generation_id", stdout.getvalue())
            self.assertNotIn("raw-provider-id", stdout.getvalue())

    def test_legacy_aliases_do_not_break_reading(self) -> None:
        with TemporaryDirectory(prefix="amof-runs-legacy-") as td:
            run_home = Path(td)
            events_path = run_home / "share" / "runs" / "chat-plans" / "run-f" / "events.jsonl"
            _write_events(
                events_path,
                [
                    {
                        "ts": "2026-05-30T10:00:00+00:00",
                        "type": "session_start",
                        "session_id": "run-f",
                        "run_id": "run-f",
                        "ticket_id": "AMOF-LEGACY-001",
                    },
                    {
                        "ts": "2026-05-30T10:00:01+00:00",
                        "type": "llm_call",
                        "session_id": "run-f",
                        "run_id": "run-f",
                        "cost_status": "unknown",
                        "cost": None,
                    },
                ],
            )

            with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                with patch.dict(os.environ, {"AMOF_HOME": str(run_home)}, clear=False):
                    code = cmd_runs(_base_args("show", run_id="run-f"))

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            text = stdout.getvalue()
            self.assertIn("run_id: run-f", text)
            self.assertIn("ticket_id: AMOF-LEGACY-001", text)
            self.assertIn("cost_status: unknown", text)


if __name__ == "__main__":
    unittest.main()
