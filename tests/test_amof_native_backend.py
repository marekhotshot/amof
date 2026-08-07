from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

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


class AmofNativeRemoteIalTransportTests(unittest.TestCase):
    def test_remote_ial_uses_ial_chat_endpoint(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "AMOF_REMOTE_IAL_BASE_URL": "http://ial.example:8787",
                    "AMOF_REMOTE_IAL_API_KEY": "test-key",
                    "AMOF_REMOTE_IAL_MODEL": "openai/gpt-4o-mini",
                    "AMOF_NATIVE_SCRIPT": "",
                },
                clear=False,
            ),
            patch.object(amof_native, "_script_path", return_value=None),
        ):
            url, headers, transport = amof_native._chat_endpoint_and_headers()
        self.assertEqual(transport, amof_native.TRANSPORT_REMOTE_IAL)
        self.assertTrue(url.endswith("/v1/ial/chat"))
        self.assertNotIn("/v1/chat/completions", url)
        self.assertIn("Authorization", headers)


class AmofNativeProposalContractTests(unittest.TestCase):
    def test_extracts_structured_write_scope_proposal(self) -> None:
        """Scripted discovery findings with marker blocks become write_scope_proposals[]."""
        from amof.execution_backends.hermes_opensandbox import (
            WRITE_SCOPE_PROPOSAL_END,
            WRITE_SCOPE_PROPOSAL_START,
        )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "note.md").write_text("x\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "docs/note.md"], cwd=workspace, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "add note"], cwd=workspace, check=True, capture_output=True
            )
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
            ).strip()
            target_id = f"github_app:example/repo:{sha}"
            proposal = {
                "target_id": target_id,
                "base_sha": sha,
                "allowed_roots": ["docs/note.md"],
                "denied_roots": [],
                "reason": "bounded follow-up justified by inspected evidence",
                "expected_checks": ["git diff --check"],
                "docs_only": True,
                "source_mutation": False,
            }
            findings = (
                f"{WRITE_SCOPE_PROPOSAL_START}\n"
                + json.dumps(proposal)
                + f"\n{WRITE_SCOPE_PROPOSAL_END}\n"
                + "Summary: truncation disclosure needed.\n"
            )
            script = Path(tmp) / "script.json"
            _write_script(script, [{"type": "final", "text": findings}])
            selection = amof_native.build_selection(
                runner_id="amof-native-ticket-write",
                requested_capabilities=["read"],
                approve_writable_roots=[],
                timeout_seconds=30,
                readable_root=str(workspace),
                accepted_base_sha=sha,
                target_id=target_id,
            )
            manifest = {
                "repos": [
                    {
                        "path": str(workspace),
                        "target_id": target_id,
                        "sha": sha,
                        "name": "example/repo",
                        "url": "https://github.com/example/repo.git",
                    }
                ]
            }
            with patch.dict(os.environ, {"AMOF_NATIVE_SCRIPT": str(script)}, clear=False):
                with patch.object(amof_native, "_run_dir", return_value=Path(tmp) / "run2"):
                    (Path(tmp) / "run2").mkdir(exist_ok=True)
                    result = amof_native.run(
                        manifest=manifest,
                        goal=(
                            "Propose a write scope for truncated status/context preview "
                            "disclosure. Emit a structured write_scope_proposal."
                        ),
                        request_id="proposal-contract-test",
                        studio_session_id=None,
                        selection=selection,
                    )
            self.assertEqual(result.get("status"), "completed", result.get("stop_reason"))
            props = result.get("write_scope_proposals") or []
            self.assertEqual(len(props), 1)
            self.assertEqual(props[0].get("target_id"), target_id)
            self.assertIn("docs/note.md", props[0].get("allowed_roots") or [])


class AmofNativeExecutionBudgetTests(unittest.TestCase):
    def test_default_budgets(self) -> None:
        with patch.dict(
            os.environ,
            {"AMOF_NATIVE_IAL_TIMEOUT_SECONDS": "", "AMOF_NATIVE_IAL_MAX_TOKENS": ""},
            clear=False,
        ):
            os.environ.pop("AMOF_NATIVE_IAL_TIMEOUT_SECONDS", None)
            os.environ.pop("AMOF_NATIVE_IAL_MAX_TOKENS", None)
            self.assertEqual(amof_native.native_ial_timeout_seconds(), 180.0)
            self.assertEqual(amof_native.native_ial_max_tokens(), 4096)

    def test_fast_call_and_token_cap(self) -> None:
        remote_body = {
            "request_id": "req-fast",
            "text": "IAL_OK",
            "stop_reason": "stop",
            "tool_calls": [],
            "tokens": {"input": 10, "output": 2},
            "model": "openai/gpt-4o-mini",
        }
        captured: dict[str, object] = {}

        class _Resp:
            def read(self) -> bytes:
                return json.dumps(remote_body).encode("utf-8")

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        def fake_urlopen(request: object, timeout: float = 0) -> _Resp:
            captured["timeout"] = timeout
            payload = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            captured["payload"] = payload
            return _Resp()

        with (
            patch.dict(
                os.environ,
                {
                    "AMOF_REMOTE_IAL_BASE_URL": "http://ial.example:8787",
                    "AMOF_REMOTE_IAL_API_KEY": "test-key",
                    "AMOF_REMOTE_IAL_MODEL": "openai/gpt-4o-mini",
                    "AMOF_NATIVE_IAL_TIMEOUT_SECONDS": "180",
                    "AMOF_NATIVE_IAL_MAX_TOKENS": "4096",
                    "AMOF_NATIVE_SCRIPT": "",
                },
                clear=False,
            ),
            patch.object(amof_native, "urlopen", side_effect=fake_urlopen),
        ):
            out = amof_native._chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                model="openai/gpt-4o-mini",
                tools=None,
            )
        self.assertEqual(captured["timeout"], 180.0)
        self.assertEqual((captured["payload"] or {}).get("max_tokens"), 4096)  # type: ignore[union-attr]
        self.assertEqual(out["choices"][0]["message"]["content"], "IAL_OK")

    def test_long_valid_call_uses_raised_budget(self) -> None:
        """Simulated call longer than old 90s budget still accepted under 180s config."""
        with patch.dict(os.environ, {"AMOF_NATIVE_IAL_TIMEOUT_SECONDS": "180"}, clear=False):
            self.assertGreater(amof_native.native_ial_timeout_seconds(), 90.0)
            self.assertEqual(amof_native.native_ial_timeout_seconds(), 180.0)

    def test_beyond_budget_returns_canonical_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            (workspace / "docs").mkdir()
            selection = amof_native.build_selection(
                runner_id="amof-native-ticket-write",
                requested_capabilities=["read", "bounded_write"],
                approve_writable_roots=["docs/"],
                timeout_seconds=900,
                readable_root=str(workspace),
            )

            def boom(*_a: object, **_k: object) -> dict:
                raise amof_native.AmofNativeTimeoutError(
                    "model transport timed out after 180s",
                    timeout_kind="REMOTE_IAL_TOTAL_TIMEOUT",
                    timeout_seconds=180.0,
                    model_turn_id="t1",
                    attempt_id="t1:attempt:1",
                )

            with (
                patch.dict(
                    os.environ,
                    {
                        "AMOF_REMOTE_IAL_BASE_URL": "http://ial.example:8787",
                        "AMOF_REMOTE_IAL_API_KEY": "k",
                        "AMOF_REMOTE_IAL_MODEL": "openai/gpt-4o-mini",
                        "AMOF_NATIVE_SCRIPT": "",
                    },
                    clear=False,
                ),
                patch.object(amof_native, "_script_path", return_value=None),
                patch.object(amof_native, "_chat_completion", side_effect=boom),
                patch.object(amof_native, "_run_dir", return_value=Path(tmp) / "run"),
            ):
                (Path(tmp) / "run").mkdir()
                result = amof_native.run(
                    manifest={"repos": [{"path": str(workspace)}]},
                    goal="do work",
                    request_id="timeout-budget-test",
                    studio_session_id=None,
                    selection=selection,
                )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(
                result["stop_reason"], amof_native.STOP_REASON_REMOTE_IAL_TOTAL_TIMEOUT
            )
            self.assertNotEqual(result["stop_reason"], "grant_enforcement_failed")

    def test_late_response_for_abandoned_attempt_discarded(self) -> None:
        abandoned = {"run:turn:1:attempt:1"}
        remote_body = {
            "request_id": "late",
            "text": "should-not-win",
            "stop_reason": "stop",
            "tool_calls": [],
            "tokens": {"input": 1, "output": 1},
        }

        class _Resp:
            def read(self) -> bytes:
                return json.dumps(remote_body).encode("utf-8")

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        with (
            patch.dict(
                os.environ,
                {
                    "AMOF_REMOTE_IAL_BASE_URL": "http://ial.example:8787",
                    "AMOF_REMOTE_IAL_API_KEY": "k",
                    "AMOF_REMOTE_IAL_MODEL": "openai/gpt-4o-mini",
                },
                clear=False,
            ),
            patch.object(amof_native, "urlopen", return_value=_Resp()),
        ):
            with self.assertRaises(amof_native.AmofNativeTimeoutError) as ctx:
                amof_native._chat_completion(
                    messages=[{"role": "user", "content": "hi"}],
                    model="openai/gpt-4o-mini",
                    tools=None,
                    model_turn_id="run:turn:1",
                    attempt_id="run:turn:1:attempt:1",
                    abandoned_attempts=abandoned,
                )
        self.assertIn("abandoned attempt", str(ctx.exception))

    def test_retry_identity_late_attempt1_cannot_satisfy_attempt2(self) -> None:
        abandoned = {"run:turn:1:attempt:1"}
        # Attempt 2 is a different identity and may succeed.
        remote_body = {
            "request_id": "ok2",
            "text": "attempt2-ok",
            "stop_reason": "stop",
            "tool_calls": [],
            "tokens": {"input": 1, "output": 1},
        }

        class _Resp:
            def read(self) -> bytes:
                return json.dumps(remote_body).encode("utf-8")

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        with (
            patch.dict(
                os.environ,
                {
                    "AMOF_REMOTE_IAL_BASE_URL": "http://ial.example:8787",
                    "AMOF_REMOTE_IAL_API_KEY": "k",
                    "AMOF_REMOTE_IAL_MODEL": "openai/gpt-4o-mini",
                },
                clear=False,
            ),
            patch.object(amof_native, "urlopen", return_value=_Resp()),
        ):
            out = amof_native._chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                model="openai/gpt-4o-mini",
                tools=None,
                model_turn_id="run:turn:1",
                attempt_id="run:turn:1:attempt:2",
                abandoned_attempts=abandoned,
            )
        self.assertEqual(out["choices"][0]["message"]["content"], "attempt2-ok")

    def test_tool_loop_timeout_does_not_duplicate_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            _init_git_repo(workspace)
            (workspace / "docs").mkdir()
            target = workspace / "docs" / "out.md"
            selection = amof_native.build_selection(
                runner_id="amof-native-ticket-write",
                requested_capabilities=["read", "bounded_write"],
                approve_writable_roots=["docs/"],
                timeout_seconds=900,
                readable_root=str(workspace),
            )
            writes = {"count": 0}

            def chat_side_effect(**kwargs: object) -> dict:
                # First turn: request a write; second turn: timeout (no auto-retry).
                if writes["count"] == 0:
                    return {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "write_file",
                                                "arguments": json.dumps(
                                                    {
                                                        "path": "docs/out.md",
                                                        "content": "once\n",
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                raise amof_native.AmofNativeTimeoutError(
                    "follow-up timed out",
                    timeout_seconds=180.0,
                    model_turn_id="t2",
                    attempt_id="t2:attempt:1",
                )

            real_write = amof_native.NativeAgentTools.write_file

            def counting_write(self: object, path: str, content: str) -> str:
                writes["count"] += 1
                return real_write(self, path, content)  # type: ignore[arg-type]

            with (
                patch.dict(
                    os.environ,
                    {
                        "AMOF_REMOTE_IAL_BASE_URL": "http://ial.example:8787",
                        "AMOF_REMOTE_IAL_API_KEY": "k",
                        "AMOF_REMOTE_IAL_MODEL": "openai/gpt-4o-mini",
                        "AMOF_NATIVE_SCRIPT": "",
                    },
                    clear=False,
                ),
                patch.object(amof_native, "_script_path", return_value=None),
                patch.object(amof_native, "_chat_completion", side_effect=chat_side_effect),
                patch.object(amof_native.NativeAgentTools, "write_file", counting_write),
                patch.object(amof_native, "_run_dir", return_value=Path(tmp) / "run"),
            ):
                (Path(tmp) / "run").mkdir()
                result = amof_native.run(
                    manifest={"repos": [{"path": str(workspace)}]},
                    goal="write once then infer",
                    request_id="tool-idempotency-test",
                    studio_session_id=None,
                    selection=selection,
                )
            self.assertEqual(writes["count"], 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "once\n")
            self.assertEqual(
                result["stop_reason"], amof_native.STOP_REASON_REMOTE_IAL_TOTAL_TIMEOUT
            )

    def test_urlopen_timeout_maps_to_timeout_error(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "AMOF_REMOTE_IAL_BASE_URL": "http://ial.example:8787",
                    "AMOF_REMOTE_IAL_API_KEY": "k",
                    "AMOF_REMOTE_IAL_MODEL": "openai/gpt-4o-mini",
                    "AMOF_NATIVE_IAL_TIMEOUT_SECONDS": "12",
                },
                clear=False,
            ),
            patch.object(
                amof_native,
                "urlopen",
                side_effect=URLError("timed out"),
            ),
        ):
            abandoned: set[str] = set()
            with self.assertRaises(amof_native.AmofNativeTimeoutError) as ctx:
                amof_native._chat_completion(
                    messages=[{"role": "user", "content": "hi"}],
                    model="openai/gpt-4o-mini",
                    tools=None,
                    model_turn_id="m1",
                    attempt_id="m1:attempt:1",
                    abandoned_attempts=abandoned,
                )
            self.assertIn("m1:attempt:1", abandoned)
            self.assertEqual(ctx.exception.timeout_seconds, 12.0)
