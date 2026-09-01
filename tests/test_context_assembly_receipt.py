from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.execution_backends import amof_native, hermes_opensandbox
from amof.execution_backends import context_assembly_receipt as receipt


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def _grant_tools(workspace: Path) -> amof_native.NativeAgentTools:
    selection = amof_native.build_selection(
        runner_id="amof-native-ticket-write",
        requested_capabilities=["read"],
        approve_writable_roots=[],
        timeout_seconds=30,
        readable_root=str(workspace),
    )
    selection = amof_native._resolve_grants_at_runtime(selection, workspace)
    enforcer = amof_native._GrantEnforcer(
        workspace=workspace,
        repo_roots=[workspace],
        grant_roots_resolved=[],
        writable=False,
    )
    return amof_native.NativeAgentTools(enforcer)


def _openai_completion(*, prompt_tokens: int, content: str = "done", tool_calls: list | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = ""
    return {
        "model": "x-ai/grok-4.6",
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 8},
        "choices": [{"message": message}],
    }


class ContextAssemblyReceiptUnitTests(unittest.TestCase):
    def test_split_preserves_build_prompt_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            selection = hermes_opensandbox.HermesBackendSelection(
                runner_id="amof-native-ticket-write",
                capabilities=["read", "bounded_write"],
                writable_roots=[str(workspace / "docs")],
                timeout_seconds=30,
                readable_root=str(workspace),
            )
            goal = "Add a liveness endpoint.\nStay on a working branch."
            prompt = hermes_opensandbox._build_prompt(
                goal,
                selection,
                workspace,
                {"repos": [{"path": str(workspace), "sha": "a" * 40}]},
                agent_label="AMOF Native Agent",
                backend_name="amof_native",
            )
            envelope, mission, override = receipt.split_user_prompt(prompt, goal)
            rebuilt = envelope + receipt.MISSION_HEADER + mission + override
            self.assertEqual(rebuilt, prompt)
            self.assertEqual(mission, goal)
            self.assertIn("APPROVED BOUNDED WRITE", override)

    def test_build_call_receipt_has_no_bodies_or_secrets(self) -> None:
        secret = "sk-secret-test-value-do-not-store"
        messages = [
            {"role": "system", "content": receipt.NATIVE_SYSTEM_CONTENT},
            {"role": "user", "content": "envelope\n\nMission:\nsealed goal text"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "read_file", "arguments": '{"path":"x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": f"token={secret}"},
        ]
        payload = receipt.build_call_receipt(
            run_id="amof-native-test",
            call_index=2,
            model="x-ai/grok-4.6",
            provider="remote-ial",
            messages=messages,
            tools=amof_native._TOOL_SPECS,
            goal="sealed goal text",
            request_id="handoff-18d0791e15264cb6-0478330a1def",
            prompt_tokens_reported=1966,
        )
        self.assertEqual(payload["schema"], receipt.RECEIPT_SCHEMA)
        self.assertEqual(payload["call_index"], 2)
        self.assertEqual(payload["backend"], "amof_native")
        self.assertEqual(payload["provider"], "remote-ial")
        names = [item["name"] for item in payload["sections"]]
        self.assertIn("system", names)
        self.assertIn("mission", names)
        self.assertIn("tool-result-1", names)
        mission = next(item for item in payload["sections"] if item["name"] == "mission")
        self.assertEqual(mission["authority_class"], "sealed-mission")
        self.assertEqual(mission["source_kind"], "handoff")
        self.assertEqual(mission["source_ref"], "handoff:18d0791e15264cb6-0478330a1def")
        self.assertIsNone(mission["tokens"])
        self.assertEqual(payload["tools"]["count"], 4)
        self.assertGreater(payload["tools"]["bytes"], 0)
        self.assertEqual(payload["assembled"]["prompt_tokens_reported"], 1966)
        self.assertGreater(payload["assembled"]["bytes"], 0)
        dumped = json.dumps(payload)
        self.assertNotIn("sealed goal text", dumped)
        self.assertNotIn(receipt.NATIVE_SYSTEM_CONTENT, dumped)
        self.assertNotIn(secret, dumped)
        self.assertNotIn("Authorization", dumped)
        self.assertIn({"class": "repository-files", "reason": "jit-only"}, payload["known_omissions"])

    def test_atomic_write_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "call-0001.json"
            receipt.atomic_write_json(path, {"ok": 1})
            with self.assertRaises(receipt.ReceiptExistsError):
                receipt.atomic_write_json(path, {"ok": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": 1})


class ContextAssemblyReceiptLoopTests(unittest.TestCase):
    def test_sequential_calls_write_distinct_receipts_and_hashes_grow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            tools = _grant_tools(workspace)
            run_dir = Path(tmp) / "run"
            event_log = run_dir / "events.jsonl"
            run_dir.mkdir()
            event_log.write_text("", encoding="utf-8")
            ctx = {
                "run_dir": run_dir,
                "run_id": "amof-native-seq",
                "request_id": "handoff-18d0791e15264cb6-0478330a1def",
                "goal": "sealed goal text",
                "event_log_path": event_log,
                "provider": "openai_compatible",
                "next_call_index": 1,
            }
            responses = [
                _openai_completion(
                    prompt_tokens=1837,
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "list_dir", "arguments": '{"path":"."}'},
                        }
                    ],
                ),
                _openai_completion(prompt_tokens=23962, content="done"),
            ]
            user_prompt = "envelope\n\nMission:\nsealed goal text"
            captured: list[dict] = []

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self) -> bytes:
                    return json.dumps(captured_response[0]).encode("utf-8")

            captured_response: list[dict] = []

            def fake_urlopen(request: Request, timeout: object = None):
                captured.append(json.loads(request.data.decode("utf-8")))
                return _Resp()

            env = {
                "OPENAI_API_KEY": "sk-test-should-not-appear-in-receipt",
                "AMOF_NATIVE_SCRIPT": "",
                "AMOF_REMOTE_IAL_BASE_URL": "",
                "AMOF_REMOTE_IAL_API_KEY": "",
                "AMOF_REMOTE_IAL_MODEL": "",
                "OPENROUTER_API_KEY": "",
            }
            captured_response.append(responses[0])
            with patch.dict(os.environ, env, clear=False):
                with patch.object(amof_native, "urlopen", side_effect=fake_urlopen):
                    # First call through real _chat_completion
                    messages = [
                        {"role": "system", "content": receipt.NATIVE_SYSTEM_CONTENT},
                        {"role": "user", "content": user_prompt},
                    ]
                    tools_spec = [spec for spec in amof_native._TOOL_SPECS if spec["function"]["name"] != "write_file"]
                    before = json.loads(json.dumps(messages))
                    before_tools = json.loads(json.dumps(tools_spec))
                    captured_response[0] = responses[0]
                    body1 = amof_native._chat_completion(
                        messages=messages,
                        model="x-ai/grok-4.6",
                        tools=tools_spec,
                        assembly_ctx=ctx,
                        call_index=1,
                    )
                    self.assertEqual(messages, before)
                    self.assertEqual(tools_spec, before_tools)
                    self.assertEqual(captured[0]["messages"], before)
                    self.assertEqual(captured[0]["tools"], before_tools)
                    self.assertEqual(captured[0]["model"], "x-ai/grok-4.6")
                    messages.append(body1["choices"][0]["message"])
                    messages.append(
                        {"role": "tool", "tool_call_id": "c1", "content": "README.md"}
                    )
                    captured_response[0] = responses[1]
                    amof_native._chat_completion(
                        messages=messages,
                        model="x-ai/grok-4.6",
                        tools=tools_spec,
                        assembly_ctx=ctx,
                        call_index=2,
                    )

            first = json.loads((run_dir / "context-assembly" / "call-0001.json").read_text(encoding="utf-8"))
            second = json.loads((run_dir / "context-assembly" / "call-0002.json").read_text(encoding="utf-8"))
            self.assertEqual(first["call_index"], 1)
            self.assertEqual(second["call_index"], 2)
            self.assertNotEqual(first["assembled"]["sha256"], second["assembled"]["sha256"])
            self.assertLess(first["assembled"]["bytes"], second["assembled"]["bytes"])
            self.assertEqual(first["assembled"]["prompt_tokens_reported"], 1837)
            self.assertEqual(second["assembled"]["prompt_tokens_reported"], 23962)
            self.assertTrue(any(item["name"].startswith("tool-result-") for item in second["sections"]))
            dumped = json.dumps(first) + json.dumps(second)
            self.assertNotIn("sealed goal text", dumped)
            self.assertNotIn("sk-test-should-not-appear-in-receipt", dumped)
            self.assertNotIn("Authorization", dumped)
            events = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(
                [item["event"] for item in events if item["event"] == "context_assembly_receipt_written"],
                ["context_assembly_receipt_written", "context_assembly_receipt_written"],
            )

    def test_receipt_write_failure_is_observable_and_does_not_fail_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            event_log = run_dir / "events.jsonl"
            run_dir.mkdir()
            event_log.write_text("", encoding="utf-8")
            ctx = {
                "run_dir": run_dir,
                "run_id": "amof-native-fail",
                "request_id": "req-fail",
                "goal": "g",
                "event_log_path": event_log,
                "provider": "openai_compatible",
            }

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self) -> bytes:
                    return json.dumps(_openai_completion(prompt_tokens=10, content="ok")).encode("utf-8")

            env = {
                "OPENAI_API_KEY": "sk-test-should-not-appear-in-receipt",
                "AMOF_NATIVE_SCRIPT": "",
                "AMOF_REMOTE_IAL_BASE_URL": "",
                "AMOF_REMOTE_IAL_API_KEY": "",
                "AMOF_REMOTE_IAL_MODEL": "",
                "OPENROUTER_API_KEY": "",
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(amof_native, "urlopen", return_value=_Resp()):
                    with patch.object(
                        receipt,
                        "persist_call_receipt",
                        side_effect=OSError("disk full"),
                    ):
                        body = amof_native._chat_completion(
                            messages=[
                                {"role": "system", "content": receipt.NATIVE_SYSTEM_CONTENT},
                                {"role": "user", "content": "hi"},
                            ],
                            model="x-ai/grok-4.6",
                            tools=[],
                            assembly_ctx=ctx,
                            call_index=1,
                        )
            self.assertEqual(body["choices"][0]["message"]["content"], "ok")
            events = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines() if line.strip()]
            failed = [item for item in events if item["event"] == "context_assembly_receipt_failed"]
            self.assertEqual(len(failed), 1)
            self.assertIn("disk full", failed[0]["error"])
            self.assertFalse((run_dir / "context-assembly" / "call-0001.json").exists())

    def test_model_loop_emits_per_turn_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            tools = _grant_tools(workspace)
            run_dir = Path(tmp) / "run"
            event_log = run_dir / "events.jsonl"
            run_dir.mkdir()
            event_log.write_text("", encoding="utf-8")
            ctx = {
                "run_dir": run_dir,
                "run_id": "amof-native-loop",
                "request_id": "handoff-18d0791e15264cb6-0478330a1def",
                "goal": "test mission",
                "event_log_path": event_log,
                "provider": "remote-ial",
                "next_call_index": 1,
            }
            sequence = [
                _openai_completion(
                    prompt_tokens=100,
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "list_dir", "arguments": '{"path":"."}'},
                        }
                    ],
                ),
                _openai_completion(prompt_tokens=250, content="finished"),
            ]

            def _fake_chat(**kwargs: object) -> dict:
                # Preserve persist by calling through a thin wrapper that still hits emit.
                idx = kwargs.get("call_index")
                body = sequence.pop(0)
                amof_native._emit_context_assembly_receipt(
                    assembly_ctx=kwargs.get("assembly_ctx"),
                    call_index=idx if isinstance(idx, int) else None,
                    model=str(kwargs.get("model") or ""),
                    messages=list(kwargs.get("messages") or []),
                    tools=kwargs.get("tools") if isinstance(kwargs.get("tools"), list) else [],
                    prompt_tokens_reported=body["usage"]["prompt_tokens"],
                )
                return body

            with patch.object(amof_native, "_chat_completion", side_effect=_fake_chat):
                status, stop, text = amof_native._run_model_loop(
                    goal="envelope\n\nMission:\ntest mission",
                    tools=tools,
                    model="x-ai/grok-4.6",
                    writable=False,
                    event_log_path=event_log,
                    deadline=None,
                    run_id="loop-req",
                    assembly_ctx=ctx,
                )
            self.assertEqual(status, "completed")
            self.assertEqual(stop, "completed")
            self.assertEqual(text, "finished")
            first = run_dir / "context-assembly" / "call-0001.json"
            second = run_dir / "context-assembly" / "call-0002.json"
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            a = json.loads(first.read_text(encoding="utf-8"))
            b = json.loads(second.read_text(encoding="utf-8"))
            self.assertNotEqual(a["assembled"]["sha256"], b["assembled"]["sha256"])


if __name__ == "__main__":
    unittest.main()
