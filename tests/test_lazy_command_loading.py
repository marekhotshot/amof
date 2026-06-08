from __future__ import annotations

import importlib
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"

if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def _clear_modules(*names: str) -> None:
    for name in names:
        sys.modules.pop(name, None)


class LazyCommandLoadingTests(unittest.TestCase):
    def test_commands_package_import_is_lazy(self) -> None:
        _clear_modules(
            "amof.commands",
            "amof.commands.help_cmd",
            "amof.commands.studio",
            "amof.commands.promote_main",
            "amof.commands.release",
        )

        commands = importlib.import_module("amof.commands")

        self.assertNotIn("amof.commands.help_cmd", sys.modules)
        self.assertNotIn("amof.commands.studio", sys.modules)
        self.assertNotIn("amof.commands.promote_main", sys.modules)
        self.assertNotIn("amof.commands.release", sys.modules)

        cmd_help = getattr(commands, "cmd_help")

        self.assertTrue(callable(cmd_help))
        self.assertIn("amof.commands.help_cmd", sys.modules)
        self.assertNotIn("amof.commands.studio", sys.modules)
        self.assertNotIn("amof.commands.promote_main", sys.modules)
        self.assertNotIn("amof.commands.release", sys.modules)

    def test_entrypoint_lazy_wrapper_only_loads_requested_command(self) -> None:
        _clear_modules(
            "amof.entrypoint",
            "amof.commands.help_cmd",
            "amof.commands.studio",
            "amof.commands.promote_main",
            "amof.commands.release",
        )

        entrypoint = importlib.import_module("amof.entrypoint")

        self.assertNotIn("amof.commands.help_cmd", sys.modules)
        self.assertNotIn("amof.commands.studio", sys.modules)
        self.assertNotIn("amof.commands.promote_main", sys.modules)
        self.assertNotIn("amof.commands.release", sys.modules)

        with redirect_stdout(StringIO()):
            result = entrypoint.cmd_help(None)

        self.assertEqual(result, 0)
        self.assertIn("amof.commands.help_cmd", sys.modules)
        self.assertNotIn("amof.commands.studio", sys.modules)
        self.assertNotIn("amof.commands.promote_main", sys.modules)
        self.assertNotIn("amof.commands.release", sys.modules)


if __name__ == "__main__":
    unittest.main()
