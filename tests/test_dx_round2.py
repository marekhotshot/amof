"""Public DX round 2: bounded_write bind, binding-root replace, doctor notes."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
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

from amof.commands.agent_cmd import _gate_plan_execute_readiness
from amof.commands.doctor import cmd_doctor
from amof.commands.help_cmd import cmd_help
from amof.commands.runs import _compute_run_summary
from amof.commands.scope import write_scope_example_path
from amof.orchestrator.events import EventLog
from amof.orchestrator.plan_execute_control import (
    VALID_CAPABILITIES,
    apply_binding_writable_roots,
    capability_help_lines,
    parse_capability_names,
)
from amof.orchestrator.planner import ExecutionPlan, Subtask
from amof.orchestrator.session import Session
from amof.orchestrator.telemetry import SessionTelemetry
from amof.orchestrator.tools.base import Guardrails, ToolCall, ToolRegistry
from amof.orchestrator.tools.write import WriteTool
from amof.orchestrator.trust_boundary import TrustState
from amof.write_scope_approvals import approve_proposal
from amof.write_scope_bindings import (
    STATUS_FAILED,
    git_rev_parse_head,
    list_bindings,
    load_binding,
)
from amof.write_scope_proposals import persist_write_scope_proposals_from_result


OPERATOR = "operator:dx-round2"


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "dx2@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "DX2 Tester"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "src").mkdir(parents=True, exist_ok=True)
    (path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return git_rev_parse_head(path)


def _result_envelope(*, session_id: str, proposal: dict) -> dict:
    return {
        "result_kind": "agent_run_result",
        "contract_version": "agent-run-v1",
        "schema_version": 1,
        "status": "completed",
        "session_id": session_id,
        "exit_code": 0,
        "stop_reason": "completed",
        "final_text": "discovery complete",
        "plan_path": None,
        "checkpoint_path": None,
        "event_log_path": "/tmp/events.jsonl",
        "journal_path": None,
        "budget_summary": {"limit": None, "spent": 0, "remaining": None},
        "write_scope_proposal": proposal,
    }


def _run_amof(amof_home: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS_ROOT)
    env["AMOF_HOME"] = str(amof_home)
    return subprocess.run(
        [sys.executable, "-m", "amof", *args],
        cwd=cwd or ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class _StubRunnerFactory:
    runner_names = ["code"]

    def runner_tool_names(self, runner: str) -> set[str]:
        return {"Read", "Write", "StrReplace"}


class DxRound2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.home = Path(self._tmpdir.name)
        self.repo = self.home / "repo"
        self.base_sha = _init_git_repo(self.repo)
        self.target_id = f"local:dx-round2:{self.base_sha}"
        self.env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _approve_src(self, *, run_id: str = "dx2-run") -> dict:
        body = {
            "target_id": self.target_id,
            "base_sha": self.base_sha,
            "allowed_roots": ["src/"],
            "denied_roots": [],
            "reason": "src-only fixture",
            "expected_checks": ["git diff --check"],
            "docs_only": False,
            "source_mutation": True,
        }
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(session_id=run_id, proposal=body),
        )
        self.assertEqual(len(outcome.persisted), 1)
        return approve_proposal(
            outcome.persisted[0]["proposal_id"],
            ttl="2h",
            approved_by=OPERATOR,
        )

    def _gate(
        self,
        *,
        approval_id: str | None,
        capabilities: list[str] | None,
        guardrails: Guardrails | None = None,
        events: EventLog | None = None,
        approve_writable_roots: list[str] | None = None,
    ):
        session = Session(session_id="dx2-session", mode="build")
        plan = ExecutionPlan(
            analysis="bounded write",
            subtasks=[
                Subtask(
                    id="S1",
                    title="Write src/ok.py",
                    description="Add src/ok.py",
                    runner="code",
                )
            ],
            execution_order=["S1"],
        )
        trust = TrustState(trusted_intent_caps={"read", "write"})
        rails = guardrails or Guardrails(
            writable_roots=[self.repo],
            mode="build",
            unattended=True,
        )
        registry = ToolRegistry(guardrails=rails, workspace_root=self.repo)
        ev = events or EventLog(
            session_id=session.id,
            runs_dir=self.home / "share" / "runs",
        )
        return _gate_plan_execute_readiness(
            "Write src/ok.py",
            plan,
            session=session,
            trust_state=trust,
            runner_factory=_StubRunnerFactory(),
            guardrails=rails,
            tool_registry=registry,
            events=ev,
            telemetry=SessionTelemetry(),
            manifest={"name": "dx2", "source": "appdata"},
            workspace_root=self.repo,
            approve_capabilities=capabilities,
            approve_writable_roots=approve_writable_roots,
            write_scope_approval=approval_id,
            no_follow_up=True,
        ), session, rails, ev

    def test_parse_bounded_write_is_recognised_as_write(self) -> None:
        caps = parse_capability_names(["bounded_write"])
        self.assertEqual(caps, {"write"})
        both = parse_capability_names(["bounded_write", "write"])
        self.assertEqual(both, {"write"})
        self.assertIn("bounded_write", VALID_CAPABILITIES)
        listed = "\n".join(capability_help_lines())
        for name in VALID_CAPABILITIES:
            self.assertIn(name, listed)

    def test_help_execute_example_binds_and_parses_bounded_write(self) -> None:
        approval = self._approve_src()
        (readiness, exit_code, meta), session, rails, _ev = self._gate(
            approval_id=approval["approval_id"],
            capabilities=["bounded_write"],
        )
        self.assertNotEqual((meta or {}).get("stop_reason"), "write_scope_bind_failed", meta)
        self.assertNotEqual((meta or {}).get("stop_reason"), "invalid_approve_capabilities", meta)
        binding = session.metadata.get("write_scope_binding")
        self.assertIsInstance(binding, dict)
        self.assertTrue(str(binding["binding_id"]).startswith("wsb-"))
        self.assertEqual(
            {Path(p).resolve() for p in rails.writable_roots},
            {Path(p).resolve() for p in binding["writable_roots"]},
        )
        self.assertNotIn(self.repo.resolve(), [Path(p).resolve() for p in rails.writable_roots])

    def test_bound_write_allows_src_and_blocks_outside(self) -> None:
        approval = self._approve_src()
        events = EventLog(
            session_id="dx2-bound-write",
            runs_dir=self.home / "share" / "runs",
        )
        (readiness, exit_code, meta), session, rails, ev = self._gate(
            approval_id=approval["approval_id"],
            capabilities=["bounded_write"],
            events=events,
        )
        self.assertNotEqual((meta or {}).get("stop_reason"), "write_scope_bind_failed", meta)
        self.assertNotEqual((meta or {}).get("stop_reason"), "invalid_approve_capabilities", meta)
        registry = ToolRegistry(
            guardrails=rails,
            events=ev,
            workspace_root=self.repo,
        )
        registry.register(WriteTool(workspace_root=self.repo))
        ok_path = self.repo / "src" / "ok.py"
        allowed = registry.execute(
            ToolCall(
                id="1",
                name="Write",
                arguments={"path": str(ok_path), "contents": "ok\n"},
            )
        )
        self.assertTrue(allowed.success, allowed.error)
        self.assertEqual(ok_path.read_text(encoding="utf-8"), "ok\n")
        outside = self.repo / "OUTSIDE.md"
        blocked = registry.execute(
            ToolCall(
                id="2",
                name="Write",
                arguments={"path": str(outside), "contents": "nope\n"},
            )
        )
        self.assertFalse(blocked.success)
        self.assertIn("scope_exceeded", blocked.error or "")
        self.assertFalse(outside.exists())
        summary = _compute_run_summary(ev.log_path)
        self.assertIsNotNone(summary.write_scope)
        self.assertTrue(str(summary.write_scope).startswith("bound wsb-"))
        self.assertIn("OUTSIDE.md", summary.blocked_paths)

    def test_approve_writable_root_cannot_widen_bound_run(self) -> None:
        approval = self._approve_src()
        (_readiness, exit_code, meta), _session, _rails, _ev = self._gate(
            approval_id=approval["approval_id"],
            capabilities=["bounded_write"],
            approve_writable_roots=[str(self.repo)],
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual((meta or {}).get("stop_reason"), "write_scope_bind_failed")
        self.assertIn("refuse mixed authority", (meta or {}).get("final_text") or "")

    def test_binding_roots_replace_do_not_append(self) -> None:
        rails = Guardrails(writable_roots=[self.repo], mode="build", unattended=True)
        src = (self.repo / "src").resolve()
        approved = apply_binding_writable_roots(rails, [str(src)])
        self.assertEqual([Path(p).resolve() for p in rails.writable_roots], [src])
        self.assertEqual(approved, [str(src)])
        self.assertNotIn(self.repo.resolve(), [Path(p).resolve() for p in rails.writable_roots])

    def test_capability_parse_failure_marks_binding_failed(self) -> None:
        approval = self._approve_src(run_id="dx2-fail-cap")
        (_readiness, exit_code, meta), session, _rails, _ev = self._gate(
            approval_id=approval["approval_id"],
            capabilities=["bounded_write", "not_a_real_cap"],
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual((meta or {}).get("stop_reason"), "invalid_approve_capabilities")
        binding = session.metadata.get("write_scope_binding")
        self.assertIsInstance(binding, dict)
        loaded = load_binding(binding["binding_id"])
        self.assertEqual(loaded["status"], STATUS_FAILED)
        active = list_bindings(status="active")
        self.assertEqual(active, [])

    def test_ungoverned_banner_names_governed_flags(self) -> None:
        from amof.commands import agent_cmd

        source = Path(agent_cmd.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "UNGOVERNED LOCAL MODE: no Write-Scope Approval bound.",
            source,
        )
        collapsed = " ".join(source.split())
        self.assertIn("--write-scope-approval <wsa-id>", collapsed)
        self.assertIn("--approve-capabilities bounded_write", collapsed)

    def test_help_capabilities_uses_parser_allow_list(self) -> None:
        with redirect_stdout(StringIO()) as stdout:
            code = cmd_help("capabilities")
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        for name in VALID_CAPABILITIES:
            self.assertIn(name, text)

    def test_import_result_example_src_only(self) -> None:
        example = write_scope_example_path("src-only")
        self.assertTrue(example.is_file())
        payload = json.loads(example.read_text(encoding="utf-8"))
        self.assertEqual(payload["result_kind"], "agent_run_result")
        self.assertEqual(payload["write_scope_proposal"]["allowed_roots"], ["src/"])
        result = _run_amof(
            self.home,
            "scope",
            "import-result",
            "--example",
            "src-only",
            "--run-id",
            "learn-001",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wsp-", result.stdout)
        self.assertIn("learning fixture", result.stderr.lower())

    def test_agent_provider_accepts_local(self) -> None:
        result = _run_amof(self.home, "agent", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local", result.stdout)
        self.assertIn("--write-scope-approval", result.stdout)
        self.assertIn("--approve-capabilities", result.stdout)

    def test_doctor_help_documents_exit_policy(self) -> None:
        result = _run_amof(self.home, "doctor", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Exit 0", result.stdout)
        self.assertIn("usable", result.stdout)
        self.assertIn("Exit 1", result.stdout)
        self.assertIn("blocked", result.stdout)

    def test_doctor_notes_printed_separately(self) -> None:
        report = {
            "verdict": "PASS",
            "layout_mode": "installed_cli",
            "workspace_root": "/tmp/adopted",
            "canonical_amof_code_path": "/tmp/cli",
            "canonical_ui_path": "/tmp/ui",
            "runtime_import_source": "/tmp/cli/__init__.py",
            "contexts": {"current": {"current_context": "default"}},
            "app_data": {
                "files": {"config_file": {"path": "/tmp/config"}},
                "roots": {
                    "evidence_dir": {"path": "/tmp/evidence"},
                    "workspaces_dir": {"path": "/tmp/ws"},
                },
            },
            "canonical_repo_policy": {"classification": "cli"},
            "surfaces": {},
            "toolchain": {},
            "notes": ["CANONICAL_REPO_DETACHED_OR_STALE: informational"],
            "warnings": ["optional tool missing: kubectl"],
            "failures": [],
        }
        with patch("amof.commands.doctor.topology_report", return_value=report):
            with redirect_stdout(StringIO()) as stdout:
                code = cmd_doctor(SimpleNamespace(json=False))
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("Notes:", text)
        self.assertIn("Warnings:", text)
        notes_at = text.index("Notes:")
        warns_at = text.index("Warnings:")
        self.assertLess(notes_at, warns_at)

    def test_documented_example_flags_exist(self) -> None:
        commands = [
            ["help", "execute"],
            ["help", "scope"],
            ["help", "handoff"],
            ["help", "capabilities"],
            ["help", "agent"],
            ["scope", "import-result", "--help"],
            ["scope", "list", "--help"],
            ["scope", "approve", "--help"],
            ["scope", "audit", "--help"],
            ["handoff", "execute-agent", "--help"],
            ["agent", "--help"],
            ["doctor", "--help"],
        ]
        for argv in commands:
            result = _run_amof(self.home, *argv)
            self.assertEqual(result.returncode, 0, f"{argv}: {result.stderr}")
        execute = _run_amof(self.home, "help", "execute")
        self.assertIn("--write-scope-approval", execute.stdout)
        self.assertIn("bounded_write", execute.stdout)
        scope = _run_amof(self.home, "scope", "import-result", "--help")
        self.assertIn("--example", scope.stdout)
        self.assertIn("src-only", scope.stdout)

    def test_runs_show_summarises_bound_block(self) -> None:
        events_path = self.home / "share" / "runs" / "dx2-show" / "events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(
            "".join(
                json.dumps(event) + "\n"
                for event in (
                    {
                        "event_type": "run_created",
                        "run_id": "dx2-show",
                        "session_id": "dx2-show",
                        "timestamp": "2026-09-02T16:00:00+00:00",
                    },
                    {
                        "event_type": "write_scope_bound",
                        "run_id": "dx2-show",
                        "session_id": "dx2-show",
                        "binding_id": "wsb-abcdef",
                        "approval_id": "wsa-abcdef",
                        "timestamp": "2026-09-02T16:00:01+00:00",
                    },
                    {
                        "event_type": "write_scope_blocked",
                        "run_id": "dx2-show",
                        "session_id": "dx2-show",
                        "path": str(self.repo / "OUTSIDE.md"),
                        "timestamp": "2026-09-02T16:00:02+00:00",
                    },
                    {
                        "event_type": "run_finished",
                        "run_id": "dx2-show",
                        "session_id": "dx2-show",
                        "timestamp": "2026-09-02T16:00:03+00:00",
                    },
                )
            ),
            encoding="utf-8",
        )
        summary = _compute_run_summary(events_path)
        self.assertEqual(summary.write_scope, "bound wsb-abcdef")
        self.assertEqual(list(summary.blocked_paths), ["OUTSIDE.md"])


if __name__ == "__main__":
    unittest.main()
