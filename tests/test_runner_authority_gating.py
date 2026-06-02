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
from amof.commands.runner import cmd_runner


RUNNER_FIXTURE = Path("tests/fixtures/runner-authority-local.yaml")
INTAKE_FIXTURE = Path("tests/fixtures/intake-authority-bounded.yaml")
BOUNDED_AUTHORITY_ARTIFACT = Path("contracts/examples/intake-authority-evaluation-bounded-action.example.json")
RUNNER_MATCH_SAMPLE = ROOT / "contracts/examples/runner-authority-gating-bounded-match.example.json"


def _runner_args(runner_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "runner_cmd": runner_cmd,
        "file": None,
        "runner_id": None,
        "intake_ref": None,
        "kind": None,
        "json": False,
        "authority_artifact": None,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


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


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


class RunnerAuthorityGatingTests(unittest.TestCase):
    def test_allowed_bounded_path_emits_runner_and_tool_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-authority-allowed-") as td:
            home = Path(td) / "home"
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                register_code, _stdout, register_stderr = _run_runner_cmd(
                    _runner_args("register", file=str(RUNNER_FIXTURE))
                )
                self.assertEqual(register_code, 0)
                self.assertEqual(register_stderr, "")

                code, stdout, stderr = _run_runner_cmd(
                    _runner_args(
                        "match",
                        intake_ref=str(INTAKE_FIXTURE),
                        authority_artifact=str(BOUNDED_AUTHORITY_ARTIFACT),
                        json=True,
                    )
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload, json.loads(RUNNER_MATCH_SAMPLE.read_text(encoding="utf-8")))

    def test_privileged_missing_approval_rejects_match_with_machine_readable_blockers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-authority-privileged-") as td:
            home = Path(td) / "home"
            authority_path = _write_json(
                Path(td) / "authority.json",
                {
                    "decision_class": "escalate",
                    "rationale": "privileged_action requires explicit approval before tool eligibility",
                    "present_context_classes": ["operator_asserted"],
                    "eligible_tools": [],
                    "ineligible_tools": [{"tool_name": "Shell", "reason": "missing approval"}],
                    "blockers": ["privileged_action requires explicit approval before tool eligibility"],
                    "expected_evidence": ["approval_ref"],
                    "emitted_evidence_refs": [],
                },
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                self.assertEqual(_run_runner_cmd(_runner_args("register", file=str(RUNNER_FIXTURE)))[0], 0)
                code, stdout, stderr = _run_runner_cmd(
                    _runner_args(
                        "match",
                        intake_ref=str(INTAKE_FIXTURE),
                        authority_artifact=str(authority_path),
                        json=True,
                    )
                )

        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["candidate_count"], 0)
        self.assertFalse(payload["authority_gate"]["allowed"])
        self.assertEqual(payload["authority_gate"]["decision_class"], "escalate")
        self.assertEqual(
            payload["authority_gate"]["blockers"],
            ["privileged_action requires explicit approval before tool eligibility"],
        )
        self.assertEqual(payload["ineligible_candidates"][0]["authority_ineligible_tools"], ["Shell"])

    def test_incompatible_context_refusal_rejects_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-authority-refuse-") as td:
            home = Path(td) / "home"
            authority_path = _write_json(
                Path(td) / "authority.json",
                {
                    "decision_class": "refuse",
                    "rationale": "untrusted context cannot upgrade authority for action eligibility",
                    "present_context_classes": ["external_untrusted", "transcript_untrusted"],
                    "eligible_tools": [],
                    "ineligible_tools": [{"tool_name": "Read", "reason": "untrusted context"}],
                    "blockers": ["untrusted context cannot upgrade authority for action eligibility"],
                    "expected_evidence": [],
                    "emitted_evidence_refs": [],
                },
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                self.assertEqual(_run_runner_cmd(_runner_args("register", file=str(RUNNER_FIXTURE)))[0], 0)
                code, stdout, stderr = _run_runner_cmd(
                    _runner_args(
                        "match",
                        intake_ref=str(INTAKE_FIXTURE),
                        authority_artifact=str(authority_path),
                        json=True,
                    )
                )

        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["authority_gate"]["decision_class"], "refuse")
        self.assertIn("untrusted context", payload["authority_gate"]["blockers"][0])

    def test_match_consumes_authority_artifact_from_intake_submission_record(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-authority-submission-") as td:
            home = Path(td) / "home"
            authority_path = Path(td) / "authority" / "decision.json"
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                set_current_context_name("local")
                submit_code, _submit_stdout, submit_stderr = _run_intake_cmd(
                    _intake_args(
                        "submit",
                        file=str(INTAKE_FIXTURE),
                        authority_artifact=str(authority_path),
                    )
                )
                self.assertEqual(submit_code, 0)
                self.assertEqual(submit_stderr, "")
                self.assertEqual(_run_runner_cmd(_runner_args("register", file=str(RUNNER_FIXTURE)))[0], 0)

                code, stdout, stderr = _run_runner_cmd(
                    _runner_args(
                        "match",
                        intake_ref="amof-intake-authority-fixture",
                        json=True,
                    )
                )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["authority_gate"]["artifact_ref"], str(authority_path))
        self.assertEqual(payload["candidates"][0]["authority_evidence"]["authority_eligible_tools"], ["Read"])


if __name__ == "__main__":
    unittest.main()

