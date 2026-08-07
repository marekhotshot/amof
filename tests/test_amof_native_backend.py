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

from amof.commands import runner as runner_cmd
from amof.execution_backends import amof_native, hermes_opensandbox


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
        json.dumps(
            {
                "provider": "amof_native_script",
                "model": "script-v1",
                "steps": steps,
            }
        ),
        encoding="utf-8",
    )


def _run_with_script(
    *,
    workspace: Path,
    script_path: Path,
    writable_roots: list[str] | None = None,
    timeout_seconds: int = 30,
    manifest: dict | None = None,
    selection_kwargs: dict | None = None,
) -> dict:
    selection = amof_native.build_selection(
        runner_id="amof-native-ticket-write",
        requested_capabilities=["read", "bounded_write"] if writable_roots else ["read"],
        approve_writable_roots=list(writable_roots or []),
        timeout_seconds=timeout_seconds,
        readable_root=str(workspace),
        **(selection_kwargs or {}),
    )
    env = {"AMOF_NATIVE_SCRIPT": str(script_path)}
    with patch.dict(os.environ, env, clear=False):
        with patch.object(amof_native, "_run_dir", return_value=script_path.parent / "run"):
            (script_path.parent / "run").mkdir(exist_ok=True)
            return amof_native.run(
                manifest=manifest or {"repos": [{"path": str(workspace)}]},
                goal="test mission",
                request_id="req-test",
                studio_session_id=None,
                selection=selection,
            )


class AmofNativeGrantNormalizationTests(unittest.TestCase):
    def test_relativizes_absolute_grant_under_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            docs = workspace / "docs"
            docs.mkdir()
            selection = amof_native.build_selection(
                runner_id="amof-native-ticket-write",
                requested_capabilities=["read", "bounded_write"],
                approve_writable_roots=[str(docs)],
                timeout_seconds=30,
                readable_root=str(workspace),
            )
            self.assertEqual(selection.writable_roots_relative, ["docs"])

    def test_traversal_grant_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            with self.assertRaises(amof_native.AmofNativeBackendError):
                amof_native.build_selection(
                    runner_id="amof-native-ticket-write",
                    requested_capabilities=["read", "bounded_write"],
                    approve_writable_roots=["../outside"],
                    timeout_seconds=30,
                    readable_root=str(workspace),
                )


class AmofNativeScriptedRunTests(unittest.TestCase):
    def test_valid_bounded_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "note.md").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "docs/note.md"], cwd=workspace, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "add note"], cwd=workspace, check=True, capture_output=True)
            script = Path(tmp) / "script.json"
            _write_script(
                script,
                [
                    {"type": "tool", "name": "read_file", "arguments": {"path": "docs/note.md"}},
                    {"type": "final", "text": "read ok"},
                ],
            )
            result = _run_with_script(workspace=workspace, script_path=script)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["task_findings"], "read ok")

    def test_valid_bounded_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            script = Path(tmp) / "script.json"
            _write_script(
                script,
                [
                    {
                        "type": "tool",
                        "name": "write_file",
                        "arguments": {"path": "docs/x.md", "content": "hello"},
                    },
                    {"type": "final", "text": "done"},
                ],
            )
            result = _run_with_script(
                workspace=workspace,
                script_path=script,
                writable_roots=["docs/"],
            )
            self.assertEqual(result["status"], "completed")
            self.assertTrue((workspace / "docs" / "x.md").is_file())
            self.assertIn("docs/x.md", result["changed_paths"])

    def test_write_outside_grant_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            script = Path(tmp) / "script.json"
            _write_script(
                script,
                [
                    {
                        "type": "tool",
                        "name": "write_file",
                        "arguments": {"path": "other/out.md", "content": "nope"},
                    },
                    {"type": "final", "text": "done"},
                ],
            )
            result = _run_with_script(
                workspace=workspace,
                script_path=script,
                writable_roots=["docs/"],
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["stop_reason"], "grant_enforcement_failed")

    def test_symlink_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            (workspace / "docs").mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            link = workspace / "docs" / "link.txt"
            link.symlink_to(outside / "secret.txt")
            script = Path(tmp) / "script.json"
            _write_script(
                script,
                [
                    {
                        "type": "tool",
                        "name": "write_file",
                        "arguments": {"path": "docs/link.txt", "content": "hack"},
                    },
                    {"type": "final", "text": "done"},
                ],
            )
            result = _run_with_script(
                workspace=workspace,
                script_path=script,
                writable_roots=["docs/"],
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["stop_reason"], "grant_enforcement_failed")

    def test_timeout_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            script = Path(tmp) / "script.json"
            _write_script(
                script,
                [
                    {"type": "tool", "name": "read_file", "arguments": {"path": "README.md"}},
                    {"type": "final", "text": "late"},
                ],
            )
            result = _run_with_script(
                workspace=workspace,
                script_path=script,
                timeout_seconds=0,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["stop_reason"], "timeout")

    def test_wrong_target_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            script = Path(tmp) / "script.json"
            _write_script(script, [{"type": "final", "text": "ok"}])
            result = _run_with_script(
                workspace=workspace,
                script_path=script,
                manifest={
                    "repos": [
                        {
                            "path": str(workspace),
                            "target_id": "target-real",
                            "sha": "a" * 40,
                        }
                    ]
                },
                selection_kwargs={"target_id": "target-wrong"},
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["stop_reason"], "target_binding_rejected")

    def test_wrong_sha_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            script = Path(tmp) / "script.json"
            _write_script(script, [{"type": "final", "text": "ok"}])
            result = _run_with_script(
                workspace=workspace,
                script_path=script,
                manifest={
                    "repos": [
                        {
                            "path": str(workspace),
                            "target_id": "target-real",
                            "sha": "b" * 40,
                        }
                    ]
                },
                selection_kwargs={"accepted_base_sha": "a" * 40},
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["stop_reason"], "target_binding_rejected")


class AmofNativeResultContractTests(unittest.TestCase):
    def test_result_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            selection = amof_native.AmofNativeBackendSelection(
                runner_id="amof-native-ticket-write",
                capabilities=["read"],
                writable_roots_relative=[],
                writable_roots_resolved=(),
                timeout_seconds=30,
                readable_root=str(workspace),
            )
            payload = amof_native._result_payload(
                run_id="amof-native-test-1",
                status="completed",
                exit_code=0,
                stop_reason="completed",
                final_text="done",
                studio_session_id=None,
                event_log_path=Path(tmp) / "events.jsonl",
                runtime_log_path=Path(tmp) / "runtime.log",
                changed_paths=[],
                selection=selection,
                health={"process_identity": {}},
                task_findings="findings",
                requested_model="script-v1",
                effective_model="script-v1",
                effective_provider="amof_native_script",
                transport="scripted",
            )
        self.assertEqual(payload["result_kind"], "agent_run_result")
        self.assertEqual(payload["contract_version"], "agent-run-v1")
        self.assertEqual(payload["backend"], "amof_native")
        self.assertEqual(payload["requested_provider"], "amof_native_script")
        self.assertEqual(payload["transport"], "scripted")
        self.assertEqual(
            payload["evidence_refs"]["backend_contract_version"],
            "amof-native-agent-runtime-v1",
        )

    def test_blocked_without_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            selection = amof_native.build_selection(
                runner_id="amof-native-ticket-write",
                requested_capabilities=["read"],
                approve_writable_roots=[],
                timeout_seconds=30,
                readable_root=str(workspace),
            )
            with patch.dict(os.environ, {}, clear=True):
                os.environ.pop("AMOF_NATIVE_SCRIPT", None)
                os.environ.pop("OPENAI_API_KEY", None)
                os.environ.pop("OPENROUTER_API_KEY", None)
                os.environ.pop("AMOF_REMOTE_IAL_API_KEY", None)
                with patch.object(amof_native, "_run_dir", return_value=Path(tmp) / "run"):
                    (Path(tmp) / "run").mkdir()
                    result = amof_native.run(
                        manifest={"repos": [{"path": str(workspace)}]},
                        goal="inspect",
                        request_id="req-blocked",
                        studio_session_id=None,
                        selection=selection,
                    )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stop_reason"], "inference_transport_unavailable")

    def test_model_metadata_scripted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.json"
            _write_script(script, [{"type": "final", "text": "ok"}])
            with patch.dict(os.environ, {"AMOF_NATIVE_SCRIPT": str(script)}):
                health = amof_native.runtime_health()
        self.assertTrue(health["dispatch_available"])
        self.assertEqual(health["requested_provider"], "amof_native_script")
        self.assertEqual(health["requested_model"], "script-v1")


class AmofNativeRunnerRegistryTests(unittest.TestCase):
    def test_template_kind_produces_valid_backend_record(self) -> None:
        payload = runner_cmd._template_payload("amof-native")
        self.assertEqual(payload["runner_id"], "amof-native-ticket-write")
        self.assertEqual(payload["backend"], amof_native.BACKEND_TYPE)
        runner_cmd._validate_backend_payload(
            payload,
            mutation_modes=list(payload["allowed_mutation_modes"]),
            capabilities=list(payload["capabilities"]),
        )

    def test_backend_is_supported(self) -> None:
        self.assertIn(amof_native.BACKEND_TYPE, runner_cmd.SUPPORTED_BACKENDS)
        self.assertIn("amof-native", runner_cmd.SUPPORTED_TEMPLATE_KINDS)

    def test_handoff_dispatch_map_includes_amof_native(self) -> None:
        from amof.commands import handoff as handoff_cmd

        source = Path(handoff_cmd.__file__).read_text(encoding="utf-8")
        self.assertIn("amof_native.BACKEND_TYPE: amof_native", source)


if __name__ == "__main__":
    unittest.main()
