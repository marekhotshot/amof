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
from amof.commands.intake import cmd_intake
from amof.commands.runs import cmd_runs


def _intake_args(intake_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "intake_cmd": intake_cmd,
        "file": None,
        "intake_id": None,
        "json": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _run_intake_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_intake(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _run_runs_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_runs(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _runs_args(runs_cmd: str, **overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {"runs_cmd": runs_cmd, "json": False}
    payload.update(overrides)
    return SimpleNamespace(**payload)


VALID_PACKET = """\
id: amof-cli-intake-smoke
version: "1.0.0"
kind: bounded_intake_task
ticket_id: AMOF-CLI-INTAKE-001
rough_intent: Validate CLI intake MVP.
bounded_goal: Create a local no-mutation intake submission record.
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


def _write_packet(path: Path, content: str = VALID_PACKET) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class CliIntakeTests(unittest.TestCase):
    def test_validate_valid_packet_passes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-validate-pass-") as td:
            packet = _write_packet(Path(td) / "intake.yaml")
            with patch.dict(os.environ, {"AMOF_HOME": str(Path(td) / "home")}, clear=False):
                code, stdout, stderr = _run_intake_cmd(_intake_args("validate", file=str(packet)))
            self.assertEqual(code, 0)
            self.assertIn("VALID intake_id=amof-cli-intake-smoke", stdout)
            self.assertEqual(stderr, "")

    def test_validate_missing_field_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-validate-fail-") as td:
            packet = _write_packet(Path(td) / "intake.yaml", VALID_PACKET.replace("ticket_id: AMOF-CLI-INTAKE-001\n", ""))
            with patch.dict(os.environ, {"AMOF_HOME": str(Path(td) / "home")}, clear=False):
                code, _stdout, stderr = _run_intake_cmd(_intake_args("validate", file=str(packet)))
            self.assertEqual(code, 1)
            self.assertIn("missing required fields: ticket_id", stderr)

    def test_validate_reports_all_missing_required_top_level_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-validate-missing-many-") as td:
            packet = _write_packet(
                Path(td) / "intake.yaml",
                "kind: bounded_intake_task\n",
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(Path(td) / "home")}, clear=False):
                code, _stdout, stderr = _run_intake_cmd(_intake_args("validate", file=str(packet)))
            self.assertEqual(code, 1)
            self.assertIn(
                "missing required fields: id, version, ticket_id, rough_intent, bounded_goal, task_kind, repo_scope, paths_to_inspect, profile_ref, mutations, validation_gates, cost_truth_policy",
                stderr,
            )

    def test_validate_rejects_fake_zero_cost_policy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-validate-cost-") as td:
            packet = _write_packet(Path(td) / "intake.yaml", VALID_PACKET.replace("unknown", "0.0"))
            with patch.dict(os.environ, {"AMOF_HOME": str(Path(td) / "home")}, clear=False):
                code, _stdout, stderr = _run_intake_cmd(_intake_args("validate", file=str(packet)))
            self.assertEqual(code, 1)
            self.assertIn("cannot be 0.0", stderr)

    def test_template_outputs_minimal_valid_bounded_intake_task(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-template-") as td:
            home = Path(td) / "home"
            code, stdout, stderr = _run_intake_cmd(
                _intake_args("template", kind="bounded_intake_task")
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")

            packet = _write_packet(Path(td) / "template.yaml", stdout)
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                validate_code, validate_stdout, validate_stderr = _run_intake_cmd(
                    _intake_args("validate", file=str(packet))
                )

            self.assertEqual(validate_code, 0)
            self.assertIn("VALID intake_id=replace-me-intake-id", validate_stdout)
            self.assertEqual(validate_stderr, "")

    def test_submit_creates_record_and_runs_are_discoverable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-submit-pass-") as td:
            home = Path(td) / "home"
            packet = _write_packet(Path(td) / "intake.yaml")
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}, clear=False):
                use_code = 0
                try:
                    set_current_context_name("local")
                except Exception:
                    use_code = 1
                self.assertEqual(use_code, 0)

                code, stdout, stderr = _run_intake_cmd(_intake_args("submit", file=str(packet)))
                self.assertEqual(code, 0)
                self.assertIn("SUBMITTED intake_id=amof-cli-intake-smoke", stdout)
                self.assertEqual(stderr, "")

                code, stdout, stderr = _run_intake_cmd(_intake_args("list"))
                self.assertEqual(code, 0)
                self.assertIn("amof-cli-intake-smoke", stdout)
                self.assertIn("read_only", stdout)
                self.assertEqual(stderr, "")

                code, stdout, stderr = _run_intake_cmd(_intake_args("show", intake_id="amof-cli-intake-smoke"))
                self.assertEqual(code, 0)
                self.assertIn("intake_id: amof-cli-intake-smoke", stdout)
                self.assertIn("context: local", stdout)
                self.assertIn("validation_result: pass", stdout)
                self.assertEqual(stderr, "")

                code, stdout, stderr = _run_runs_cmd(_runs_args("list"))
                self.assertEqual(code, 0)
                self.assertIn("intake-", stdout)
                self.assertEqual(stderr, "")

                record_path = home / "share" / "intake" / "submissions" / "amof-cli-intake-smoke.json"
                self.assertTrue(record_path.exists())
                record_payload = json.loads(record_path.read_text(encoding="utf-8"))
                self.assertEqual(record_payload["mutation_mode"], "read_only")
                events_path = Path(record_payload["events_path"])
                self.assertTrue(events_path.exists())
                events_text = events_path.read_text(encoding="utf-8")
                self.assertIn('"intake_submitted"', events_text)
                self.assertIn('"intake_validated"', events_text)
                self.assertIn('"run_finished"', events_text)
                self.assertIn('"cost_status": "unknown"', events_text)
                self.assertIn('"cost": null', events_text)
                self.assertNotIn("0.0", events_text)

    def test_submit_fails_when_mutation_is_requested(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-submit-mutation-") as td:
            content = VALID_PACKET.replace("allowed: []", "allowed:\n    - edit")
            packet = _write_packet(Path(td) / "intake.yaml", content)
            with patch.dict(os.environ, {"AMOF_HOME": str(Path(td) / "home")}, clear=False):
                code, _stdout, stderr = _run_intake_cmd(_intake_args("submit", file=str(packet)))
            self.assertEqual(code, 1)
            self.assertIn("planning-only no-mutation", stderr)

    def test_submit_fails_closed_for_unavailable_remote_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-submit-fail-closed-") as td:
            home = Path(td) / "home"
            packet = _write_packet(Path(td) / "intake.yaml")
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
                code, _stdout, stderr = _run_intake_cmd(_intake_args("submit", file=str(packet)))
            self.assertEqual(code, 1)
            self.assertIn("FAIL_CLOSED", stderr)
            self.assertIn("No silent fallback", stderr)

    def test_remote_context_uses_remote_intake_api_for_submit_list_and_show(self) -> None:
        class FakeResponse:
            def __init__(self, status_code: int, payload: dict[str, object]) -> None:
                self.status_code = status_code
                self._payload = payload
                self.text = json.dumps(payload)

            def json(self) -> dict[str, object]:
                return self._payload

        with tempfile.TemporaryDirectory(prefix="amof-intake-submit-remote-") as td:
            home = Path(td) / "home"
            packet = _write_packet(Path(td) / "intake.yaml")
            detail_reads = 0
            calls: list[dict[str, object]] = []
            remote_summary = {
                "intake_id": "amof-cli-intake-smoke",
                "ticket_id": "AMOF-CLI-INTAKE-001",
                "status": "submitted",
                "context": "cloud-dev",
                "created_at": "2026-06-02T10:44:32.902438+00:00",
                "mutation_mode": "read_only",
                "validation": "pass",
                "source": "amof_cli",
                "intake_backend": "s3",
                "intake_s3_key": "clouddev/private-ial-intakes/2026/06/02/amof-cli-intake-smoke.json",
                "intake_upload_status": "uploaded",
            }

            def fake_request(
                method: str,
                url: str,
                headers: dict[str, str] | None = None,
                params: dict[str, object] | None = None,
                json: dict[str, object] | None = None,
                timeout: float | None = None,
            ) -> FakeResponse:
                del timeout
                calls.append(
                    {
                        "method": method,
                        "url": url,
                        "headers": dict(headers or {}),
                        "params": dict(params or {}),
                        "json": dict(json or {}),
                    }
                )
                self.assertEqual(headers and headers.get("Authorization"), "Bearer unit-test-token")
                if method == "GET" and url.endswith("/v1/ial/intakes/amof-cli-intake-smoke"):
                    nonlocal detail_reads
                    detail_reads += 1
                    if detail_reads == 1:
                        return FakeResponse(
                            404,
                            {
                                "detail": {
                                    "code": "ial_intake_not_found",
                                    "message": "Planning-only intake was not found for the requested intake_id.",
                                }
                            },
                        )
                    return FakeResponse(
                        200,
                        {
                            "intake_id": "amof-cli-intake-smoke",
                            "summary": remote_summary,
                            "object": {
                                "backend": "s3",
                                "s3_key": remote_summary["intake_s3_key"],
                                "object_url": None,
                            },
                            "source": {
                                "backend": "s3",
                                "configured_backend": "s3",
                                "fallback_used": False,
                            },
                        },
                    )
                if method == "POST" and url.endswith("/v1/ial/intakes"):
                    self.assertEqual(json and json.get("intake_id"), "amof-cli-intake-smoke")
                    self.assertEqual(json and json.get("ticket_id"), "AMOF-CLI-INTAKE-001")
                    self.assertEqual(json and json.get("context"), "cloud-dev")
                    self.assertEqual(json and json.get("source"), "amof_cli")
                    return FakeResponse(
                        200,
                        {
                            "intake_id": "amof-cli-intake-smoke",
                            "summary": remote_summary,
                            "source": {
                                "backend": "s3",
                                "configured_backend": "s3",
                                "fallback_used": False,
                            },
                        },
                    )
                if method == "GET" and url.endswith("/v1/ial/intakes"):
                    self.assertEqual(params, {"limit": 200})
                    return FakeResponse(
                        200,
                        {
                            "items": [remote_summary],
                            "count": 1,
                            "limit": 200,
                            "next_cursor": None,
                            "source": {
                                "backend": "s3",
                                "configured_backend": "s3",
                                "fallback_used": False,
                            },
                        },
                    )
                raise AssertionError(f"Unexpected request: {method} {url}")

            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(home),
                    "AMOF_REMOTE_IAL_BASE_URL": "https://ial.example.test",
                    "AMOF_REMOTE_IAL_API_KEY": "unit-test-token",
                },
                clear=False,
            ):
                set_current_context_name("cloud-dev")
                with patch("amof.commands.intake.requests.request", side_effect=fake_request):
                    code, stdout, stderr = _run_intake_cmd(_intake_args("submit", file=str(packet), json=True))
                    self.assertEqual(code, 0)
                    self.assertEqual(stderr, "")
                    submit_payload = json.loads(stdout)
                    self.assertEqual(submit_payload["intake_id"], "amof-cli-intake-smoke")
                    self.assertEqual(submit_payload["context"], "cloud-dev")
                    self.assertEqual(submit_payload["source"], "amof_cli")
                    self.assertEqual(submit_payload["intake_backend"], "s3")

                    code, stdout, stderr = _run_intake_cmd(_intake_args("list", json=True))
                    self.assertEqual(code, 0)
                    self.assertEqual(stderr, "")
                    list_payload = json.loads(stdout)
                    self.assertEqual([row["intake_id"] for row in list_payload], ["amof-cli-intake-smoke"])
                    self.assertEqual(list_payload[0]["run_id"], "")

                    code, stdout, stderr = _run_intake_cmd(_intake_args("show", intake_id="amof-cli-intake-smoke", json=True))
                    self.assertEqual(code, 0)
                    self.assertEqual(stderr, "")
                    show_payload = json.loads(stdout)
                    self.assertEqual(show_payload["intake_id"], "amof-cli-intake-smoke")
                    self.assertEqual(show_payload["source_backend"], "s3")
                    self.assertEqual(show_payload["intake_s3_key"], remote_summary["intake_s3_key"])

            record_path = home / "share" / "intake" / "submissions" / "amof-cli-intake-smoke.json"
            self.assertFalse(record_path.exists())
            self.assertEqual([call["method"] for call in calls], ["GET", "POST", "GET", "GET"])

    def test_submit_does_not_mutate_target_repo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-intake-submit-no-mutate-") as td:
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

            packet = _write_packet(Path(td) / "intake.yaml")
            with patch.dict(os.environ, {"AMOF_HOME": str(Path(td) / "home")}, clear=False):
                code, _stdout, _stderr = _run_intake_cmd(_intake_args("submit", file=str(packet)))
            self.assertEqual(code, 0)

            after = subprocess.run(["git", "status", "--short"], cwd=repo, check=True, capture_output=True, text=True).stdout
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
