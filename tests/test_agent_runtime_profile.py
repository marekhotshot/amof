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


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import agent_cmd
from amof.orchestrator.tools.base import GuardrailConfig, Guardrails


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
    stop_reason = "completed"

    def __init__(self, *args, **kwargs) -> None:
        self.llm = kwargs.get("llm") or (args[0] if args else None)
        self.model_router = kwargs.get("model_router")
        self.tools = kwargs.get("tools")

    def run(self, goal: str) -> str:
        return "fake agent response"


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
                "OPENROUTER_API_KEY": "fake-openrouter-key",
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
                "OPENROUTER_API_KEY": "fake-openrouter-key",
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


if __name__ == "__main__":
    unittest.main()
