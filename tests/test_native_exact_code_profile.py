import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amof.execution_backends import amof_native


class ExactCodeProfileTests(unittest.TestCase):
    def test_partial_local_ial_configuration_never_falls_back(self):
        with patch.dict(os.environ, {"AMOF_REMOTE_IAL_BASE_URL": "http://127.0.0.1:18761",
                                  "AMOF_REMOTE_IAL_API_KEY": "fixture", "AMOF_REMOTE_IAL_MODEL": ""}):
            with self.assertRaisesRegex(amof_native.AmofNativeBackendError, "external fallback denied"):
                amof_native._chat_endpoint_and_headers()

    def _tools(self, root: Path, paths: list[str]):
        enforcer = amof_native._GrantEnforcer(
            workspace=root, repo_roots=[root],
            grant_roots_resolved=[root / path for path in paths], writable=True,
        )
        return amof_native.NativeAgentTools(enforcer)

    def test_exact_grant_exposes_only_named_reads_and_governed_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src/existing.ts").write_text("export const value = 1;\n")
            paths = ["src/existing.ts", "src/new.ts"]
            with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(paths)}):
                tools = self._tools(root, paths)
                self.assertEqual(tools.read_file(paths[0]), "export const value = 1;\n")
                self.assertIn("NEW_APPROVED_FILE", tools.read_file(paths[1]))
                for name, args in (("list_dir", {"path": "."}), ("glob", {"pattern": "**/*"}),
                                   ("read_file", {"path": "src/other.ts"})):
                    with self.assertRaises(amof_native.AmofNativeBackendError):
                        tools.dispatch_tool(name, args)
                self.assertEqual(tools.enforcer.resolve_write_path(paths[1]), root / paths[1])
                with self.assertRaises(amof_native.AmofNativeBackendError):
                    tools.enforcer.resolve_write_path("src/other.ts")

    def test_mismatched_or_broad_profile_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ["src/a.ts", "src/b.ts"]
            for scope in (["src/a.ts", "src/other.ts"], ["src/a.ts", "src/a.ts"],
                          ["src/a.ts", "src/b.ts", "src/c.ts"]):
                with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(scope)}):
                    with self.assertRaises(amof_native.AmofNativeBackendError):
                        self._tools(root, paths)

    def test_large_exact_file_requires_bounded_line_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src/large.ts").write_text("".join(f"line {i}\n" for i in range(5000)))
            paths = ["src/large.ts", "src/new.ts"]
            with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(paths)}):
                tools = self._tools(root, paths)
                preview = tools.dispatch_tool("read_file", {"path": paths[0]})
                self.assertIn("LARGE_APPROVED_FILE", preview)
                self.assertLess(len(preview), 400)
                window = tools.dispatch_tool("read_file", {"path": paths[0],
                                                        "start_line": 320, "line_count": 20})
                self.assertIn("320: line 319", window)
                self.assertNotIn("5000: line 4999", window)
                with self.assertRaises(amof_native.AmofNativeBackendError):
                    tools.dispatch_tool("read_file", {"path": paths[0],
                                                      "start_line": 1, "line_count": 121})

    def test_model_receives_only_exact_code_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ["src/a.ts", "src/b.ts"]
            offered = []

            def fake_chat(**kwargs):
                offered.extend(spec["function"]["name"] for spec in kwargs["tools"])
                return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

            with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(paths)}), \
                    patch.object(amof_native, "_chat_completion", side_effect=fake_chat):
                status, _, _ = amof_native._run_model_loop(
                    goal="Use only exact files", tools=self._tools(root, paths),
                    model="fixture", writable=True, event_log_path=root / "events.jsonl", deadline=None,
                )
            self.assertEqual(status, "completed")
            self.assertEqual(offered, ["read_file", "write_file"])


if __name__ == "__main__":
    unittest.main()
