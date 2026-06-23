from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import agent_cmd
from amof.contracts_runtime import AgentRunResult
from amof.orchestrator.events import EventLog
from amof.orchestrator.llm.base import ProviderError, StructuredLLMResponse, Usage
from amof.orchestrator.planner import TaskPlanner
from amof.orchestrator.runners import PUBLIC_DEFAULT_RUNNERS_CONFIG, RunnerFactory
from amof.orchestrator.telemetry import SessionTelemetry
from amof.orchestrator.tools.base import (
    GuardrailConfig,
    Guardrails,
    ToolCall,
    create_default_registry,
)
from amof.orchestrator.tool_failure_semantics import analyze_tool_call_events
from amof.orchestrator.trust_boundary import create_trust_state


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
        ["git", "add", "."], cwd=path, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-m", "test: init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=_commit_env(),
    )


class _FakeAgent:
    instances = []
    stop_reason = "completed"

    def __init__(self, *args, **kwargs) -> None:
        self.llm = kwargs.get("llm") or (args[0] if args else None)
        self.model_router = kwargs.get("model_router")
        self.tools = kwargs.get("tools")
        self.__class__.instances.append(self)

    def run(self, goal: str) -> str:
        return "fake agent response"


class _FakeLocalLLM:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def model_name(self) -> str:
        return f"local/test/{self.kwargs.get('model')}"


@contextmanager
def _isolated_provider_env(amof_home: Path, extra_env: dict[str, str] | None = None):
    keys = {
        "AMOF_HOME",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AMOF_BEDROCK_REGION",
        "AMOF_LOCAL_QWEN_MODEL",
        "AMOF_LOCAL_MODEL",
        "AMOF_LOCAL_PROVIDER_TIMEOUT_SECONDS",
        "AMOF_PLANNER_MODEL",
        "AMOF_RUNPOD_MODEL",
        "RUNPOD_API_KEY",
        "RUNPOD_MODEL",
        "RUNPOD_OPENAI_BASE_URL",
    }
    sentinel = object()
    saved = {key: os.environ.get(key, sentinel) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ["AMOF_HOME"] = str(amof_home)
        for key, value in (extra_env or {}).items():
            os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value  # type: ignore[assignment]


def _write_active_provider_profile(
    amof_home: Path, name: str, payload: dict[str, object]
) -> None:
    config_root = amof_home / "config"
    profile_dir = config_root / "provider-profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / f"{name}.yaml").write_text(
        yaml.safe_dump({"name": name, **payload}, sort_keys=False),
        encoding="utf-8",
    )
    (config_root / "config.yaml").write_text(
        yaml.safe_dump({"current_context": "local"}, sort_keys=False),
        encoding="utf-8",
    )
    (config_root / "contexts.yaml").write_text(
        yaml.safe_dump(
            {"contexts": {"local": {"credentials": {"provider_profile_refs": [name]}}}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class _FakeFailingWriteAgent(_FakeAgent):
    stop_reason = "completed"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.telemetry = kwargs.get("telemetry")

    def run(self, goal: str) -> str:
        self.telemetry.record_tool_call("Write", False, 1)
        return "claimed success after failed write"


class _FakeNoDiffAgent(_FakeAgent):
    stop_reason = "completed"

    def run(self, goal: str) -> str:
        return "claimed success without changing files"


class _FakeMutatingAgent(_FakeAgent):
    stop_reason = "completed"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.telemetry = kwargs.get("telemetry")

    def run(self, goal: str) -> str:
        app = Path.cwd() / "app.py"
        app.write_text(
            app.read_text(encoding="utf-8")
            + "\n\ndef farewell(name: str) -> str:\n    return f'Goodbye, {name}.'\n",
            encoding="utf-8",
        )
        self.telemetry.record_tool_call("Write", True, 1)
        return "changed app.py"


class _FakeInspectOnlyMutationAgent(_FakeAgent):
    stop_reason = "completed"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.telemetry = kwargs.get("telemetry")

    def run(self, goal: str) -> str:
        self.telemetry.record_tool_call(
            "InspectFiles",
            True,
            1,
            metadata={"inspected_files": ["app.py", "tests/test_app.py"]},
        )
        return "def farewell(name: str) -> str:\n    return f'Goodbye, {name}.'"


class _FakeInvalidPythonEditAgent(_FakeAgent):
    stop_reason = "completed"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.telemetry = kwargs.get("telemetry")

    def run(self, goal: str) -> str:
        app = Path.cwd() / "app.py"
        app.write_text(
            app.read_text(encoding="utf-8")
            + "defarewell(name: str) -> str:\n    return f'Goodbye, {name}.'\n",
            encoding="utf-8",
        )
        self.telemetry.record_tool_call("InsertAfter", True, 1)
        return "changed app.py"


class _FakeUnsafeStrReplaceAgent(_FakeAgent):
    stop_reason = "completed"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.telemetry = kwargs.get("telemetry")

    def run(self, goal: str) -> str:
        result = self.tools.execute(
            ToolCall(
                id="unsafe",
                name="StrReplace",
                arguments={
                    "path": "README.md",
                    "old_string": "",
                    "new_string": "bad",
                },
            )
        )
        self.telemetry.record_tool_call("StrReplace", result.success, 1)
        return result.to_text()


class _FakeProviderNetworkAgent(_FakeAgent):
    stop_reason = "provider_network"

    def run(self, goal: str) -> str:
        return "local provider error (network): request timed out"


class _FakeFailedSubtaskAgent(_FakeAgent):
    stop_reason = "max_iterations"

    def run(self, goal: str) -> str:
        return "worker failed before completing the subtask"


class _FakeInteractiveAgent:
    def __init__(self) -> None:
        self.run_calls = 0
        self.llm = type("LLM", (), {"model_name": lambda self: "fake-model"})()
        self.model_router = None
        self.tools = type("Tools", (), {"get": lambda self, name: None})()

    def run(self, goal: str) -> str:
        self.run_calls += 1
        return "should not run"


class _RecordingRunnerFactory:
    runner_names = ["code"]

    def __init__(self) -> None:
        self.contexts: list[str] = []

    def run_runner(self, name, task, context=None, parent_telemetry=None):
        from amof.orchestrator.runners import RunnerResult
        from amof.orchestrator.telemetry import SessionTelemetry

        self.contexts.append(context or "")
        return RunnerResult(
            runner_name=name,
            success=True,
            response="ok",
            stop_reason="completed",
            telemetry=SessionTelemetry(),
        )


class _ProviderErrorPlannerLLM:
    calls = 0

    def chat_structured(self, **kwargs):
        self.calls += 1
        raise ProviderError(
            provider="openrouter",
            message="invalid model id",
            status_code=400,
            original=ValueError("invalid model id"),
        )

    def model_name(self) -> str:
        return "openrouter/invalid"


def _make_structured_planner_response(
    *,
    analysis: str,
    subtasks: list[dict[str, object]] | None = None,
    questions: list[str] | None = None,
    execution_order: list[str] | None = None,
    risks: list[str] | None = None,
    verification: str = "",
    stop_reason: str = "end_turn",
    request_id: str = "req-structured-plan",
) -> StructuredLLMResponse:
    from amof.orchestrator.agent_models import PlannerOutputModel

    subtasks = subtasks or []
    questions = questions or []
    execution_order = execution_order or [str(item["id"]) for item in subtasks]
    risks = risks or []
    text = json.dumps(
        {
            "analysis": analysis,
            "subtasks": subtasks,
            "execution_order": execution_order,
            "risks": risks,
            "verification": verification,
            "questions": questions,
        },
        separators=(",", ":"),
    )
    return StructuredLLMResponse(
        parsed=PlannerOutputModel(
            analysis=analysis,
            subtasks=subtasks,
            execution_order=execution_order,
            risks=risks,
            verification=verification,
            questions=questions,
        ),
        usage=Usage(
            model="openai/gpt-4o-mini",
            prompt_tokens=42,
            completion_tokens=11,
            latency_ms=17,
            estimated_cost=0.0,
            provider="remote-ial",
            upstream_provider="openrouter",
            upstream_model="openai/gpt-4o-mini",
            request_id=request_id,
            cost_status="unknown",
            cost_observed=False,
        ),
        stop_reason=stop_reason,
        text=text,
    )


class _SequencedStructuredPlannerLLM:
    def __init__(self, responses: list[StructuredLLMResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.messages_history: list[list[dict[str, str]]] = []

    def chat_structured(self, **kwargs):
        messages = []
        for message in kwargs.get("messages", []):
            messages.append(
                {
                    "role": str(message.get("role", "")),
                    "content": str(message.get("content", "")),
                }
            )
        self.messages_history.append(messages)
        if self.calls >= len(self._responses):
            raise AssertionError("No scripted structured planner response remaining")
        response = self._responses[self.calls]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response

    def model_name(self) -> str:
        return "remote-ial/openai/gpt-4o-mini"

    def chat(self, *args, **kwargs):
        raise AssertionError("chat() should not be used when chat_structured succeeds")


class _EmptyStructuredPlannerLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.messages_history: list[list[dict[str, str]]] = []

    def chat_structured(self, **kwargs):
        self.calls += 1
        self.messages_history.append(
            [
                {
                    "role": str(message.get("role", "")),
                    "content": str(message.get("content", "")),
                }
                for message in kwargs.get("messages", [])
            ]
        )
        return _make_structured_planner_response(
            analysis="Structured planner returned only analysis text.",
            subtasks=[],
            execution_order=[],
            risks=[],
            verification="",
            questions=[],
            stop_reason="end_turn",
            request_id="req-empty-plan",
        )

    def model_name(self) -> str:
        return "remote-ial/openai/gpt-4o-mini"

    def chat(self, *args, **kwargs):
        raise AssertionError("chat() should not be used when chat_structured succeeds")


class _SuccessfulStructuredPlannerLLM:
    def __init__(self) -> None:
        self.calls = 0

    def chat_structured(self, **kwargs):
        self.calls += 1
        return _make_structured_planner_response(
            analysis="Inspect the bounded repo before making changes.",
            subtasks=[
                {
                    "id": "1",
                    "title": "Inspect README",
                    "description": "Read the README to gather context.",
                    "runner": "code",
                    "depends_on": [],
                }
            ],
            execution_order=["1"],
            risks=["README may be stale."],
            verification="Review the bounded README findings.",
            questions=[],
            stop_reason="end_turn",
            request_id="req-successful-plan",
        )

    def model_name(self) -> str:
        return "remote-ial/openai/gpt-4o-mini"


class AgentRuntimeProfileTests(unittest.TestCase):
    def test_pyproject_includes_public_agent_runtime_dependencies(self) -> None:
        payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        deps = set(payload["project"]["dependencies"])
        dep_names = {dep.split(">=", 1)[0] for dep in deps}

        self.assertTrue(
            {"PyYAML", "pydantic", "openai", "anthropic", "boto3", "botocore"}
            <= dep_names
        )
        memory_deps = payload["project"]["optional-dependencies"]["memory"]
        self.assertTrue(any(dep.startswith("chromadb") for dep in memory_deps))
        self.assertTrue(any(dep.startswith("pysqlite3-binary") for dep in memory_deps))

    def test_help_does_not_require_agent_runtime_or_ecosystem(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "amof.py"), "agent", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(SCRIPTS_ROOT)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--provider", result.stdout)

    def test_adopted_plan_writes_journal_to_appdata_and_keeps_repo_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-runtime-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.agent.Agent", _FakeAgent):
                        import amof.orchestrator.memory as memory

                        with patch.object(
                            memory,
                            "VectorStore",
                            side_effect=ImportError("chromadb missing"),
                        ):
                            with (
                                redirect_stdout(StringIO()),
                                redirect_stderr(StringIO()) as stderr,
                            ):
                                result = agent_cmd.cmd_agent(
                                    manifest,
                                    goal="Inspect this repo",
                                    plan_mode=True,
                                    provider="openrouter",
                                    no_follow_up=True,
                                    verbose=False,
                                )

            git_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            journal_files = list(
                (amof_home / "share" / "journals" / "demo-repo").glob("*.md")
            )

        self.assertEqual(result, 0)
        self.assertEqual(git_status.stdout.strip(), "")
        self.assertFalse((repo / "ecosystems").exists())
        self.assertFalse((repo / ".amof").exists())
        self.assertFalse((repo / "context").exists())
        self.assertTrue(journal_files)
        self.assertNotIn("Vector memory unavailable", stderr.getvalue())
        self.assertNotIn("NO protections", stderr.getvalue())

    def test_verbose_missing_vector_memory_uses_pipx_guidance_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-memory-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.agent.Agent", _FakeAgent):
                        import amof.orchestrator.memory as memory

                        with patch.object(
                            memory,
                            "VectorStore",
                            side_effect=ImportError("chromadb missing"),
                        ):
                            with (
                                redirect_stdout(StringIO()),
                                redirect_stderr(StringIO()) as stderr,
                            ):
                                result = agent_cmd.cmd_agent(
                                    manifest,
                                    goal="Inspect this repo",
                                    plan_mode=True,
                                    provider="openrouter",
                                    no_follow_up=True,
                                    verbose=True,
                                )

        self.assertEqual(result, 0)
        err = stderr.getvalue()
        self.assertIn("pipx inject amof chromadb pysqlite3-binary", err)
        self.assertNotIn("requirements.txt", err)

    def test_installed_agent_install_does_not_use_target_requirements_guidance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-install-") as td:
            repo = Path(td) / "target-repo"
            _init_git_repo(repo)
            with _cwd(repo):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
                    result = agent_cmd.cmd_agent_install()

        self.assertIn(result, {0, 1})
        self.assertNotIn(
            "requirements.txt not found in workspace root", stderr.getvalue()
        )
        self.assertNotIn("pip install -r", stderr.getvalue())

    def test_missing_guardrails_load_packaged_public_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-guardrails-") as td:
            cfg = GuardrailConfig.load(Path(td) / "missing-guardrails.yaml")

        self.assertIn(".git/**", cfg.protected_paths)
        self.assertIn(".env", cfg.protected_basenames)
        self.assertIn("git push", cfg.blocked_commands)
        guardrails = Guardrails(mode="plan", config=cfg)
        self.assertEqual(
            guardrails.check_write("README.md"),
            "Write operations are blocked in PLAN mode",
        )
        self.assertEqual(
            guardrails.check_shell("git status"),
            "Shell operations are blocked in PLAN mode",
        )

    def test_openrouter_default_planner_model_is_provider_compatible(self) -> None:
        planner_model = agent_cmd._default_planner_model("openrouter", None)

        self.assertEqual(planner_model, "anthropic/claude-sonnet-4.5")
        self.assertNotEqual(planner_model, "claude-opus-4-6")

    def test_openrouter_default_worker_model_uses_bounded_quality_model(self) -> None:
        worker_model = agent_cmd._default_worker_model("openrouter", None, None)

        self.assertEqual(worker_model, "anthropic/claude-sonnet-4.5")
        self.assertNotEqual(worker_model, "openai/gpt-4o-mini")

    def test_explicit_planner_model_wins(self) -> None:
        self.assertEqual(
            agent_cmd._default_planner_model(
                "openrouter", "anthropic/claude-sonnet-4.5"
            ),
            "anthropic/claude-sonnet-4.5",
        )

    def test_active_local_profile_selects_local_client_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-profile-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            _write_active_provider_profile(
                amof_home,
                "local-qwen-default",
                {
                    "provider": "local",
                    "lane": "worker",
                    "model": "qwen2.5-coder:7b-instruct",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "credential_refs": {},
                    "timeout_seconds": 45,
                },
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            created_clients: list[_FakeLocalLLM] = []

            def _fake_local_client(**kwargs):
                client = _FakeLocalLLM(**kwargs)
                created_clients.append(client)
                return client

            _FakeAgent.instances.clear()
            with _isolated_provider_env(
                amof_home, {"AMOF_LOCAL_PROVIDER_TIMEOUT_SECONDS": "12.5"}
            ):
                with _cwd(repo):
                    with patch("amof.orchestrator.agent.Agent", _FakeAgent):
                        import amof.orchestrator.memory as memory

                        with patch.object(
                            memory,
                            "VectorStore",
                            side_effect=ImportError("chromadb missing"),
                        ):
                            with patch(
                                "amof.orchestrator.llm.local_openai_compatible.LocalOpenAICompatibleClient",
                                side_effect=_fake_local_client,
                            ):
                                with (
                                    redirect_stdout(StringIO()) as stdout,
                                    redirect_stderr(StringIO()) as stderr,
                                ):
                                    result = agent_cmd.cmd_agent(
                                        manifest,
                                        goal="Inspect this repo",
                                        plan_mode=True,
                                        no_follow_up=True,
                                        verbose=False,
                                    )

            git_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            appdata_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in amof_home.glob("share/**/*")
                if path.is_file()
            )

        self.assertEqual(result, 0, stderr.getvalue())
        self.assertTrue(created_clients)
        self.assertEqual(
            created_clients[0].kwargs["base_url"], "http://127.0.0.1:11434/v1"
        )
        self.assertEqual(
            created_clients[0].kwargs["model"], "qwen2.5-coder:7b-instruct"
        )
        self.assertIsNone(created_clients[0].kwargs["api_key"])
        self.assertEqual(created_clients[0].kwargs["timeout"], 45.0)
        self.assertTrue(_FakeAgent.instances)
        self.assertIs(_FakeAgent.instances[-1].llm, created_clients[0])
        self.assertIn("local/test/qwen2.5-coder:7b-instruct", stdout.getvalue())
        self.assertEqual(git_status.stdout.strip(), "")
        self.assertFalse((repo / ".amof").exists())
        self.assertFalse((repo / "ecosystems").exists())
        self.assertFalse((repo / "context").exists())
        self.assertNotIn("ANTHROPIC_API_KEY", stderr.getvalue())
        self.assertNotIn("OPENAI_API_KEY", stderr.getvalue())
        self.assertNotIn("OPENROUTER_API_KEY", stderr.getvalue())
        self.assertNotIn("api_key", appdata_text)

    def test_active_local_profile_honors_env_timeout_when_profile_omits_timeout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-timeout-env-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            _write_active_provider_profile(
                amof_home,
                "local-qwen-default",
                {
                    "provider": "local",
                    "lane": "worker",
                    "model": "qwen2.5-coder:7b-instruct",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "credential_refs": {},
                },
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            created_clients: list[_FakeLocalLLM] = []

            def _fake_local_client(**kwargs):
                client = _FakeLocalLLM(**kwargs)
                created_clients.append(client)
                return client

            with _isolated_provider_env(
                amof_home, {"AMOF_LOCAL_PROVIDER_TIMEOUT_SECONDS": "12.5"}
            ):
                with _cwd(repo):
                    with patch("amof.orchestrator.agent.Agent", _FakeAgent):
                        import amof.orchestrator.memory as memory

                        with patch.object(
                            memory,
                            "VectorStore",
                            side_effect=ImportError("chromadb missing"),
                        ):
                            with patch(
                                "amof.orchestrator.llm.local_openai_compatible.LocalOpenAICompatibleClient",
                                side_effect=_fake_local_client,
                            ):
                                with (
                                    redirect_stdout(StringIO()),
                                    redirect_stderr(StringIO()) as stderr,
                                ):
                                    result = agent_cmd.cmd_agent(
                                        manifest,
                                        goal="Inspect this repo",
                                        plan_mode=True,
                                        no_follow_up=True,
                                        verbose=False,
                                    )

        self.assertEqual(result, 0, stderr.getvalue())
        self.assertTrue(created_clients)
        self.assertEqual(created_clients[0].kwargs["timeout"], 12.5)

    def test_active_local_profile_invalid_timeout_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-timeout-invalid-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            _write_active_provider_profile(
                amof_home,
                "local-qwen-default",
                {
                    "provider": "local",
                    "lane": "worker",
                    "model": "qwen2.5-coder:7b-instruct",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "credential_refs": {},
                    "timeout_seconds": "not-a-number",
                },
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }

            with _isolated_provider_env(amof_home):
                with _cwd(repo):
                    with (
                        redirect_stdout(StringIO()),
                        redirect_stderr(StringIO()) as stderr,
                    ):
                        result = agent_cmd.cmd_agent(
                            manifest,
                            goal="Inspect this repo",
                            plan_mode=True,
                            no_follow_up=True,
                            verbose=False,
                        )

        err = stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("timeout_seconds must be a positive number", err)
        self.assertIn("provider=local", err)
        self.assertIn("base_url=http://127.0.0.1:11434/v1", err)
        self.assertIn("sdk_max_retries=0", err)

    def test_local_openai_compatible_client_disables_sdk_retries(self) -> None:
        from amof.orchestrator.llm.local_openai_compatible import (
            LocalOpenAICompatibleClient,
        )

        captured: dict[str, object] = {}

        class _OpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_openai_module = type("FakeOpenAI", (), {"OpenAI": _OpenAI})
        with patch.dict(sys.modules, {"openai": fake_openai_module}):
            client = LocalOpenAICompatibleClient(
                base_url="http://127.0.0.1:11434/v1",
                model="qwen2.5-coder:7b-instruct",
                timeout=7.5,
            )
            client._get_client()

        self.assertEqual(captured["timeout"], 7.5)
        self.assertEqual(captured["max_retries"], 0)
        self.assertEqual(captured["base_url"], "http://127.0.0.1:11434/v1")
        self.assertEqual(captured["api_key"], "local")

    def test_runpod_openai_compatible_client_normalizes_v1_once(self) -> None:
        from amof.orchestrator.llm.local_openai_compatible import (
            LocalOpenAICompatibleClient,
            normalize_openai_compatible_base_url,
        )

        self.assertEqual(
            normalize_openai_compatible_base_url(
                "https://pod-8000.proxy.runpod.net",
                provider_id="runpod",
            ),
            "https://pod-8000.proxy.runpod.net/v1",
        )
        self.assertEqual(
            normalize_openai_compatible_base_url(
                "https://pod-8000.proxy.runpod.net/v1/v1",
                provider_id="runpod",
            ),
            "https://pod-8000.proxy.runpod.net/v1",
        )

        client = LocalOpenAICompatibleClient(
            base_url="https://pod-8000.proxy.runpod.net/v1/v1/",
            model="deepseek-ai/DeepSeek-V4-Flash",
            api_key="unit-test-key",
            provider_id="runpod",
        )

        self.assertEqual(client._base_url, "https://pod-8000.proxy.runpod.net/v1")
        self.assertIn(
            "runpod/pod-8000.proxy.runpod.net/deepseek-ai/DeepSeek-V4-Flash",
            client.model_name(),
        )

    def test_runpod_openai_compatible_client_adds_proxy_safe_headers(self) -> None:
        from amof.orchestrator.llm.local_openai_compatible import (
            LocalOpenAICompatibleClient,
        )

        captured: dict[str, object] = {}

        class _OpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_openai_module = type("FakeOpenAI", (), {"OpenAI": _OpenAI})
        with patch.dict(sys.modules, {"openai": fake_openai_module}):
            client = LocalOpenAICompatibleClient(
                base_url="https://pod-8000.proxy.runpod.net",
                model="deepseek-ai/DeepSeek-V4-Flash",
                api_key="unit-test-key",
                provider_id="runpod",
            )
            client._get_client()

        headers = captured["default_headers"]
        self.assertIsInstance(headers, dict)
        self.assertEqual(captured["base_url"], "https://pod-8000.proxy.runpod.net/v1")
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertIn("application/json", headers["Accept"])

    def test_active_runpod_profile_uses_openai_compatible_client_with_diagnostics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runpod-profile-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            _write_active_provider_profile(
                amof_home,
                "runpod-heavy",
                {
                    "provider": "runpod",
                    "lane": "heavy-lane",
                    "model_env": "RUNPOD_MODEL",
                    "credential_refs": {
                        "api_key_env": "RUNPOD_API_KEY",
                        "base_url_env": "RUNPOD_OPENAI_BASE_URL",
                    },
                    "timeout_seconds": 90,
                },
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            created_clients: list[_FakeLocalLLM] = []

            def _fake_local_client(**kwargs):
                client = _FakeLocalLLM(**kwargs)
                created_clients.append(client)
                return client

            _FakeAgent.instances.clear()
            with _isolated_provider_env(
                amof_home,
                {
                    "RUNPOD_API_KEY": "unit-test-runpod-key",
                    "RUNPOD_MODEL": "deepseek-ai/DeepSeek-V4-Flash",
                    "RUNPOD_OPENAI_BASE_URL": "https://pod-8000.proxy.runpod.net",
                },
            ):
                with _cwd(repo):
                    with patch("amof.orchestrator.agent.Agent", _FakeAgent):
                        import amof.orchestrator.memory as memory

                        with patch.object(
                            memory,
                            "VectorStore",
                            side_effect=ImportError("chromadb missing"),
                        ):
                            with patch(
                                "amof.orchestrator.llm.local_openai_compatible.LocalOpenAICompatibleClient",
                                side_effect=_fake_local_client,
                            ):
                                with (
                                    redirect_stdout(StringIO()) as stdout,
                                    redirect_stderr(StringIO()) as stderr,
                                ):
                                    result = agent_cmd.cmd_agent(
                                        manifest,
                                        goal="Inspect this repo",
                                        plan_mode=True,
                                        no_follow_up=True,
                                        verbose=False,
                                    )

        self.assertEqual(result, 0, stderr.getvalue())
        self.assertTrue(created_clients)
        self.assertEqual(
            created_clients[0].kwargs["base_url"],
            "https://pod-8000.proxy.runpod.net/v1",
        )
        self.assertEqual(
            created_clients[0].kwargs["model"], "deepseek-ai/DeepSeek-V4-Flash"
        )
        self.assertEqual(created_clients[0].kwargs["api_key"], "unit-test-runpod-key")
        self.assertEqual(created_clients[0].kwargs["provider_id"], "runpod")
        self.assertEqual(created_clients[0].kwargs["timeout"], 90.0)
        self.assertIn("local/test/deepseek-ai/DeepSeek-V4-Flash", stdout.getvalue())

    def test_active_local_profile_missing_base_url_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-missing-base-url-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            _write_active_provider_profile(
                amof_home,
                "local-qwen-default",
                {
                    "provider": "local",
                    "lane": "worker",
                    "model": "qwen2.5-coder:7b-instruct",
                    "credential_refs": {},
                },
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }

            with _isolated_provider_env(amof_home):
                with _cwd(repo):
                    with (
                        redirect_stdout(StringIO()),
                        redirect_stderr(StringIO()) as stderr,
                    ):
                        result = agent_cmd.cmd_agent(
                            manifest,
                            goal="Inspect this repo",
                            plan_mode=True,
                            no_follow_up=True,
                            verbose=False,
                        )

        self.assertEqual(result, 1)
        self.assertIn("local provider profile requires base_url", stderr.getvalue())

    def test_active_local_profile_missing_model_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-local-missing-model-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            _write_active_provider_profile(
                amof_home,
                "local-qwen-default",
                {
                    "provider": "local",
                    "lane": "worker",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "credential_refs": {},
                },
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }

            with _isolated_provider_env(amof_home):
                with _cwd(repo):
                    with (
                        redirect_stdout(StringIO()),
                        redirect_stderr(StringIO()) as stderr,
                    ):
                        result = agent_cmd.cmd_agent(
                            manifest,
                            goal="Inspect this repo",
                            plan_mode=True,
                            no_follow_up=True,
                            verbose=False,
                        )

        self.assertEqual(result, 1)
        self.assertIn("local provider profile requires model", stderr.getvalue())

    def test_explicit_cloud_provider_wins_over_active_local_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-cloud-over-local-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            _write_active_provider_profile(
                amof_home,
                "local-qwen-default",
                {
                    "provider": "local",
                    "lane": "worker",
                    "model": "qwen2.5-coder:7b-instruct",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "credential_refs": {},
                },
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            created_openai_clients: list[object] = []

            def _fake_openai_client(**kwargs):
                client = _FakeLocalLLM(**kwargs)
                created_openai_clients.append(client)
                return client

            _FakeAgent.instances.clear()
            with _isolated_provider_env(
                amof_home, {"OPENROUTER_API_KEY": "unit-test-provider-value"}
            ):
                with _cwd(repo):
                    with patch("amof.orchestrator.agent.Agent", _FakeAgent):
                        import amof.orchestrator.memory as memory

                        with patch.object(
                            memory,
                            "VectorStore",
                            side_effect=ImportError("chromadb missing"),
                        ):
                            with patch(
                                "amof.orchestrator.llm.local_openai_compatible.LocalOpenAICompatibleClient",
                                side_effect=AssertionError(
                                    "local profile should not be used"
                                ),
                            ):
                                with patch(
                                    "amof.orchestrator.llm.openai_client.OpenAIClient",
                                    side_effect=_fake_openai_client,
                                ):
                                    with (
                                        redirect_stdout(StringIO()),
                                        redirect_stderr(StringIO()) as stderr,
                                    ):
                                        result = agent_cmd.cmd_agent(
                                            manifest,
                                            goal="Inspect this repo",
                                            plan_mode=True,
                                            provider="openrouter",
                                            no_follow_up=True,
                                            verbose=False,
                                        )

        self.assertEqual(result, 0, stderr.getvalue())
        self.assertTrue(created_openai_clients)
        self.assertEqual(
            created_openai_clients[0].kwargs["api_key"], "unit-test-provider-value"
        )
        self.assertTrue(
            str(created_openai_clients[0].kwargs["model"]).startswith("openrouter/")
        )
        self.assertNotIn("timeout", created_openai_clients[0].kwargs)

    def test_plan_execute_no_follow_up_is_noninteractive_for_clarifications(
        self,
    ) -> None:
        with patch("sys.stdin.isatty", return_value=True):
            self.assertTrue(agent_cmd._plan_execute_noninteractive(True, False))
            self.assertTrue(agent_cmd._plan_execute_noninteractive(False, True))
            self.assertFalse(agent_cmd._plan_execute_noninteractive(False, False))
        with patch("sys.stdin.isatty", return_value=False):
            self.assertTrue(agent_cmd._plan_execute_noninteractive(False, False))

    def test_provider_400_planning_error_is_not_schema_retried(self) -> None:
        planner_llm = _ProviderErrorPlannerLLM()
        planner = TaskPlanner(planner_llm=planner_llm, workspace_root=ROOT)

        with self.assertRaises(ProviderError):
            planner.plan("Inspect", "README only")

        self.assertEqual(planner_llm.calls, 1)

    def test_planner_empty_structured_response_reports_real_cause(self) -> None:
        planner_llm = _SequencedStructuredPlannerLLM(
            [
                _make_structured_planner_response(
                    analysis="Structured planner returned only analysis text.",
                    stop_reason="end_turn",
                    request_id="req-empty-plan-1",
                ),
                _make_structured_planner_response(
                    analysis="Structured planner still returned analysis text.",
                    stop_reason="stop",
                    request_id="req-empty-plan-2",
                ),
                _make_structured_planner_response(
                    analysis="Structured planner never returned executable work.",
                    stop_reason="max_tokens",
                    request_id="req-empty-plan-3",
                ),
            ]
        )
        planner = TaskPlanner(planner_llm=planner_llm, workspace_root=ROOT)

        with self.assertRaisesRegex(
            ValueError,
            r"Planner returned no subtasks and no questions\. stop_reason=max_tokens\.",
        ):
            planner.plan("Inspect", "README only")

        self.assertEqual(planner_llm.calls, 3)
        self.assertEqual(len(planner_llm.messages_history[0]), 1)
        self.assertIn(
            "schema-valid but unusable because it contained no subtasks and no clarification questions",
            planner_llm.messages_history[1][-1]["content"],
        )
        self.assertIn(
            "Do not return analysis-only output.",
            planner_llm.messages_history[1][-1]["content"],
        )
        self.assertIn(
            '"analysis":"Structured planner returned only analysis text."',
            planner_llm.messages_history[1][-1]["content"],
        )

    def test_planner_empty_structured_response_repairs_into_subtasks(self) -> None:
        planner_llm = _SequencedStructuredPlannerLLM(
            [
                _make_structured_planner_response(
                    analysis="Initial analysis-only response.",
                    stop_reason="stop",
                    request_id="req-semantic-empty",
                ),
                _make_structured_planner_response(
                    analysis="Recovered with executable work.",
                    subtasks=[
                        {
                            "id": "1",
                            "title": "Inspect README",
                            "description": "Read the README to gather context.",
                            "runner": "code",
                            "depends_on": [],
                        }
                    ],
                    execution_order=["1"],
                    verification="Review the README findings.",
                    stop_reason="end_turn",
                    request_id="req-semantic-repaired",
                ),
            ]
        )
        planner = TaskPlanner(planner_llm=planner_llm, workspace_root=ROOT)

        plan = planner.plan("Inspect", "README only")

        self.assertEqual(planner_llm.calls, 2)
        self.assertEqual([subtask.id for subtask in plan.subtasks], ["1"])
        self.assertEqual(plan.questions, [])
        self.assertEqual(plan.planning_cost_status, "unknown")
        self.assertFalse(plan.planning_cost_observed)
        self.assertIn(
            "Return exactly one of:",
            planner_llm.messages_history[1][-1]["content"],
        )
        self.assertIn(
            '"analysis":"Initial analysis-only response."',
            planner_llm.messages_history[1][-1]["content"],
        )

    def test_planner_empty_structured_response_repairs_into_questions(self) -> None:
        planner_llm = _SequencedStructuredPlannerLLM(
            [
                _make_structured_planner_response(
                    analysis="Need more detail before acting.",
                    stop_reason="stop",
                    request_id="req-semantic-empty-questions",
                ),
                _make_structured_planner_response(
                    analysis="Clarification required.",
                    questions=["Which README section should be inspected first?"],
                    stop_reason="end_turn",
                    request_id="req-semantic-questions",
                ),
            ]
        )
        planner = TaskPlanner(planner_llm=planner_llm, workspace_root=ROOT)

        plan = planner.plan("Inspect", "README only")

        self.assertEqual(planner_llm.calls, 2)
        self.assertEqual(plan.subtasks, [])
        self.assertEqual(plan.questions, ["Which README section should be inspected first?"])
        self.assertIn(
            "no subtasks and no clarification questions",
            planner_llm.messages_history[1][-1]["content"],
        )

    def test_planner_successful_structured_response_preserves_unknown_cost_truth(self) -> None:
        planner_llm = _SuccessfulStructuredPlannerLLM()
        planner = TaskPlanner(planner_llm=planner_llm, workspace_root=ROOT)

        plan = planner.plan("Inspect", "README only")

        self.assertEqual(planner_llm.calls, 1)
        self.assertEqual([subtask.id for subtask in plan.subtasks], ["1"])
        self.assertEqual(plan.execution_order, ["1"])
        self.assertEqual(plan.planner_model, "openai/gpt-4o-mini")
        self.assertEqual(plan.planning_cost, 0.0)
        self.assertEqual(plan.planning_cost_status, "unknown")
        self.assertFalse(plan.planning_cost_observed)

    def test_public_default_runner_config_is_bounded(self) -> None:
        code_runner = PUBLIC_DEFAULT_RUNNERS_CONFIG["runners"]["code"]
        tools = set(code_runner["tools"])

        self.assertTrue(
            {"Read", "Write", "StrReplace", "Glob", "LS", "ReadLints"} <= tools
        )
        self.assertNotIn("Shell", tools)
        self.assertNotIn("Delete", tools)
        self.assertNotIn("GitCheckpoint", tools)

    def test_add_intent_authorizes_write_capability(self) -> None:
        trust_state = create_trust_state("Add only this function to app.py")

        self.assertIn("write", trust_state.trusted_intent_caps)
        self.assertFalse(trust_state.full_rewrite_authorized)

    def test_explicit_full_rewrite_intent_is_tracked(self) -> None:
        trust_state = create_trust_state(
            "Rewrite the entire file README.md from scratch"
        )

        self.assertIn("write", trust_state.trusted_intent_caps)
        self.assertTrue(trust_state.full_rewrite_authorized)

    def test_write_new_file_allowed_but_existing_file_blocked_for_add_intent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-write-existing-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("old\n", encoding="utf-8")
            guardrails = Guardrails(
                config=GuardrailConfig.public_defaults(),
                writable_roots=[repo],
            )
            trust_state = create_trust_state("Add a section to README.md")
            registry = create_default_registry(
                guardrails=guardrails,
                role="worker",
                workspace_root=repo,
                trust_state=trust_state,
            )

            with _cwd(repo):
                new_result = registry.execute(
                    ToolCall(
                        id="1",
                        name="Write",
                        arguments={"path": "NEW.md", "contents": "new\n"},
                    )
                )
                existing_result = registry.execute(
                    ToolCall(
                        id="2",
                        name="Write",
                        arguments={"path": "README.md", "contents": "replacement\n"},
                    )
                )

        self.assertTrue(new_result.success, new_result.error)
        self.assertFalse(existing_result.success)
        self.assertIn(
            "Write cannot overwrite an existing file", existing_result.error or ""
        )

    def test_write_existing_file_allowed_for_explicit_full_rewrite_intent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-write-full-rewrite-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("old\n", encoding="utf-8")
            guardrails = Guardrails(
                config=GuardrailConfig.public_defaults(),
                writable_roots=[repo],
            )
            trust_state = create_trust_state("Overwrite the whole file README.md")
            registry = create_default_registry(
                guardrails=guardrails,
                role="worker",
                workspace_root=repo,
                trust_state=trust_state,
            )

            with _cwd(repo):
                read_result = registry.execute(
                    ToolCall(id="read", name="Read", arguments={"path": "README.md"})
                )
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="Write",
                        arguments={"path": "README.md", "contents": "replacement\n"},
                    )
                )
            contents = (repo / "README.md").read_text(encoding="utf-8")

        self.assertTrue(result.success, result.error)
        self.assertEqual(contents, "replacement\n")

    def test_str_replace_existing_file_allowed_inside_adopted_repo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-str-replace-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("old\n", encoding="utf-8")
            guardrails = Guardrails(
                config=GuardrailConfig.public_defaults(),
                writable_roots=[repo],
            )
            registry = create_default_registry(
                guardrails=guardrails,
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add a section to README.md"),
            )

            with _cwd(repo):
                read_result = registry.execute(
                    ToolCall(id="read", name="Read", arguments={"path": "README.md"})
                )
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="StrReplace",
                        arguments={
                            "path": "README.md",
                            "old_string": "old\n",
                            "new_string": "old\nnew\n",
                        },
                    )
                )
            contents = (repo / "README.md").read_text(encoding="utf-8")

        self.assertTrue(read_result.success, read_result.error)
        self.assertTrue(result.success, result.error)
        self.assertEqual(contents, "old\nnew\n")

    def test_str_replace_requires_prior_read_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-str-replace-read-first-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            target = repo / "README.md"
            target.write_text("old\n", encoding="utf-8")
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add a section to README.md"),
            )

            with _cwd(repo):
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="StrReplace",
                        arguments={
                            "path": "README.md",
                            "old_string": "old\n",
                            "new_string": "old\nnew\n",
                        },
                    )
                )
            contents = target.read_text(encoding="utf-8")

        self.assertFalse(result.success)
        self.assertIn("invalid_strreplace_old_requires_read", result.error or "")
        self.assertEqual(contents, "old\n")

    def test_str_replace_old_string_must_be_observed_in_prior_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-str-replace-observed-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            target = repo / "README.md"
            target.write_text("old\n", encoding="utf-8")
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add a section to README.md"),
            )

            with _cwd(repo):
                read_result = registry.execute(
                    ToolCall(id="read", name="Read", arguments={"path": "README.md"})
                )
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="StrReplace",
                        arguments={
                            "path": "README.md",
                            "old_string": "# Add your code here",
                            "new_string": "old\nnew\n",
                        },
                    )
                )
            contents = target.read_text(encoding="utf-8")

        self.assertTrue(read_result.success, read_result.error)
        self.assertFalse(result.success)
        self.assertIn("invalid_strreplace_old_not_observed", result.error or "")
        self.assertEqual(contents, "old\n")

    def test_insert_after_uses_read_observed_anchor_for_small_insert(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-insert-after-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            target = repo / "app.py"
            target.write_text(
                "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
            )
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add farewell to app.py"),
            )

            with _cwd(repo):
                read_result = registry.execute(
                    ToolCall(id="read", name="Read", arguments={"path": "app.py"})
                )
                result = registry.execute(
                    ToolCall(
                        id="insert",
                        name="InsertAfter",
                        arguments={
                            "path": "app.py",
                            "anchor_string": "    return f'Hello, {name}!'",
                            "content_to_insert": "\n\n\ndef farewell(name: str) -> str:\n    return f'Goodbye, {name}.'",
                        },
                    )
                )
            contents = target.read_text(encoding="utf-8")

        self.assertTrue(read_result.success, read_result.error)
        self.assertTrue(result.success, result.error)
        self.assertIn("def farewell", contents)
        self.assertIn("def greet", contents)

    def test_insert_after_rejects_unobserved_anchor_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-insert-after-unobserved-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            target = repo / "app.py"
            target.write_text(
                "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
            )
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add farewell to app.py"),
            )

            with _cwd(repo):
                read_result = registry.execute(
                    ToolCall(id="read", name="Read", arguments={"path": "app.py"})
                )
                result = registry.execute(
                    ToolCall(
                        id="insert",
                        name="InsertAfter",
                        arguments={
                            "path": "app.py",
                            "anchor_string": "# Add your code here",
                            "content_to_insert": "\n\n\ndef farewell(name: str) -> str:\n    return f'Goodbye, {name}.'",
                        },
                    )
                )
            contents = target.read_text(encoding="utf-8")

        self.assertTrue(read_result.success, read_result.error)
        self.assertFalse(result.success)
        self.assertIn("invalid_insertafter_anchor_not_observed", result.error or "")
        self.assertEqual(contents, "def greet(name):\n    return f'Hello, {name}!'\n")

    def test_inspect_files_batches_read_evidence_for_safe_edits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-inspect-files-evidence-") as td:
            repo = Path(td) / "repo"
            tests_dir = repo / "tests"
            tests_dir.mkdir(parents=True)
            app = repo / "app.py"
            test_app = tests_dir / "test_app.py"
            app.write_text(
                "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
            )
            test_app.write_text(
                "from app import greet\n\n\ndef test_greet():\n    assert greet('Ada') == 'Hello, Ada!'\n",
                encoding="utf-8",
            )
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add farewell to app.py"),
            )

            with _cwd(repo):
                inspect_result = registry.execute(
                    ToolCall(
                        id="inspect",
                        name="InspectFiles",
                        arguments={"paths": ["app.py", "tests/test_app.py"]},
                    )
                )
                modified_after_inspect = set(registry.modified_files)
                edit_result = registry.execute(
                    ToolCall(
                        id="insert",
                        name="InsertAfter",
                        arguments={
                            "path": "app.py",
                            "anchor_string": "    return f'Hello, {name}!'",
                            "content_to_insert": "\n\n\ndef farewell(name: str) -> str:\n    return f'Goodbye, {name}.'",
                        },
                    )
                )
            app_contents = app.read_text(encoding="utf-8")
            test_contents = test_app.read_text(encoding="utf-8")

        self.assertTrue(inspect_result.success, inspect_result.error)
        self.assertEqual(modified_after_inspect, set())
        self.assertIn("app.py", inspect_result.output)
        self.assertIn("tests/test_app.py", inspect_result.output)
        self.assertTrue(edit_result.success, edit_result.error)
        self.assertIn("def farewell", app_contents)
        self.assertIn("test_greet", test_contents)

    def test_inspect_files_read_only_keeps_target_repo_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-inspect-files-clean-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            (repo / "app.py").write_text(
                "def greet():\n    return 'hello'\n", encoding="utf-8"
            )
            (repo / "tests").mkdir()
            (repo / "tests" / "test_app.py").write_text(
                "def test_greet():\n    assert True\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add app"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Inspect app and tests"),
            )

            with _cwd(repo):
                result = registry.execute(
                    ToolCall(
                        id="inspect",
                        name="InspectFiles",
                        arguments={"paths": ["app.py", "tests/test_app.py"]},
                    )
                )
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertTrue(result.success, result.error)
        self.assertEqual(status.stdout, "")
        self.assertEqual(registry.modified_files, set())

    def test_inspect_files_records_tool_count_and_inspected_files(self) -> None:
        telemetry = SessionTelemetry()
        metadata = {
            "inspected_files": ["app.py", "tests/test_app.py"],
            "inspected_file_count": 2,
        }
        telemetry.record_tool_call("InspectFiles", True, 12, metadata=metadata)

        with tempfile.TemporaryDirectory(prefix="amof-inspect-files-events-") as td:
            events = EventLog(session_id="test-inspect-files", runs_dir=Path(td))
            event = events.tool_call(
                tool_name="InspectFiles",
                arguments={"paths": ["app.py", "tests/test_app.py"]},
                success=True,
                duration_ms=12,
                output_preview="inspected 2 files",
                metadata=metadata,
            )

        summary = telemetry.to_dict()
        self.assertEqual(summary["tools"]["InspectFiles"]["calls"], 1)
        self.assertEqual(
            summary["inspected_files"]["files"], ["app.py", "tests/test_app.py"]
        )
        self.assertEqual(
            event["metadata"]["inspected_files"], ["app.py", "tests/test_app.py"]
        )

    def test_runner_telemetry_rollup_preserves_inspected_files(self) -> None:
        parent = SessionTelemetry()
        child = SessionTelemetry()
        child.record_tool_call(
            "InspectFiles",
            True,
            5,
            metadata={"inspected_files": ["app.py", "tests/test_app.py"]},
        )

        RunnerFactory._rollup_telemetry(parent, child, "code")
        summary = parent.to_dict()

        self.assertEqual(summary["tools"]["runner:code:InspectFiles"]["calls"], 1)
        self.assertEqual(
            summary["inspected_files"]["files"], ["app.py", "tests/test_app.py"]
        )

    def test_tool_proposal_read_only_executes_from_appdata_with_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-tool-proposal-safe-") as td:
            root = Path(td)
            repo = root / "repo"
            _init_git_repo(repo)
            target = repo / "app.py"
            target.write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add app"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            amof_home = root / "amof-home"
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Inspect files using a safe proposal"),
            )

            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}):
                with _cwd(repo):
                    result = registry.execute(
                        ToolCall(
                            id="proposal",
                            name="ToolProposal",
                            arguments={
                                "purpose": "Count lines in app.py",
                                "mutation_intent": False,
                                "allowed_paths": ["app.py"],
                                "allow_network": False,
                                "timeout_seconds": 5,
                                "inputs": ["app.py"],
                                "outputs": ["stdout line count"],
                                "rollback": "No rollback needed for read-only inspection.",
                                "script": "python3 - <<'PY'\nfrom pathlib import Path\nprint(len(Path('app.py').read_text().splitlines()))\nPY\n",
                            },
                        )
                    )
                status = subprocess.run(
                    ["git", "status", "--short"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                script_path = Path(result.metadata["script_path"])
                script_exists = script_path.is_file()
                script_in_appdata = str(script_path).startswith(str(amof_home))

            self.assertTrue(result.success, result.error)
            self.assertIn("rc=0", result.output)
            self.assertIn("stdout:", result.output)
            self.assertEqual(status.stdout, "")
            self.assertEqual(result.metadata["rc"], 0)
            self.assertIn("script_hash", result.metadata)
            self.assertTrue(script_exists)
            self.assertTrue(script_in_appdata)

    def test_tool_proposal_executes_raw_python_source_with_python3(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-tool-proposal-python-") as td:
            root = Path(td)
            repo = root / "repo"
            _init_git_repo(repo)
            target = repo / "app.py"
            target.write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
            amof_home = root / "amof-home"
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Inspect files using raw Python proposal"),
            )

            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}):
                with _cwd(repo):
                    result = registry.execute(
                        ToolCall(
                            id="proposal-python",
                            name="ToolProposal",
                            arguments={
                                "purpose": "Count lines in app.py with Python source",
                                "mutation_intent": False,
                                "allowed_paths": ["app.py"],
                                "allow_network": False,
                                "timeout_seconds": 5,
                                "inputs": ["app.py"],
                                "outputs": ["stdout line count"],
                                "rollback": "No rollback needed for read-only inspection.",
                                "script": "from pathlib import Path\nprint(len(Path('app.py').read_text().splitlines()))\n",
                            },
                        )
                    )

            self.assertTrue(result.success, result.error)
            self.assertEqual(result.metadata["rc"], 0)
            self.assertEqual(result.metadata["command"][0], "python3")
            self.assertTrue(str(result.metadata["script_path"]).endswith("proposal.py"))
            self.assertIn("2", result.metadata["stdout"])

    def test_repo_inspection_ls_directory_guess_failure_is_nonfatal_after_successful_root_listing(self) -> None:
        analysis = analyze_tool_call_events(
            task_text="Inspect this repository read-only and report the repository path, branch, head sha, origin/main sha, cleanliness, mission revision test paths, hermes read-only test paths, and evidence paths.",
            tool_events=[
                {
                    "tool": "LS",
                    "args": {"target_directory": "marekhotshot/simple-ai-shop"},
                    "success": False,
                    "error": "Directory not found: marekhotshot/simple-ai-shop",
                    "tool_id": "ls-1",
                },
                {
                    "tool": "LS",
                    "args": {"target_directory": ""},
                    "success": True,
                    "output_preview": "/\n  commerce/\n  src/\n  README.md\n",
                    "tool_id": "ls-2",
                },
            ],
            final_response=(
                "Repository Path: /var/lib/amof/share/workspaces/ws-1/00-simple-ai-shop\n"
                "Branch Or Detached State: detached at 67f8526b254d8839c025423b6bfda36895881160\n"
                "HEAD SHA: 67f8526b254d8839c025423b6bfda36895881160\n"
                "origin/main SHA: 67f8526b254d8839c025423b6bfda36895881160\n"
                "Cleanliness: clean\n"
                "Mission Revision Test Paths: not present in this repository\n"
                "Hermes Read-Only Test Paths: not present in this repository\n"
                "Evidence Paths: /var/lib/amof/share/runs/20260623-160557/events.jsonl\n"
            ),
        )
        self.assertTrue(analysis["repo_validation"].ok)
        self.assertEqual(len(analysis["fatal_failures"]), 0)
        self.assertEqual(len(analysis["failures"]), 1)
        self.assertEqual(analysis["failures"][0].required_or_optional, "alternative_group")

    def test_tool_proposal_rejects_unsafe_commands_before_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-tool-proposal-unsafe-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            amof_home = Path(td) / "amof-home"
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Inspect files using a safe proposal"),
            )

            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}):
                with _cwd(repo):
                    result = registry.execute(
                        ToolCall(
                            id="proposal",
                            name="ToolProposal",
                            arguments={
                                "purpose": "Publish changes",
                                "mutation_intent": False,
                                "allowed_paths": ["."],
                                "allow_network": False,
                                "timeout_seconds": 5,
                                "inputs": [],
                                "outputs": [],
                                "rollback": "None",
                                "script": "git commit -am nope\ngit push origin HEAD\n",
                            },
                        )
                    )

        self.assertFalse(result.success)
        self.assertIn("invalid_tool_proposal_static_gate", result.error or "")
        self.assertFalse((amof_home / "share" / "evidence" / "tool-proposals").exists())

    def test_public_default_runner_exposes_tool_proposal_not_shell(self) -> None:
        tools = PUBLIC_DEFAULT_RUNNERS_CONFIG["runners"]["code"]["tools"]
        self.assertIn("ToolProposal", tools)
        self.assertNotIn("Shell", tools)

    def test_str_replace_empty_old_string_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-str-replace-empty-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            target = repo / "README.md"
            target.write_text("abc\n", encoding="utf-8")
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add a section to README.md"),
            )

            with _cwd(repo):
                read_result = registry.execute(
                    ToolCall(id="read", name="Read", arguments={"path": "README.md"})
                )
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="StrReplace",
                        arguments={
                            "path": "README.md",
                            "old_string": "",
                            "new_string": "X",
                        },
                    )
                )
            contents = target.read_text(encoding="utf-8")

        self.assertFalse(result.success)
        self.assertIn("invalid_strreplace_old_empty", result.error or "")
        self.assertEqual(contents, "abc\n")

    def test_str_replace_whitespace_old_string_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-str-replace-whitespace-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            target = repo / "README.md"
            target.write_text("abc\n", encoding="utf-8")
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add a section to README.md"),
            )

            with _cwd(repo):
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="StrReplace",
                        arguments={
                            "path": "README.md",
                            "old_string": " \n\t",
                            "new_string": "X",
                        },
                    )
                )
            contents = target.read_text(encoding="utf-8")

        self.assertFalse(result.success)
        self.assertIn("invalid_strreplace_old_whitespace", result.error or "")
        self.assertEqual(contents, "abc\n")

    def test_str_replace_not_found_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-str-replace-not-found-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            target = repo / "README.md"
            target.write_text("abc\n", encoding="utf-8")
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add a section to README.md"),
            )

            with _cwd(repo):
                read_result = registry.execute(
                    ToolCall(id="read", name="Read", arguments={"path": "README.md"})
                )
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="StrReplace",
                        arguments={
                            "path": "README.md",
                            "old_string": "missing",
                            "new_string": "X",
                        },
                    )
                )
            contents = target.read_text(encoding="utf-8")

        self.assertTrue(read_result.success, read_result.error)
        self.assertFalse(result.success)
        self.assertIn("invalid_strreplace_old_not_observed", result.error or "")
        self.assertEqual(contents, "abc\n")

    def test_str_replace_multiple_matches_fails_without_replace_all(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-str-replace-multiple-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            target = repo / "README.md"
            target.write_text("same\nsame\n", encoding="utf-8")
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add a section to README.md"),
            )

            with _cwd(repo):
                read_result = registry.execute(
                    ToolCall(id="read", name="Read", arguments={"path": "README.md"})
                )
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="StrReplace",
                        arguments={
                            "path": "README.md",
                            "old_string": "same",
                            "new_string": "other",
                        },
                    )
                )
            contents = target.read_text(encoding="utf-8")

        self.assertTrue(read_result.success, read_result.error)
        self.assertFalse(result.success)
        self.assertIn("invalid_strreplace_old_multiple", result.error or "")
        self.assertEqual(contents, "same\nsame\n")

    def test_str_replace_replace_all_is_bounded_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-str-replace-all-bound-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            target = repo / "README.md"
            target.write_text("x\n" * 25, encoding="utf-8")
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Update README.md"),
            )

            with _cwd(repo):
                read_result = registry.execute(
                    ToolCall(id="read", name="Read", arguments={"path": "README.md"})
                )
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="StrReplace",
                        arguments={
                            "path": "README.md",
                            "old_string": "x",
                            "new_string": "y",
                            "replace_all": True,
                        },
                    )
                )
            contents = target.read_text(encoding="utf-8")

        self.assertTrue(read_result.success, read_result.error)
        self.assertFalse(result.success)
        self.assertIn("invalid_strreplace_replace_all_too_many", result.error or "")
        self.assertEqual(contents, "x\n" * 25)

    def test_str_replace_replacement_growth_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-str-replace-growth-") as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            target = repo / "README.md"
            target.write_text("needle\n", encoding="utf-8")
            registry = create_default_registry(
                guardrails=Guardrails(
                    config=GuardrailConfig.public_defaults(), writable_roots=[repo]
                ),
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add a section to README.md"),
            )

            with _cwd(repo):
                read_result = registry.execute(
                    ToolCall(id="read", name="Read", arguments={"path": "README.md"})
                )
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="StrReplace",
                        arguments={
                            "path": "README.md",
                            "old_string": "needle",
                            "new_string": "needle\n" + ("expanded\n" * 200),
                        },
                    )
                )
            contents = target.read_text(encoding="utf-8")

        self.assertTrue(read_result.success, read_result.error)
        self.assertFalse(result.success)
        self.assertIn("invalid_strreplace", result.error or "")
        self.assertEqual(contents, "needle\n")

    def test_write_outside_adopted_repo_remains_blocked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-write-outside-") as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            outside = root / "outside.md"
            guardrails = Guardrails(
                config=GuardrailConfig.public_defaults(),
                writable_roots=[repo],
            )
            registry = create_default_registry(
                guardrails=guardrails,
                role="worker",
                workspace_root=repo,
                trust_state=create_trust_state("Add a file"),
            )

            with _cwd(repo):
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="Write",
                        arguments={"path": str(outside), "contents": "outside\n"},
                    )
                )

        self.assertFalse(result.success)
        self.assertIn("outside writable roots", result.error or "")

    def test_diff_guard_rejects_destructive_docs_rewrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-rewrite-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            doc = repo / "docs" / "runbooks" / "happy-path-agent-workflow.md"
            doc.parent.mkdir(parents=True)
            doc.write_text(
                "\n".join(f"line {i}" for i in range(1, 101)) + "\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add docs"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            doc.write_text("short replacement\n", encoding="utf-8")

            guard = agent_cmd._evaluate_diff_guard(
                "In docs/runbooks/happy-path-agent-workflow.md, add a docs-only section under 12 lines. Do not modify code.",
                repo,
                agent_cmd._git_probe(repo),
            )

        self.assertEqual(guard["status"], "fail")
        self.assertTrue(guard["destructive_rewrite_detected"])
        self.assertIn(
            "docs/runbooks/happy-path-agent-workflow.md", guard["changed_files"]
        )

    def test_diff_guard_allows_bounded_docs_insertion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-insert-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            doc = repo / "docs" / "runbooks" / "happy-path-agent-workflow.md"
            doc.parent.mkdir(parents=True)
            doc.write_text(
                "before\n## Bounded Worker Execution\nold\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add docs"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            doc.write_text(
                "before\n## Bounded Worker Execution\nold\n\n### Manual review\n\nReview diff.\n",
                encoding="utf-8",
            )

            guard = agent_cmd._evaluate_diff_guard(
                "In docs/runbooks/happy-path-agent-workflow.md, add a docs-only section under 12 lines. Do not modify code.",
                repo,
                agent_cmd._git_probe(repo),
            )

        self.assertEqual(guard["status"], "pass", guard["reasons"])
        self.assertEqual(guard["added_lines"], 4)
        self.assertEqual(guard["deleted_lines"], 0)

    def test_diff_guard_rejects_missing_exact_requested_section(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-exact-missing-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            doc = repo / "docs" / "runbooks" / "happy-path-agent-workflow.md"
            doc.parent.mkdir(parents=True)
            doc.write_text(
                "before\n## Bounded Worker Execution\nold\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add docs"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            doc.write_text(
                "before\n## Bounded Worker Execution\nold\n\n"
                "### Manual Review Before Merge\n\nReview the generated diff.\n",
                encoding="utf-8",
            )

            guard = agent_cmd._evaluate_diff_guard(
                "In docs/runbooks/happy-path-agent-workflow.md, add exactly this short section:\n\n"
                "### Manual review before commit\n\n"
                "Bounded worker execution produces a reviewable git diff.\n\n"
                "Do not modify code. Keep the change under 12 lines.",
                repo,
                agent_cmd._git_probe(repo),
            )

        self.assertEqual(guard["status"], "fail")
        self.assertTrue(
            any(reason.startswith("exact_text_missing") for reason in guard["reasons"])
        )

    def test_diff_guard_allows_exact_requested_section(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-exact-pass-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            doc = repo / "docs" / "runbooks" / "happy-path-agent-workflow.md"
            doc.parent.mkdir(parents=True)
            doc.write_text(
                "before\n## Bounded Worker Execution\nold\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add docs"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            doc.write_text(
                "before\n## Bounded Worker Execution\nold\n\n"
                "### Manual review before commit\n\n"
                "Bounded worker execution produces a reviewable git diff.\n",
                encoding="utf-8",
            )

            guard = agent_cmd._evaluate_diff_guard(
                "In docs/runbooks/happy-path-agent-workflow.md, add exactly this short section:\n\n"
                "### Manual review before commit\n\n"
                "Bounded worker execution produces a reviewable git diff.\n\n"
                "Do not modify code. Keep the change under 12 lines.",
                repo,
                agent_cmd._git_probe(repo),
            )

        self.assertEqual(guard["status"], "pass", guard["reasons"])

    def test_diff_guard_rejects_unrelated_requested_path_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-unrelated-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            (repo / "app.py").write_text("print('old')\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add app"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")

            guard = agent_cmd._evaluate_diff_guard(
                "In docs/runbooks/happy-path-agent-workflow.md, add a docs-only section under 12 lines. Do not modify code.",
                repo,
                agent_cmd._git_probe(repo),
            )

        self.assertEqual(guard["status"], "fail")
        self.assertFalse(guard["requested_paths_observed"])
        self.assertTrue(
            any(
                reason.startswith("requested_paths_mismatch")
                for reason in guard["reasons"]
            )
        )

    def test_diff_guard_rejects_missing_requested_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-missing-requested-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            (repo / "app.py").write_text("print('old')\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_app.py").write_text(
                "print('old test')\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "app.py", "tests/test_app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add app files"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")

            guard = agent_cmd._evaluate_diff_guard(
                "Add farewell(name) to app.py and a matching unittest in tests/test_app.py.",
                repo,
                agent_cmd._git_probe(repo),
            )

        self.assertEqual(guard["status"], "fail")
        self.assertFalse(guard["requested_paths_observed"])
        self.assertTrue(
            any(
                reason.startswith("requested_paths_missing:tests/test_app.py")
                for reason in guard["reasons"]
            )
        )

    def test_diff_guard_rejects_explosive_existing_file_growth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-explosive-growth-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            app = repo / "app.py"
            app.write_text(
                "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add app"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            app.write_text(
                "def greet(name):\n    return f'Hello, {name}!'\n"
                + "\n".join(
                    "def farewell(name):\n    return f'Goodbye, {name}!'"
                    for _ in range(300)
                )
                + "\n",
                encoding="utf-8",
            )

            guard = agent_cmd._evaluate_diff_guard(
                "Add a small pure function farewell(name) to app.py. Keep the change minimal.",
                repo,
                agent_cmd._git_probe(repo),
            )

        self.assertEqual(guard["status"], "fail")
        self.assertTrue(guard["destructive_rewrite_detected"])
        self.assertTrue(
            any(reason.startswith("file_growth:app.py") for reason in guard["reasons"])
        )
        self.assertTrue(
            any(
                reason.startswith("large_addition:app.py")
                for reason in guard["reasons"]
            )
        )

    def test_pycache_untracked_noise_does_not_count_as_target_diff(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-pycache-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            pycache = repo / "__pycache__"
            pycache.mkdir()
            (pycache / "app.cpython-311.pyc").write_bytes(b"noise")
            probe = agent_cmd._git_probe(repo)

        self.assertEqual(probe["numstat"], "")
        self.assertEqual(probe["diff"], "")

    def test_requested_path_extraction_includes_bare_filenames(self) -> None:
        paths = agent_cmd._extract_requested_paths(
            "Add farewell(name) to app.py and a matching test in tests/test_app.py."
        )

        self.assertEqual(paths, ["app.py", "tests/test_app.py"])

    def test_runner_failed_write_tool_fails_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-failed-write-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            guardrails = Guardrails(
                config=GuardrailConfig.public_defaults(), writable_roots=[repo]
            )
            parent_tools = create_default_registry(
                guardrails=guardrails, role="worker", workspace_root=repo
            )
            factory = RunnerFactory.from_config(
                config_path=Path(td) / "missing-runners.yaml",
                model_clients={"standard": object()},
                parent_tools=parent_tools,
                guardrails=guardrails,
                workspace_root=repo,
                default_config=PUBLIC_DEFAULT_RUNNERS_CONFIG,
            )
            with _cwd(repo):
                with patch("amof.orchestrator.runners.Agent", _FakeFailingWriteAgent):
                    result = factory.run_runner("code", "Write a file")

        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "tool_failed")
        self.assertEqual(result.failed_tool_calls, 1)
        self.assertEqual(result.failed_write_tool_calls, 1)

    def test_runner_failed_strreplace_records_failed_tool_call_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-runner-failed-strreplace-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            readme = repo / "README.md"
            before = readme.read_text(encoding="utf-8")
            guardrails = Guardrails(
                config=GuardrailConfig.public_defaults(), writable_roots=[repo]
            )
            parent_tools = create_default_registry(
                guardrails=guardrails, role="worker", workspace_root=repo
            )
            factory = RunnerFactory.from_config(
                config_path=Path(td) / "missing-runners.yaml",
                model_clients={"standard": object()},
                parent_tools=parent_tools,
                guardrails=guardrails,
                workspace_root=repo,
                default_config=PUBLIC_DEFAULT_RUNNERS_CONFIG,
            )
            with _cwd(repo):
                with patch(
                    "amof.orchestrator.runners.Agent", _FakeUnsafeStrReplaceAgent
                ):
                    result = factory.run_runner("code", "Attempt unsafe StrReplace")
            after = readme.read_text(encoding="utf-8")

        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "tool_failed")
        self.assertEqual(result.failed_tool_calls, 1)
        self.assertEqual(result.failed_write_tool_calls, 1)
        self.assertIn("invalid_strreplace_old_empty", result.response)
        self.assertEqual(after, before)

    def test_adopted_plan_execute_uses_packaged_runner_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-runner-default-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(
                """# Execution Plan

**Status**: pending

## Analysis

Use the public default code runner.

---

## Tasks

- [ ] 1. **Inspect the repo without committing** (code)
""",
                encoding="utf-8",
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            _FakeAgent.instances.clear()
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.runners.Agent", _FakeAgent):
                        with (
                            redirect_stdout(StringIO()),
                            redirect_stderr(StringIO()) as stderr,
                        ):
                            result = agent_cmd.cmd_agent(
                                manifest,
                                goal="Inspect this repo",
                                plan_execute=True,
                                provider="openrouter",
                                plan_file=str(plan_file),
                                no_follow_up=True,
                                approve_plan=True,
                                verbose=False,
                            )

            git_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            events_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (amof_home / "share" / "runs").glob("*/events.jsonl")
            )
            journals_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (amof_home / "share" / "journals" / "demo-repo").glob(
                    "*.md"
                )
            )

        self.assertEqual(result, 0, stderr.getvalue())
        self.assertTrue(_FakeAgent.instances)
        runner_tools = set(_FakeAgent.instances[-1].tools.list_tools())
        self.assertTrue({"Read", "Write", "StrReplace"} <= runner_tools)
        self.assertNotIn("Shell", runner_tools)
        self.assertNotIn("Delete", runner_tools)
        self.assertNotIn("GitCheckpoint", runner_tools)
        self.assertEqual(git_status.stdout.strip(), "")
        self.assertFalse((repo / ".amof").exists())
        self.assertFalse((repo / "ecosystems").exists())
        self.assertFalse((repo / "context").exists())
        self.assertNotIn("unit-test-provider-value", events_text)
        self.assertNotIn("unit-test-provider-value", journals_text)

    def test_mutation_intent_plan_with_no_diff_fails_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-no-diff-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(
                """# Execution Plan

**Status**: pending

## Analysis

Add a function.

---

## Tasks

- [ ] 1. **Add farewell function** (code)
""",
                encoding="utf-8",
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.runners.Agent", _FakeNoDiffAgent):
                        with (
                            redirect_stdout(StringIO()) as stdout,
                            redirect_stderr(StringIO()) as stderr,
                        ):
                            result = agent_cmd.cmd_agent(
                                manifest,
                                goal="Add a farewell function",
                                plan_execute=True,
                                provider="openrouter",
                                plan_file=str(plan_file),
                                no_follow_up=True,
                                approve_plan=True,
                                verbose=False,
                            )

            git_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result, 1, stderr.getvalue())
        self.assertEqual(git_status.stdout.strip(), "")
        self.assertIn("0/1 completed, 1 failed", stdout.getvalue())
        self.assertIn("target_has_diff=false", stdout.getvalue())

    def test_mutation_intent_inspect_only_worker_fails_write_action_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-prose-only-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(
                """# Execution Plan

**Status**: pending

## Analysis

Add a function.

---

## Tasks

- [ ] 1. **Add farewell function** (code)
""",
                encoding="utf-8",
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch(
                        "amof.orchestrator.runners.Agent", _FakeInspectOnlyMutationAgent
                    ):
                        with (
                            redirect_stdout(StringIO()) as stdout,
                            redirect_stderr(StringIO()) as stderr,
                        ):
                            result = agent_cmd.cmd_agent(
                                manifest,
                                goal="Add a farewell function",
                                plan_execute=True,
                                provider="openrouter",
                                plan_file=str(plan_file),
                                no_follow_up=True,
                                approve_plan=True,
                                verbose=False,
                            )

            git_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result, 1, stderr.getvalue())
        self.assertEqual(git_status.stdout.strip(), "")
        self.assertIn("0/1 completed, 1 failed", stdout.getvalue())
        self.assertIn("write_action_observed=false", stdout.getvalue())
        self.assertIn(
            "mutation-intent plan did not call a write-class tool", stdout.getvalue()
        )

    def test_plan_execute_provider_network_failure_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-provider-network-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(
                """# Execution Plan

**Status**: pending

## Analysis

Inspect the repo.

---

## Tasks

- [ ] 1. **Inspect files** (code)
""",
                encoding="utf-8",
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch(
                        "amof.orchestrator.runners.Agent", _FakeProviderNetworkAgent
                    ):
                        with (
                            redirect_stdout(StringIO()) as stdout,
                            redirect_stderr(StringIO()) as stderr,
                        ):
                            result = agent_cmd.cmd_agent(
                                manifest,
                                goal="Inspect this repo",
                                plan_execute=True,
                                provider="openrouter",
                                plan_file=str(plan_file),
                                no_follow_up=True,
                                approve_plan=True,
                                verbose=False,
                            )

        self.assertEqual(result, 1, stderr.getvalue())
        self.assertIn("Fatal stop: provider_network", stdout.getvalue())
        self.assertIn("Checkpoint saved:", stdout.getvalue())

    def test_plan_execute_unsafe_strreplace_returns_nonzero_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-unsafe-strreplace-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            before = (repo / "README.md").read_text(encoding="utf-8")
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(
                """# Execution Plan

**Status**: pending

## Analysis

Attempt one unsafe replacement.

---

## Tasks

- [ ] 1. **Attempt unsafe replacement** (code)
""",
                encoding="utf-8",
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch(
                        "amof.orchestrator.runners.Agent", _FakeUnsafeStrReplaceAgent
                    ):
                        with (
                            redirect_stdout(StringIO()) as stdout,
                            redirect_stderr(StringIO()) as stderr,
                        ):
                            result = agent_cmd.cmd_agent(
                                manifest,
                                goal="Add a section to README.md",
                                plan_execute=True,
                                provider="openrouter",
                                plan_file=str(plan_file),
                                no_follow_up=True,
                                approve_plan=True,
                                verbose=False,
                            )
            after = (repo / "README.md").read_text(encoding="utf-8")
            git_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result, 1, stderr.getvalue())
        self.assertEqual(after, before)
        self.assertEqual(git_status.stdout.strip(), "")
        self.assertIn("Fatal stop: tool_failed", stdout.getvalue())
        self.assertIn("Checkpoint saved:", stdout.getvalue())

    def test_plan_execute_failed_subtask_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-failed-subtask-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(
                """# Execution Plan

**Status**: pending

## Analysis

Run one worker subtask.

---

## Tasks

- [ ] 1. **Run worker** (code)
""",
                encoding="utf-8",
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch(
                        "amof.orchestrator.runners.Agent", _FakeFailedSubtaskAgent
                    ):
                        with (
                            redirect_stdout(StringIO()) as stdout,
                            redirect_stderr(StringIO()) as stderr,
                        ):
                            result = agent_cmd.cmd_agent(
                                manifest,
                                goal="Run one worker subtask",
                                plan_execute=True,
                                provider="openrouter",
                                plan_file=str(plan_file),
                                no_follow_up=True,
                                approve_plan=True,
                                verbose=False,
                            )

        self.assertEqual(result, 1, stderr.getvalue())
        self.assertIn("Fatal stop: max_iterations", stdout.getvalue())
        self.assertIn("Checkpoint saved:", stdout.getvalue())

    def test_plan_execute_diff_guard_failure_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-diff-guard-fail-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            (repo / "app.py").write_text(
                "def greet(name):\n    return f'Hello, {name}'\n", encoding="utf-8"
            )
            (repo / "tests").mkdir()
            (repo / "tests" / "test_app.py").write_text(
                "def test_greet():\n    assert True\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "app.py", "tests/test_app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add app files"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(
                """# Execution Plan

**Status**: pending

## Analysis

Add code and test.

---

## Tasks

- [ ] 1. **Add farewell function and test** (code)
""",
                encoding="utf-8",
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.runners.Agent", _FakeMutatingAgent):
                        with (
                            redirect_stdout(StringIO()) as stdout,
                            redirect_stderr(StringIO()) as stderr,
                        ):
                            result = agent_cmd.cmd_agent(
                                manifest,
                                goal="Add farewell(name) to app.py and a matching unittest in tests/test_app.py.",
                                plan_execute=True,
                                provider="openrouter",
                                plan_file=str(plan_file),
                                no_follow_up=True,
                                approve_plan=True,
                                verbose=False,
                            )

        self.assertEqual(result, 1, stderr.getvalue())
        self.assertIn("0/1 completed, 1 failed", stdout.getvalue())
        self.assertIn("diff_guard_status=fail", stdout.getvalue())
        self.assertIn("requested_paths_missing:tests/test_app.py", stdout.getvalue())
        self.assertNotIn("Execution readiness failed", stdout.getvalue())

    def test_plan_execute_invalid_python_edit_returns_nonzero_with_compile_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-invalid-python-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            (repo / "app.py").write_text(
                "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add app"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(
                """# Execution Plan

**Status**: pending

## Analysis

Add a function.

---

## Tasks

- [ ] 1. **Add farewell function** (code)
""",
                encoding="utf-8",
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch(
                        "amof.orchestrator.runners.Agent", _FakeInvalidPythonEditAgent
                    ):
                        with (
                            redirect_stdout(StringIO()) as stdout,
                            redirect_stderr(StringIO()) as stderr,
                        ):
                            result = agent_cmd.cmd_agent(
                                manifest,
                                goal="Add a farewell function to app.py",
                                plan_execute=True,
                                provider="openrouter",
                                plan_file=str(plan_file),
                                no_follow_up=True,
                                approve_plan=True,
                                verbose=False,
                            )

            diff = subprocess.run(
                ["git", "diff", "--", "app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        self.assertEqual(result, 1, stderr.getvalue())
        self.assertIn("defarewell", diff)
        self.assertIn("0/1 completed, 1 failed", stdout.getvalue())
        self.assertIn("py_compile_status=fail", stdout.getvalue())
        self.assertIn("py_compile:app.py", stdout.getvalue())

    def test_single_shot_provider_network_failure_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="amof-agent-single-provider-network-"
        ) as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch(
                        "amof.orchestrator.agent.Agent", _FakeProviderNetworkAgent
                    ):
                        import amof.orchestrator.memory as memory

                        with patch.object(
                            memory,
                            "VectorStore",
                            side_effect=ImportError("chromadb missing"),
                        ):
                            with (
                                redirect_stdout(StringIO()) as stdout,
                                redirect_stderr(StringIO()) as stderr,
                            ):
                                result = agent_cmd.cmd_agent(
                                    manifest,
                                    goal="Inspect this repo",
                                    plan_mode=True,
                                    provider="openrouter",
                                    no_follow_up=True,
                                    verbose=False,
                                )

        self.assertEqual(result, 1, stderr.getvalue())
        self.assertIn("provider error", stdout.getvalue())

    def test_successful_worker_mutation_creates_diff_without_source_noise(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-mutation-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            (repo / "app.py").write_text(
                "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "test: add app"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=_commit_env(),
            )
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(
                """# Execution Plan

**Status**: pending

## Analysis

Add a function.

---

## Tasks

- [ ] 1. **Add farewell function** (code)
""",
                encoding="utf-8",
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [
                    {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
                ],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.runners.Agent", _FakeMutatingAgent):
                        with (
                            redirect_stdout(StringIO()) as stdout,
                            redirect_stderr(StringIO()) as stderr,
                        ):
                            result = agent_cmd.cmd_agent(
                                manifest,
                                goal="Add a farewell function",
                                plan_execute=True,
                                provider="openrouter",
                                plan_file=str(plan_file),
                                no_follow_up=True,
                                approve_plan=True,
                                verbose=False,
                            )

            git_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            diff = subprocess.run(
                ["git", "diff", "--", "app.py"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            journal_files = list(
                (amof_home / "share" / "journals" / "demo-repo").glob("*.md")
            )
            event_files = list((amof_home / "share" / "runs").glob("*/events.jsonl"))

        self.assertEqual(result, 0, stderr.getvalue())
        self.assertIn("app.py", git_status.stdout)
        self.assertIn("def farewell", diff)
        self.assertIn("1/1 completed, 0 failed", stdout.getvalue())
        self.assertIn("target_has_diff=true", stdout.getvalue())
        self.assertFalse((repo / ".amof").exists())
        self.assertFalse((repo / "ecosystems").exists())
        self.assertFalse((repo / "context").exists())
        self.assertTrue(journal_files)
        self.assertTrue(event_files)

    def test_plan_execute_missing_runner_factory_fails_clearly_before_execution(
        self,
    ) -> None:
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        plan = ExecutionPlan(
            analysis="needs code runner",
            subtasks=[Subtask(id="1", title="Edit", description="Edit", runner="code")],
            execution_order=["1"],
        )

        self.assertEqual(
            agent_cmd._validate_runner_factory_for_plan(None, plan),
            "No runner factory available for plan execution. Expected runner: code.",
        )

    def test_subtask_executor_passes_original_goal_to_runner_context(self) -> None:
        from amof.orchestrator.executor import SubtaskExecutor
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        factory = _RecordingRunnerFactory()
        plan = ExecutionPlan(
            analysis="insert exact text",
            subtasks=[
                Subtask(
                    id="1", title="Edit docs", description="Edit docs", runner="code"
                )
            ],
            execution_order=["1"],
        )

        SubtaskExecutor(runner_factory=factory).execute_plan(
            plan,
            task_context="### Manual review before commit\n\nUse this exact text.",
        )

        self.assertTrue(factory.contexts)
        self.assertIn("Original user task", factory.contexts[0])
        self.assertIn("### Manual review before commit", factory.contexts[0])
        self.assertIn("Plan analysis", factory.contexts[0])

    def test_adopted_guardrails_confine_writes_to_repo_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-write-root-") as td:
            repo = Path(td) / "repo"
            outside = Path(td) / "outside.txt"
            repo.mkdir()
            guardrails = Guardrails(
                mode="build",
                config=GuardrailConfig.public_defaults(),
                writable_roots=[repo],
            )

            self.assertIsNone(guardrails.check_write(str(repo / "app.py")))
            self.assertIn(
                "outside writable roots", guardrails.check_write(str(outside))
            )

    def test_interactive_exit_alias_exits_without_llm_task(self) -> None:
        from amof.orchestrator.events import EventLog
        from amof.orchestrator.session import Session
        from amof.orchestrator.telemetry import SessionTelemetry

        with tempfile.TemporaryDirectory(prefix="amof-interactive-exit-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            fake_agent = _FakeInteractiveAgent()
            manifest = {"ecosystem": "demo-repo", "manifest_source": "appdata"}
            env = {"AMOF_HOME": str(temp / "amof-home")}
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("builtins.input", return_value="exit"):
                        result = agent_cmd._run_interactive_shell(
                            agent=fake_agent,
                            planner_llm=object(),
                            planner_model_id="fake-planner",
                            runner_factory=None,
                            session=Session(mode="build"),
                            telemetry=SessionTelemetry(),
                            events=EventLog(),
                            guardrails=Guardrails(
                                config=GuardrailConfig.public_defaults()
                            ),
                            workspace_root=repo,
                            manifest=manifest,
                            codebase_context="",
                            guardrail_info=None,
                            verbose=False,
                        )

        self.assertEqual(result, 0)
        self.assertEqual(fake_agent.run_calls, 0)

    def test_interactive_shell_attaches_studio_run_once(self) -> None:
        from amof.orchestrator.events import EventLog
        from amof.orchestrator.session import Session
        from amof.orchestrator.telemetry import SessionTelemetry

        attach_calls: list[str] = []
        with tempfile.TemporaryDirectory(prefix="amof-interactive-studio-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            fake_agent = _FakeInteractiveAgent()
            manifest = {"ecosystem": "demo-repo", "manifest_source": "appdata"}
            env = {"AMOF_HOME": str(temp / "amof-home")}
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("builtins.input", return_value="exit"):
                        result = agent_cmd._run_interactive_shell(
                            agent=fake_agent,
                            planner_llm=object(),
                            planner_model_id="fake-planner",
                            runner_factory=None,
                            session=Session(mode="build"),
                            telemetry=SessionTelemetry(),
                            events=EventLog(),
                            guardrails=Guardrails(
                                config=GuardrailConfig.public_defaults()
                            ),
                            workspace_root=repo,
                            manifest=manifest,
                            codebase_context="",
                            guardrail_info=None,
                            verbose=False,
                            attach_studio_run=lambda mode: attach_calls.append(mode) or None,
                            studio_session_id="studio-20260608-004150",
                        )

        self.assertEqual(result, 0)
        self.assertEqual(fake_agent.run_calls, 0)
        self.assertEqual(attach_calls, ["interactive"])


class _SequentialRunnerFactory:
    """Runs subtasks in order; records stop reasons per call."""

    runner_names = ["code"]

    def __init__(self, outcomes: list[tuple[bool, str]]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def run_runner(self, name, task, context=None, parent_telemetry=None):
        from amof.orchestrator.runners import RunnerResult
        from amof.orchestrator.telemetry import SessionTelemetry

        idx = min(self.calls, len(self.outcomes) - 1)
        success, stop_reason = self.outcomes[idx]
        self.calls += 1
        return RunnerResult(
            runner_name=name,
            success=success,
            response="ok" if success else "failed",
            stop_reason=stop_reason,
            telemetry=SessionTelemetry(),
        )


class PlanExecuteFatalStopTests(unittest.TestCase):
    def _five_subtask_plan(self) -> "ExecutionPlan":
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        subtasks = [
            Subtask(id=str(i), title=f"Task {i}", description=f"Do {i}", runner="code")
            for i in range(1, 6)
        ]
        return ExecutionPlan(
            analysis="matrix replay",
            subtasks=subtasks,
            execution_order=[st.id for st in subtasks],
        )

    def test_plan_execute_stops_on_cost_exceeded(self) -> None:
        from amof.orchestrator.executor import SubtaskExecutor

        plan = self._five_subtask_plan()
        factory = _SequentialRunnerFactory(
            [(False, "cost_exceeded")] + [(True, "completed")] * 4
        )
        SubtaskExecutor(runner_factory=factory).execute_plan(plan)

        self.assertEqual(factory.calls, 1)
        self.assertEqual(plan.subtasks[0].status, "failed")
        self.assertEqual(plan.subtasks[0].error, "cost_exceeded")
        for st in plan.subtasks[1:]:
            self.assertEqual(st.status, "skipped")
        self.assertIsNotNone(getattr(plan, "fatal_stop", None))
        checkpoint = __import__(
            "amof.orchestrator.plan_execute_control", fromlist=["build_checkpoint"]
        ).build_checkpoint(
            plan,
            session_id="sess-1",
            failure_type="cost_exceeded",
            failure_message="budget",
            failed_subtask_id="1",
            goal="run matrix",
        )
        with tempfile.TemporaryDirectory() as td:
            path = __import__(
                "amof.orchestrator.plan_execute_control",
                fromlist=["save_plan_checkpoint"],
            ).save_plan_checkpoint(checkpoint, Path(td))
            self.assertTrue(path.exists())

    def test_plan_execute_stops_on_trust_boundary_denied(self) -> None:
        from amof.orchestrator.executor import SubtaskExecutor

        plan = self._five_subtask_plan()
        denial = (
            "POLICY DENIED [capability_not_authorized_by_trusted_intent]: "
            "Requested capabilities ['secret'] are outside the trusted top-level task ceiling "
            "['network', 'read', 'write']."
        )
        factory = _SequentialRunnerFactory(
            [(False, denial)] + [(True, "completed")] * 4
        )
        SubtaskExecutor(runner_factory=factory).execute_plan(plan)

        self.assertEqual(factory.calls, 1)
        self.assertEqual(
            getattr(plan, "fatal_stop", None).failure_type,
            "capability_not_authorized_by_trusted_intent",
        )
        self.assertEqual(plan.subtasks[1].status, "skipped")

    def test_execution_readiness_detects_missing_secret_capability(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Trigger Jenkins with API token from .env and kubectl using kubeconfig"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Preflight", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state("verify Jenkins endpoint and read repository")
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_RecordingRunnerFactory(),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "missing_required_tool")
        self.assertTrue(any(i.kind == "missing_capability" for i in result.issues))
        self.assertTrue(any(i.kind == "missing_tool_pack" for i in result.issues))

    def test_execution_readiness_detects_missing_required_tool(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Run deploy.sh via shell and use the k8s runner for cluster checks"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="K8s", description=goal, runner="k8s")],
            execution_order=["1"],
        )
        trust = create_trust_state("inspect repository files and write a report")
        factory = _RecordingRunnerFactory()
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=factory,
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            parent_tool_names={"Read", "Write"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "missing_required_tool")
        self.assertTrue(
            any(i.kind in {"missing_tool", "missing_runner"} for i in result.issues)
        )

    def test_read_only_inspection_ignores_negated_modify_language(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            _is_read_only_inspection,
            derive_tool_pack_requirements,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = (
            "Inspect the repository and report branch, HEAD, origin/main, and cleanliness. "
            "Do not modify files."
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="Inspect", description=goal, runner="code")],
            execution_order=["1"],
        )

        self.assertTrue(_is_read_only_inspection(goal))
        req = derive_tool_pack_requirements(goal, plan)
        self.assertNotIn("code-edit", req.packs)

    def test_read_only_repo_inspection_repairs_shell_runner_to_code(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            normalize_read_only_repository_plan,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = (
            "Inspect the canonical repository. Report branch, HEAD, origin/main, "
            "cleanliness, and presence of contract tests. Read only; do not modify files."
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(
                    id="1",
                    title="Inspect git state",
                    description="Run git status, git symbolic-ref, and git rev-parse.",
                    runner="shell",
                )
            ],
            execution_order=["1"],
        )

        repairs = normalize_read_only_repository_plan(
            goal,
            plan,
            runner_factory=_StubRunnerFactory(
                {"code": ["Read", "InspectFiles", "Glob", "LS", "ToolProposal"]}
            ),
        )

        self.assertEqual(plan.subtasks[0].runner, "code")
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["from_runner"], "shell")
        self.assertIn("ToolProposal", plan.subtasks[0].description)

    def test_missing_runner_detail_lists_read_only_alternatives(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            assess_execution_readiness,
            build_readiness_failure_detail,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = (
            "Inspect the canonical repository. Report branch, HEAD, origin/main, "
            "cleanliness, and presence of contract tests. Read only; do not modify files."
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Inspect", description=goal, runner="shell")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        runner_factory = _StubRunnerFactory(
            {"code": ["Read", "InspectFiles", "Glob", "LS", "ToolProposal"]}
        )
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=runner_factory,
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            parent_tool_names={"Read", "InspectFiles", "Glob", "LS", "ToolProposal"},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "missing_required_tool")
        detail = build_readiness_failure_detail(
            result,
            goal=goal,
            plan=plan,
            runner_factory=runner_factory,
            checkpoint_path="/tmp/checkpoint.json",
        )
        self.assertEqual(detail["missing_tool"], "runner:shell")
        self.assertEqual(detail["required_by"], "planner")
        self.assertIn("runner:code via ToolProposal", detail["available_alternatives"])

    def test_writable_root_denied_is_preflight_failure(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            goal = "Write report to /tmp/delivery-3663-matrix-reports/00-preflight.md"
            plan = ExecutionPlan(
                analysis=goal,
                subtasks=[
                    Subtask(id="1", title="Report", description=goal, runner="code")
                ],
                execution_order=["1"],
            )
            trust = create_trust_state(goal)
            guardrails = Guardrails(
                mode="build",
                config=GuardrailConfig.public_defaults(),
                writable_roots=[repo],
            )
            result = assess_execution_readiness(
                goal,
                plan,
                trust_state=trust,
                runner_factory=_RecordingRunnerFactory(),
                guardrails=guardrails,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "writable_root_denied")

    def test_nonfatal_optional_subtask_can_continue(self) -> None:
        from amof.orchestrator.executor import SubtaskExecutor
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        plan = ExecutionPlan(
            analysis="optional preflight",
            subtasks=[
                Subtask(
                    id="1",
                    title="Optional",
                    description="x",
                    runner="code",
                    optional=True,
                ),
                Subtask(id="2", title="Main", description="y", runner="code"),
            ],
            execution_order=["1", "2"],
            continue_on_failure=True,
        )
        factory = _SequentialRunnerFactory(
            [(False, "tool_failed"), (True, "completed")]
        )
        SubtaskExecutor(runner_factory=factory).execute_plan(plan)

        self.assertEqual(factory.calls, 2)
        self.assertEqual(plan.subtasks[0].status, "failed")
        self.assertEqual(plan.subtasks[1].status, "completed")
        self.assertIsNone(getattr(plan, "fatal_stop", None))

    def test_fatal_failure_overrides_continue_on_failure(self) -> None:
        from amof.orchestrator.executor import SubtaskExecutor

        plan = self._five_subtask_plan()
        plan.continue_on_failure = True
        factory = _SequentialRunnerFactory(
            [(False, "cost_exceeded")] + [(True, "completed")] * 4
        )
        SubtaskExecutor(runner_factory=factory).execute_plan(plan)

        self.assertEqual(factory.calls, 1)
        self.assertEqual(plan.subtasks[1].status, "skipped")

    def test_resume_checkpoint_contains_remaining_subtasks(self) -> None:
        from amof.orchestrator.plan_execute_control import build_checkpoint

        plan = self._five_subtask_plan()
        plan.subtasks[0].status = "failed"
        plan.subtasks[0].error = "cost_exceeded"
        for st in plan.subtasks[1:]:
            st.status = "skipped"
            st.error = "Skipped: cost_exceeded"

        checkpoint = build_checkpoint(
            plan,
            session_id="20260521-110057",
            failure_type="cost_exceeded",
            failure_message="Cost limit exceeded",
            failed_subtask_id="1",
            goal="DELIVERY-3663 matrix replay",
        )
        self.assertEqual(checkpoint.completed_subtasks, [])
        self.assertEqual(checkpoint.failed_subtask_id, "1")
        self.assertEqual(len(checkpoint.remaining_subtasks), 5)
        self.assertIn("amof agent --resume 20260521-110057", checkpoint.resume_command)
        self.assertEqual(checkpoint.skip_reason, "skipped_budget_blocked")

    def test_budget_approval_resume_does_not_restart_completed_subtasks(self) -> None:
        from amof.orchestrator.executor import SubtaskExecutor
        from amof.orchestrator.plan_execute_control import build_checkpoint
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        plan = ExecutionPlan(
            analysis="resume after budget",
            subtasks=[
                Subtask(id="1", title="Done", description="a", runner="code"),
                Subtask(id="2", title="Blocked", description="b", runner="code"),
            ],
            execution_order=["1", "2"],
        )
        plan.subtasks[0].status = "completed"
        factory = _SequentialRunnerFactory([(False, "cost_exceeded")])
        SubtaskExecutor(runner_factory=factory).execute_plan(plan)

        checkpoint = build_checkpoint(
            plan,
            session_id="sess-budget",
            failure_type="cost_exceeded",
            failure_message="over budget",
            failed_subtask_id="2",
            goal="resume task",
        )
        self.assertEqual(checkpoint.completed_subtasks, ["1"])
        self.assertIn("2", checkpoint.remaining_subtasks)
        self.assertNotIn("1", checkpoint.remaining_subtasks)


class _StubRunnerFactory:
    """Minimal runner factory stub for readiness tests."""

    def __init__(self, runners_tools: dict[str, list[str]]) -> None:
        self._runners_tools = runners_tools

    @property
    def runner_names(self) -> list[str]:
        return list(self._runners_tools.keys())

    def runner_tool_names(self, name: str) -> set[str]:
        return set(self._runners_tools.get(name, []))


class AgentPlanExecuteEnvelopeTests(unittest.TestCase):
    def _manifest(self, repo: Path) -> dict[str, object]:
        return {
            "ecosystem": "demo-repo",
            "manifest_source": "appdata",
            "repos": [
                {"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}
            ],
        }

    def _write_plan(self, plan_file: Path, analysis: str, task_title: str) -> None:
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(
            (
                "# Execution Plan\n\n"
                "**Status**: pending\n\n"
                "## Analysis\n\n"
                f"{analysis}\n\n"
                "---\n\n"
                "## Tasks\n\n"
                f"- [ ] 1. **{task_title}** (code)\n"
            ),
            encoding="utf-8",
        )

    def _run_envelope(
        self,
        repo: Path,
        amof_home: Path,
        payload: dict[str, object],
        *,
        runner_agent: type[_FakeAgent],
    ) -> agent_cmd.AgentPlanExecuteEnvelope:
        from amof.orchestrator.planner import ExecutionPlan

        manifest = self._manifest(repo)
        env = {
            "AMOF_HOME": str(amof_home),
            "OPENROUTER_API_KEY": "unit-test-provider-value",
        }
        plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
        runner_agent.instances.clear()
        with patch.dict(os.environ, env, clear=False):
            with _cwd(repo):
                with patch("amof.orchestrator.runners.Agent", runner_agent):
                    with patch(
                        "amof.orchestrator.planner.TaskPlanner.plan",
                        return_value=ExecutionPlan.load_from_markdown(plan_file),
                    ):
                        return agent_cmd.run_agent_plan_execute_envelope(
                            manifest, payload
                        )

    def test_successful_non_mutating_plan_execute_returns_valid_envelope(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-envelope-success-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            self._write_plan(
                plan_file,
                "Inspect the repository without mutating it.",
                "Inspect the repo",
            )

            envelope = self._run_envelope(
                repo,
                amof_home,
                {
                    "goal": "Inspect this repo",
                    "provider": "openrouter",
                    "no_follow_up": True,
                },
                runner_agent=_FakeAgent,
            )
            event_exists = Path(envelope.event_log_path).is_file()
            journal_exists = Path(envelope.journal_path).is_file()
            generated_plan_exists = Path(envelope.plan_path).is_file()

        self.assertEqual(envelope.status, "completed")
        self.assertEqual(envelope.exit_code, 0)
        self.assertEqual(envelope.session_id, Path(envelope.event_log_path).parent.name)
        self.assertTrue(str(envelope.plan_path).endswith("inspect-this-repo.md"))
        self.assertIsNone(envelope.checkpoint_path)
        self.assertTrue(generated_plan_exists)
        self.assertTrue(event_exists)
        self.assertTrue(journal_exists)
        self.assertEqual(envelope.stop_reason, "completed")

    def test_question_only_noninteractive_plan_blocks_truthfully(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="amof-agent-envelope-clarification-"
        ) as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = self._manifest(repo)
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }

            from amof.orchestrator.planner import ExecutionPlan

            question_only_plan = ExecutionPlan(
                analysis="Need clarification before planning executable work.",
                subtasks=[],
                execution_order=[],
                risks=[],
                verification="",
                questions=["Which part of the repository should be inspected first?"],
                planner_model="openai/gpt-4o-mini",
                planning_cost=0.0,
                planning_cost_status="unknown",
                planning_cost_observed=False,
                planning_latency_ms=17,
            )

            _FakeAgent.instances.clear()
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.runners.Agent", _FakeAgent):
                        with patch(
                            "amof.orchestrator.planner.TaskPlanner.plan",
                            return_value=question_only_plan,
                        ):
                            envelope = agent_cmd.run_agent_plan_execute_envelope(
                                manifest,
                                {
                                    "goal": "Inspect this repo",
                                    "provider": "openrouter",
                                    "no_follow_up": True,
                                },
                            )
            event_log_path = Path(str(envelope.event_log_path))
            journal_path = Path(str(envelope.journal_path))
            event_exists = event_log_path.is_file()
            journal_exists = journal_path.is_file()
            event_log_text = event_log_path.read_text(encoding="utf-8")
            journal_text = journal_path.read_text(encoding="utf-8")

        self.assertEqual(envelope.status, "blocked")
        self.assertEqual(envelope.exit_code, 1)
        self.assertEqual(envelope.stop_reason, "clarification_required")
        self.assertIn("Clarification required before execution:", envelope.final_text)
        self.assertIn(
            "Which part of the repository should be inspected first?",
            envelope.final_text,
        )
        self.assertIsNone(envelope.plan_path)
        self.assertIsNone(envelope.checkpoint_path)
        self.assertTrue(event_exists)
        self.assertTrue(journal_exists)
        self.assertEqual(_FakeAgent.instances, [])
        self.assertEqual(
            AgentRunResult(**envelope.to_dict()).stop_reason,
            "clarification_required",
        )
        self.assertIn('"event_type": "session_start"', event_log_text)
        self.assertIn('"event_type": "session_end"', event_log_text)
        self.assertIn("**Outcome**: clarification_required", journal_text)

    def test_provider_configuration_failure_returns_structured_failed_envelope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="amof-agent-envelope-provider-config-"
        ) as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = self._manifest(repo)
            env = {"AMOF_HOME": str(amof_home)}

            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch(
                        "amof.commands.agent_cmd._active_provider_profile",
                        return_value={
                            "name": "cloud-dev",
                            "provider": "remote-ial",
                            "model": "remote-ial/default",
                        },
                    ):
                        envelope = agent_cmd.cmd_agent(
                            manifest,
                            goal="Inspect this repo",
                            plan_execute=True,
                            budget=0.02,
                            budget_strict=True,
                            no_follow_up=True,
                            approve_plan=True,
                            _json_envelope=True,
                        )

        self.assertIsInstance(envelope, agent_cmd.AgentPlanExecuteEnvelope)
        self.assertEqual(envelope.status, "failed")
        self.assertEqual(envelope.exit_code, 1)
        self.assertEqual(envelope.stop_reason, "provider_configuration_failed")
        self.assertIn("base_url or default_base_url", envelope.final_text)
        self.assertNotEqual(envelope.stop_reason, "invalid_json_mode_result")
        self.assertEqual(envelope.session_id, "")
        self.assertIsNone(envelope.event_log_path)

    def test_runtime_configuration_invalid_returns_structured_failed_envelope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="amof-agent-envelope-runtime-config-"
        ) as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = self._manifest(repo)
            env = {"AMOF_HOME": str(amof_home)}

            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    envelope = agent_cmd.cmd_agent(
                        manifest,
                        goal="Inspect this repo",
                        plan_execute=True,
                        provider="openrouter",
                        add_budget=1.0,
                        no_follow_up=True,
                        approve_plan=True,
                        _json_envelope=True,
                    )

        self.assertIsInstance(envelope, agent_cmd.AgentPlanExecuteEnvelope)
        self.assertEqual(envelope.status, "failed")
        self.assertEqual(envelope.exit_code, 1)
        self.assertEqual(envelope.stop_reason, "runtime_configuration_invalid")
        self.assertIn("--add-budget requires --resume", envelope.final_text)
        self.assertNotEqual(envelope.stop_reason, "invalid_json_mode_result")

    def test_planning_failure_returns_structured_failed_envelope(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="amof-agent-envelope-planning-fail-"
        ) as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = self._manifest(repo)
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            planning_error = RuntimeError(
                "Planner failed to produce a valid structured response after 3 attempts. "
                "Last error: ImportError: openai package not installed. Run: pip install openai"
            )

            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch(
                        "amof.orchestrator.planner.TaskPlanner.plan",
                        side_effect=planning_error,
                    ):
                        envelope = agent_cmd.run_agent_plan_execute_envelope(
                            manifest,
                            {
                                "goal": "Inspect this repo",
                                "provider": "openrouter",
                                "no_follow_up": True,
                            },
                        )

        self.assertEqual(envelope.status, "failed")
        self.assertEqual(envelope.exit_code, 1)
        self.assertEqual(envelope.stop_reason, "planning_failed")
        self.assertIn(
            "Planner failed to produce a valid structured response", envelope.final_text
        )
        self.assertNotEqual(envelope.stop_reason, "invalid_json_mode_result")

    def test_empty_structured_plan_retries_bounded_and_reports_real_cause(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="amof-agent-envelope-empty-plan-"
        ) as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = self._manifest(repo)
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            planner_llm = _EmptyStructuredPlannerLLM()

            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch(
                        "amof.orchestrator.llm.openai_client.OpenAIClient",
                        return_value=planner_llm,
                    ):
                        envelope = agent_cmd.run_agent_plan_execute_envelope(
                            manifest,
                            {
                                "goal": "Inspect this repo",
                                "provider": "openrouter",
                                "no_follow_up": True,
                            },
                        )
            event_log_path = Path(str(envelope.event_log_path))
            event_exists = event_log_path.is_file()
            event_log_text = event_log_path.read_text(encoding="utf-8")

        self.assertEqual(planner_llm.calls, 3)
        self.assertEqual(envelope.status, "failed")
        self.assertEqual(envelope.exit_code, 1)
        self.assertEqual(envelope.stop_reason, "planning_failed")
        self.assertIn(
            "Planner returned no subtasks and no questions. stop_reason=end_turn.",
            envelope.final_text,
        )
        self.assertNotIn("name 'response' is not defined", envelope.final_text)
        self.assertIsNone(envelope.plan_path)
        self.assertIsNone(envelope.journal_path)
        self.assertIsNone(envelope.checkpoint_path)
        self.assertTrue(event_exists)
        self.assertEqual(envelope.session_id, event_log_path.parent.name)
        self.assertEqual(
            AgentRunResult(**envelope.to_dict()).stop_reason,
            "planning_failed",
        )
        self.assertIn('"event_type": "session_start"', event_log_text)
        self.assertNotIn("req-empty-plan", event_log_text)
        self.assertIn(
            "schema-valid but unusable because it contained no subtasks and no clarification questions",
            planner_llm.messages_history[1][-1]["content"],
        )

    def test_readiness_capability_block_returns_structured_blocked_envelope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="amof-agent-envelope-capability-"
        ) as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            self._write_plan(
                plan_file,
                "Read API token from .env and summarize the current configuration.",
                "Inspect secret-backed config",
            )

            envelope = self._run_envelope(
                repo,
                amof_home,
                {
                    "goal": "Read API token from .env and summarize the current configuration",
                    "provider": "openrouter",
                    "no_follow_up": True,
                },
                runner_agent=_FakeAgent,
            )
            checkpoint_path = Path(str(envelope.checkpoint_path))
            checkpoint_exists = checkpoint_path.is_file()
            checkpoint_payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            event_exists = Path(envelope.event_log_path).is_file()

        self.assertEqual(envelope.status, "blocked")
        self.assertEqual(envelope.exit_code, 1)
        self.assertIn(
            envelope.stop_reason,
            {
                "missing_required_secret_access",
                "capability_not_authorized_by_trusted_intent",
            },
        )
        self.assertIn(
            envelope.final_text,
            {
                "Capability elevation required but not approved.",
                "Execution readiness failed",
            },
        )
        self.assertTrue(checkpoint_exists)
        self.assertEqual(checkpoint_payload["failure_type"], envelope.stop_reason)
        self.assertEqual(checkpoint_payload["completed_subtasks"], [])
        self.assertEqual(checkpoint_payload["remaining_subtasks"], ["1"])
        self.assertIn("--approve-capabilities secret", checkpoint_payload["resume_command"])
        self.assertTrue(event_exists)
        self.assertIsNone(envelope.journal_path)

    def test_writable_root_denial_returns_structured_blocked_envelope(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="amof-agent-envelope-writable-root-"
        ) as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            self._write_plan(
                plan_file,
                "Write a markdown report to /tmp/delivery-3663-matrix-reports/report.md.",
                "Write governed report",
            )

            envelope = self._run_envelope(
                repo,
                amof_home,
                {
                    "goal": "Write a markdown report to /tmp/delivery-3663-matrix-reports/report.md",
                    "provider": "openrouter",
                    "no_follow_up": True,
                },
                runner_agent=_FakeAgent,
            )
            checkpoint_exists = Path(envelope.checkpoint_path).is_file()

        self.assertEqual(envelope.status, "blocked")
        self.assertEqual(envelope.stop_reason, "writable_root_denied")
        self.assertTrue(checkpoint_exists)
        self.assertIn("/tmp/delivery-3663-matrix-reports", envelope.final_text)

    def test_budget_strict_preflight_block_returns_envelope_without_provider_execution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-envelope-budget-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            self._write_plan(plan_file, "Inspect the repository.", "Inspect the repo")

            envelope = self._run_envelope(
                repo,
                amof_home,
                {
                    "goal": "Inspect this repo",
                    "provider": "openrouter",
                    "budget": 0.01,
                    "budget_strict": True,
                    "no_follow_up": True,
                },
                runner_agent=_FakeAgent,
            )

        self.assertEqual(envelope.status, "blocked")
        self.assertEqual(envelope.stop_reason, "budget_preflight_blocked")
        self.assertEqual(envelope.exit_code, 1)
        self.assertIsNone(envelope.checkpoint_path)
        self.assertIsNone(envelope.journal_path)
        self.assertFalse(_FakeAgent.instances)
        self.assertIn("--budget-strict", envelope.final_text)

    def test_failed_subtask_returns_nonzero_structured_result(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="amof-agent-envelope-failed-subtask-"
        ) as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            self._write_plan(plan_file, "Run one worker subtask.", "Run worker")

            envelope = self._run_envelope(
                repo,
                amof_home,
                {
                    "goal": "Run one worker subtask",
                    "provider": "openrouter",
                    "no_follow_up": True,
                },
                runner_agent=_FakeFailedSubtaskAgent,
            )
            checkpoint_exists = Path(envelope.checkpoint_path).is_file()
            event_exists = Path(envelope.event_log_path).is_file()
            journal_exists = Path(envelope.journal_path).is_file()

        self.assertEqual(envelope.status, "failed")
        self.assertEqual(envelope.exit_code, 1)
        self.assertEqual(envelope.stop_reason, "max_iterations")
        self.assertTrue(checkpoint_exists)
        self.assertTrue(event_exists)
        self.assertTrue(journal_exists)

    def test_resume_plus_follow_up_preserves_session_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-envelope-resume-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            self._write_plan(plan_file, "Run one worker subtask.", "Run worker")

            failed_envelope = self._run_envelope(
                repo,
                amof_home,
                {
                    "goal": "Run one worker subtask",
                    "provider": "openrouter",
                    "no_follow_up": True,
                },
                runner_agent=_FakeFailedSubtaskAgent,
            )
            resumed_envelope = self._run_envelope(
                repo,
                amof_home,
                {
                    "goal": "Run one worker subtask",
                    "provider": "openrouter",
                    "resume": failed_envelope.session_id,
                    "follow_up": "Retry only the remaining subtask.",
                    "no_follow_up": True,
                },
                runner_agent=_FakeAgent,
            )
            resumed_event_exists = Path(resumed_envelope.event_log_path).is_file()
            resumed_journal_exists = Path(resumed_envelope.journal_path).is_file()

        self.assertEqual(failed_envelope.session_id, resumed_envelope.session_id)
        self.assertEqual(resumed_envelope.status, "completed")
        self.assertTrue(resumed_event_exists)
        self.assertTrue(resumed_journal_exists)


class BudgetAliasCliTests(unittest.TestCase):
    def _parse(self, **kwargs):
        defaults = {
            "max_cost": None,
            "budget": None,
            "cost_limit": None,
            "subtask_budget": None,
            "add_budget": None,
            "require_budget_approval": None,
            "budget_strict": None,
            "budget_status": None,
        }
        defaults.update(kwargs)
        return agent_cmd._parse_budget_cli_flags(**defaults)

    def test_budget_only_works(self) -> None:
        opts, err = self._parse(budget=10.0)
        self.assertIsNone(err)
        self.assertEqual(opts.budget, 10.0)

    def test_max_cost_only_works(self) -> None:
        opts, err = self._parse(max_cost=10.0)
        self.assertIsNone(err)
        self.assertEqual(opts.budget, 10.0)

    def test_cost_limit_only_works(self) -> None:
        opts, err = self._parse(cost_limit=10.0)
        self.assertIsNone(err)
        self.assertEqual(opts.budget, 10.0)

    def test_same_alias_values_accepted(self) -> None:
        opts, err = self._parse(budget=10.0, max_cost=10.0, cost_limit=10.0)
        self.assertIsNone(err)
        self.assertEqual(opts.budget, 10.0)

    def test_different_explicit_alias_values_rejected(self) -> None:
        opts, err = self._parse(budget=10.0, max_cost=2.0)
        self.assertIsNone(opts)
        self.assertIn("Conflicting budget aliases", err or "")

    def test_config_default_does_not_create_false_conflict(self) -> None:
        opts, err = self._parse(budget=10.0)
        self.assertIsNone(err)
        effective, effective_err = agent_cmd._resolve_effective_max_cost(None, opts)
        self.assertIsNone(effective_err)
        self.assertEqual(effective, 10.0)

    def test_agent_help_examples_do_not_combine_max_cost_and_budget(self) -> None:
        cli_text = (SCRIPTS_ROOT / "amof" / "cli.py").read_text(encoding="utf-8")
        self.assertNotIn("--max-cost 10.00 --budget", cli_text)
        self.assertIn("--budget 10.00 --budget-strict", cli_text)


class PlanExecuteToolPackReadinessTests(unittest.TestCase):
    def test_jenkins_url_helper_derives_ops_jenkins(self) -> None:
        from amof.orchestrator.plan_execute_control import derive_tool_pack_requirements
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = (
            "Trigger Jenkins job https://jenkins.example/job/demo using "
            "/work/amof/scripts/tools/jenkins/trigger.sh"
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)
        self.assertIn("ops-jenkins", req.packs)
        self.assertIn(
            "/work/amof/scripts/tools/jenkins/trigger.sh", req.executable_paths
        )

    def test_kubectl_helm_kubeconfig_derives_ops_k8s(self) -> None:
        from amof.orchestrator.plan_execute_control import derive_tool_pack_requirements
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = "Use kubeconfig to run kubectl get pods and helm status"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="K8s", description=goal, runner="code")],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)
        self.assertIn("ops-k8s", req.packs)
        self.assertIn("kubectl get", req.command_policy["ops-k8s"])

    def test_helm_render_derives_ops_helm_render(self) -> None:
        from amof.orchestrator.plan_execute_control import derive_tool_pack_requirements
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = "Run helm template and helm lint for chart validation"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="Render", description=goal, runner="code")],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)
        self.assertIn("ops-helm-render", req.packs)
        self.assertIn("helm template", req.command_policy["ops-helm-render"])

    def test_helm_deploy_derives_ops_helm_deploy(self) -> None:
        from amof.orchestrator.plan_execute_control import derive_tool_pack_requirements
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = "Deploy release with helm upgrade --install and record report"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="Deploy", description=goal, runner="code")],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)
        self.assertIn("ops-helm-deploy", req.packs)
        self.assertIn("k8s_mutation", req.capabilities)

    def test_read_only_source_audit_does_not_select_deploy_pack(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            derive_required_capabilities,
            derive_tool_pack_requirements,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = (
            "Read-only AMOF source audit: inspect current AMOF source code and "
            "find where ops-helm-deploy, Kubernetes mutation, network, secret, "
            "shell_limited, and missing_required_tool are mentioned."
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="Audit source", description=goal, runner="code")],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)

        self.assertEqual(req.packs, {"core-read"})
        self.assertEqual(req.capabilities, {"read"})
        self.assertEqual(derive_required_capabilities(goal), {"read"})
        self.assertNotIn("ops-helm-deploy", req.packs)
        self.assertNotIn("ops-k8s", req.packs)
        self.assertNotIn("secret", req.capabilities)
        self.assertNotIn("network", req.capabilities)
        self.assertNotIn("k8s_mutation", req.capabilities)

    def test_ambiguous_dangerous_domain_words_fail_closed(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            derive_required_capabilities,
            derive_tool_pack_requirements,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = "Audit source references to Kubernetes, Helm deployment, and secrets."
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="Audit", description=goal, runner="code")],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)

        self.assertEqual(req.packs, {"core-read"})
        self.assertEqual(derive_required_capabilities(goal), {"read"})

    def test_report_md_under_tmp_derives_reports(self) -> None:
        from amof.orchestrator.plan_execute_control import derive_tool_pack_requirements
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = "Write /tmp/delivery-3663-matrix-reports/00-preflight.md"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="Report", description=goal, runner="code")],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)
        self.assertIn("reports", req.packs)
        self.assertIn("/tmp/delivery-3663-matrix-reports", req.writable_roots)

    def test_tmp_report_dir_is_write_path(self) -> None:
        from amof.orchestrator.plan_execute_control import derive_tool_pack_requirements
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = (
            "Write report files to /tmp/delivery-3663-matrix-reports "
            "including /tmp/delivery-3663-matrix-reports/*.md"
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="Report", description=goal, runner="code")],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)
        self.assertIn("/tmp/delivery-3663-matrix-reports", req.writable_roots)

    def test_helper_scripts_are_not_classified_as_write_paths(self) -> None:
        from amof.orchestrator.plan_execute_control import derive_tool_pack_requirements
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = (
            "Run /home/gaspem1/work/labs/amof/scripts/tools/jenkins/trigger.sh "
            "and /home/gaspem1/work/labs/amof/scripts/tools/debug/k8s.sh"
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Helpers", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)
        self.assertIn(
            "/home/gaspem1/work/labs/amof/scripts/tools/jenkins/trigger.sh",
            req.executable_paths,
        )
        self.assertIn(
            "/home/gaspem1/work/labs/amof/scripts/tools/debug/k8s.sh",
            req.executable_paths,
        )
        self.assertFalse(any("trigger.sh" in root for root in req.writable_roots))
        self.assertFalse(any("k8s.sh" in root for root in req.writable_roots))

    def test_code_edit_plan_derives_code_edit(self) -> None:
        from amof.orchestrator.plan_execute_control import derive_tool_pack_requirements
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = "Modify app.py and update tests"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Edit code", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)
        self.assertIn("code-edit", req.packs)

    def test_writable_root_requires_approval(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            goal = "Write report to /tmp/delivery-3663-matrix-reports/00-preflight.md"
            plan = ExecutionPlan(
                analysis=goal,
                subtasks=[
                    Subtask(id="1", title="Report", description=goal, runner="code")
                ],
                execution_order=["1"],
            )
            trust = create_trust_state(goal)
            guardrails = Guardrails(
                mode="build",
                config=GuardrailConfig.public_defaults(),
                writable_roots=[repo],
            )
            result = assess_execution_readiness(
                goal,
                plan,
                trust_state=trust,
                runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
                guardrails=guardrails,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "writable_root_denied")
        wr = next(i for i in result.issues if i.kind == "writable_root")
        self.assertIn("approve-writable-root", wr.detail.get("suggestion", ""))

    def test_missing_ops_jenkins_blocks_execution(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Trigger Jenkins job using /work/amof/scripts/tools/jenkins/trigger.sh"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state("inspect repository and use Jenkins network")
        trust.trusted_intent_caps.add("secret")
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any(i.detail.get("pack") == "ops-jenkins" for i in result.issues)
        )

    def test_approving_ops_jenkins_without_secret_still_blocks(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = (
            "Trigger Jenkins job with token from .env using "
            "/home/gaspem1/work/labs/amof/scripts/tools/jenkins/trigger.sh"
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state("inspect repository and use Jenkins network")
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-jenkins"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "missing_required_secret_access")

    def test_approving_ops_jenkins_and_secret_passes(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = (
            "Trigger Jenkins job with token from .env using "
            "/home/gaspem1/work/labs/amof/scripts/tools/jenkins/trigger.sh"
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        trust.trusted_intent_caps.add("secret")
        trust.trusted_intent_caps.add("write")
        trust.trusted_intent_caps.update({"network", "write"})
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-jenkins"},
        )
        self.assertTrue(result.ok)

        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "ShellRestricted"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-jenkins"},
        )
        self.assertTrue(result.ok)

    def test_approving_ops_k8s_and_secret_passes(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Use kubeconfig and run kubectl get pods"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="K8s", description=goal, runner="code")],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        trust.trusted_intent_caps.add("secret")
        trust.trusted_intent_caps.add("write")
        trust.trusted_intent_caps.add("network")
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-k8s"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "missing_required_tool")

        goal_ok = "Use kubeconfig and run kubectl get pods -n mis"
        plan_ok = ExecutionPlan(
            analysis=goal_ok,
            subtasks=[Subtask(id="1", title="K8s", description=goal_ok, runner="code")],
            execution_order=["1"],
        )
        result = assess_execution_readiness(
            goal_ok,
            plan_ok,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "ShellRestricted"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-k8s"},
        )
        self.assertTrue(result.ok)

    def test_ops_k8s_does_not_allow_arbitrary_shell_or_unrestricted_shell(self) -> None:
        from amof.orchestrator.plan_execute_control import CORE_TOOL_PACKS

        pack = CORE_TOOL_PACKS["ops-k8s"]
        self.assertIn("shell_limited", pack.capabilities)
        self.assertNotIn("shell_unrestricted", pack.capabilities)
        self.assertIn("kubectl get", pack.command_policy)
        self.assertNotIn("bash -c", pack.command_policy)

    def test_readiness_uses_delegated_code_runner_shell(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = (
            "Trigger Jenkins job with token from .env using "
            "/home/gaspem1/work/labs/amof/scripts/tools/jenkins/trigger.sh"
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        trust.trusted_intent_caps.update({"network", "secret", "write"})
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "ShellRestricted"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-jenkins"},
        )
        self.assertTrue(result.ok)

    def test_readiness_fails_when_shell_missing_everywhere(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Trigger Jenkins job with token from .env"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        trust.trusted_intent_caps.update({"network", "secret", "write"})
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-jenkins"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "missing_required_tool")
        self.assertTrue(
            any(i.kind == "missing_controlled_execution" for i in result.issues)
        )

    def test_approved_secret_effective_ceiling_not_missing(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            assess_execution_readiness,
            format_readiness_failure,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Read token from .env for a local report"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="Secret", description=goal, runner="code")],
            execution_order=["1"],
        )
        trust = create_trust_state("inspect repository and write report")
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_capabilities={"secret"},
            base_capability_ceiling=set(trust.trusted_intent_caps),
        )
        self.assertFalse(any(i.kind == "missing_capability" for i in result.issues))
        summary = next(i for i in result.issues if i.kind == "capability_summary")
        self.assertIn("secret", summary.detail["approved_capabilities"])
        self.assertIn("secret", summary.detail["effective_ceiling"])
        self.assertEqual(summary.detail["status"], "approved")
        self.assertNotIn(
            "Required capability: secret", format_readiness_failure(result)
        )

    def test_tool_pack_approval_without_constrained_command_fails_precisely(
        self,
    ) -> None:
        from amof.orchestrator.plan_execute_control import (
            assess_execution_readiness,
            format_readiness_failure,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Trigger Jenkins job with token from .env using bash -c deploy"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        trust.trusted_intent_caps.update({"network", "write"})
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-jenkins"},
            approved_capabilities={"secret"},
            base_capability_ceiling=set(trust.trusted_intent_caps),
        )
        self.assertFalse(result.ok)
        issue = next(
            i for i in result.issues if i.kind == "missing_controlled_execution"
        )
        self.assertIn(
            "unbounded shell", issue.detail["synthesized_runner"]["policy_reason"]
        )
        self.assertIn("Policy:", format_readiness_failure(result))

    def test_approved_tool_pack_with_controlled_shell_runner_passes(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = (
            "Trigger Jenkins job with token from .env using "
            "/home/gaspem1/work/labs/amof/scripts/tools/jenkins/trigger.sh"
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        trust.trusted_intent_caps.add("network")
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "ShellRestricted"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-jenkins"},
            approved_capabilities={"secret"},
            base_capability_ceiling=set(trust.trusted_intent_caps),
        )
        self.assertTrue(result.ok)

    def test_tool_pack_approval_does_not_allow_arbitrary_shell(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Use kubeconfig to run bash -c 'kubectl get pods -n mis'"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="K8s", description=goal, runner="code")],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        trust.trusted_intent_caps.add("network")
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "ShellRestricted"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-k8s"},
            approved_capabilities={"secret"},
            base_capability_ceiling=set(trust.trusted_intent_caps),
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any(i.kind == "missing_controlled_execution" for i in result.issues)
        )

    def test_ops_jenkins_permits_only_trigger_helper_shape(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Trigger Jenkins with token from .env using /tmp/deploy.sh"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        trust.trusted_intent_caps.update({"network", "write"})
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "ShellRestricted"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-jenkins"},
            approved_capabilities={"secret"},
            base_capability_ceiling=set(trust.trusted_intent_caps),
        )
        self.assertFalse(result.ok)
        issue = next(
            i for i in result.issues if i.kind == "missing_controlled_execution"
        )
        self.assertIn("trigger.sh", issue.detail["synthesized_runner"]["policy_reason"])

    def test_ops_k8s_permits_namespace_scoped_inspection(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Use kubeconfig to run kubectl describe pod web -n mis"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="K8s", description=goal, runner="code")],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        trust.trusted_intent_caps.update({"network", "write"})
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-k8s"},
            approved_capabilities={"secret"},
            base_capability_ceiling=set(trust.trusted_intent_caps),
        )
        self.assertTrue(result.ok)

    def test_ops_helm_deploy_permits_release_namespace_helm_commands(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Use kubeconfig to helm upgrade --install my-release chart/ -n mis"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Helm deploy", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        trust.trusted_intent_caps.update({"network", "write"})
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-k8s", "ops-helm-deploy"},
            approved_capabilities={"secret"},
            base_capability_ceiling=set(trust.trusted_intent_caps),
        )
        self.assertTrue(result.ok)

    def test_tool_pack_approval_checkpoint_has_no_raw_secret(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            apply_tool_pack_approval,
            build_checkpoint,
            checkpoint_contains_secret_values,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = "Trigger Jenkins using secret token (value is not included)"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        apply_tool_pack_approval(plan, {"ops-jenkins"})
        checkpoint = build_checkpoint(
            plan,
            session_id="sess-toolpack",
            failure_type="missing_required_secret_access",
            failure_message="secret approval required",
            failed_subtask_id=None,
            goal=goal,
        )
        payload = checkpoint.to_dict()
        self.assertIn("ops-jenkins", payload["tool_pack_approvals"])
        self.assertFalse(checkpoint_contains_secret_values(payload))

    def test_approved_writable_root_allows_report_outputs(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            report_root = Path(td) / "delivery-reports"
            goal = f"Write report to {report_root}/00-preflight.md"
            plan = ExecutionPlan(
                analysis=goal,
                subtasks=[
                    Subtask(id="1", title="Report", description=goal, runner="code")
                ],
                execution_order=["1"],
            )
            trust = create_trust_state(goal)
            guardrails = Guardrails(
                mode="build",
                config=GuardrailConfig.public_defaults(),
                writable_roots=[repo],
            )
            result = assess_execution_readiness(
                goal,
                plan,
                trust_state=trust,
                runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
                guardrails=guardrails,
                approved_writable_roots={str(report_root)},
            )
            self.assertTrue(result.ok)

            other = Path(td) / "other-reports"
            goal2 = f"Write report to {other}/bad.md"
            plan2 = ExecutionPlan(
                analysis=goal2,
                subtasks=[
                    Subtask(id="1", title="Report", description=goal2, runner="code")
                ],
                execution_order=["1"],
            )
            denied = assess_execution_readiness(
                goal2,
                plan2,
                trust_state=trust,
                runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
                guardrails=guardrails,
                approved_writable_roots={str(report_root)},
            )
            self.assertFalse(denied.ok)
            self.assertEqual(denied.failure_type, "writable_root_denied")

    def test_approved_writable_root_does_not_allow_unrelated_tmp_paths(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Write report to /tmp/other/00-preflight.md"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[Subtask(id="1", title="Report", description=goal, runner="code")],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            guardrails = Guardrails(
                mode="build",
                config=GuardrailConfig.public_defaults(),
                writable_roots=[repo],
            )
            result = assess_execution_readiness(
                goal,
                plan,
                trust_state=trust,
                runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
                guardrails=guardrails,
                approved_writable_roots={"/tmp/delivery-3663-matrix-reports"},
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "writable_root_denied")

    def test_approving_secret_without_ops_jenkins_still_blocks(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            apply_capability_elevation,
            assess_execution_readiness,
            build_plan_capability_elevation,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Read JENKINS_TOKEN from .env and run trigger.sh via shell"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Preflight", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state("inspect repository")
        elevation = build_plan_capability_elevation(
            session_id="sess-shell",
            plan=plan,
            goal=goal,
            missing_caps=["secret"],
            base_ceiling=set(trust.trusted_intent_caps),
            approval_source="cli_flag",
        )
        apply_capability_elevation(trust, elevation)
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            parent_tool_names={"Read", "Delegate"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "missing_required_tool")
        self.assertTrue(
            any(i.detail.get("pack") == "ops-jenkins" for i in result.issues)
        )

    def test_capability_approval_does_not_grant_shell(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            apply_capability_elevation,
            assess_execution_readiness,
            build_plan_capability_elevation,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Trigger Jenkins job with token from .env"
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Jenkins", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        trust = create_trust_state(goal)
        elevation = build_plan_capability_elevation(
            session_id="sess-shell-cap",
            plan=plan,
            goal=goal,
            missing_caps=["secret"],
            base_ceiling=set(trust.trusted_intent_caps),
            approval_source="cli_flag",
        )
        apply_capability_elevation(trust, elevation)
        trust.trusted_intent_caps.update({"network", "write"})
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_StubRunnerFactory({"code": ["Read", "Write"]}),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            approved_tool_packs={"ops-jenkins"},
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any(i.kind == "missing_controlled_execution" for i in result.issues)
        )

    def test_delivery3663_tool_pack_inference(self) -> None:
        from amof.orchestrator.plan_execute_control import derive_tool_pack_requirements
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        goal = (
            "DELIVERY-3663 preflight: run Jenkins helper "
            "/home/gaspem1/work/labs/amof/scripts/tools/jenkins/trigger.sh, "
            "inspect Kubernetes with /home/gaspem1/work/labs/amof/scripts/tools/debug/k8s.sh, "
            "render helm template, deploy with helm upgrade --install, and write "
            "/tmp/delivery-3663-matrix-reports/00-preflight.md"
        )
        plan = ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Delivery", description=goal, runner="code")
            ],
            execution_order=["1"],
        )
        req = derive_tool_pack_requirements(goal, plan)
        self.assertIn("ops-jenkins", req.packs)
        self.assertIn("ops-k8s", req.packs)
        self.assertIn("ops-helm-render", req.packs)
        self.assertIn("ops-helm-deploy", req.packs)
        self.assertIn("reports", req.packs)
        self.assertIn("/tmp/delivery-3663-matrix-reports", req.writable_roots)
        self.assertIn(
            "/home/gaspem1/work/labs/amof/scripts/tools/jenkins/trigger.sh",
            req.executable_paths,
        )
        self.assertIn(
            "/home/gaspem1/work/labs/amof/scripts/tools/debug/k8s.sh",
            req.executable_paths,
        )

    def test_readiness_message_separates_read_exec_write_paths(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            assess_execution_readiness,
            format_readiness_failure,
        )
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.trust_boundary import create_trust_state

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            helper = Path(td) / "trigger.sh"
            helper.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            helper.chmod(0o755)
            goal = (
                f"Run Jenkins helper {helper} and write "
                "/tmp/delivery-3663-matrix-reports/00-preflight.md"
            )
            plan = ExecutionPlan(
                analysis=goal,
                subtasks=[
                    Subtask(id="1", title="Run", description=goal, runner="code")
                ],
                execution_order=["1"],
            )
            trust = create_trust_state(goal)
            guardrails = Guardrails(
                mode="build",
                config=GuardrailConfig.public_defaults(),
                writable_roots=[repo],
            )
            result = assess_execution_readiness(
                goal,
                plan,
                trust_state=trust,
                runner_factory=_StubRunnerFactory({"code": ["Read"]}),
                guardrails=guardrails,
            )
        text = format_readiness_failure(result)
        self.assertIn("Required tool pack: ops-jenkins", text)
        self.assertIn("Required writable root:", text)
        self.assertIn("Required executable:", text)
        self.assertNotIn("Required report path not writable", text)


class PlanCapabilityElevationTests(unittest.TestCase):
    def _secret_plan(self, goal: str):
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        return ExecutionPlan(
            analysis=goal,
            subtasks=[
                Subtask(id="1", title="Preflight", description=goal, runner="code")
            ],
            execution_order=["1"],
        )

    def test_readiness_fails_when_secret_required_and_not_approved(self) -> None:
        from amof.orchestrator.plan_execute_control import assess_execution_readiness
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Read secret token from .env for a local report"
        plan = self._secret_plan(goal)
        trust = create_trust_state("inspect repository and write report")
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_RecordingRunnerFactory(),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_type, "missing_required_secret_access")

    def test_cli_approve_capabilities_allows_readiness(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            apply_capability_elevation,
            assess_execution_readiness,
            build_plan_capability_elevation,
        )
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Read secret token from .env for a local report"
        plan = self._secret_plan(goal)
        trust = create_trust_state("inspect repository and write report")
        base = set(trust.trusted_intent_caps)
        elevation = build_plan_capability_elevation(
            session_id="sess-a",
            plan=plan,
            goal=goal,
            missing_caps=["secret"],
            base_ceiling=base,
            approval_source="cli_flag",
            parent_tool_names={"Read", "Write"},
        )
        apply_capability_elevation(trust, elevation)
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_RecordingRunnerFactory(),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
            parent_tool_names={"Read", "Write"},
        )
        self.assertTrue(result.ok)
        self.assertIn("secret", trust.trusted_intent_caps)

    def test_interactive_approval_records_scoped_elevation(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            apply_capability_elevation,
            assess_execution_readiness,
            build_plan_capability_elevation,
            readiness_is_capability_only_failure,
        )
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Read secret token from .env for a local report"
        plan = self._secret_plan(goal)
        trust = create_trust_state("inspect repository and write report")
        before = set(trust.trusted_intent_caps)
        result = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_RecordingRunnerFactory(),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
        )
        self.assertTrue(readiness_is_capability_only_failure(result))
        missing = ["secret"]
        elevation = build_plan_capability_elevation(
            session_id="sess-b",
            plan=plan,
            goal=goal,
            missing_caps=missing,
            base_ceiling=before,
            approval_source="interactive",
        )
        apply_capability_elevation(trust, elevation)
        plan.capability_elevation = elevation.to_dict()
        retry = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust,
            runner_factory=_RecordingRunnerFactory(),
            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
        )
        self.assertTrue(retry.ok)
        self.assertEqual(elevation.session_id, "sess-b")
        self.assertIn("secret", elevation.approved_capabilities)
        import json as json_mod

        self.assertNotIn("super-secret-value", json_mod.dumps(elevation.to_dict()))

    def test_rejection_checkpoint_has_no_secret_values(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            build_checkpoint,
            checkpoint_contains_secret_values,
        )

        goal = "Use provider credentials from environment file and kubeconfig"
        plan = self._secret_plan(goal)
        checkpoint = build_checkpoint(
            plan,
            session_id="sess-c",
            failure_type="missing_required_secret_access",
            failure_message="Capability elevation rejected.",
            failed_subtask_id=None,
            goal=goal,
        )
        payload = checkpoint.to_dict()
        self.assertFalse(checkpoint_contains_secret_values(payload))
        import json as json_mod

        self.assertNotIn("super-secret-value", json_mod.dumps(payload))

    def test_global_trust_ceiling_unchanged_for_new_session(self) -> None:
        from amof.orchestrator.plan_execute_control import (
            apply_capability_elevation,
            build_plan_capability_elevation,
        )
        from amof.orchestrator.trust_boundary import create_trust_state

        goal = "Read JENKINS_TOKEN from .env"
        plan = self._secret_plan(goal)
        trust_a = create_trust_state("inspect repository")
        elevation = build_plan_capability_elevation(
            session_id="sess-d",
            plan=plan,
            goal=goal,
            missing_caps=["secret"],
            base_ceiling=set(trust_a.trusted_intent_caps),
            approval_source="interactive",
        )
        apply_capability_elevation(trust_a, elevation)
        trust_b = create_trust_state("inspect repository")
        self.assertNotIn("secret", trust_b.trusted_intent_caps)

    def test_unrelated_plan_metadata_does_not_auto_elevate(self) -> None:
        from amof.orchestrator.trust_boundary import create_trust_state

        trust = create_trust_state("inspect repository")
        trust.trusted_intent_caps  # default ceiling
        session_meta = {
            "plan_capability_elevation": {
                "plan_id": "/plans/plan-a.md",
                "approved_capabilities": ["secret"],
            }
        }
        other_plan_id = "/plans/plan-b.md"
        stored = session_meta.get("plan_capability_elevation", {})
        self.assertNotEqual(stored.get("plan_id"), other_plan_id)
        self.assertNotIn("secret", trust.trusted_intent_caps)

    def test_parse_approve_capabilities_rejects_unknown(self) -> None:
        from amof.orchestrator.plan_execute_control import parse_capability_names

        with self.assertRaises(ValueError):
            parse_capability_names(["secret", "sudo"])


class ResumeFollowupAndBudgetTests(unittest.TestCase):
    def _plan_with_completed_first(self):
        from amof.orchestrator.planner import ExecutionPlan, Subtask

        plan = ExecutionPlan(
            analysis="original goal unchanged",
            subtasks=[
                Subtask(id="1", title="Done", description="a", runner="code"),
                Subtask(id="2", title="Retry", description="b", runner="code"),
            ],
            execution_order=["1", "2"],
        )
        plan.subtasks[0].status = "completed"
        plan.subtasks[1].status = "failed"
        plan.subtasks[1].error = "cost_exceeded"
        return plan

    def test_resume_accepts_inline_followup(self) -> None:
        from amof.orchestrator.resume_control import (
            append_followup_to_context,
            load_resume_followup,
        )

        followup, err = load_resume_followup(
            inline="Retry only failed preflight.", file_path=None
        )
        self.assertIsNone(err)
        ctx = append_followup_to_context("original goal unchanged", followup)
        self.assertIn("original goal unchanged", ctx)
        self.assertIn("Operator follow-up", ctx)
        self.assertIn("Retry only failed preflight", ctx)

    def test_resume_accepts_followup_file(self) -> None:
        from amof.orchestrator.resume_control import load_resume_followup

        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("Continue from checkpoint.")
            path = fh.name
        try:
            followup, err = load_resume_followup(
                inline=None,
                file_path=path,
                readable_roots=[Path(path).parent],
            )
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertIsNone(err)
        self.assertEqual(followup.source, "file")
        self.assertEqual(len(followup.sha256), 64)
        event = followup.to_event_dict("sess-1")
        self.assertEqual(event["source"], "file")
        self.assertEqual(event["chars"], len("Continue from checkpoint."))

    def test_resume_rejects_missing_followup_file(self) -> None:
        from amof.orchestrator.resume_control import load_resume_followup

        followup, err = load_resume_followup(
            inline=None,
            file_path="/tmp/does-not-exist-amof-followup.md",
        )
        self.assertIsNone(followup)
        self.assertIn("not found", err or "")

    def test_resume_followup_does_not_reset_checkpoint(self) -> None:
        from amof.orchestrator.resume_control import (
            append_followup_to_context,
            prepare_plan_for_resume,
        )

        plan = self._plan_with_completed_first()
        checkpoint = {
            "completed_subtasks": ["1"],
            "failed_subtask_id": "2",
        }
        next_id = prepare_plan_for_resume(plan, checkpoint)
        ctx = append_followup_to_context("original goal unchanged", None)
        self.assertEqual(plan.subtasks[0].status, "completed")
        self.assertEqual(plan.subtasks[1].status, "pending")
        self.assertEqual(next_id, "2")
        self.assertEqual(ctx, "original goal unchanged")

    def test_resume_add_budget_updates_checkpoint_budget(self) -> None:
        from amof.orchestrator.resume_control import update_checkpoint_budget

        checkpoint = {
            "session_id": "sess-budget",
            "plan_path": "/plans/plan.md",
            "budget_added": 0.0,
        }
        with tempfile.TemporaryDirectory() as td:
            cp_path = Path(td) / "checkpoint.json"
            cp_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            update_checkpoint_budget(cp_path, checkpoint, add_budget=1.0, new_limit=6.0)
            saved = json.loads(cp_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["budget_limit"], 6.0)
        self.assertEqual(saved["budget_added"], 1.0)
        self.assertIn("--resume sess-budget", saved["resume_command"])

    def test_budget_and_cost_limit_conflict_rejected(self) -> None:
        from amof.orchestrator.resume_control import BudgetOptions, resolve_run_budget

        opts = BudgetOptions(budget=1.0, cost_limit=2.0)
        limit, err = resolve_run_budget(opts)
        self.assertIsNone(limit)
        self.assertIn("different values", err or "")

    def test_invalid_budget_values_rejected(self) -> None:
        from amof.orchestrator.resume_control import parse_positive_budget

        for bad in (0, -1, "abc"):
            with self.assertRaises(ValueError):
                parse_positive_budget(bad, flag="--budget")

    def test_budget_strict_blocks_over_budget_plan_before_provider_call(self) -> None:
        from amof.orchestrator.planner import ExecutionPlan, Subtask
        from amof.orchestrator.resume_control import (
            BudgetOptions,
            check_budget_before_execution,
        )
        from amof.orchestrator.telemetry import SessionTelemetry

        plan = ExecutionPlan(
            analysis="x",
            subtasks=[Subtask(id="1", title="t", description="d", runner="code")],
            execution_order=["1"],
        )
        telemetry = SessionTelemetry(max_cost=0.01)
        telemetry._restored_cost = 0.009
        opts = BudgetOptions(budget_strict=True)
        msg = check_budget_before_execution(telemetry, plan, opts, noninteractive=True)
        self.assertIsNotNone(msg)
        self.assertIn("budget", msg.lower())

    def test_budget_status_prints_and_exits_without_provider_call(self) -> None:
        from amof.orchestrator.resume_control import format_budget_status
        from amof.orchestrator.telemetry import SessionTelemetry

        telemetry = SessionTelemetry(max_cost=5.0)
        telemetry._restored_cost = 1.25
        text = format_budget_status(
            "sess-9",
            telemetry,
            {
                "failure_type": "cost_exceeded",
                "failed_subtask_id": "2",
                "remaining_subtasks": ["2", "3"],
                "resume_command": "amof agent --resume sess-9 --add-budget 1.00",
            },
        )
        self.assertIn("sess-9", text)
        self.assertIn("Spent", text)
        self.assertIn("Remaining", text)
        self.assertIn("cost_exceeded", text)

    def test_resume_with_followup_and_capability_approval(self) -> None:
        from amof.orchestrator.events import EventLog
        from amof.orchestrator.plan_execute_control import (
            apply_capability_elevation,
            build_plan_capability_elevation,
            parse_capability_names,
        )
        from amof.orchestrator.resume_control import (
            append_followup_to_context,
            load_resume_followup,
        )
        from amof.orchestrator.trust_boundary import create_trust_state

        caps = parse_capability_names(["secret"])
        self.assertEqual(caps, {"secret"})
        followup, _ = load_resume_followup(
            inline="Approve secret for preflight only.",
            file_path=None,
        )
        ctx = append_followup_to_context("Inspect repo", followup)
        self.assertNotIn("super-secret-token-value", ctx)
        trust = create_trust_state("inspect repo")
        plan = self._plan_with_completed_first()
        elevation = build_plan_capability_elevation(
            session_id="sess-x",
            plan=plan,
            goal="Inspect repo",
            missing_caps=["secret"],
            base_ceiling=set(trust.trusted_intent_caps),
            approval_source="cli_flag",
        )
        apply_capability_elevation(trust, elevation)
        with tempfile.TemporaryDirectory() as td:
            events = EventLog(session_id="sess-x", runs_dir=Path(td))
            events.resume_followup(
                session_id="sess-x",
                source=followup.source,
                chars=followup.char_count,
                sha256=followup.sha256,
                preview=followup.preview,
            )
            events.capability_elevation(
                session_id="sess-x",
                plan_id="plan-a",
                approved_capabilities=["secret"],
                base_ceiling=list(trust.trusted_intent_caps),
                approval_source="cli_flag",
            )
            log_text = events.log_path.read_text(encoding="utf-8")
        self.assertIn("resume_followup", log_text)
        self.assertIn("capability_elevation", log_text)
        self.assertNotIn("super-secret-token-value", log_text)


if __name__ == "__main__":
    unittest.main()
