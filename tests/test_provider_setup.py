from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.app_config import get_context, get_current_context_name
from amof.app_paths import provider_profiles_dir
from amof.commands import setup as setup_cmd


def _args(**overrides):
    values = {
        "setup_cmd": "provider",
        "provider_template": None,
        "list_templates": False,
        "profile_name": None,
        "lane": None,
        "model": None,
        "model_env": None,
        "api_key_env": None,
        "base_url": None,
        "base_url_env": None,
        "activate": False,
        "dry_run": False,
        "yes": False,
        "print_template": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ProviderProfileTemplateTests(unittest.TestCase):
    def test_provider_templates_exist_and_parse(self) -> None:
        template_dir = ROOT / "templates" / "provider-profiles"
        expected = {f"{name}.example.yaml" for name in setup_cmd.PROVIDER_TEMPLATE_ORDER}
        actual = {path.name for path in template_dir.glob("*.example.yaml")}

        self.assertEqual(actual, expected)
        for template_path in template_dir.glob("*.example.yaml"):
            payload = yaml.safe_load(template_path.read_text(encoding="utf-8"))
            self.assertIsInstance(payload, dict)
            for field in ("name", "provider", "lane", "model_family", "credential_refs", "redaction_policy"):
                self.assertIn(field, payload)
            self.assertIs(payload["redaction_policy"]["record_secret_names_only"], True)
            self.assertIs(payload["allow_direct_git_write"], False)

    def test_provider_templates_contain_no_obvious_raw_secrets(self) -> None:
        template_dir = ROOT / "templates" / "provider-profiles"
        for template_path in template_dir.glob("*.example.yaml"):
            text = template_path.read_text(encoding="utf-8")
            self.assertNotIn("sk-", text)
            self.assertNotIn("github_pat_", text)
            self.assertNotIn("BEGIN PRIVATE KEY", text)


class ProviderSetupCommandTests(unittest.TestCase):
    def test_setup_provider_list_lists_all_templates(self) -> None:
        with redirect_stdout(StringIO()) as stdout:
            result = setup_cmd.cmd_setup(_args(list_templates=True))

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        for provider_name in setup_cmd.PROVIDER_TEMPLATE_ORDER:
            self.assertIn(provider_name, output)

    def test_print_template_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-provider-print-") as td:
            with patch.dict("os.environ", {"AMOF_HOME": str(Path(td) / "amof-home")}, clear=False):
                with redirect_stdout(StringIO()) as stdout:
                    result = setup_cmd.cmd_setup(
                        _args(provider_template="openrouter", print_template=True)
                    )

                self.assertEqual(result, 0)
                payload = yaml.safe_load(stdout.getvalue())
                self.assertEqual(payload["provider"], "openrouter")
                self.assertFalse(provider_profiles_dir().exists())

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-provider-dry-run-") as td:
            with patch.dict("os.environ", {"AMOF_HOME": str(Path(td) / "amof-home")}, clear=False):
                with redirect_stdout(StringIO()) as stdout:
                    result = setup_cmd.cmd_setup(
                        _args(provider_template="openrouter", profile_name="my-openrouter", dry_run=True)
                    )

                self.assertEqual(result, 0)
                self.assertIn("Dry run only", stdout.getvalue())
                self.assertFalse(provider_profiles_dir().exists())

    def test_writing_openrouter_profile_creates_app_data_yaml(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-provider-write-") as td:
            with patch.dict("os.environ", {"AMOF_HOME": str(Path(td) / "amof-home")}, clear=False):
                result = setup_cmd.cmd_setup(
                    _args(provider_template="openrouter", profile_name="my-openrouter", yes=True)
                )

                target = provider_profiles_dir() / "my-openrouter.yaml"
                self.assertEqual(result, 0)
                self.assertTrue(target.exists())
                payload = yaml.safe_load(target.read_text(encoding="utf-8"))
                self.assertEqual(payload["name"], "my-openrouter")
                self.assertEqual(payload["credential_refs"]["api_key_env"], "OPENROUTER_API_KEY")
                self.assertNotIn("api_key", payload)

    def test_local_qwen_supports_base_url_and_model_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-provider-local-") as td:
            with patch.dict("os.environ", {"AMOF_HOME": str(Path(td) / "amof-home")}, clear=False):
                result = setup_cmd.cmd_setup(
                    _args(
                        provider_template="local-qwen",
                        profile_name="local-qwen",
                        base_url="http://localhost:11434/v1",
                        model="qwen2.5-coder:7b",
                        yes=True,
                    )
                )

                payload = yaml.safe_load(
                    (provider_profiles_dir() / "local-qwen.yaml").read_text(encoding="utf-8")
                )
                self.assertEqual(result, 0)
                self.assertEqual(payload["base_url"], "http://localhost:11434/v1")
                self.assertEqual(payload["model"], "qwen2.5-coder:7b")

    def test_runpod_supports_base_url_env_and_api_key_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-provider-runpod-") as td:
            with patch.dict("os.environ", {"AMOF_HOME": str(Path(td) / "amof-home")}, clear=False):
                result = setup_cmd.cmd_setup(
                    _args(
                        provider_template="runpod",
                        profile_name="runpod-heavy",
                        base_url_env="RUNPOD_OPENAI_BASE_URL",
                        api_key_env="RUNPOD_API_KEY",
                        yes=True,
                    )
                )

                payload = yaml.safe_load(
                    (provider_profiles_dir() / "runpod-heavy.yaml").read_text(encoding="utf-8")
                )
                self.assertEqual(result, 0)
                self.assertEqual(payload["credential_refs"]["base_url_env"], "RUNPOD_OPENAI_BASE_URL")
                self.assertEqual(payload["credential_refs"]["api_key_env"], "RUNPOD_API_KEY")

    def test_activate_updates_current_context_provider_profile_refs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-provider-activate-") as td:
            with patch.dict("os.environ", {"AMOF_HOME": str(Path(td) / "amof-home")}, clear=False):
                result = setup_cmd.cmd_setup(
                    _args(
                        provider_template="openrouter",
                        profile_name="openrouter-default",
                        activate=True,
                        yes=True,
                    )
                )

                context = get_context(get_current_context_name())
                refs = context["credentials"]["provider_profile_refs"]
                self.assertEqual(result, 0)
                self.assertIn("openrouter-default", refs)

    def test_setup_command_does_not_require_ecosystem(self) -> None:
        import amof.entrypoint as entrypoint

        self.assertIn("setup", entrypoint.NO_ECOSYSTEM_COMMANDS)
        with patch.object(sys, "argv", ["amof", "setup", "provider", "--list"]):
            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(StringIO()) as stdout:
                    entrypoint.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("openrouter", stdout.getvalue())

    def test_api_key_env_rejects_secret_looking_values(self) -> None:
        with redirect_stderr(StringIO()) as stderr:
            result = setup_cmd.cmd_setup(
                _args(provider_template="openrouter", api_key_env="sk-test-secret-value", dry_run=True)
            )

        self.assertEqual(result, 1)
        self.assertIn("expects an environment variable name", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
