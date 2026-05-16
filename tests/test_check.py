import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from amof.commands.check import cmd_check


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


if __name__ == "__main__":
    unittest.main()
