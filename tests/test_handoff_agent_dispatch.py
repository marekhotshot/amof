from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import amof.entrypoint as entrypoint
from amof.commands import agent_cmd, handoff
from amof.commands import studio as studio_cmd


def _execute_args(**overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "command": "handoff",
        "handoff_cmd": "execute-agent",
        "ecosystem": "demo-repo",
        "handoff_id": "handoff-test-001",
        "preview": True,
        "confirm": False,
        "provider": None,
        "model": None,
        "planner_model": None,
        "budget": None,
        "budget_strict": False,
        "subtask_budget": None,
        "approve_capabilities": None,
        "approve_tool_packs": None,
        "approve_writable_roots": None,
        "runner_id": None,
        "runner_timeout_seconds": 900,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _run_execute(args: SimpleNamespace, amof_home: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = handoff.cmd_handoff_execute_agent(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _accept_args(**overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "command": "handoff",
        "handoff_cmd": "accept-agent",
        "handoff_id": "handoff-test-001",
        "confirm": True,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _run_accept(args: SimpleNamespace, amof_home: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = handoff.cmd_handoff_accept_agent(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _status_args(**overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "command": "handoff",
        "handoff_cmd": "status",
        "handoff_id": "handoff-test-001",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _run_status(args: SimpleNamespace, amof_home: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = handoff.cmd_handoff_status(args)
    return code, stdout.getvalue(), stderr.getvalue()


@contextmanager
def _cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _commit_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "AMOF Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "AMOF Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return env


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "test: init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=_commit_env(),
    )


class _FakeRunnerAgent:
    instances: list["_FakeRunnerAgent"] = []
    stop_reason = "completed"

    def __init__(self, *args, **kwargs) -> None:
        self.llm = kwargs.get("llm") or (args[0] if args else None)
        self.model_router = kwargs.get("model_router")
        self.tools = kwargs.get("tools")
        self.__class__.instances.append(self)

    def run(self, goal: str) -> str:
        return f"fake agent response for {goal}"


def _write_packet(
    amof_home: Path,
    *,
    handoff_id: str = "handoff-test-001",
    target: str = "amof-agent",
    text: str = "Execute this bounded goal.",
    studio_session_id: str | None = None,
) -> Path:
    payload = handoff.PreparedPayload(
        text=text,
        character_count=len(text),
        utf8_byte_count=len(text.encode("utf-8")),
        sha256=handoff.hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    packet = handoff.PreparedHandoffPacket(
        schema_version=1,
        handoff_id=handoff_id,
        source="chatgpt",
        target=target,
        studio_session_id=studio_session_id,
        payload_kind="selected_text",
        payload=payload,
        state="prepared",
    )
    with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
        return handoff._write_packet(packet)


def _write_runner_record(amof_home: Path, runner_id: str = "hermes-local-ticket-write", *, backend: str = "hermes_opensandbox") -> Path:
    path = amof_home / "share" / "runners" / "registry" / f"{runner_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "runner_id": runner_id,
                "name": "Hermes Local Ticket Write",
                "context": "local",
                "status": "available",
                "backend": backend,
                "capabilities": ["intake.validate", "intake.plan", "execution.scan_report", "read", "bounded_write"],
                "supported_task_kinds": ["other"],
                "allowed_mutation_modes": ["read_only", "bounded_worktree"],
                "max_concurrency": 1,
                "trust_level": "local",
                "registration_source": "test",
                "endpoint_ref": "hermes-local",
                "authority": {"writable_roots_required": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _correlation_envelope(
    *,
    request_id: str = "handoff-test-001",
    status: str = "completed",
    exit_code: int = 0,
    stop_reason: str = "completed",
    session_id: str = "session-1",
    studio_session_id: str | None = None,
    plan_path: str | None = "/tmp/plan.md",
    checkpoint_path: str | None = None,
    event_log_path: str | None = "/tmp/events.jsonl",
    journal_path: str | None = "/tmp/journal.md",
) -> agent_cmd.AgentPlanExecuteCorrelationEnvelope:
    return agent_cmd.AgentPlanExecuteCorrelationEnvelope(
        schema_version=1,
        request_id=request_id,
        result={
            "schema_version": 1,
            "status": status,
            "session_id": session_id,
            "exit_code": exit_code,
            "stop_reason": stop_reason,
            "final_text": f"{status} result",
            "plan_path": plan_path,
            "checkpoint_path": checkpoint_path,
            "event_log_path": event_log_path,
            "journal_path": journal_path,
            "budget_summary": {"limit": 1.0, "spent": 0.0, "remaining": 1.0},
            **(
                {"studio_session_id": studio_session_id}
                if studio_session_id is not None
                else {}
            ),
        },
    )


class HandoffAgentDispatchTests(unittest.TestCase):
    def _manifest(self, repo: Path) -> dict[str, object]:
        return {
            "ecosystem": "demo-repo",
            "manifest_source": "appdata",
            "repos": [
                {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
            ],
        }

    def _write_plan(self, plan_file: Path) -> None:
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(
            (
                "# Execution Plan\n\n"
                "**Status**: pending\n\n"
                "## Analysis\n\n"
                "Inspect the repository without mutating it.\n\n"
                "---\n\n"
                "## Tasks\n\n"
                "- [ ] 1. **Inspect the repo** (code)\n"
            ),
            encoding="utf-8",
        )

    def _create_studio_session(self) -> str:
        payload = studio_cmd._create_studio_session()
        return str(payload["manifest"]["studio_session_id"])

    def test_valid_prepared_amof_agent_packet_executes_and_maps_goal_and_request_id(
        self,
    ) -> None:
        captured: dict[str, object] = {}

        def _fake_runtime(
            manifest: dict[str, object],
            payload: dict[str, object],
            *,
            studio_session_id: str | None = None,
        ):
            captured["manifest"] = manifest
            captured["payload"] = payload
            captured["studio_session_id"] = studio_session_id
            return _correlation_envelope(studio_session_id=studio_session_id)

        with TemporaryDirectory(prefix="amof-handoff-dispatch-success-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with (
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    side_effect=_fake_runtime,
                ),
            ):
                code, stdout, stderr = _run_execute(
                    _execute_args(confirm=True), amof_home
                )

            receipt = json.loads(stdout)
            payload = captured["payload"]
            assert isinstance(payload, dict)
            self.assertEqual(code, 0)
            self.assertEqual(payload["goal"], "Execute this bounded goal.")
            self.assertEqual(payload["request_id"], "handoff-test-001")
            self.assertEqual(payload["mode"], "plan-execute")
            self.assertTrue(payload["no_follow_up"])
            self.assertIsNone(captured["studio_session_id"])
            self.assertIn("[handoff] Execute-agent preview", stderr)
            self.assertEqual(receipt["handoff_id"], "handoff-test-001")
            self.assertEqual(receipt["request_id"], "handoff-test-001")
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["session_id"], "session-1")

    def test_explicit_hermes_runner_dispatches_backend_and_not_builtin(self) -> None:
        captured: dict[str, object] = {}

        def _fake_hermes(**kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            selection = kwargs["selection"]
            return {
                "schema_version": 1,
                "result_kind": "agent_run_result",
                "contract_version": "agent-run-v1",
                "status": "completed",
                "session_id": "hermes-session-1",
                "exit_code": 0,
                "stop_reason": "completed",
                "final_text": "hermes ok",
                "runner_id": selection.runner_id,
                "backend": "hermes_opensandbox",
                "plan_path": None,
                "checkpoint_path": None,
                "event_log_path": "/tmp/hermes-events.jsonl",
                "runtime_log_path": "/tmp/hermes-runtime.log",
                "journal_path": None,
                "changed_paths": [],
                "validation_summary": {"status": "not_run"},
                "approved_capabilities": list(selection.capabilities),
                "effective_capabilities": list(selection.capabilities),
                "evidence_refs": {},
                "budget_summary": {"limit": None, "spent": 0.0, "remaining": None},
            }

        with TemporaryDirectory(prefix="amof-handoff-hermes-selected-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            _write_runner_record(amof_home)
            with (
                patch("amof.commands.handoff._load_execution_manifest", return_value={"ecosystem": "demo-repo", "repos": []}),
                patch("amof.commands.handoff.hermes_opensandbox.run", side_effect=_fake_hermes),
                patch("amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope") as builtin,
            ):
                code, stdout, stderr = _run_execute(
                    _execute_args(confirm=True, runner_id="hermes-local-ticket-write"), amof_home
                )

            receipt = json.loads(stdout)
            builtin.assert_not_called()
            self.assertEqual(code, 0)
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["session_id"], "hermes-session-1")
            self.assertIn("runtime_log_path", receipt["evidence"])
            self.assertEqual(captured["request_id"], "handoff-test-001")
            self.assertIn("selected_execution_configuration", stderr)

    def test_explicit_unsupported_runner_fails_closed_without_builtin_substitution(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-runner-fail-closed-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            _write_runner_record(amof_home, backend="planning_only")
            with (
                patch("amof.commands.handoff._load_execution_manifest", return_value={"ecosystem": "demo-repo", "repos": []}),
                patch("amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope") as builtin,
            ):
                code, stdout, _stderr = _run_execute(
                    _execute_args(confirm=True, runner_id="hermes-local-ticket-write"), amof_home
                )

            receipt = json.loads(stdout)
            result = json.loads(Path(receipt["result_path"]).read_text(encoding="utf-8"))
            builtin.assert_not_called()
            self.assertEqual(code, 1)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["stop_reason"], "selected_runner_dispatch_failed")
            self.assertIn("does not provide dispatch backend", result["final_text"])

    def test_explicit_hermes_read_only_maps_only_read_capability(self) -> None:
        captured: dict[str, object] = {}

        def _fake_hermes(**kwargs: object) -> dict[str, object]:
            selection = kwargs["selection"]
            captured["capabilities"] = list(selection.capabilities)
            result = _correlation_envelope(session_id="ignored").result
            result.update(
                {
                    "session_id": "hermes-read-only",
                    "runner_id": selection.runner_id,
                    "backend": "hermes_opensandbox",
                    "runtime_log_path": "/tmp/runtime.log",
                    "changed_paths": [],
                    "validation_summary": {},
                    "approved_capabilities": list(selection.capabilities),
                    "effective_capabilities": list(selection.capabilities),
                    "evidence_refs": {},
                }
            )
            return result

        with TemporaryDirectory(prefix="amof-handoff-hermes-readonly-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            _write_runner_record(amof_home)
            with (
                patch("amof.commands.handoff._load_execution_manifest", return_value={"ecosystem": "demo-repo", "repos": []}),
                patch("amof.commands.handoff.hermes_opensandbox.run", side_effect=_fake_hermes),
            ):
                code, _stdout, _stderr = _run_execute(
                    _execute_args(confirm=True, runner_id="hermes-local-ticket-write"), amof_home
                )
            self.assertEqual(code, 0)
            self.assertEqual(captured["capabilities"], ["read"])

    def test_explicit_hermes_bounded_write_requires_writable_root(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-hermes-write-root-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            _write_runner_record(amof_home)
            with (
                patch("amof.commands.handoff._load_execution_manifest", return_value={"ecosystem": "demo-repo", "repos": []}),
                patch("amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope") as builtin,
            ):
                code, stdout, _stderr = _run_execute(
                    _execute_args(
                        confirm=True,
                        runner_id="hermes-local-ticket-write",
                        approve_capabilities=["bounded_write"],
                    ),
                    amof_home,
                )
            receipt = json.loads(stdout)
            result = json.loads(Path(receipt["result_path"]).read_text(encoding="utf-8"))
            builtin.assert_not_called()
            self.assertEqual(code, 1)
            self.assertIn("require at least one explicit writable root", result["final_text"])

    def test_explicit_hermes_dangerous_capability_fails_closed(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-hermes-dangers-fail-closed-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            _write_runner_record(amof_home)
            with (
                patch("amof.commands.handoff._load_execution_manifest", return_value={"ecosystem": "demo-repo", "repos": []}),
                patch("amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope") as builtin,
            ):
                code, stdout, _stderr = _run_execute(
                    _execute_args(
                        confirm=True,
                        runner_id="hermes-local-ticket-write",
                        approve_capabilities=["secrets"],
                    ),
                    amof_home,
                )
            receipt = json.loads(stdout)
            result = json.loads(Path(receipt["result_path"]).read_text(encoding="utf-8"))
            builtin.assert_not_called()
            self.assertEqual(code, 1)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["stop_reason"], "selected_runner_dispatch_failed")
            self.assertIn("dangerous capabilities", result["final_text"])

    def test_explicit_hermes_preserves_studio_session_id(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-hermes-studio-") as td:
            amof_home = Path(td)
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                studio_session_id = self._create_studio_session()
            _write_packet(amof_home, studio_session_id=studio_session_id)
            _write_runner_record(amof_home)

            def _fake_hermes(**kwargs: object) -> dict[str, object]:
                result = _correlation_envelope(
                    session_id="hermes-studio",
                    studio_session_id=kwargs["studio_session_id"],
                    event_log_path="/tmp/hermes-events.jsonl",
                ).result
                result.update(
                    {
                        "runner_id": "hermes-local-ticket-write",
                        "backend": "hermes_opensandbox",
                        "runtime_log_path": "/tmp/hermes-runtime.log",
                        "changed_paths": [],
                        "validation_summary": {},
                        "approved_capabilities": ["read"],
                        "effective_capabilities": ["read"],
                        "evidence_refs": {},
                    }
                )
                return result

            with (
                patch("amof.commands.handoff._load_execution_manifest", return_value={"ecosystem": "demo-repo", "repos": []}),
                patch("amof.commands.handoff.hermes_opensandbox.run", side_effect=_fake_hermes),
            ):
                code, stdout, _stderr = _run_execute(
                    _execute_args(confirm=True, runner_id="hermes-local-ticket-write"), amof_home
                )
            receipt = json.loads(stdout)
            result = json.loads(Path(receipt["result_path"]).read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(receipt["studio_session_id"], studio_session_id)
            self.assertEqual(result["studio_session_id"], studio_session_id)

    def test_accept_agent_writes_accepted_state_and_tracking_ref(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-accept-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            code, stdout, stderr = _run_accept(_accept_args(confirm=True), amof_home)
            state = json.loads(
                (
                    amof_home / "share" / "handoff" / "state" / "handoff-test-001.json"
                ).read_text(encoding="utf-8")
            )

        payload = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["tracking_ref"], "handoff-test-001")
        self.assertEqual(state["status"], "accepted")

    def test_status_projects_planning_waiting_executing_and_result_missing(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-status-projection-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            run_id = "hermes-20260611-000001-handoff-test-001"
            run_dir = amof_home / "share" / "runs" / "hermes-opensandbox" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "request.json").write_text(
                json.dumps({"request_id": "handoff-test-001"}) + "\n",
                encoding="utf-8",
            )
            (run_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "event": "run_created",
                        "timestamp": "2099-06-11T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                handoff._write_execution_state(
                    handoff.HandoffExecutionState(
                        schema_version=1,
                        handoff_id="handoff-test-001",
                        status="execution_started",
                        request_id="handoff-test-001",
                        updated_at="2099-06-11T00:00:00Z",
                    )
                )

            code, stdout, _stderr = _run_status(_status_args(), amof_home)
            planning = json.loads(stdout)

            (run_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "event": "run_created",
                        "timestamp": "2020-06-11T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            code_waiting, stdout_waiting, _stderr_waiting = _run_status(
                _status_args(), amof_home
            )
            waiting = json.loads(stdout_waiting)

            (run_dir / "runtime.log").write_text("runtime output\n", encoding="utf-8")
            code_executing, stdout_executing, _stderr_executing = _run_status(
                _status_args(), amof_home
            )
            executing = json.loads(stdout_executing)

            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                handoff._write_execution_state(
                    handoff.HandoffExecutionState(
                        schema_version=1,
                        handoff_id="handoff-test-001",
                        status="failed",
                        request_id="handoff-test-001",
                        updated_at="2026-06-11T00:01:00Z",
                    )
                )
            code_missing, stdout_missing, _stderr_missing = _run_status(
                _status_args(), amof_home
            )
            missing = json.loads(stdout_missing)

        self.assertEqual(code, 0)
        self.assertEqual(planning["status"], "planning")
        self.assertEqual(code_waiting, 0)
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(code_executing, 0)
        self.assertEqual(executing["status"], "executing")
        self.assertEqual(code_missing, 0)
        self.assertEqual(missing["status"], "result_missing")
        self.assertEqual(missing["failure_classification"], "result_missing")

    def test_execute_agent_after_accept_agent_is_allowed(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-accept-then-execute-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            accept_code, _accept_stdout, _accept_stderr = _run_accept(
                _accept_args(confirm=True), amof_home
            )
            with (
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    return_value=_correlation_envelope(),
                ),
            ):
                execute_code, execute_stdout, _execute_stderr = _run_execute(
                    _execute_args(confirm=True), amof_home
                )

        receipt = json.loads(execute_stdout)
        self.assertEqual(accept_code, 0)
        self.assertEqual(execute_code, 0)
        self.assertEqual(receipt["status"], "completed")

    def test_preview_without_confirmation_invokes_nothing(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-preview-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with patch(
                "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope"
            ) as runtime_mock:
                code, stdout, stderr = _run_execute(
                    _execute_args(confirm=False), amof_home
                )

            self.assertEqual(code, 0)
            self.assertEqual(stdout, "")
            runtime_mock.assert_not_called()
            self.assertIn("Execute-agent preview", stderr)
            self.assertIn("no agent execution occurred", stderr)
            self.assertFalse((amof_home / "share" / "handoff" / "results").exists())

    def test_packet_studio_session_is_forwarded_and_persisted_in_result(self) -> None:
        studio_session_id = "studio-20260608-004150"
        captured: dict[str, object] = {}

        def _fake_runtime(
            manifest: dict[str, object],
            payload: dict[str, object],
            *,
            studio_session_id: str | None = None,
        ):
            captured["studio_session_id"] = studio_session_id
            return _correlation_envelope(studio_session_id=studio_session_id)

        with TemporaryDirectory(prefix="amof-handoff-dispatch-studio-field-") as td:
            amof_home = Path(td)
            _write_packet(amof_home, studio_session_id=studio_session_id)
            with (
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    side_effect=_fake_runtime,
                ),
            ):
                code, stdout, stderr = _run_execute(
                    _execute_args(confirm=True), amof_home
                )
            receipt = json.loads(stdout)
            result_payload = json.loads(
                Path(receipt["result_path"]).read_text(encoding="utf-8")
            )

        self.assertEqual(code, 0)
        self.assertEqual(captured["studio_session_id"], studio_session_id)
        self.assertEqual(receipt["studio_session_id"], studio_session_id)
        self.assertEqual(result_payload["studio_session_id"], studio_session_id)
        self.assertIn(f"studio_session_id: {studio_session_id}", stderr)

    def test_real_runtime_dispatch_reuses_existing_studio_attachment_path(self) -> None:
        from amof.orchestrator.planner import ExecutionPlan

        with tempfile.TemporaryDirectory(prefix="amof-handoff-dispatch-studio-real-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            self._write_plan(plan_file)
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                studio_session_id = self._create_studio_session()
                _write_packet(amof_home, studio_session_id=studio_session_id)
                _FakeRunnerAgent.instances.clear()
                with _cwd(repo):
                    with (
                        patch(
                            "amof.commands.handoff._load_execution_manifest",
                            return_value=self._manifest(repo),
                        ),
                        patch("amof.orchestrator.runners.Agent", _FakeRunnerAgent),
                        patch(
                            "amof.orchestrator.planner.TaskPlanner.plan",
                            return_value=ExecutionPlan.load_from_markdown(plan_file),
                        ),
                        patch.dict(
                            os.environ,
                            {"OPENROUTER_API_KEY": "unit-test-provider-value"},
                            clear=False,
                        ),
                    ):
                        code, stdout, _stderr = _run_execute(
                            _execute_args(confirm=True, provider="openrouter"),
                            amof_home,
                        )
                receipt = json.loads(stdout)
                result_payload = json.loads(
                    Path(receipt["result_path"]).read_text(encoding="utf-8")
                )
                studio_payload = studio_cmd._studio_payload(studio_session_id)
                studio_events = (
                    amof_home / "share" / "studio" / studio_session_id / "events.jsonl"
                ).read_text(encoding="utf-8")

        run_attached_events = [
            json.loads(line)
            for line in studio_events.splitlines()
            if line.strip() and json.loads(line).get("event_type") == "run.attached"
        ]
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["studio_session_id"], studio_session_id)
        self.assertEqual(result_payload["studio_session_id"], studio_session_id)
        self.assertEqual(studio_payload["summary"]["attached_runs_count"], 1)
        self.assertEqual(len(studio_payload["attached_runs"]), 1)
        self.assertEqual(len(run_attached_events), 1)
        self.assertEqual(
            studio_payload["attached_runs"][0]["studio_session_id"], studio_session_id
        )
        self.assertEqual(
            studio_payload["attached_runs"][0]["run_id"], result_payload["session_id"]
        )
        self.assertTrue(_FakeRunnerAgent.instances)

    def test_invalid_studio_session_fails_truthfully(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-handoff-dispatch-studio-invalid-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            _write_packet(amof_home, studio_session_id="studio-does-not-exist")
            with _cwd(repo):
                with (
                    patch(
                        "amof.commands.handoff._load_execution_manifest",
                        return_value=self._manifest(repo),
                    ),
                    patch(
                        "amof.orchestrator.llm.openai_client.OpenAIClient"
                    ) as openai_client,
                ):
                    code, stdout, _stderr = _run_execute(
                        _execute_args(confirm=True, provider="openrouter"),
                        amof_home,
                    )
            receipt = json.loads(stdout)
            result_payload = json.loads(
                Path(receipt["result_path"]).read_text(encoding="utf-8")
            )

        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["stop_reason"], "studio_session_invalid")
        self.assertEqual(receipt["studio_session_id"], "studio-does-not-exist")
        self.assertEqual(result_payload["studio_session_id"], "studio-does-not-exist")
        self.assertEqual(result_payload["session_id"], "")
        self.assertFalse(openai_client.called)

    def test_ended_studio_session_fails_truthfully(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-handoff-dispatch-studio-ended-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                studio_session_id = self._create_studio_session()
                studio_cmd._end_studio_session(studio_session_id)
            _write_packet(amof_home, studio_session_id=studio_session_id)
            with _cwd(repo):
                with (
                    patch(
                        "amof.commands.handoff._load_execution_manifest",
                        return_value=self._manifest(repo),
                    ),
                    patch(
                        "amof.orchestrator.llm.openai_client.OpenAIClient"
                    ) as openai_client,
                ):
                    code, stdout, _stderr = _run_execute(
                        _execute_args(confirm=True, provider="openrouter"),
                        amof_home,
                    )
            receipt = json.loads(stdout)
            result_payload = json.loads(
                Path(receipt["result_path"]).read_text(encoding="utf-8")
            )

        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["stop_reason"], "studio_session_invalid")
        self.assertEqual(receipt["studio_session_id"], studio_session_id)
        self.assertEqual(result_payload["studio_session_id"], studio_session_id)
        self.assertEqual(result_payload["session_id"], "")
        self.assertFalse(openai_client.called)

    def test_wrong_target_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-target-") as td:
            amof_home = Path(td)
            _write_packet(amof_home, target="zed")
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), amof_home)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("amof-agent", stderr)
        self.assertIn("zed", stderr)

    def test_missing_packet_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-missing-") as td:
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), Path(td))

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("not found", stderr)

    def test_malformed_packet_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-malformed-") as td:
            amof_home = Path(td)
            outbox = amof_home / "share" / "handoff" / "outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            (outbox / "handoff-test-001.json").write_text(
                '{"schema_version":1,"handoff_id":"handoff-test-001","source":"chatgpt","target":"amof-agent","payload_kind":"selected_text","state":"prepared"}\n',
                encoding="utf-8",
            )
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), amof_home)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("payload", stderr)

    def test_oversized_and_nul_payloads_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-oversized-") as td:
            amof_home = Path(td)
            huge = "a" * 40001
            _write_packet(amof_home, text=huge)
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), amof_home)
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("40000", stderr)

        with TemporaryDirectory(prefix="amof-handoff-dispatch-nul-") as td:
            amof_home = Path(td)
            outbox = _write_packet(amof_home)
            packet = json.loads(outbox.read_text(encoding="utf-8"))
            packet["payload"]["text"] = "abc\u0000def"
            outbox.write_text(json.dumps(packet) + "\n", encoding="utf-8")
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), amof_home)
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("NUL", stderr)

    def test_already_executed_packet_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-reexec-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                handoff._write_execution_state(
                    handoff.HandoffExecutionState(
                        schema_version=1,
                        handoff_id="handoff-test-001",
                        status="completed",
                        request_id="handoff-test-001",
                        updated_at="2026-06-07T00:00:00Z",
                        started_at="2026-06-07T00:00:00Z",
                        completed_at="2026-06-07T00:01:00Z",
                    )
                )
            code, stdout, stderr = _run_execute(_execute_args(confirm=True), amof_home)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("re-execution", stderr)

    def test_optional_canonical_execution_fields_map_correctly(self) -> None:
        captured: dict[str, object] = {}

        def _fake_runtime(
            manifest: dict[str, object],
            payload: dict[str, object],
            *,
            studio_session_id: str | None = None,
        ):
            captured["payload"] = payload
            captured["studio_session_id"] = studio_session_id
            return _correlation_envelope(studio_session_id=studio_session_id)

        args = _execute_args(
            confirm=True,
            provider="openrouter",
            model="openai/gpt-4o-mini",
            planner_model="anthropic/claude-sonnet-4.5",
            budget=1.25,
            budget_strict=True,
            subtask_budget=0.5,
            approve_capabilities=["secret"],
            approve_tool_packs=["ops-jenkins"],
            approve_writable_roots=["/tmp/out"],
        )
        with TemporaryDirectory(prefix="amof-handoff-dispatch-options-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with (
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    side_effect=_fake_runtime,
                ),
            ):
                code, stdout, stderr = _run_execute(args, amof_home)

        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(code, 0)
        self.assertEqual(payload["provider"], "openrouter")
        self.assertEqual(payload["model"], "openai/gpt-4o-mini")
        self.assertEqual(payload["planner_model"], "anthropic/claude-sonnet-4.5")
        self.assertEqual(payload["budget"], 1.25)
        self.assertTrue(payload["budget_strict"])
        self.assertEqual(payload["subtask_budget"], 0.5)
        self.assertEqual(payload["approve_capabilities"], ["secret"])
        self.assertEqual(payload["approve_tool_packs"], ["ops-jenkins"])
        self.assertEqual(payload["approve_writable_roots"], ["/tmp/out"])
        self.assertIsNone(captured["studio_session_id"])
        self.assertIn("selected_execution_configuration", stderr)
        self.assertIn("openrouter", stderr)
        self.assertNotIn("Execute this bounded goal.", stdout)
        self.assertEqual(json.loads(stdout)["status"], "completed")

    def test_budget_strict_and_readiness_blocks_and_failed_subtasks_are_truthful(
        self,
    ) -> None:
        scenarios = [
            ("blocked", 1, "budget_preflight_blocked"),
            ("blocked", 1, "missing_required_secret_access"),
            ("failed", 1, "max_iterations"),
            ("completed", 0, "completed"),
        ]
        for status, exit_code, stop_reason in scenarios:
            with self.subTest(status=status, stop_reason=stop_reason):
                with TemporaryDirectory(
                    prefix=f"amof-handoff-dispatch-{status}-"
                ) as td:
                    amof_home = Path(td)
                    _write_packet(amof_home)
                    with (
                        patch(
                            "amof.commands.handoff._load_execution_manifest",
                            return_value={"ecosystem": "demo-repo", "repos": []},
                        ),
                        patch(
                            "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                            return_value=_correlation_envelope(
                                status=status,
                                exit_code=exit_code,
                                stop_reason=stop_reason,
                            ),
                        ),
                    ):
                        code, stdout, _stderr = _run_execute(
                            _execute_args(confirm=True), amof_home
                        )

                    receipt = json.loads(stdout)
                    self.assertEqual(code, exit_code)
                    self.assertEqual(receipt["status"], status)
                    self.assertEqual(receipt["exit_code"], exit_code)
                    self.assertEqual(receipt["stop_reason"], stop_reason)

    def test_canonical_session_and_evidence_references_are_preserved(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-evidence-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with (
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    return_value=_correlation_envelope(
                        session_id="session-42",
                        plan_path="/tmp/plan.md",
                        checkpoint_path="/tmp/checkpoint.json",
                        event_log_path="/tmp/events.jsonl",
                        journal_path="/tmp/journal.md",
                    ),
                ),
            ):
                code, stdout, _stderr = _run_execute(
                    _execute_args(confirm=True), amof_home
                )

            receipt = json.loads(stdout)
            self.assertEqual(code, 0)
            self.assertEqual(receipt["session_id"], "session-42")
            self.assertEqual(receipt["evidence"]["plan_path"], "/tmp/plan.md")
            self.assertEqual(
                receipt["evidence"]["checkpoint_path"], "/tmp/checkpoint.json"
            )
            self.assertEqual(receipt["evidence"]["event_log_path"], "/tmp/events.jsonl")
            self.assertEqual(receipt["evidence"]["journal_path"], "/tmp/journal.md")

    def test_result_file_permissions_and_stdout_receipt_shape(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-result-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with (
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    return_value=_correlation_envelope(),
                ),
            ):
                code, stdout, stderr = _run_execute(
                    _execute_args(confirm=True), amof_home
                )

            receipt = json.loads(stdout)
            result_path = Path(receipt["result_path"])
            self.assertEqual(code, 0)
            self.assertEqual(
                stdout.strip(),
                json.dumps(
                    receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ),
            )
            self.assertTrue(result_path.is_file())
            self.assertEqual(stat.S_IMODE(os.stat(result_path.parent).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(result_path).st_mode), 0o600)
            self.assertIn("Execute-agent preview", stderr)
            self.assertNotIn("Execute this bounded goal.", stdout)

    def test_no_network_subprocess_or_external_app_integration_is_introduced(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="amof-handoff-dispatch-no-external-") as td:
            amof_home = Path(td)
            _write_packet(amof_home)
            with (
                patch(
                    "subprocess.run", side_effect=AssertionError("subprocess forbidden")
                ),
                patch(
                    "socket.create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
                patch(
                    "amof.commands.handoff._load_execution_manifest",
                    return_value={"ecosystem": "demo-repo", "repos": []},
                ),
                patch(
                    "amof.commands.handoff.agent_cmd.run_external_agent_plan_execute_envelope",
                    return_value=_correlation_envelope(),
                ),
            ):
                code, stdout, _stderr = _run_execute(
                    _execute_args(confirm=True), amof_home
                )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "completed")


class HandoffDispatchEntrypointTests(unittest.TestCase):
    def test_handoff_dispatch_uses_no_ecosystem_entrypoint_branch(self) -> None:
        args = _execute_args(confirm=True)
        with (
            patch("amof.entrypoint.parse_args", return_value=args),
            patch("amof.entrypoint.cmd_handoff", return_value=4) as handoff_mock,
        ):
            with self.assertRaises(SystemExit) as exc:
                entrypoint.main()

        self.assertEqual(exc.exception.code, 4)
        handoff_mock.assert_called_once_with(args)

    def test_existing_agent_help_surface_remains_available(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "amof.py"), "agent", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(SCRIPTS_ROOT)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--request-json", result.stdout)
        self.assertIn("--json", result.stdout)


if __name__ == "__main__":
    unittest.main()
