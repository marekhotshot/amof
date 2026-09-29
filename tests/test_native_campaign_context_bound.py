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


if __name__ == "__main__":
    unittest.main()
