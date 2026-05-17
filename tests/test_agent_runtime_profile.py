from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import agent_cmd
from amof.orchestrator.llm.base import ProviderError
from amof.orchestrator.planner import TaskPlanner
from amof.orchestrator.runners import PUBLIC_DEFAULT_RUNNERS_CONFIG, RunnerFactory
from amof.orchestrator.trust_boundary import create_trust_state
from amof.orchestrator.tools.base import GuardrailConfig, Guardrails, ToolCall, create_default_registry


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
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True, text=True)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, text=True)
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
        "AMOF_PLANNER_MODEL",
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


def _write_active_provider_profile(amof_home: Path, name: str, payload: dict[str, object]) -> None:
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


class AgentRuntimeProfileTests(unittest.TestCase):
    def test_pyproject_includes_public_agent_runtime_dependencies(self) -> None:
        payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        deps = set(payload["project"]["dependencies"])
        dep_names = {dep.split(">=", 1)[0] for dep in deps}

        self.assertTrue({"PyYAML", "pydantic", "openai", "anthropic", "boto3", "botocore"} <= dep_names)
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
                "repos": [{"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.agent.Agent", _FakeAgent):
                        import amof.orchestrator.memory as memory

                        with patch.object(memory, "VectorStore", side_effect=ImportError("chromadb missing")):
                            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
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
            journal_files = list((amof_home / "share" / "journals" / "demo-repo").glob("*.md"))

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
                "repos": [{"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.agent.Agent", _FakeAgent):
                        import amof.orchestrator.memory as memory

                        with patch.object(memory, "VectorStore", side_effect=ImportError("chromadb missing")):
                            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
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

    def test_installed_agent_install_does_not_use_target_requirements_guidance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-install-") as td:
            repo = Path(td) / "target-repo"
            _init_git_repo(repo)
            with _cwd(repo):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
                    result = agent_cmd.cmd_agent_install()

        self.assertIn(result, {0, 1})
        self.assertNotIn("requirements.txt not found in workspace root", stderr.getvalue())
        self.assertNotIn("pip install -r", stderr.getvalue())

    def test_missing_guardrails_load_packaged_public_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-guardrails-") as td:
            cfg = GuardrailConfig.load(Path(td) / "missing-guardrails.yaml")

        self.assertIn(".git/**", cfg.protected_paths)
        self.assertIn(".env", cfg.protected_basenames)
        self.assertIn("git push", cfg.blocked_commands)
        guardrails = Guardrails(mode="plan", config=cfg)
        self.assertEqual(guardrails.check_write("README.md"), "Write operations are blocked in PLAN mode")
        self.assertEqual(guardrails.check_shell("git status"), "Shell operations are blocked in PLAN mode")

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
            agent_cmd._default_planner_model("openrouter", "anthropic/claude-sonnet-4.5"),
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
                },
            )
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [{"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}],
            }
            created_clients: list[_FakeLocalLLM] = []

            def _fake_local_client(**kwargs):
                client = _FakeLocalLLM(**kwargs)
                created_clients.append(client)
                return client

            _FakeAgent.instances.clear()
            with _isolated_provider_env(amof_home):
                with _cwd(repo):
                    with patch("amof.orchestrator.agent.Agent", _FakeAgent):
                        import amof.orchestrator.memory as memory

                        with patch.object(memory, "VectorStore", side_effect=ImportError("chromadb missing")):
                            with patch(
                                "amof.orchestrator.llm.local_openai_compatible.LocalOpenAICompatibleClient",
                                side_effect=_fake_local_client,
                            ):
                                with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
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
        self.assertEqual(created_clients[0].kwargs["base_url"], "http://127.0.0.1:11434/v1")
        self.assertEqual(created_clients[0].kwargs["model"], "qwen2.5-coder:7b-instruct")
        self.assertIsNone(created_clients[0].kwargs["api_key"])
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
                "repos": [{"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}],
            }

            with _isolated_provider_env(amof_home):
                with _cwd(repo):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
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
                "repos": [{"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}],
            }

            with _isolated_provider_env(amof_home):
                with _cwd(repo):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
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
                "repos": [{"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}],
            }
            created_openai_clients: list[object] = []

            def _fake_openai_client(**kwargs):
                client = _FakeLocalLLM(**kwargs)
                created_openai_clients.append(client)
                return client

            _FakeAgent.instances.clear()
            with _isolated_provider_env(amof_home, {"OPENROUTER_API_KEY": "unit-test-provider-value"}):
                with _cwd(repo):
                    with patch("amof.orchestrator.agent.Agent", _FakeAgent):
                        import amof.orchestrator.memory as memory

                        with patch.object(memory, "VectorStore", side_effect=ImportError("chromadb missing")):
                            with patch(
                                "amof.orchestrator.llm.local_openai_compatible.LocalOpenAICompatibleClient",
                                side_effect=AssertionError("local profile should not be used"),
                            ):
                                with patch(
                                    "amof.orchestrator.llm.openai_client.OpenAIClient",
                                    side_effect=_fake_openai_client,
                                ):
                                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
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
        self.assertEqual(created_openai_clients[0].kwargs["api_key"], "unit-test-provider-value")
        self.assertTrue(str(created_openai_clients[0].kwargs["model"]).startswith("openrouter/"))

    def test_plan_execute_no_follow_up_is_noninteractive_for_clarifications(self) -> None:
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

    def test_public_default_runner_config_is_bounded(self) -> None:
        code_runner = PUBLIC_DEFAULT_RUNNERS_CONFIG["runners"]["code"]
        tools = set(code_runner["tools"])

        self.assertTrue({"Read", "Write", "StrReplace", "Glob", "LS", "ReadLints"} <= tools)
        self.assertNotIn("Shell", tools)
        self.assertNotIn("Delete", tools)
        self.assertNotIn("GitCheckpoint", tools)

    def test_add_intent_authorizes_write_capability(self) -> None:
        trust_state = create_trust_state("Add only this function to app.py")

        self.assertIn("write", trust_state.trusted_intent_caps)
        self.assertFalse(trust_state.full_rewrite_authorized)

    def test_explicit_full_rewrite_intent_is_tracked(self) -> None:
        trust_state = create_trust_state("Rewrite the entire file README.md from scratch")

        self.assertIn("write", trust_state.trusted_intent_caps)
        self.assertTrue(trust_state.full_rewrite_authorized)

    def test_write_new_file_allowed_but_existing_file_blocked_for_add_intent(self) -> None:
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
        self.assertIn("Write cannot overwrite an existing file", existing_result.error or "")

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
                result = registry.execute(
                    ToolCall(
                        id="1",
                        name="StrReplace",
                        arguments={"path": "README.md", "old_string": "old\n", "new_string": "old\nnew\n"},
                    )
                )
            contents = (repo / "README.md").read_text(encoding="utf-8")

        self.assertTrue(result.success, result.error)
        self.assertEqual(contents, "old\nnew\n")

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
            doc.write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "test: add docs"], cwd=repo, check=True, capture_output=True, text=True, env=_commit_env())
            doc.write_text("short replacement\n", encoding="utf-8")

            guard = agent_cmd._evaluate_diff_guard(
                "In docs/runbooks/happy-path-agent-workflow.md, add a docs-only section under 12 lines. Do not modify code.",
                repo,
                agent_cmd._git_probe(repo),
            )

        self.assertEqual(guard["status"], "fail")
        self.assertTrue(guard["destructive_rewrite_detected"])
        self.assertIn("docs/runbooks/happy-path-agent-workflow.md", guard["changed_files"])

    def test_diff_guard_allows_bounded_docs_insertion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-insert-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            doc = repo / "docs" / "runbooks" / "happy-path-agent-workflow.md"
            doc.parent.mkdir(parents=True)
            doc.write_text("before\n## Bounded Worker Execution\nold\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "test: add docs"], cwd=repo, check=True, capture_output=True, text=True, env=_commit_env())
            doc.write_text("before\n## Bounded Worker Execution\nold\n\n### Manual review\n\nReview diff.\n", encoding="utf-8")

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
            doc.write_text("before\n## Bounded Worker Execution\nold\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "test: add docs"], cwd=repo, check=True, capture_output=True, text=True, env=_commit_env())
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
        self.assertTrue(any(reason.startswith("exact_text_missing") for reason in guard["reasons"]))

    def test_diff_guard_allows_exact_requested_section(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-exact-pass-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            doc = repo / "docs" / "runbooks" / "happy-path-agent-workflow.md"
            doc.parent.mkdir(parents=True)
            doc.write_text("before\n## Bounded Worker Execution\nold\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "test: add docs"], cwd=repo, check=True, capture_output=True, text=True, env=_commit_env())
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
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "test: add app"], cwd=repo, check=True, capture_output=True, text=True, env=_commit_env())
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")

            guard = agent_cmd._evaluate_diff_guard(
                "In docs/runbooks/happy-path-agent-workflow.md, add a docs-only section under 12 lines. Do not modify code.",
                repo,
                agent_cmd._git_probe(repo),
            )

        self.assertEqual(guard["status"], "fail")
        self.assertFalse(guard["requested_paths_observed"])
        self.assertTrue(any(reason.startswith("requested_paths_mismatch") for reason in guard["reasons"]))

    def test_diff_guard_rejects_missing_requested_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-missing-requested-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            (repo / "app.py").write_text("print('old')\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_app.py").write_text("print('old test')\n", encoding="utf-8")
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
            any(reason.startswith("requested_paths_missing:tests/test_app.py") for reason in guard["reasons"])
        )

    def test_diff_guard_rejects_explosive_existing_file_growth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-diff-explosive-growth-") as td:
            repo = Path(td) / "repo"
            _init_git_repo(repo)
            app = repo / "app.py"
            app.write_text("def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True, text=True)
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
                    "def farewell(name):\n    return f'Goodbye, {name}!'" for _ in range(300)
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
        self.assertTrue(any(reason.startswith("file_growth:app.py") for reason in guard["reasons"]))
        self.assertTrue(any(reason.startswith("large_addition:app.py") for reason in guard["reasons"]))

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
            guardrails = Guardrails(config=GuardrailConfig.public_defaults(), writable_roots=[repo])
            parent_tools = create_default_registry(guardrails=guardrails, role="worker", workspace_root=repo)
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
                "repos": [{"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}],
            }
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            _FakeAgent.instances.clear()
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.runners.Agent", _FakeAgent):
                        with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
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
                for path in (amof_home / "share" / "journals" / "demo-repo").glob("*.md")
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
                "repos": [{"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}],
            }
            env = {"AMOF_HOME": str(amof_home), "OPENROUTER_API_KEY": "unit-test-provider-value"}
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.runners.Agent", _FakeNoDiffAgent):
                        with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
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

    def test_successful_worker_mutation_creates_diff_without_source_noise(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-agent-mutation-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            (repo / "app.py").write_text("def greet(name: str) -> str:\n    return f'Hello, {name}!'\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "test: add app"], cwd=repo, check=True, capture_output=True, text=True, env=_commit_env())
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
                "repos": [{"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}],
            }
            env = {"AMOF_HOME": str(amof_home), "OPENROUTER_API_KEY": "unit-test-provider-value"}
            with patch.dict(os.environ, env, clear=False):
                with _cwd(repo):
                    with patch("amof.orchestrator.runners.Agent", _FakeMutatingAgent):
                        with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
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
            journal_files = list((amof_home / "share" / "journals" / "demo-repo").glob("*.md"))
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

    def test_plan_execute_missing_runner_factory_fails_clearly_before_execution(self) -> None:
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
            subtasks=[Subtask(id="1", title="Edit docs", description="Edit docs", runner="code")],
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
            self.assertIn("outside writable roots", guardrails.check_write(str(outside)))

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
                            guardrails=Guardrails(config=GuardrailConfig.public_defaults()),
                            workspace_root=repo,
                            manifest=manifest,
                            codebase_context="",
                            guardrail_info=None,
                            verbose=False,
                        )

        self.assertEqual(result, 0)
        self.assertEqual(fake_agent.run_calls, 0)


if __name__ == "__main__":
    unittest.main()
