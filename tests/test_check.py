import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof.commands.check import check_command_exists, cmd_check


class CheckCommandTests(unittest.TestCase):
    def test_basic_public_check_allows_git_identity_and_ssh_warnings(self) -> None:
        def fake_check_command_exists(cmd: str) -> tuple[bool, str]:
            versions = {
                "git": "git version 2.43.0",
                "python3": "Python 3.12.3",
                "docker": "Docker version 29.4.0",
                "helm": "not found",
                "aws": "not found",
                "kubectl": "not found",
                "cursor": "3.0.16",
            }
            return (cmd in {"git", "python3", "docker", "cursor"}, versions[cmd])

        stdout = io.StringIO()
        with (
            patch("amof.commands.check.check_command_exists", side_effect=fake_check_command_exists),
            patch(
                "amof.commands.check.check_git_config",
                return_value=["Git user.name not configured", "Git user.email not configured"],
            ),
            patch("amof.commands.check.check_ssh_key", return_value=(False, "~/.ssh directory not found")),
            patch("amof.commands.check.check_env_file", return_value=(True, "No optional .env overrides configured")),
            redirect_stdout(stdout),
        ):
            rc = cmd_check({"repos": []})

        output = stdout.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Git user.name not configured", output)
        self.assertIn("Git user.email not configured", output)
        self.assertIn("~/.ssh directory not found", output)
        self.assertIn("optional warnings", output)

    def test_check_fails_when_required_tool_missing(self) -> None:
        def fake_check_command_exists(cmd: str) -> tuple[bool, str]:
            versions = {
                "git": "not found",
                "python3": "Python 3.12.3",
                "docker": "not found",
                "helm": "not found",
                "aws": "not found",
                "kubectl": "not found",
                "cursor": "not found",
            }
            return (cmd == "python3", versions[cmd])

        stdout = io.StringIO()
        with (
            patch("amof.commands.check.check_command_exists", side_effect=fake_check_command_exists),
            patch("amof.commands.check.check_git_config", return_value=[]),
            patch("amof.commands.check.check_ssh_key", return_value=(False, "~/.ssh directory not found")),
            patch("amof.commands.check.check_env_file", return_value=(True, "No optional .env overrides configured")),
            redirect_stdout(stdout),
        ):
            rc = cmd_check({"repos": []})

        output = stdout.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("MISSING - Version control", output)
        self.assertIn("Required prerequisites missing", output)

    def test_check_command_exists_reports_timed_out_version_probe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-check-timeout-") as td:
            bin_dir = Path(td)
            cursor_path = bin_dir / "cursor"
            cursor_path.write_text(
                "#!/usr/bin/env bash\n"
                "sleep 2\n",
                encoding="utf-8",
            )
            cursor_path.chmod(0o755)

            original_path = os.environ.get("PATH", "")
            with patch.dict(os.environ, {"PATH": f"{bin_dir}:{original_path}"}):
                found, version = check_command_exists("cursor", probe_timeout_seconds=0.1)

        self.assertTrue(found)
        self.assertEqual(version, "installed (version probe timed out: cursor --version)")

    def test_cmd_check_completes_when_optional_cursor_version_probe_hangs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-check-cursor-hang-") as td:
            bin_dir = Path(td)
            cursor_path = bin_dir / "cursor"
            cursor_path.write_text(
                "#!/usr/bin/env bash\n"
                "sleep 2\n",
                encoding="utf-8",
            )
            cursor_path.chmod(0o755)

            original_path = os.environ.get("PATH", "")
            stdout = io.StringIO()
            with (
                patch.dict(os.environ, {"PATH": f"{bin_dir}:{original_path}"}),
                patch(
                    "amof.commands.check.check_command_exists",
                    side_effect=lambda cmd: (
                        check_command_exists(cmd, probe_timeout_seconds=0.1)
                        if cmd == "cursor"
                        else (cmd in {"git", "python3"}, f"{cmd} ok")
                    ),
                ),
                patch("amof.commands.check.check_git_config", return_value=[]),
                patch("amof.commands.check.check_ssh_key", return_value=(True, "Found id_ed25519")),
                patch("amof.commands.check.check_env_file", return_value=(True, "No optional .env overrides configured")),
                redirect_stdout(stdout),
            ):
                rc = cmd_check({"repos": []})

        output = stdout.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("installed (version probe timed out: cursor --version)", output)
        self.assertIn("optional warnings", output)


if __name__ == "__main__":
    unittest.main()
