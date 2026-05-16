from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
RELEASE_ROUTER_PATH = SCRIPTS_ROOT / "amof" / "api" / "routers" / "release.py"
CLI_PATH = SCRIPTS_ROOT / "amof" / "cli.py"
ENTRYPOINT_PATH = SCRIPTS_ROOT / "amof" / "entrypoint.py"
START_DEV_PATH = REPO_ROOT / "scripts" / "start-dev.sh"
START_RESTORED_UI_DEV_PATH = REPO_ROOT / "scripts" / "start-restored-ui-dev.sh"
SMOKE_TEST_PERSISTENCE_PATH = REPO_ROOT / "scripts" / "smoke-test-persistence.sh"
SMOKE_TEST_HEADLESS_RUN_PATH = REPO_ROOT / "scripts" / "smoke-test-headless-run.sh"
RUN_RETENTION_PATH = REPO_ROOT / "scripts" / "apply-run-retention.sh"
CLOUDFLARE_DNS_PLAN_PATH = REPO_ROOT / "scripts" / "cloudflare-dns-plan-hotshot-sk.py"
CLOUDFLARE_DNS_WRAPPER_PATH = REPO_ROOT / "scripts" / "cloudflare-dns-hotshot-sk.sh"
CHECK_COMMAND_PATH = SCRIPTS_ROOT / "amof" / "commands" / "check.py"

if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.api import command_builder
from amof.commands import check as check_command


class PublicLifecycleSurfaceTests(unittest.TestCase):
    def test_public_command_builders_raise_removed_surface_error(self) -> None:
        root = REPO_ROOT
        for builder in (
            command_builder.build_lifecycle_build_command,
            command_builder.build_cluster_lifecycle_build_command,
            command_builder.build_lifecycle_deploy_command,
        ):
            with self.assertRaisesRegex(RuntimeError, "removed from public AMOF canonical main"):
                builder(root)

    def test_release_router_source_hides_public_build_and_deploy(self) -> None:
        source = RELEASE_ROUTER_PATH.read_text(encoding="utf-8")
        self.assertIn("_PUBLIC_RUNTIME_SURFACE_NOTE", source)
        self.assertIn("supported_actions = [\"promote\"]", source)
        self.assertIn("elif action_lower == \"deploy\":", source)
        self.assertIn("_PUBLIC_RELEASE_PROBE_REMOVED_NOTE", source)
        self.assertNotIn("_resolve_live_probe_kubeconfig", source)
        self.assertNotIn("Unsupported deploy profile assumption in live probe", source)

    def test_ticket_build_write_is_not_exposed_in_public_cli(self) -> None:
        cli_source = CLI_PATH.read_text(encoding="utf-8")
        entrypoint_source = ENTRYPOINT_PATH.read_text(encoding="utf-8")
        self.assertNotIn('"build-write"', cli_source)
        self.assertNotIn("cmd_ticket_build_write", entrypoint_source)

    def test_start_dev_wrapper_scripts_are_absent_from_public_repo(self) -> None:
        self.assertFalse(START_DEV_PATH.exists())
        self.assertFalse(START_RESTORED_UI_DEV_PATH.exists())

    def test_wave5_runtime_operator_scripts_are_absent_from_public_repo(self) -> None:
        for path in (
            SMOKE_TEST_PERSISTENCE_PATH,
            SMOKE_TEST_HEADLESS_RUN_PATH,
            RUN_RETENTION_PATH,
            CLOUDFLARE_DNS_PLAN_PATH,
            CLOUDFLARE_DNS_WRAPPER_PATH,
        ):
            self.assertFalse(path.exists(), str(path))

    def test_public_check_command_does_not_require_kubeconfig(self) -> None:
        source = CHECK_COMMAND_PATH.read_text(encoding="utf-8")
        self.assertNotIn("KUBECONFIG", source)
        self.assertIn('recommended = ["GIT_TOKEN"]', source)

    def test_public_check_env_file_treats_env_as_optional(self) -> None:
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            try:
                os.chdir(tmp_path)
                ok, status = check_command.check_env_file()
                self.assertTrue(ok)
                self.assertIn("optional .env", status)

                (tmp_path / ".env").write_text("EXAMPLE_VAR=1\n", encoding="utf-8")
                ok, status = check_command.check_env_file()
                self.assertTrue(ok)
                self.assertNotIn("KUBECONFIG", status)
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
