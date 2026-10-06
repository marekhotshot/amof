import hashlib
import json
import os
import unittest
from unittest.mock import patch

from amof.execution_backends import amof_native


class CampaignContextBoundTests(unittest.TestCase):
    def test_project_route_bounds_actual_remote_request_without_campaign_id(self):
        messages = [{"role": "user", "content": "Mandal project packet"}]
        for index in range(9):
            call_id = f"read-{index}"
            messages.extend([
                {"role": "assistant", "tool_calls": [{"id": call_id, "name": "read_file",
                    "arguments": {"path": "app/page.tsx"}}]},
                {"role": "tool", "results": [{"id": call_id,
                    "tool_call_id": call_id, "content": "allowed source" * 750}]},
            ])
        seen = []

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def read(self):
                return json.dumps({"stop_reason": "stop", "content": [],
                    "tokens": {"input": 1, "output": 1}}).encode()

        def fake_urlopen(request, timeout=0):
            seen.append(json.loads(request.data))
            self.assertLessEqual(len(request.data), amof_native.CAMPAIGN_REQUEST_SOFT_LIMIT_BYTES)
            return Response()

        with patch.dict(os.environ, {
            "AMOF_REMOTE_IAL_BASE_URL": "http://ial.example:8787",
            "AMOF_REMOTE_IAL_API_KEY": "fixture", "AMOF_REMOTE_IAL_MODEL": "x-ai/grok-4.6",
            "AMOF_NATIVE_INFERENCE_PROJECT_ID": "project-mandal-fixture",
            "AMOF_NATIVE_INFERENCE_CAMPAIGN_ID": "", "AMOF_NATIVE_SCRIPT": "",
        }), patch.object(amof_native, "urlopen", side_effect=fake_urlopen):
            amof_native._chat_completion(messages=messages, model="x-ai/grok-4.6", tools=[])
        self.assertEqual(len(seen), 1)
        self.assertNotIn("campaign_id", seen[0])
        self.assertEqual(seen[0]["messages"][0]["content"], "Mandal project packet")
        self.assertEqual(messages[-1]["results"][0]["content"], "allowed source" * 750)

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
