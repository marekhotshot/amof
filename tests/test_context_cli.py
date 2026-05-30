from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands.operational_context import cmd_operational_context


def _args(service: str, context_target: str | None = None, *, emit_json: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        service=service,
        context_target=context_target,
        json=emit_json,
        plain=False,
        controlplane_mode=None,
        controlplane_url=None,
        execution_backend=None,
        workspace_backend=None,
        evidence_backend=None,
        browser_backend=None,
        browser_recordings=None,
        browser_human_in_loop=None,
        browser_allowed_hosts=None,
        kubeconfig_ref=None,
        namespace=None,
    )


def _run_context_cmd(args: SimpleNamespace) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cmd_operational_context(args)
    return code, stdout.getvalue(), stderr.getvalue()


class ContextCliTests(unittest.TestCase):
    def test_show_defaults_to_local_with_built_in_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-context-cli-") as td:
            amof_home = Path(td) / "amof-home"
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                code, out, _err = _run_context_cmd(_args("show"))
        self.assertEqual(code, 0)
        self.assertIn("resolved_context: local", out)
        self.assertIn("source_of_resolution: built_in_default_local", out)

    def test_list_includes_required_contexts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-context-cli-list-") as td:
            amof_home = Path(td) / "amof-home"
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                code, out, _err = _run_context_cmd(_args("list", emit_json=True))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        names = {entry["name"] for entry in payload["contexts"]}
        self.assertIn("local", names)
        self.assertIn("cloud-dev", names)
        self.assertIn("msg-aws-dev", names)

    def test_use_local_persists_selection_in_user_local_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-context-cli-use-") as td:
            amof_home = Path(td) / "amof-home"
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                code, out, _err = _run_context_cmd(_args("use", "local"))
                config_path = amof_home / "config" / "config.yaml"
                payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertIn("active context set to local", out)
        self.assertEqual(payload.get("current_context"), "local")

    def test_invalid_context_name_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-context-cli-invalid-") as td:
            amof_home = Path(td) / "amof-home"
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                code, _out, err = _run_context_cmd(_args("use", "not-a-context"))
        self.assertEqual(code, 1)
        self.assertIn("unknown AMOF context", err)

    def test_doctor_fails_closed_for_remote_context_without_required_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-context-cli-doctor-") as td:
            amof_home = Path(td) / "amof-home"
            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(amof_home),
                    "AMOF_REMOTE_IAL_BASE_URL": "",
                    "AMOF_REMOTE_IAL_API_KEY": "",
                },
                clear=False,
            ):
                use_code, _, _ = _run_context_cmd(_args("use", "cloud-dev"))
                code, out, err = _run_context_cmd(_args("doctor", emit_json=True))
        self.assertEqual(use_code, 0)
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload.get("status"), "fail_closed")
        self.assertEqual(payload.get("resolved_context"), "cloud-dev")
        self.assertEqual(payload.get("context", {}).get("availability"), "unavailable")
        self.assertIn("FAIL_CLOSED", err)
        self.assertIn("AMOF_REMOTE_IAL_BASE_URL", err)
        self.assertIn("AMOF_REMOTE_IAL_API_KEY", err)

    def test_doctor_does_not_print_secret_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-context-cli-secret-") as td:
            amof_home = Path(td) / "amof-home"
            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(amof_home),
                    "AMOF_REMOTE_IAL_BASE_URL": "https://ial.example.test",
                    "AMOF_REMOTE_IAL_API_KEY": "super-secret-value",
                },
                clear=False,
            ):
                use_code, _, _ = _run_context_cmd(_args("use", "cloud-dev"))
                code, out, err = _run_context_cmd(_args("doctor"))
        self.assertEqual(use_code, 0)
        self.assertEqual(code, 0)
        self.assertNotIn("super-secret-value", out)
        self.assertNotIn("super-secret-value", err)


if __name__ == "__main__":
    unittest.main()
