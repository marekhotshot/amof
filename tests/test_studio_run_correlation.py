from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import agent_cmd
from amof.commands import studio as studio_cmd


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


class StudioRunCorrelationTests(unittest.TestCase):
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

    def _run_correlated_plan_execute(
        self,
        repo: Path,
        amof_home: Path,
        *,
        studio_session_id: str,
    ) -> agent_cmd.AgentPlanExecuteEnvelope:
        from amof.orchestrator.planner import ExecutionPlan

        manifest = self._manifest(repo)
        plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
        self._write_plan(plan_file)
        env = {
            "AMOF_HOME": str(amof_home),
            "OPENROUTER_API_KEY": "unit-test-provider-value",
        }
        _FakeRunnerAgent.instances.clear()
        with patch.dict(os.environ, env, clear=False):
            with _cwd(repo):
                with patch("amof.orchestrator.runners.Agent", _FakeRunnerAgent):
                    with patch(
                        "amof.orchestrator.planner.TaskPlanner.plan",
                        return_value=ExecutionPlan.load_from_markdown(plan_file),
                    ):
                        envelope = agent_cmd.cmd_agent(
                            manifest,
                            goal="Inspect this repo",
                            plan_execute=True,
                            provider="openrouter",
                            no_follow_up=True,
                            approve_plan=True,
                            studio_session_id=studio_session_id,
                            _json_envelope=True,
                        )
        self.assertIsInstance(envelope, agent_cmd.AgentPlanExecuteEnvelope)
        return envelope

    def test_plan_execute_envelope_and_studio_ledger_share_correlation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-studio-run-correlation-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                studio_session_id = self._create_studio_session()
                envelope = self._run_correlated_plan_execute(
                    repo,
                    amof_home,
                    studio_session_id=studio_session_id,
                )
                studio_payload = studio_cmd._studio_payload(studio_session_id)

            events = [
                json.loads(line)
                for line in Path(str(envelope.event_log_path)).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            studio_events = (
                amof_home / "share" / "studio" / studio_session_id / "events.jsonl"
            ).read_text(encoding="utf-8")

        self.assertEqual(envelope.status, "completed")
        self.assertEqual(envelope.exit_code, 0)
        self.assertEqual(envelope.studio_session_id, studio_session_id)
        self.assertTrue(events)
        self.assertTrue(all(event.get("studio_session_id") == studio_session_id for event in events))
        self.assertIn('"run.attached"', studio_events)
        attached_runs = studio_payload["attached_runs"]
        self.assertEqual(len(attached_runs), 1)
        self.assertEqual(attached_runs[0]["run_id"], envelope.session_id)
        self.assertEqual(attached_runs[0]["studio_session_id"], studio_session_id)
        self.assertEqual(attached_runs[0]["status"], "finished")

    def test_unknown_studio_session_fails_closed_before_provider_setup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-studio-run-missing-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = self._manifest(repo)
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }

            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch(
                        "amof.orchestrator.llm.openai_client.OpenAIClient"
                    ) as openai_client:
                        envelope = agent_cmd.cmd_agent(
                            manifest,
                            goal="Inspect this repo",
                            plan_execute=True,
                            provider="openrouter",
                            no_follow_up=True,
                            approve_plan=True,
                            studio_session_id="studio-does-not-exist",
                            _json_envelope=True,
                        )

        self.assertIsInstance(envelope, agent_cmd.AgentPlanExecuteEnvelope)
        self.assertEqual(envelope.status, "failed")
        self.assertEqual(envelope.stop_reason, "studio_session_invalid")
        self.assertEqual(envelope.session_id, "")
        self.assertEqual(envelope.studio_session_id, "studio-does-not-exist")
        self.assertFalse(openai_client.called)

    def test_ended_studio_session_fails_closed_before_provider_setup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-studio-run-ended-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = self._manifest(repo)
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }

            with patch.dict(os.environ, env, clear=False):
                studio_session_id = self._create_studio_session()
                studio_cmd._end_studio_session(studio_session_id)
                with _cwd(repo):
                    with patch(
                        "amof.orchestrator.llm.openai_client.OpenAIClient"
                    ) as openai_client:
                        envelope = agent_cmd.cmd_agent(
                            manifest,
                            goal="Inspect this repo",
                            plan_execute=True,
                            provider="openrouter",
                            no_follow_up=True,
                            approve_plan=True,
                            studio_session_id=studio_session_id,
                            _json_envelope=True,
                        )

        self.assertIsInstance(envelope, agent_cmd.AgentPlanExecuteEnvelope)
        self.assertEqual(envelope.status, "failed")
        self.assertEqual(envelope.stop_reason, "studio_session_invalid")
        self.assertEqual(envelope.session_id, "")
        self.assertEqual(envelope.studio_session_id, studio_session_id)
        self.assertFalse(openai_client.called)
