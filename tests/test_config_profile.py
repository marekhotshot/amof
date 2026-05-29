from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import config as config_cmd
from amof.commands import profile as profile_cmd


class ConfigProfileTests(unittest.TestCase):
    def test_profile_example_loads_remote_ial_openrouter(self) -> None:
        payload = profile_cmd._load_public_profile_example("remote-ial-openrouter")
        self.assertEqual(payload["provider"], "remote-ial")
        self.assertEqual(payload["credential_refs"]["api_key_env"], "AMOF_REMOTE_IAL_API_KEY")
        self.assertEqual(payload["credential_refs"]["base_url_env"], "AMOF_REMOTE_IAL_BASE_URL")

    def test_config_render_redacted_does_not_leak_secret_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-config-render-") as td:
            temp = Path(td)
            profile_payload = profile_cmd._load_public_profile_example("remote-ial-openrouter")
            config_root = temp / "config"
            (config_root / "provider-profiles").mkdir(parents=True, exist_ok=True)
            (config_root / "config.yaml").write_text("current_context: local\n", encoding="utf-8")
            (config_root / "contexts.yaml").write_text(
                "contexts:\n  local:\n    credentials:\n      provider_profile_refs:\n        - remote-ial-openrouter\n",
                encoding="utf-8",
            )
            (config_root / "provider-profiles" / "remote-ial-openrouter.yaml").write_text(
                json.dumps(profile_payload),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(temp),
                    "AMOF_REMOTE_IAL_API_KEY": "unit-test-secret-value-123",
                    "AMOF_REMOTE_IAL_BASE_URL": "https://ial.example.test",
                },
                clear=False,
            ):
                out = io.StringIO()
                with patch("sys.stdout", out):
                    exit_code = config_cmd.cmd_config(type("Args", (), {"config_cmd": "render", "redacted": True})())
            self.assertEqual(exit_code, 0)
            rendered = out.getvalue()
            self.assertIn('"api_key_env": "AMOF_REMOTE_IAL_API_KEY"', rendered)
            self.assertNotIn("unit-test-secret-value-123", rendered)

    def test_config_doctor_reports_missing_env_vars_by_name_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-config-doctor-") as td:
            temp = Path(td)
            profile_payload = profile_cmd._load_public_profile_example("remote-ial-openrouter")
            config_root = temp / "config"
            (config_root / "provider-profiles").mkdir(parents=True, exist_ok=True)
            (config_root / "config.yaml").write_text("current_context: local\n", encoding="utf-8")
            (config_root / "contexts.yaml").write_text(
                "contexts:\n  local:\n    credentials:\n      provider_profile_refs:\n        - remote-ial-openrouter\n",
                encoding="utf-8",
            )
            (config_root / "provider-profiles" / "remote-ial-openrouter.yaml").write_text(
                json.dumps(profile_payload),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(temp)}, clear=False):
                os.environ.pop("AMOF_REMOTE_IAL_API_KEY", None)
                os.environ.pop("AMOF_REMOTE_IAL_BASE_URL", None)
                out = io.StringIO()
                with patch("sys.stdout", out):
                    exit_code = config_cmd.cmd_config(type("Args", (), {"config_cmd": "doctor"})())
            self.assertEqual(exit_code, 1)
            rendered = out.getvalue()
            self.assertIn("AMOF_REMOTE_IAL_API_KEY", rendered)
            self.assertIn("AMOF_REMOTE_IAL_BASE_URL", rendered)
            self.assertNotIn("unit-test-secret-value-123", rendered)


if __name__ == "__main__":
    unittest.main()
