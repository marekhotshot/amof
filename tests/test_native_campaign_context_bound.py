import hashlib
import json
import unittest

from amof.execution_backends import amof_native


class CampaignContextBoundTests(unittest.TestCase):
    def test_repeated_reads_are_bounded_without_changing_latest_result_or_identity(self):
        original = "approved source excerpt\n" * 450
        messages = [{"role": "user", "content": "Implement the scoped change"}]
        for index in range(10):
            messages.append({"role": "assistant", "tool_calls": [{"id": f"call-{index}",
                "name": "read_file", "arguments": {"path": "src/approved.ts"}}]})
            messages.append({"role": "tool", "results": [{"id": f"call-{index}",
                "tool_call_id": f"call-{index}", "content": original}]})
        payload = {"system": "bounded task", "messages": messages, "tools": [],
                   "model": "openai/gpt-5.4", "campaign_id": "fixture"}
        self.assertGreater(len(json.dumps(payload).encode()), 65_536)
        amof_native._bound_campaign_tool_history(payload)
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), 60_000)
        self.assertEqual(messages[-1]["results"][0]["content"], original)
        self.assertEqual(messages[0]["content"], "Implement the scoped change")
        self.assertIn(hashlib.sha256(original.encode()).hexdigest(),
                      messages[2]["results"][0]["content"])
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "call-0")

    def test_unbounded_goal_fails_before_external_request(self):
        payload = {"messages": [{"role": "user", "content": "x" * 65_000}],
                   "tools": [], "system": "fixture"}
        with self.assertRaisesRegex(amof_native.AmofNativeBackendError, "remains too large"):
            amof_native._bound_campaign_tool_history(payload)

    def test_repeated_tool_cycles_compact_without_dangling_latest_call(self):
        messages = [{"role": "user", "content": "Bounded source-link task"}]
        for index in range(95):
            call_id = f"call-{index}"
            messages.append({"role": "assistant", "content": "Read the selected source. " * 18,
                             "tool_calls": [{"id": call_id, "name": "read_file",
                                             "arguments": {"path": "src/approved.ts", "start_line": index}}]})
            messages.append({"role": "tool", "results": [{"id": call_id,
                             "tool_call_id": call_id, "content": "small allowed excerpt"}]})
        payload = {"system": "bounded task", "messages": messages, "tools": [],
                   "model": "openai/gpt-6.1-sol", "campaign_id": "fixture"}
        self.assertGreater(len(json.dumps(payload).encode()), 60_000)
        amof_native._bound_campaign_tool_history(payload)
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), 60_000)
        self.assertEqual(messages[0]["content"], "Bounded source-link task")
        self.assertIn("Earlier model/tool exchanges omitted", messages[1]["content"])
        self.assertEqual(messages[-2]["tool_calls"][0]["id"], "call-94")
        self.assertEqual(messages[-1]["results"][0]["tool_call_id"], "call-94")
        self.assertEqual(messages[-1]["results"][0]["content"], "small allowed excerpt")
        calls = {call["id"] for message in messages for call in message.get("tool_calls", [])}
        self.assertTrue(all(result["tool_call_id"] in calls for message in messages
                            for result in message.get("results", [])))

    def test_compaction_preserves_parent_confirmed_edit_and_current_hash(self):
        edit_result = (
            "replaced text in src/mission.ts\n"
            "FILE_SHA256: 14715489fabb1ed5dc5cbbdf08f8b3ada632d48d60f93b1799e75bdbcf677af1"
        )
        messages = [{"role": "user", "content": "Complete the scoped code change"},
                    {"role": "assistant", "tool_calls": [{"id": "edit-1",
                     "name": "replace_text", "arguments": {"path": "src/mission.ts"}}]},
                    {"role": "tool", "results": [{"id": "edit-1",
                     "tool_call_id": "edit-1", "content": edit_result}]}]
        for index in range(95):
            call_id = f"read-{index}"
            messages.append({"role": "assistant", "content": "Inspect the source. " * 18,
                             "tool_calls": [{"id": call_id, "name": "read_file",
                                             "arguments": {"path": "src/mission.ts"}}]})
            messages.append({"role": "tool", "results": [{"id": call_id,
                             "tool_call_id": call_id, "content": "source excerpt" * 25}]})
        payload = {"system": "bounded task", "messages": messages, "tools": [],
                   "model": "openai/gpt-6.1-sol", "campaign_id": "fixture"}
        self.assertGreater(len(json.dumps(payload).encode()), 60_000)
        amof_native._bound_campaign_tool_history(payload)
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), 60_000)
        self.assertEqual(messages[2]["tool_calls"][0]["id"], "edit-1")
        self.assertEqual(messages[3]["results"][0]["content"], edit_result)
        self.assertEqual(messages[-2]["tool_calls"][0]["id"], "read-94")
        self.assertEqual(messages[-1]["results"][0]["tool_call_id"], "read-94")


if __name__ == "__main__":
    unittest.main()
