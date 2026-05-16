from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import update as update_cmd
from amof.commands import uninstall as uninstall_cmd


def _args(**overrides):
    values = {
        "check": False,
        "target_version": None,
        "yes": False,
        "dry_run": False,
        "verbose": False,
        "source_url": update_cmd.DEFAULT_SOURCE_URL,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class UpdateCommandTests(unittest.TestCase):
    def test_update_command_is_no_ecosystem_command(self) -> None:
        import amof.entrypoint as entrypoint

        self.assertIn("update", entrypoint.NO_ECOSYSTEM_COMMANDS)

    def test_update_command_appears_in_help(self) -> None:
        import amof.cli as cli

        with patch.object(sys, "argv", ["amof", "--help"]), self.assertRaises(SystemExit) as raised:
            with redirect_stdout(StringIO()) as stdout:
                cli.parse_args()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("update", stdout.getvalue())

    def test_update_runs_without_ecosystem_when_already_current(self) -> None:
        import amof.entrypoint as entrypoint

        with patch.object(sys, "argv", ["amof", "update", "--version", f"v{update_cmd.__version__}", "--dry-run"]):
            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(StringIO()) as stdout:
                    entrypoint.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("AMOF is already up to date", stdout.getvalue())

    def test_latest_stable_tag_parsing_ignores_prereleases_and_peeled_duplicates(self) -> None:
        latest = update_cmd.parse_latest_stable_tag(
            [
                "aaa refs/tags/v2.0.1",
                "bbb refs/tags/v2.0.1^{}",
                "ccc refs/tags/v2.1.0-alpha.1",
                "ddd refs/tags/v2.1.0",
                "eee refs/tags/v10.0.0",
            ]
        )

        self.assertEqual(latest, "v10.0.0")

    def test_current_equals_target_exits_cleanly(self) -> None:
        calls = []
        result = update_cmd.cmd_update(
            _args(target_version=f"v{update_cmd.__version__}"),
            runner=lambda *a, **k: calls.append((a, k)),
        )

        self.assertEqual(result, 0)
        self.assertEqual(calls, [])

    def test_pipx_managed_update_calls_pipx_install_force(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        info = update_cmd.InstallInfo(
            method="pipx",
            executable="/home/user/.local/share/pipx/venvs/amof/bin/python",
            prefix="/home/user/.local/share/pipx/venvs/amof",
            runtime_path="/home/user/.local/share/pipx/venvs/amof/lib/python/site-packages/amof/commands/update.py",
            detail="pipx test install",
        )
        result = update_cmd.cmd_update(
            _args(target_version="v9.9.9", yes=True),
            install_info=info,
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [["/usr/bin/pipx", "install", "--force", "git+https://github.com/marekhotshot/amof.git@v9.9.9"]],
        )

    def test_update_dry_run_does_not_call_subprocess_mutation(self) -> None:
        calls = []
        info = update_cmd.InstallInfo(
            method="pipx",
            executable="/x/pipx/venvs/amof/bin/python",
            prefix="/x/pipx/venvs/amof",
            runtime_path="/x/pipx/venvs/amof/amof/commands/update.py",
            detail="pipx test install",
        )

        result = update_cmd.cmd_update(
            _args(target_version="v9.9.9", dry_run=True),
            install_info=info,
            runner=lambda *a, **k: calls.append((a, k)),
            which=lambda name: "/usr/bin/pipx",
        )

        self.assertEqual(result, 0)
        self.assertEqual(calls, [])

    def test_source_checkout_update_refuses_self_update(self) -> None:
        calls = []
        info = update_cmd.InstallInfo(
            method="source",
            executable=sys.executable,
            prefix=sys.prefix,
            runtime_path=str(ROOT / "scripts" / "amof" / "commands" / "update.py"),
            detail="source checkout",
        )

        with redirect_stderr(StringIO()) as stderr:
            result = update_cmd.cmd_update(
                _args(target_version="v9.9.9", yes=True),
                install_info=info,
                runner=lambda *a, **k: calls.append((a, k)),
            )

        self.assertEqual(result, 1)
        self.assertIn("source checkout install", stderr.getvalue())
        self.assertEqual(calls, [])


class UninstallCommandTests(unittest.TestCase):
    def test_pipx_managed_uninstall_calls_pipx_not_pip_inside_venv(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="uninstalled amof\n", stderr="")

        info = update_cmd.InstallInfo(
            method="pipx",
            executable="/home/user/.local/share/pipx/venvs/amof/bin/python",
            prefix="/home/user/.local/share/pipx/venvs/amof",
            runtime_path="/home/user/.local/share/pipx/venvs/amof/lib/python/site-packages/amof/commands/uninstall.py",
            detail="pipx test install",
        )
        result = uninstall_cmd.cmd_uninstall(
            SimpleNamespace(yes=True),
            install_info=info,
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        )

        self.assertEqual(result, 0)
        self.assertEqual(calls, [["/usr/bin/pipx", "uninstall", "amof"]])
        self.assertFalse(any("-m" in call and "pip" in call for call in calls))

    def test_pipx_managed_uninstall_recommends_pipx_when_missing(self) -> None:
        calls = []
        info = update_cmd.InstallInfo(
            method="pipx",
            executable="/home/user/.local/share/pipx/venvs/amof/bin/python",
            prefix="/home/user/.local/share/pipx/venvs/amof",
            runtime_path="/home/user/.local/share/pipx/venvs/amof/lib/python/site-packages/amof/commands/uninstall.py",
            detail="pipx test install",
        )

        with redirect_stderr(StringIO()) as stderr:
            result = uninstall_cmd.cmd_uninstall(
                SimpleNamespace(yes=True),
                install_info=info,
                runner=lambda *a, **k: calls.append((a, k)),
                which=lambda name: None,
            )

        self.assertEqual(result, 1)
        self.assertIn("pipx uninstall amof", stderr.getvalue())
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
