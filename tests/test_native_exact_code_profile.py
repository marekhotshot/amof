import json
import hashlib
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
                self.assertIn("export const value = 1;\n", tools.read_file(paths[0]))
                self.assertIn(hashlib.sha256(b"export const value = 1;\n").hexdigest(), tools.read_file(paths[0]))
                with self.assertRaisesRegex(amof_native.AmofNativeBackendError, "requires replace_text"):
                    tools.write_file(paths[0], "overwrite")
                self.assertIn("NEW_APPROVED_FILE", tools.read_file(paths[1]))
                for name, args in (("list_dir", {"path": "."}), ("glob", {"pattern": "**/*"}),
                                   ("read_file", {"path": "src/other.ts"})):
                    with self.assertRaises(amof_native.AmofNativeBackendError):
                        tools.dispatch_tool(name, args)
                self.assertEqual(tools.enforcer.resolve_write_path(paths[1]), root / paths[1])
                with self.assertRaises(amof_native.AmofNativeBackendError):
                    tools.enforcer.resolve_write_path("src/other.ts")

    def test_edit_tool_returns_parent_outcome_hash_for_next_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src/existing.ts").write_text("old\n", encoding="utf-8")
            path = "src/existing.ts"
            outcome_hash = hashlib.sha256(b"new\n").hexdigest()
            paths = [path, "src/new.ts"]
            with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(paths)}):
                tools = self._tools(root, paths)

                def completed_edit(*_args):
                    tools.write_receipts.append({"status": "COMPLETED", "actual_sha256": outcome_hash})

                with patch.object(tools, "replace_text", side_effect=completed_edit):
                    result = tools.dispatch_tool("replace_text", {
                        "path": path,
                        "expected_file_sha256": hashlib.sha256(b"old\n").hexdigest(),
                        "expected_old": "old", "replacement": "new",
                    })
                self.assertEqual(result, f"replaced text in {path}\nFILE_SHA256: {outcome_hash}")

                tools.write_receipts.clear()
                with patch.object(tools, "replace_text", side_effect=lambda *_: tools.write_receipts.append(
                        {"status": "COMPLETED"})):
                    with self.assertRaisesRegex(amof_native.AmofNativeBackendError, "without an outcome hash"):
                        tools.dispatch_tool("replace_text", {
                            "path": path, "expected_file_sha256": outcome_hash,
                            "expected_old": "old", "replacement": "new",
                        })

    def test_mismatched_or_broad_profile_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ["src/a.ts", "src/b.ts"]
            for scope in (["src/a.ts", "src/other.ts"], ["src/a.ts", "src/a.ts"],
                          ["src/a.ts", "src/b.ts", "src/c.ts"]):
                with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(scope)}):
                    with self.assertRaises(amof_native.AmofNativeBackendError):
                        self._tools(root, paths)

    def test_noop_replace_does_not_contact_parent_or_claim_edit_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            path = root / "src/existing.ts"
            path.write_text("const value = 1;\n")
            paths = ["src/existing.ts", "src/new.ts"]
            with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(paths),
                                      "AMOF_NATIVE_WRITE_SOCKET": str(root / "missing.sock"),
                                      "AMOF_NATIVE_WRITE_TOKEN": "fixture"}):
                tools = self._tools(root, paths)
                with self.assertRaisesRegex(amof_native.AmofNativeBackendError, "changed fragment"):
                    tools.replace_text(paths[0], hashlib.sha256(path.read_bytes()).hexdigest(),
                                       "const value = 1;", "const value = 1;")
                self.assertEqual(tools.write_receipts, [])
                self.assertEqual(path.read_text(), "const value = 1;\n")

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
            self.assertEqual(offered, ["replace_text", "read_file", "write_file"])

    def test_packet_bound_first_write_offers_only_write_file_on_first_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ["src/new.ts", "src/existing.ts"]
            (root / "src").mkdir()
            (root / paths[1]).write_text("export const existing = true;\n")
            offered = []

            def fake_chat(**kwargs):
                offered.append([spec["function"]["name"] for spec in kwargs["tools"]])
                return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

            goal = "Create the store first.\nAMOF_FIRST_WRITE_PATH: src/new.ts\n"
            with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(paths)}), \
                    patch.object(amof_native, "_chat_completion", side_effect=fake_chat):
                amof_native._run_model_loop(goal=goal, tools=self._tools(root, paths),
                    model="fixture", writable=True, event_log_path=root / "events.jsonl", deadline=None)
            self.assertEqual(offered, [["write_file"]])
            for bad in ("src/existing.ts", "src/other.ts", "../src/new.ts"):
                with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(paths)}):
                    with self.assertRaises(amof_native.AmofNativeBackendError):
                        amof_native._run_model_loop(
                            goal=f"AMOF_FIRST_WRITE_PATH: {bad}", tools=self._tools(root, paths),
                            model="fixture", writable=True, event_log_path=root / "events.jsonl", deadline=None)

    def test_packet_bound_first_edit_requires_existing_hash_and_only_offers_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            path = "src/existing.ts"
            (root / path).write_text("export const old = true;\n")
            digest = hashlib.sha256((root / path).read_bytes()).hexdigest()
            paths = [path, "src/new.ts"]
            offered = []

            def fake_chat(**kwargs):
                offered.append([spec["function"]["name"] for spec in kwargs["tools"]])
                return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

            goal = f"AMOF_FIRST_EDIT_PATH: {path}\nAMOF_FIRST_EDIT_SHA256: {digest}\n"
            with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(paths)}), \
                    patch.object(amof_native, "_chat_completion", side_effect=fake_chat):
                status, reason, _ = amof_native._run_model_loop(goal=goal, tools=self._tools(root, paths),
                    model="fixture", writable=True, event_log_path=root / "events.jsonl", deadline=None)
                self.assertEqual(offered, [["replace_text"]])
                self.assertEqual((status, reason), ("failed", "first_edit_not_requested"))
                offered.clear()
                (root / path).write_text("changed concurrently\n")
                with self.assertRaisesRegex(amof_native.AmofNativeBackendError, "hash changed"):
                    amof_native._run_model_loop(goal=goal, tools=self._tools(root, paths),
                        model="fixture", writable=True, event_log_path=root / "events.jsonl", deadline=None)
                self.assertEqual(offered, [])

    def test_first_edit_rejects_wrong_tool_path_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            target = root / "src/existing.ts"
            target.write_text("const old = true;\n")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            paths = ["src/existing.ts", "src/other.ts"]
            calls = []

            def fake_chat(**kwargs):
                if calls:
                    return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}
                calls.append(1)
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "bad", "function": {"name": "replace_text", "arguments": json.dumps({
                        "path": "src/other.ts", "expected_file_sha256": digest,
                        "expected_old": "old", "replacement": "new"})}}]}}]}

            goal = f"AMOF_FIRST_EDIT_PATH: src/existing.ts\nAMOF_FIRST_EDIT_SHA256: {digest}\n"
            with patch.dict(os.environ, {"AMOF_NATIVE_EXACT_CODE_SCOPE_JSON": json.dumps(paths)}), \
                    patch.object(amof_native, "_chat_completion", side_effect=fake_chat):
                status, reason, _ = amof_native._run_model_loop(goal=goal,
                    tools=self._tools(root, paths), model="fixture", writable=True,
                    event_log_path=root / "events.jsonl", deadline=None)
            self.assertEqual((status, reason), ("failed", "first_edit_not_completed"))
            self.assertEqual(target.read_text(), "const old = true;\n")


if __name__ == "__main__":
    unittest.main()
