from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import subprocess
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
from amof.commands.loop import cmd_loop
from amof.commands.runner import cmd_runner
from amof.commands.runs import cmd_runs


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
id: amof-long-run-loop-test
version: "1.0.0"
kind: bounded_intake_task
ticket_id: AMOF-300-LONG-RUN-BOUNDED-LOOPS-001
rough_intent: Validate bounded loop discipline.
bounded_goal: Run scan/report-only loop iterations with no mutation and no dispatch.
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
    requirement: Intake remains planning-only and scan/report only.
    failure_action: stop
cost_truth_policy:
  missing_cost_representation: unknown
"""


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


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


def _loop_args(loop_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "loop_cmd": loop_cmd,
        "intake_ref": None,
        "loop_run_id": None,
        "max_loops": None,
        "limit": 0,
        "json": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _runs_args(runs_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "runs_cmd": runs_cmd,
        "run_id": None,
        "limit": 0,
        "lines": 20,
        "follow": False,
        "poll_seconds": 0.01,
        "max_polls": 2,
        "json": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _run_runner_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_runner(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _run_loop_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_loop(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _run_runs_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_runs(args)
    return code, stdout.getvalue(), stderr.getvalue()


class LongRunBoundedLoopsTests(unittest.TestCase):
    def test_valid_intake_and_runner_runs_bounded_loop_to_max(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-loop-max-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)

                code, stdout, stderr = _run_loop_cmd(_loop_args("run", intake_ref=str(intake_path), max_loops=2, json=True))
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["loop_count"], 2)
            self.assertEqual(payload["max_loops"], 2)
            self.assertEqual(payload["stop_reason"], "max_loops_reached")
            self.assertEqual(payload["mutation_status"], "NO_MUTATION_PERFORMED")
            self.assertEqual(payload["dispatch_status"], "NO_REMOTE_EXECUTION_DISPATCHED")
            self.assertEqual(len(payload["scan_ids"]), 2)
            self.assertTrue(Path(payload["events_path"]).exists())
            self.assertTrue(Path(payload["reports_path"]).exists())
            self.assertTrue((Path(payload["reports_path"]) / payload["run_id"] / "report.json").exists())

    def test_show_includes_required_fixed_phrases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-loop-show-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                code, stdout, _stderr = _run_loop_cmd(_loop_args("run", intake_ref=str(intake_path), max_loops=1, json=True))
                payload = json.loads(stdout)
                loop_run_id = str(payload["run_id"])
                code, show_stdout, show_stderr = _run_loop_cmd(_loop_args("show", loop_run_id=loop_run_id))
            self.assertEqual(code, 0)
            self.assertEqual(show_stderr, "")
            self.assertIn("mutation_status: NO_MUTATION_PERFORMED", show_stdout)
            self.assertIn("dispatch_status: NO_REMOTE_EXECUTION_DISPATCHED", show_stdout)

    def test_logs_emit_per_iteration_events_and_scan_links(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-loop-logs-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                code, stdout, _stderr = _run_loop_cmd(_loop_args("run", intake_ref=str(intake_path), max_loops=2, json=True))
                payload = json.loads(stdout)
                loop_run_id = str(payload["run_id"])
                code, logs_stdout, logs_stderr = _run_loop_cmd(_loop_args("logs", loop_run_id=loop_run_id, json=True))
            self.assertEqual(code, 0)
            self.assertEqual(logs_stderr, "")
            events = json.loads(logs_stdout)
            started = [item for item in events if item.get("event_type") == "loop_iteration_started"]
            scan_created = [item for item in events if item.get("event_type") == "loop_execution_scan_created"]
            self.assertEqual(len(started), 2)
            self.assertEqual(len(scan_created), 2)
            self.assertEqual(payload["scan_ids"][0], scan_created[0]["scan_id"])
            self.assertEqual(payload["scan_ids"][1], scan_created[1]["scan_id"])
            for scan_id in payload["scan_ids"]:
                report_path = home / "share" / "execution-scans" / scan_id / "report.json"
                self.assertTrue(report_path.exists())
                report_payload = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report_payload.get("outcome"), "NO_EXECUTION_PERFORMED")

    def test_invalid_intake_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-loop-invalid-intake-") as td:
            home = Path(td) / "home"
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE.replace("kind: bounded_intake_task", "kind: wrong"))
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                code, _stdout, stderr = _run_loop_cmd(_loop_args("run", intake_ref=str(intake_path), max_loops=1))
            self.assertEqual(code, 1)
            self.assertIn("intake validation failed", stderr)

    def test_no_eligible_runner_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-loop-no-eligible-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER.replace("execution.scan_report", "different.capability"))
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                code, stdout, _stderr = _run_loop_cmd(_loop_args("run", intake_ref=str(intake_path), max_loops=2, json=True))
            self.assertEqual(code, 1)
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["stop_reason"], "fail_closed_gate_triggered")
            self.assertEqual(payload["failure_class"], "runner_eligibility_failure")

    def test_mutation_request_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-loop-mutation-blocked-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE.replace("allowed: []", "allowed:\n    - edit"))
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                code, _stdout, stderr = _run_loop_cmd(_loop_args("run", intake_ref=str(intake_path), max_loops=1))
            self.assertEqual(code, 1)
            self.assertIn("planning-only intake only", stderr)

    def test_runs_cli_discovers_loop_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-loop-runs-discovery-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                code, stdout, _stderr = _run_loop_cmd(_loop_args("run", intake_ref=str(intake_path), max_loops=1, json=True))
                payload = json.loads(stdout)
                loop_run_id = str(payload["run_id"])
                code, runs_stdout, runs_stderr = _run_runs_cmd(_runs_args("list", json=True))
            self.assertEqual(code, 0)
            self.assertEqual(runs_stderr, "")
            listed = json.loads(runs_stdout)
            run_ids = {str(item.get("run_id") or "") for item in listed}
            self.assertIn(loop_run_id, run_ids)

    def test_loop_run_does_not_mutate_repo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-loop-repo-no-mutation-") as td:
            home = Path(td) / "home"
            repo_path = Path(td) / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True, text=True)
            (repo_path / "README.md").write_text("loop smoke\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, capture_output=True, text=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=amof-test",
                    "-c",
                    "user.email=amof-test@example.invalid",
                    "commit",
                    "-m",
                    "init",
                ],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
            )

            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                code, _stdout, _stderr = _run_loop_cmd(_loop_args("run", intake_ref=str(intake_path), max_loops=1))
            self.assertEqual(code, 0)
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
