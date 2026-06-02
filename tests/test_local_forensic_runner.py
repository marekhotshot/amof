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

from amof.commands.runner import (  # noqa: E402
    LOCAL_FORENSIC_COMMAND_PACK,
    RunnerCliError,
    _execute_local_forensic_command,
    cmd_runner,
)


def _runner_args(runner_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "runner_cmd": runner_cmd,
        "intake_ref": None,
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


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "amof-test@example.invalid"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "AMOF Test"], cwd=str(path), check=True)
    _write(path / "src.ts", "const primaryImage = '/uploads/products/example.png';\nconst DATA_ROOT = '/data/uploads';\n")
    subprocess.run(["git", "add", "src.ts"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "test fixture"], cwd=str(path), check=True, capture_output=True, text=True)


def _valid_intake(repo_path: Path) -> str:
    return f"""\
id: local-forensic-test
version: intake/v1
kind: bounded_intake_task
ticket_id: AMOF-RUNNER-LOCAL-FORENSIC-EXECUTOR-001
rough_intent: Run local read-only forensic evidence capture.
bounded_goal: Capture repository image/static-serving evidence without mutation.
task_kind: repo_runtime_adoption
repo_scope:
  - {repo_path}
paths_to_inspect:
  - {repo_path}
profile_ref: cloud-dev
mutations:
  allowed: []
  forbidden:
    - secrets
    - production mutation
    - DB writes
    - migrations
    - DNS changes
    - redeploy
    - restart
validation_gates:
  - name: read_only
    requirement: read-only only
    failure_action: stop
cost_truth_policy:
  missing_cost_representation: unknown
"""


class LocalForensicRunnerTests(unittest.TestCase):
    def test_valid_read_only_intake_creates_run_and_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-forensic-valid-") as td:
            root = Path(td)
            home = root / "home"
            repo = root / "repo"
            repo.mkdir()
            _init_git_repo(repo)
            intake_path = _write(root / "intake.yaml", _valid_intake(repo))

            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                code, stdout, stderr = _run_runner_cmd(
                    _runner_args("run-local-forensic", intake_ref=str(intake_path), json=True)
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["mutation_mode"], "read_only")
            self.assertEqual(payload["paths_inspected"], [str(repo.resolve(strict=False))])
            self.assertEqual(len(payload["commands_run"]), len(LOCAL_FORENSIC_COMMAND_PACK))
            self.assertTrue(Path(payload["report_path"]).exists())
            self.assertTrue(Path(payload["events_path"]).exists())
            self.assertTrue(Path(payload["run_path"]).exists())

    def test_intake_with_allowed_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-forensic-mutation-") as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            _init_git_repo(repo)
            intake_path = _write(root / "intake.yaml", _valid_intake(repo).replace("allowed: []", "allowed:\n    - edit"))

            code, _stdout, stderr = _run_runner_cmd(_runner_args("run-local-forensic", intake_ref=str(intake_path)))

            self.assertEqual(code, 1)
            self.assertIn("requires mutations.allowed == []", stderr)

    def test_missing_read_only_gate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-forensic-gate-") as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            _init_git_repo(repo)
            intake_path = _write(root / "intake.yaml", _valid_intake(repo).replace("name: read_only", "name: manual_review"))

            code, _stdout, stderr = _run_runner_cmd(_runner_args("run-local-forensic", intake_ref=str(intake_path)))

            self.assertEqual(code, 1)
            self.assertIn("requires validation gate named read_only", stderr)

    def test_missing_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-forensic-missing-path-") as td:
            root = Path(td)
            missing = root / "missing"
            intake_path = _write(root / "intake.yaml", _valid_intake(missing))

            code, _stdout, stderr = _run_runner_cmd(_runner_args("run-local-forensic", intake_ref=str(intake_path)))

            self.assertEqual(code, 1)
            self.assertIn("inspection path not found", stderr)

    def test_command_allowlist_cannot_be_bypassed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-forensic-allowlist-") as td:
            with self.assertRaises(RunnerCliError):
                _execute_local_forensic_command("kubectl get pods", cwd=Path(td))

    def test_report_contains_git_status_and_grep_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-forensic-report-") as td:
            root = Path(td)
            home = root / "home"
            repo = root / "repo"
            repo.mkdir()
            _init_git_repo(repo)
            intake_path = _write(root / "intake.yaml", _valid_intake(repo))

            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                code, stdout, _stderr = _run_runner_cmd(
                    _runner_args("run-local-forensic", intake_ref=str(intake_path), json=True)
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            report = Path(payload["report_path"]).read_text(encoding="utf-8")
            self.assertIn("git status --short", report)
            self.assertIn("git rev-parse HEAD", report)
            self.assertIn("DATA_ROOT", report)
            self.assertIn("/uploads/products/example.png", report)

    def test_no_kubectl_curl_or_db_commands_are_run(self) -> None:
        commands = [item.command for item in LOCAL_FORENSIC_COMMAND_PACK]
        joined = "\n".join(commands).lower()
        self.assertNotIn("kubectl", joined)
        self.assertNotIn("curl", joined)
        self.assertNotIn("psql", joined)
        self.assertNotIn("mysql", joined)
        self.assertNotIn("sqlite", joined)
        self.assertNotIn("db ", joined)


if __name__ == "__main__":
    unittest.main()
