from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


def _write_script(path: Path, steps: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"provider": "amof_native_script", "model": "script-v1", "steps": steps}),
        encoding="utf-8",
    )


def _run_with_script(*, workspace: Path, script_path: Path, writable_roots: list[str] | None = None) -> dict:
    selection = amof_native.build_selection(
        runner_id="amof-native-ticket-write",
        requested_capabilities=["read", "bounded_write"] if writable_roots else ["read"],
        approve_writable_roots=list(writable_roots or []),
        timeout_seconds=30,
        readable_root=str(workspace),
    )
    with patch.dict(os.environ, {"AMOF_NATIVE_SCRIPT": str(script_path)}, clear=False):
        with patch.object(amof_native, "_run_dir", return_value=script_path.parent / "run"):
            (script_path.parent / "run").mkdir(exist_ok=True)
            return amof_native.run(
                manifest={"repos": [{"path": str(workspace)}]},
                goal="test mission",
                request_id="handoff-18d0791e15264cb6-0478330a1def",
                studio_session_id=None,
                selection=selection,
            )


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

    def test_build_receipt_matches_operator_shape_without_bodies(self) -> None:
        user_prompt = "envelope-line\n\nMission:\nsealed goal text"
        payload = receipt.build_receipt(
            run_id="amof-native-test",
            model="x-ai/grok-4.6",
            system_text=receipt.NATIVE_SYSTEM_CONTENT,
            user_prompt=user_prompt,
            goal="sealed goal text",
            tool_specs=amof_native._TOOL_SPECS,
            request_id="handoff-18d0791e15264cb6-0478330a1def",
            prompt_tokens=1837,
        )
        self.assertEqual(payload["schema"], "amof.context-assembly.receipt.v1")
        self.assertEqual(payload["call_index"], 1)
        self.assertEqual(payload["model"], "x-ai/grok-4.6")
        names = [item["name"] for item in payload["sections"]]
        self.assertEqual(names, ["system", "runtime-envelope", "mission"])
        mission = payload["sections"][2]
        self.assertEqual(mission["authority_class"], "sealed-mission")
        self.assertEqual(mission["source"], "handoff:18d0791e15264cb6-0478330a1def")
        self.assertEqual(mission["bytes"], len("sealed goal text".encode("utf-8")))
        self.assertIsNone(mission["tokens"])
        self.assertEqual(payload["tools"]["count"], 4)
        self.assertEqual(payload["assembled"]["prompt_tokens"], 1837)
        dumped = json.dumps(payload)
        self.assertNotIn("sealed goal text", dumped)
        self.assertNotIn(receipt.NATIVE_SYSTEM_CONTENT, dumped)
        self.assertIn({"class": "repository-files", "reason": "jit-only"}, payload["omissions"])

    def test_record_prompt_tokens_joins_provider_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context-assembly.json"
            payload = receipt.build_receipt(
                run_id="run",
                model="script-v1",
                system_text=receipt.NATIVE_SYSTEM_CONTENT,
                user_prompt="e\n\nMission:\ng",
                goal="g",
                tool_specs=[],
            )
            self.assertIsNone(payload["assembled"]["prompt_tokens"])
            receipt.write_receipt(path, payload)
            receipt.record_prompt_tokens(path, 1837)
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated["assembled"]["prompt_tokens"], 1837)
            self.assertEqual(updated["assembled"]["sha256"], payload["assembled"]["sha256"])


class ContextAssemblyReceiptNativePathTests(unittest.TestCase):
    def test_scripted_run_writes_receipt_and_evidence_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            script = Path(tmp) / "script.json"
            _write_script(script, [{"type": "final", "text": "ok"}])
            result = _run_with_script(
                workspace=workspace,
                script_path=script,
                writable_roots=["docs/"],
            )
            self.assertEqual(result["status"], "completed")
            receipt_path = Path(result["evidence_refs"]["context_assembly_receipt"])
            self.assertTrue(receipt_path.is_file())
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], receipt.RECEIPT_SCHEMA)
            self.assertEqual(payload["call_index"], 1)
            self.assertEqual(payload["tools"]["count"], 4)
            self.assertIsNone(payload["assembled"]["prompt_tokens"])
            self.assertEqual(payload["sections"][0]["source"], "amof_native:_chat_completion")
            mission = next(item for item in payload["sections"] if item["name"] == "mission")
            self.assertEqual(mission["source"], "handoff:18d0791e15264cb6-0478330a1def")
            dumped = json.dumps(payload)
            self.assertNotIn("test mission", dumped)
            schema_path = ROOT / "contracts" / "context-assembly-receipt.v1.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            try:
                import jsonschema
            except ImportError:
                self.assertIn("sha256", payload["assembled"])
            else:
                jsonschema.validate(payload, schema)

    def test_model_loop_joins_provider_prompt_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
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
            tools = amof_native.NativeAgentTools(enforcer)
            event_log = Path(tmp) / "events.jsonl"
            event_log.write_text("", encoding="utf-8")
            receipt_path = Path(tmp) / "context-assembly.json"
            receipt.write_receipt(
                receipt_path,
                receipt.build_receipt(
                    run_id="run-token",
                    model="x-ai/grok-4.6",
                    system_text=receipt.NATIVE_SYSTEM_CONTENT,
                    user_prompt="e\n\nMission:\ng",
                    goal="g",
                    tool_specs=[],
                ),
            )

            def _fake_chat(**_kwargs: object) -> dict:
                return {
                    "model": "x-ai/grok-4.6",
                    "usage": {"prompt_tokens": 1837, "completion_tokens": 12},
                    "choices": [{"message": {"role": "assistant", "content": "done"}}],
                }

            with patch.object(amof_native, "_chat_completion", side_effect=_fake_chat):
                status, stop, _text = amof_native._run_model_loop(
                    goal="e\n\nMission:\ng",
                    tools=tools,
                    model="x-ai/grok-4.6",
                    writable=False,
                    event_log_path=event_log,
                    deadline=None,
                    run_id="req-token",
                    receipt_path=receipt_path,
                )
            self.assertEqual(status, "completed")
            self.assertEqual(stop, "completed")
            updated = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["assembled"]["prompt_tokens"], 1837)


if __name__ == "__main__":
    unittest.main()
