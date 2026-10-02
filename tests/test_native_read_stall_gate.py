"""R-007 regression: repeated workspace reads lead to a bounded edit/checkpoint."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from amof.execution_backends import amof_native


class ReadStallGateTests(unittest.TestCase):
    def run_loop(self, *, marker=True, next_name="replace_text"):
        seen = []
        calls = []

        class Tools:
            workspace_directory = "services/operator-console/"
            exact_document_path = None
            exact_code_paths = None
            enforcer = type("E", (), {"grant_roots": []})()
            repo_root = Path(".")

            def dispatch_tool(self, name, arguments):
                calls.append(name)
                return "FILE_SHA256: " + "a" * 64 if name == "replace_text" else "source"

        turn = 0

        def response(**kwargs):
            nonlocal turn
            turn += 1
            names = [x["function"]["name"] for x in kwargs["tools"]]
            seen.append((names, kwargs["messages"]))
            if turn <= 7:
                name, args = "read_file", {"path": "services/operator-console/src/a.ts", "start_line": 1, "line_count": 5}
            elif turn == 8:
                name, args = next_name, {"path": "services/operator-console/src/a.ts", "expected_old": "a", "replacement": "b"}
            else:
                return {"choices": [{"message": {"role": "assistant", "content": "done"}}], "usage": {}, "model": "fixture"}
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{turn}", "function": {"name": name, "arguments": json.dumps(args)}}]}}], "usage": {}, "model": "fixture"}

        with tempfile.TemporaryDirectory() as directory, patch.object(amof_native, "_chat_completion", side_effect=response), patch.object(amof_native, "_grant_tree_digest", return_value="g"):
            result = amof_native._run_model_loop(
                goal="AMOF_READ_STALL_GATE: enabled\nEdit source" if marker else "Edit source",
                tools=Tools(), model="fixture", writable=True,
                event_log_path=Path(directory) / "events.jsonl", deadline=None,
            )
        return result, seen, calls

    def test_repeated_reads_force_one_edit_turn_then_restore_tools(self):
        result, seen, calls = self.run_loop()
        self.assertEqual(result[:2], ("completed", "completed"))
        self.assertEqual(set(seen[7][0]), {"write_file", "replace_text"})
        self.assertTrue(any("WORKSPACE_READ_STALL" in str(message.get("content")) for message in seen[7][1]))
        self.assertEqual(sum(message.get("role") == "user" for message in seen[7][1]), 1)
        self.assertIn("read_file", seen[8][0])
        self.assertEqual(calls.count("replace_text"), 1)

    def test_repeated_read_attempt_fails_without_mutation(self):
        result, seen, calls = self.run_loop(next_name="read_file")
        self.assertEqual(result[:2], ("failed", "workspace_read_stall_checkpoint"))
        self.assertEqual(set(seen[7][0]), {"write_file", "replace_text"})
        self.assertEqual(calls, ["read_file"] * 7)

    def test_other_workspace_tasks_keep_existing_tools(self):
        result, seen, calls = self.run_loop(marker=False)
        self.assertEqual(result[:2], ("completed", "completed"))
        self.assertIn("read_file", seen[7][0])


if __name__ == "__main__":
    unittest.main()
