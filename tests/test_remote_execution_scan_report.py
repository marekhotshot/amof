from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.app_config import set_current_context_name
from amof.commands.execution import cmd_execution
from amof.commands.runner import cmd_runner


def _execution_args(execution_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "execution_cmd": execution_cmd,
        "intake_ref": None,
        "scan_id": None,
        "json": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _runner_args(runner_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "runner_cmd": runner_cmd,
        "file": None,
        "runner_id": None,
        "intake_ref": None,
        "json": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _run_execution_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_execution(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _run_runner_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_runner(args)
    return code, stdout.getvalue(), stderr.getvalue()


VALID_RUNNER = """\
runner_id: local-planning-runner
name: Local Planning Runner
context: local
status: available
capabilities:
  - intake.validate
  - intake.plan
  - execution.scan_report
supported_task_kinds:
  - other
allowed_mutation_modes:
  - read_only
max_concurrency: 1
labels:
  - local
  - planning
trust_level: local
registration_source: local_file
endpoint_ref: local-only
"""


VALID_INTAKE = """\
id: amof-execution-scan-test
version: "1.0.0"
kind: bounded_intake_task
ticket_id: AMOF-REMOTE-EXECUTION-SCAN-REPORT-001
rough_intent: Validate execution scan/report output.
bounded_goal: Build scan/report metadata only.
task_kind: other
repo_scope:
  - .
paths_to_inspect:
  - .
profile_ref: remote-ial-openrouter
mutations:
  allowed: []
  forbidden:
    - edit
    - deploy
    - promote
    - push
validation_gates:
  - name: read_only
    requirement: scan/report remains non-executable.
    failure_action: stop
cost_truth_policy:
  missing_cost_representation: unknown
"""


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _latest_scan_id(home: Path) -> str:
    scans_root = home / "share" / "execution-scans"
    candidates = sorted([item.name for item in scans_root.iterdir() if item.is_dir()])
    if not candidates:
        raise AssertionError("no execution scan directories found")
    return candidates[-1]


class RemoteExecutionScanReportTests(unittest.TestCase):
    def test_valid_intake_and_runner_produces_ready_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-execution-scan-ready-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)

                code, stdout, stderr = _run_execution_cmd(_execution_args("scan", intake_ref=str(intake_path), json=True))
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "ready")
            self.assertEqual(payload["outcome"], "NO_EXECUTION_PERFORMED")
            self.assertEqual(payload["ticket_id"], "AMOF-REMOTE-EXECUTION-SCAN-REPORT-001")
            self.assertGreaterEqual(len(payload["eligible_runners"]), 1)
            self.assertIn("report_path", payload)
            self.assertTrue(Path(payload["report_path"]).exists())
            self.assertTrue(Path(payload["events_path"]).exists())

    def test_invalid_intake_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-execution-scan-invalid-intake-") as td:
            home = Path(td) / "home"
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE.replace("kind: bounded_intake_task", "kind: wrong"))
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                code, _stdout, stderr = _run_execution_cmd(_execution_args("scan", intake_ref=str(intake_path)))
            self.assertEqual(code, 1)
            self.assertIn("intake validation failed", stderr)

    def test_no_runner_registry_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-execution-scan-no-registry-") as td:
            home = Path(td) / "home"
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, stderr = _run_execution_cmd(_execution_args("scan", intake_ref=str(intake_path)))
            self.assertEqual(code, 1)
            self.assertIn("runner registry not found", stderr)

    def test_no_eligible_runner_produces_blocked_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-execution-scan-blocked-no-eligible-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER.replace("execution.scan_report", "different.capability"))
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)
                code, stdout, stderr = _run_execution_cmd(_execution_args("scan", intake_ref=str(intake_path), json=True))
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["outcome"], "NO_EXECUTION_PERFORMED")
            self.assertIn("no eligible runner candidates", payload["blocked_reasons"])

    def test_context_mismatch_produces_blocked_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-execution-scan-context-mismatch-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE + "context: cloud-dev\n")
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)
                code, stdout, stderr = _run_execution_cmd(_execution_args("scan", intake_ref=str(intake_path), json=True))
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "blocked")
            self.assertTrue(any("active context is 'local'" in reason for reason in payload["blocked_reasons"]))

    def test_mutation_request_produces_blocked_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-execution-scan-mutation-blocked-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE.replace("allowed: []", "allowed:\n    - edit"))
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)
                code, stdout, stderr = _run_execution_cmd(_execution_args("scan", intake_ref=str(intake_path), json=True))
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "blocked")
            self.assertIn("intake is not planning-only read_only", payload["blocked_reasons"])

    def test_report_command_resolves_scan_and_shows_no_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-execution-report-show-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)
                code, _stdout, _stderr = _run_execution_cmd(_execution_args("scan", intake_ref=str(intake_path)))
                self.assertEqual(code, 0)
                scan_id = _latest_scan_id(home)
                code, stdout, stderr = _run_execution_cmd(_execution_args("report", scan_id=scan_id))
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("outcome: NO_EXECUTION_PERFORMED", stdout)

    def test_scan_storage_paths_are_under_amof_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-execution-scan-storage-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)
                code, stdout, _stderr = _run_execution_cmd(_execution_args("scan", intake_ref=str(intake_path), json=True))
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertTrue(str(payload["report_path"]).startswith(str(home)))
            self.assertTrue(str(payload["events_path"]).startswith(str(home)))


if __name__ == "__main__":
    unittest.main()
