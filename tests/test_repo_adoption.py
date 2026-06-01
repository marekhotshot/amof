from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
AMOF_SCRIPT = ROOT / "scripts" / "amof.py"
SCRIPTS_ROOT = ROOT / "scripts"


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


def _amof_env(amof_home: Path) -> dict[str, str]:
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(SCRIPTS_ROOT)
        if not existing_pythonpath
        else os.pathsep.join([str(SCRIPTS_ROOT), existing_pythonpath])
    )
    env["AMOF_HOME"] = str(amof_home)
    for key in (
        "AMOF_CWD",
        "AMOF_WORKSPACE_ROOT",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AMOF_BEDROCK_REGION",
    ):
        env.pop(key, None)
    return env


def _run_amof(cwd: Path, amof_home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(AMOF_SCRIPT), *args],
        cwd=cwd,
        env=_amof_env(amof_home),
        capture_output=True,
        text=True,
    )


class RepoAdoptionTests(unittest.TestCase):
    def test_agent_before_adoption_fails_with_git_root_guidance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-before-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            _init_git_repo(repo)

            result = _run_amof(repo, temp / ".amof-home", "agent", "--plan", "Inspect", "--no-follow-up")

        self.assertEqual(result.returncode, 1)
        self.assertIn("no ecosystem resolved", result.stderr)
        self.assertIn(f"Detected git root: {repo}", result.stderr)
        self.assertIn("Run: amof init --adopt .", result.stderr)
        self.assertIn("Then: amof agent -e demo-repo --plan \"Inspect this repo\" --no-follow-up", result.stderr)
        self.assertNotIn("Error: --ecosystem/-e is required", result.stderr)

    def test_init_adopt_creates_appdata_binding_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-init-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)

            result = _run_amof(repo, amof_home, "init", "--adopt", ".")
            registry_path = amof_home / "config" / "workspaces.yaml"
            payload = yaml.safe_load(registry_path.read_text(encoding="utf-8"))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('amof agent --plan "Inspect this repo"', result.stdout)
            binding = payload["repo_bindings"][str(repo)]
            self.assertEqual(binding["git_root"], str(repo))
            self.assertEqual(binding["ecosystem"], "demo-repo")
            self.assertEqual(binding["repo_name"], "demo-repo")
            self.assertEqual(binding["default_ref"], "main")
            self.assertEqual(binding["manifest_source"], "appdata")
            manifest = payload["adopted_ecosystems"]["demo-repo"]
            self.assertEqual(manifest["manifest_source"], "appdata")
            self.assertEqual(manifest["repos"][0]["path"], str(repo))
            self.assertFalse(manifest["repos"][0]["readonly"])

    def test_agent_after_adoption_reaches_provider_validation_without_ecosystem_flag(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-agent-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)
            init_result = _run_amof(repo, amof_home, "init", "--adopt", ".")
            self.assertEqual(init_result.returncode, 0, init_result.stderr)

            result = _run_amof(repo, amof_home, "agent", "--plan", "Inspect", "--no-follow-up")

        self.assertEqual(result.returncode, 1)
        self.assertIn("[agent] ANTHROPIC_API_KEY not set.", result.stderr)
        self.assertNotIn("no ecosystem resolved", result.stderr)
        self.assertNotIn("--ecosystem/-e is required", result.stderr)

    def test_context_command_resolves_adopted_ecosystem_alias_and_repo_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-context-") as td:
            temp = Path(td)
            repo = temp / "hotshot.sk"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)
            init_result = _run_amof(repo, amof_home, "init", "--adopt", ".", "--name", "hotshot-sk")
            self.assertEqual(init_result.returncode, 0, init_result.stderr)

            ecosystem_result = _run_amof(repo, amof_home, "context", "hotshot-sk")
            repo_name_result = _run_amof(repo, amof_home, "context", "hotshot.sk")

        self.assertEqual(ecosystem_result.returncode, 0, ecosystem_result.stderr)
        self.assertEqual(repo_name_result.returncode, 0, repo_name_result.stderr)
        self.assertIn("Context generated under context/hotshot-sk", ecosystem_result.stdout)
        self.assertIn("Context generated under context/hotshot.sk", repo_name_result.stdout)
        self.assertNotIn("not found in manifest", ecosystem_result.stderr)
        self.assertNotIn("not found in manifest", repo_name_result.stderr)

    def test_re_adopt_same_repo_replaces_old_ecosystem_alias_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-idempotent-") as td:
            temp = Path(td)
            repo = temp / "hotshot.sk"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)

            first = _run_amof(repo, amof_home, "init", "--adopt", ".", "--name", "hotshot-sk")
            second = _run_amof(repo, amof_home, "init", "--adopt", ".", "--name", "hotshot-sk-renamed")
            registry_path = amof_home / "config" / "workspaces.yaml"
            payload = yaml.safe_load(registry_path.read_text(encoding="utf-8"))

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(payload["repo_bindings"]), 1)
        self.assertEqual(payload["repo_bindings"][str(repo)]["ecosystem"], "hotshot-sk-renamed")
        self.assertIn("hotshot-sk-renamed", payload["adopted_ecosystems"])
        self.assertNotIn("hotshot-sk", payload["adopted_ecosystems"])
        self.assertEqual(len(payload["adopted_ecosystems"]), 1)

    def test_active_openrouter_profile_drives_agent_default_provider(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-provider-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)
            init_result = _run_amof(repo, amof_home, "init", "--adopt", ".")
            self.assertEqual(init_result.returncode, 0, init_result.stderr)
            setup_result = _run_amof(
                repo,
                amof_home,
                "setup",
                "provider",
                "openrouter",
                "--name",
                "openrouter-default",
                "--activate",
                "--yes",
            )
            self.assertEqual(setup_result.returncode, 0, setup_result.stderr)

            result = _run_amof(repo, amof_home, "agent", "--plan", "Inspect", "--no-follow-up")

        self.assertEqual(result.returncode, 1)
        self.assertIn("[agent] OPENROUTER_API_KEY not set.", result.stderr)
        self.assertNotIn("ANTHROPIC_API_KEY", result.stderr)
        self.assertNotIn("--ecosystem/-e is required", result.stderr)

    def test_active_bedrock_profile_drives_agent_default_provider_without_unrelated_credentials(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-provider-bedrock-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)
            init_result = _run_amof(repo, amof_home, "init", "--adopt", ".")
            self.assertEqual(init_result.returncode, 0, init_result.stderr)
            setup_result = _run_amof(
                repo,
                amof_home,
                "setup",
                "provider",
                "bedrock",
                "--name",
                "bedrock-default",
                "--activate",
                "--yes",
            )
            self.assertEqual(setup_result.returncode, 0, setup_result.stderr)

            result = _run_amof(repo, amof_home, "agent", "--plan", "Inspect", "--no-follow-up")

        self.assertEqual(result.returncode, 1)
        self.assertIn("[agent] AWS_REGION not set for Bedrock.", result.stderr)
        self.assertNotIn("OPENROUTER_API_KEY", result.stderr)
        self.assertNotIn("RUNPOD_API_KEY", result.stderr)

    def test_explicit_provider_overrides_active_provider_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-provider-override-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)
            self.assertEqual(_run_amof(repo, amof_home, "init", "--adopt", ".").returncode, 0)
            self.assertEqual(
                _run_amof(
                    repo,
                    amof_home,
                    "setup",
                    "provider",
                    "openrouter",
                    "--name",
                    "openrouter-default",
                    "--activate",
                    "--yes",
                ).returncode,
                0,
            )

            result = _run_amof(
                repo,
                amof_home,
                "agent",
                "--provider",
                "anthropic",
                "--plan",
                "Inspect",
                "--no-follow-up",
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("[agent] ANTHROPIC_API_KEY not set.", result.stderr)
        self.assertNotIn("OPENROUTER_API_KEY not set", result.stderr)

    def test_multiple_active_provider_profiles_require_explicit_provider(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-provider-multiple-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)
            self.assertEqual(_run_amof(repo, amof_home, "init", "--adopt", ".").returncode, 0)
            for provider_name, profile_name in (("openrouter", "openrouter-default"), ("openai", "openai-default")):
                result = _run_amof(
                    repo,
                    amof_home,
                    "setup",
                    "provider",
                    provider_name,
                    "--name",
                    profile_name,
                    "--activate",
                    "--yes",
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            result = _run_amof(repo, amof_home, "agent", "--plan", "Inspect", "--no-follow-up")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Multiple active provider profiles configured", result.stderr)

    def test_unsupported_active_provider_profile_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-provider-xai-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)
            self.assertEqual(_run_amof(repo, amof_home, "init", "--adopt", ".").returncode, 0)
            setup_result = _run_amof(
                repo,
                amof_home,
                "setup",
                "provider",
                "xai",
                "--name",
                "xai-default",
                "--activate",
                "--yes",
            )
            self.assertEqual(setup_result.returncode, 0, setup_result.stderr)

            result = _run_amof(repo, amof_home, "agent", "--plan", "Inspect", "--no-follow-up")

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "Provider profile xai-default uses provider xai, but this provider is not supported by amof agent CLI yet.",
            result.stderr,
        )

    def test_adopt_setup_and_agent_provider_validation_keep_repo_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-clean-agent-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)
            self.assertEqual(_run_amof(repo, amof_home, "init", "--adopt", ".").returncode, 0)
            self.assertEqual(
                _run_amof(
                    repo,
                    amof_home,
                    "setup",
                    "provider",
                    "openrouter",
                    "--name",
                    "openrouter-default",
                    "--activate",
                    "--yes",
                ).returncode,
                0,
            )
            result = _run_amof(repo, amof_home, "agent", "--plan", "Inspect", "--no-follow-up")
            git_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(git_status.stdout.strip(), "")
        self.assertFalse((repo / "ecosystems").exists())
        self.assertFalse((repo / ".amof").exists())
        self.assertFalse((repo / "context").exists())
        self.assertNotIn("NO protections", result.stderr)
        self.assertNotIn("Vector memory unavailable", result.stderr)

    def test_explicit_ecosystem_overrides_adopted_binding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-explicit-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)
            init_result = _run_amof(repo, amof_home, "init", "--adopt", ".")
            self.assertEqual(init_result.returncode, 0, init_result.stderr)

            result = _run_amof(
                repo,
                amof_home,
                "-e",
                "missing-explicit-eco",
                "agent",
                "--plan",
                "Inspect",
                "--no-follow-up",
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Ecosystem 'missing-explicit-eco' not found", result.stderr)
        self.assertNotIn("[agent] ANTHROPIC_API_KEY not set.", result.stderr)

    def test_adoption_does_not_write_files_into_target_repo_by_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-clean-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / ".amof-home"
            _init_git_repo(repo)

            result = _run_amof(repo, amof_home, "init", "--adopt", ".")
            git_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git_status.stdout.strip(), "")
        self.assertFalse((repo / ".amof").exists())

    def test_non_git_path_adoption_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-adopt-nongit-") as td:
            temp = Path(td)
            path = temp / "not-a-repo"
            path.mkdir()

            result = _run_amof(path, temp / ".amof-home", "init", "--adopt", ".")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Path is not inside a git repository", result.stderr)
        self.assertIn("amof init --adopt .", result.stderr)


if __name__ == "__main__":
    unittest.main()
