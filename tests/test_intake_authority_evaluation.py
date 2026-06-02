from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.app_config import set_current_context_name
from amof.commands.intake import cmd_intake


FIXTURE_INTAKE = Path("tests/fixtures/intake-authority-bounded.yaml")
SAMPLE_ARTIFACT = ROOT / "contracts/examples/intake-authority-evaluation-bounded-action.example.json"


def _intake_args(intake_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "intake_cmd": intake_cmd,
        "file": None,
        "intake_id": None,
        "json": False,
        "authority_json": False,
        "authority_artifact": None,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _run_intake_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_intake(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _write_packet(path: Path, *, authority: str) -> Path:
    path.write_text(
        f"""\
id: amof-intake-authority-test
version: "1.0.0"
kind: bounded_intake_task
ticket_id: AMOF-INTAKE-AUTHORITY-EVALUATION-001
rough_intent: Exercise authority evaluation.
bounded_goal: Produce authority artifact only.
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
authority:
{authority}
""",
        encoding="utf-8",
    )
    return path


class IntakeAuthorityEvaluationTests(unittest.TestCase):
    def test_default_validate_does_not_invoke_optional_authority_evaluation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-authority-default-") as td:
            packet = _write_packet(
                Path(td) / "intake.yaml",
                authority="  []\n",
            )
            code, stdout, stderr = _run_intake_cmd(_intake_args("validate", file=str(packet)))

        self.assertEqual(code, 0)
        self.assertIn("VALID intake_id=amof-intake-authority-test", stdout)
        self.assertEqual(stderr, "")

    def test_validate_authority_json_matches_sample_artifact_from_fixture(self) -> None:
        code, stdout, stderr = _run_intake_cmd(
            _intake_args("validate", file=str(FIXTURE_INTAKE), authority_json=True)
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), json.loads(SAMPLE_ARTIFACT.read_text(encoding="utf-8")))

    def test_submit_persists_authority_artifact_and_records_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-authority-submit-") as td:
            home = Path(td) / "home"
            artifact_path = Path(td) / "authority" / "decision.json"
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, stdout, stderr = _run_intake_cmd(
                    _intake_args(
                        "submit",
                        file=str(FIXTURE_INTAKE),
                        authority_artifact=str(artifact_path),
                    )
                )

            self.assertEqual(code, 0)
            self.assertIn("SUBMITTED intake_id=amof-intake-authority-fixture", stdout)
            self.assertEqual(stderr, "")
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["decision_class"], "bounded_action")
            self.assertEqual([item["tool_name"] for item in artifact["eligible_tools"]], ["Read"])
            self.assertEqual([item["tool_name"] for item in artifact["ineligible_tools"]], ["Shell", "MissingTool"])

            record_path = home / "share" / "intake" / "submissions" / "amof-intake-authority-fixture.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["authority_decision_path"], str(artifact_path))

    def test_submit_escalation_is_explicit_and_persists_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-authority-escalate-") as td:
            home = Path(td) / "home"
            packet = _write_packet(
                Path(td) / "intake.yaml",
                authority="""\
  decision_class: privileged_action
  rationale: Shell execution requested without approval.
  present_context_classes:
    - operator_asserted
  requested_tools:
    - Shell
""",
            )
            artifact_path = Path(td) / "authority.json"
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                code, _stdout, stderr = _run_intake_cmd(
                    _intake_args(
                        "submit",
                        file=str(packet),
                        authority_artifact=str(artifact_path),
                    )
                )

            self.assertEqual(code, 1)
            self.assertIn("authority decision blocked submit: escalate", stderr)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["decision_class"], "escalate")
            self.assertEqual(artifact["blockers"], ["privileged_action requires explicit approval before tool eligibility"])

    def test_validate_refusal_is_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-authority-refuse-") as td:
            packet = _write_packet(
                Path(td) / "intake.yaml",
                authority="""\
  decision_class: bounded_action
  present_context_classes:
    - external_untrusted
    - transcript_untrusted
  requested_tools:
    - Read
""",
            )
            code, stdout, stderr = _run_intake_cmd(
                _intake_args("validate", file=str(packet), authority_json=True)
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        artifact = json.loads(stdout)
        self.assertEqual(artifact["decision_class"], "refuse")
        self.assertEqual(artifact["eligible_tools"], [])
        self.assertIn("untrusted context cannot upgrade authority", artifact["blockers"][0])

    def test_validate_answer_only_avoids_execution_selection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-authority-answer-") as td:
            packet = _write_packet(
                Path(td) / "intake.yaml",
                authority="""\
  decision_class: answer_only
  rationale: Operator asked for explanation only.
  present_context_classes:
    - operator_asserted
  requested_tools:
    - Read
    - Shell
""",
            )
            code, stdout, stderr = _run_intake_cmd(
                _intake_args("validate", file=str(packet), authority_json=True)
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        artifact = json.loads(stdout)
        self.assertEqual(artifact["decision_class"], "answer_only")
        self.assertEqual(artifact["eligible_tools"], [])
        self.assertEqual([item["tool_name"] for item in artifact["ineligible_tools"]], ["Read", "Shell"])
        self.assertIn("avoids execution tool selection", artifact["ineligible_tools"][1]["reason"])


if __name__ == "__main__":
    unittest.main()

