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

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.app_config import set_current_context_name
from amof.commands.execution import cmd_execution
from amof.commands.intake import cmd_intake
from amof.commands.runner import cmd_runner


def _runner_args(runner_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "runner_cmd": runner_cmd,
        "file": None,
        "runner_id": None,
        "intake_ref": None,
        "kind": None,
        "json": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _intake_args(intake_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {"intake_cmd": intake_cmd, "file": None, "intake_id": None, "json": False}
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _execution_args(execution_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "execution_cmd": execution_cmd,
        "intake_ref": None,
        "scan_id": None,
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


def _run_intake_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_intake(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _run_execution_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_execution(args)
    return code, stdout.getvalue(), stderr.getvalue()


VALID_RUNNER = """\
runner_id: local-planning-runner
name: Local Planning Runner
context: local
status: available
capabilities:
  - intake.validate
  - intake.plan
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
id: amof-runner-registration-intake
version: "1.0.0"
kind: bounded_intake_task
ticket_id: AMOF-RUNNER-REGISTRATION-001
rough_intent: Validate runner registration matching.
bounded_goal: Match read-only intake to local planning runner without dispatch.
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
    requirement: Intake remains planning-only.
    failure_action: stop
cost_truth_policy:
  missing_cost_representation: unknown
"""


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class RunnerRegistrationTests(unittest.TestCase):
    def test_template_outputs_valid_local_planning_runner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-template-") as td:
            home = Path(td) / "home"
            code, stdout, stderr = _run_runner_cmd(_runner_args("template", kind="local-planning"))
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")

            payload = yaml.safe_load(stdout)
            self.assertEqual(payload["runner_id"], "local-planning")
            self.assertEqual(payload["context"], "local")
            self.assertEqual(payload["allowed_mutation_modes"], ["read_only"])
            self.assertIn("intake.validate", payload["capabilities"])
            self.assertIn("intake.plan", payload["capabilities"])
            self.assertIn("execution.scan_report", payload["capabilities"])
            self.assertNotIn("endpoint_ref", payload)

            runner_path = _write(Path(td) / "runner.yaml", stdout)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                register_code, register_stdout, register_stderr = _run_runner_cmd(
                    _runner_args("register", file=str(runner_path))
                )
            self.assertEqual(register_code, 0)
            self.assertIn("REGISTERED runner_id=local-planning", register_stdout)
            self.assertEqual(register_stderr, "")

    def test_hermes_template_registers_and_doctor_reports_dispatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-hermes-template-") as td:
            home = Path(td) / "home"
            code, stdout, stderr = _run_runner_cmd(_runner_args("template", kind="hermes-opensandbox"))
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = yaml.safe_load(stdout)
            self.assertEqual(payload["backend"], "hermes_opensandbox")
            self.assertIn("bounded_worktree", payload["allowed_mutation_modes"])
            self.assertIn("bounded_write", payload["capabilities"])
            runner_path = _write(Path(td) / "hermes.yaml", stdout)
            health = {
                "dispatch_available": True,
                "runtime_health": "ready",
                "execution_endpoint": "/tmp/hermes",
                "process_identity": {"hermes_executable": "/tmp/hermes"},
                "cancellation_support": "timeout_process_termination",
                "log_event_support": "stdout_stderr_event_jsonl",
            }
            with (
                patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False),
                patch("amof.commands.runner.hermes_opensandbox.runtime_health", return_value=health),
            ):
                set_current_context_name("local")
                register_code, register_stdout, register_stderr = _run_runner_cmd(
                    _runner_args("register", file=str(runner_path))
                )
                self.assertEqual(register_code, 0)
                self.assertIn("backend=hermes_opensandbox", register_stdout)
                self.assertIn("dispatch_available=yes", register_stdout)
                self.assertEqual(register_stderr, "")
                doctor_code, doctor_stdout, doctor_stderr = _run_runner_cmd(_runner_args("doctor", json=True))

            self.assertEqual(doctor_code, 0)
            self.assertEqual(doctor_stderr, "")
            doctor = json.loads(doctor_stdout)
            self.assertEqual(doctor["dispatch"], "available")
            self.assertTrue(doctor["runners"][0]["dispatch_available"])
            self.assertEqual(doctor["runners"][0]["backend_type"], "hermes_opensandbox")

    def test_hermes_doctor_reports_unavailable_runtime_truthfully(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-hermes-unavailable-") as td:
            home = Path(td) / "home"
            code, stdout, _stderr = _run_runner_cmd(_runner_args("template", kind="hermes-opensandbox"))
            self.assertEqual(code, 0)
            runner_path = _write(Path(td) / "hermes.yaml", stdout)
            health = {
                "dispatch_available": False,
                "runtime_health": "unavailable",
                "execution_endpoint": "/missing/hermes",
                "process_identity": {"hermes_executable": "/missing/hermes"},
                "cancellation_support": "timeout_process_termination",
                "log_event_support": "stdout_stderr_event_jsonl",
            }
            with (
                patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False),
                patch("amof.commands.runner.hermes_opensandbox.runtime_health", return_value=health),
            ):
                set_current_context_name("local")
                register_code, _register_stdout, _register_stderr = _run_runner_cmd(
                    _runner_args("register", file=str(runner_path))
                )
                self.assertEqual(register_code, 0)
                doctor_code, doctor_stdout, doctor_stderr = _run_runner_cmd(_runner_args("doctor", json=True))

            self.assertEqual(doctor_code, 0)
            self.assertEqual(doctor_stderr, "")
            doctor = json.loads(doctor_stdout)
            self.assertEqual(doctor["dispatch"], "none")
            self.assertFalse(doctor["runners"][0]["dispatch_available"])
            self.assertEqual(doctor["runners"][0]["runtime_health"], "unavailable")

    def test_generated_intake_and_runner_template_match_and_scan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-template-dogfood-") as td:
            home = Path(td) / "home"
            runner_code, runner_stdout, runner_stderr = _run_runner_cmd(
                _runner_args("template", kind="local-planning")
            )
            self.assertEqual(runner_code, 0)
            self.assertEqual(runner_stderr, "")
            runner_path = _write(Path(td) / "runner.yaml", runner_stdout)

            intake_code, intake_stdout, intake_stderr = _run_intake_cmd(
                _intake_args("template", kind="bounded_intake_task")
            )
            self.assertEqual(intake_code, 0)
            self.assertEqual(intake_stderr, "")
            intake_path = _write(Path(td) / "intake.yaml", intake_stdout)

            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")

                code, stdout, stderr = _run_intake_cmd(_intake_args("validate", file=str(intake_path)))
                self.assertEqual(code, 0)
                self.assertIn("VALID intake_id=replace-me-intake-id", stdout)
                self.assertEqual(stderr, "")

                code, stdout, stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)
                self.assertIn("REGISTERED runner_id=local-planning", stdout)
                self.assertEqual(stderr, "")

                code, stdout, stderr = _run_runner_cmd(_runner_args("doctor"))
                self.assertEqual(code, 0)
                self.assertIn("RUNNER_REGISTRY_OK", stdout)
                self.assertEqual(stderr, "")

                code, stdout, stderr = _run_runner_cmd(_runner_args("list"))
                self.assertEqual(code, 0)
                self.assertIn("local-planning", stdout)
                self.assertEqual(stderr, "")

                code, stdout, stderr = _run_runner_cmd(_runner_args("match", intake_ref=str(intake_path)))
                self.assertEqual(code, 0)
                self.assertIn("candidates=1", stdout)
                self.assertIn("no_dispatch=yes", stdout)
                self.assertEqual(stderr, "")

                code, stdout, stderr = _run_execution_cmd(_execution_args("scan", intake_ref=str(intake_path)))
                self.assertEqual(code, 0)
                self.assertIn("outcome=NO_EXECUTION_PERFORMED", stdout)
                self.assertIn("status=ready", stdout)
                self.assertIn("eligible_runners=1", stdout)
                self.assertEqual(stderr, "")

    def test_register_valid_runner_passes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-register-valid-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, stdout, stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
            self.assertEqual(code, 0)
            self.assertIn("REGISTERED runner_id=local-planning-runner", stdout)
            self.assertIn("no_dispatch=yes", stdout)
            self.assertEqual(stderr, "")

    def test_register_missing_fields_fail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-register-missing-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER.replace("context: local\n", ""))
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                code, _stdout, stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
            self.assertEqual(code, 1)
            self.assertIn("missing required field: context", stderr)

    def test_register_rejects_secret_like_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-register-secret-") as td:
            home = Path(td) / "home"
            runner_path = _write(
                Path(td) / "runner.yaml",
                VALID_RUNNER + "api_key: sk-or-secret-should-not-pass\n",
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                code, _stdout, stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
            self.assertEqual(code, 1)
            self.assertIn("secret-like content is not allowed", stderr)

    def test_list_show_and_doctor_work(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-list-show-doctor-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)

                code, stdout, stderr = _run_runner_cmd(_runner_args("list"))
                self.assertEqual(code, 0)
                self.assertIn("local-planning-runner", stdout)
                self.assertIn("read_only", stdout)
                self.assertEqual(stderr, "")

                code, stdout, stderr = _run_runner_cmd(_runner_args("show", runner_id="local-planning-runner"))
                self.assertEqual(code, 0)
                self.assertIn("runner_id: local-planning-runner", stdout)
                self.assertIn("context: local", stdout)
                self.assertNotIn("sk-or-", stdout)
                self.assertEqual(stderr, "")

                code, stdout, stderr = _run_runner_cmd(_runner_args("doctor"))
                self.assertEqual(code, 0)
                self.assertIn("RUNNER_REGISTRY_OK", stdout)
                self.assertIn("dispatch=none", stdout)
                self.assertEqual(stderr, "")

    def test_match_planning_only_context_compatible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-match-ok-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)

                code, stdout, stderr = _run_runner_cmd(_runner_args("match", intake_ref=str(intake_path)))
                self.assertEqual(code, 0)
                self.assertIn("candidates=1", stdout)
                self.assertIn("planning_only=yes", stdout)
                self.assertIn("no_dispatch=yes", stdout)
                self.assertIn("no_remote_execution=yes", stdout)
                self.assertEqual(stderr, "")

    def test_match_rejects_mutating_intake(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-match-mutation-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(
                Path(td) / "intake.yaml",
                VALID_INTAKE.replace("allowed: []", "allowed:\n    - edit"),
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)

                code, _stdout, stderr = _run_runner_cmd(_runner_args("match", intake_ref=str(intake_path)))
                self.assertEqual(code, 1)
                self.assertIn("planning-only intake only", stderr)

    def test_match_fails_closed_for_unavailable_remote_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-match-fail-closed-") as td:
            home = Path(td) / "home"
            runner_path = _write(
                Path(td) / "runner.yaml",
                VALID_RUNNER.replace("context: local", "context: cloud-dev"),
            )
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(home),
                    "AMOF_REMOTE_IAL_BASE_URL": "",
                    "AMOF_REMOTE_IAL_API_KEY": "",
                },
                clear=False,
            ):
                set_current_context_name("cloud-dev")
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)
                code, _stdout, stderr = _run_runner_cmd(_runner_args("match", intake_ref=str(intake_path)))
            self.assertEqual(code, 1)
            self.assertIn("FAIL_CLOSED", stderr)
            self.assertIn("No silent fallback", stderr)

    def test_register_does_not_mutate_repo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-no-mutate-") as td:
            repo = Path(td) / "repo"
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
                cwd=repo,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            before = subprocess.run(["git", "status", "--short"], cwd=repo, check=True, capture_output=True, text=True).stdout

            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
            self.assertEqual(code, 0)
            after = subprocess.run(["git", "status", "--short"], cwd=repo, check=True, capture_output=True, text=True).stdout
            self.assertEqual(before, after)

    def test_match_accepts_intake_submission_id_reference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-match-intake-id-") as td:
            home = Path(td) / "home"
            runner_path = _write(Path(td) / "runner.yaml", VALID_RUNNER)
            intake_path = _write(Path(td) / "intake.yaml", VALID_INTAKE)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, _stderr = _run_intake_cmd(_intake_args("submit", file=str(intake_path)))
                self.assertEqual(code, 0)
                code, _stdout, _stderr = _run_runner_cmd(_runner_args("register", file=str(runner_path)))
                self.assertEqual(code, 0)
                code, stdout, stderr = _run_runner_cmd(_runner_args("match", intake_ref="amof-runner-registration-intake"))
            self.assertEqual(code, 0)
            self.assertIn("candidates=1", stdout)
            self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
