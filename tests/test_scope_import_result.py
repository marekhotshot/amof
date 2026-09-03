from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
EXAMPLE = ROOT / "scripts" / "amof" / "contracts" / "examples" / "agent-run-result.example.json"


def _run_amof(amof_home: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS_ROOT)
    env["AMOF_HOME"] = str(amof_home)
    return subprocess.run(
        [sys.executable, "-m", "amof", *args],
        cwd=cwd or ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class ScopeImportResultTests(unittest.TestCase):
    def test_import_result_persists_proposal_ids(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-import-result-") as td:
            home = Path(td) / "amof-home"
            result = _run_amof(
                home,
                "scope",
                "import-result",
                str(EXAMPLE),
                "--run-id",
                "import-run-001",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("wsp-", result.stdout)
            listed = _run_amof(home, "scope", "list", "--json")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            payload = json.loads(listed.stdout)
            self.assertTrue(any(item.get("proposal_id", "").startswith("wsp-") for item in payload))

    def test_import_result_fails_closed_on_invalid_envelope(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-import-invalid-") as td:
            home = Path(td) / "amof-home"
            bad = Path(td) / "not-a-result.json"
            bad.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
            result = _run_amof(
                home,
                "scope",
                "import-result",
                str(bad),
                "--run-id",
                "import-run-bad",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("[scope]", result.stderr)
            self.assertIn("result_kind", result.stderr)

    def test_scope_list_empty_prints_import_hint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-scope-empty-") as td:
            home = Path(td) / "amof-home"
            result = _run_amof(home, "scope", "list")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("0 proposals", result.stdout)
            self.assertIn("amof scope import-result", result.stdout)


if __name__ == "__main__":
    unittest.main()
