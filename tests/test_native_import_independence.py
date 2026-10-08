"""Native must not load the Hermes adapter as its implementation library."""

import os
import subprocess
import sys
import unittest
from pathlib import Path


class NativeImportIndependenceTests(unittest.TestCase):
    def test_native_import_does_not_import_hermes_adapter(self) -> None:
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        env = dict(os.environ, PYTHONPATH=str(scripts))
        completed = subprocess.run(
            [sys.executable, "-c", "import sys; import amof.execution_backends.amof_native; "
             "assert 'amof.execution_backends.hermes_opensandbox' not in sys.modules"],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_other_adapters_do_not_import_hermes_as_shared_runtime(self) -> None:
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        env = dict(os.environ, PYTHONPATH=str(scripts))
        for module in ("claude_code", "cursor_agent"):
            with self.subTest(module=module):
                completed = subprocess.run(
                    [sys.executable, "-c", f"import sys; import amof.execution_backends.{module}; "
                     "assert 'amof.execution_backends.hermes_opensandbox' not in sys.modules"],
                    env=env, text=True, capture_output=True, check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
