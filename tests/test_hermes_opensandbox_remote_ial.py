from __future__ import annotations

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

from amof.execution_backends import hermes_opensandbox


def _selection() -> hermes_opensandbox.HermesBackendSelection:
    return hermes_opensandbox.HermesBackendSelection(
        runner_id="hermes-local-ticket-write",
        capabilities=["read"],
        writable_roots=[],
        timeout_seconds=30,
        readable_root=None,
    )


def _health() -> dict[str, object]:
    return {
        "backend_contract_version": "hermes-cli-remote-ial-v1",
        "runtime_contract": "Hermes CLI + Remote IAL",
        "isolation_model": "runtime_owner_workspace",
        "dispatch_available": True,
        "runtime_health": "ready",
        "hermes_runtime": "ready",
        "inference_transport": "remote_ial",
        "inference_health": "ready",
        "requested_provider": "remote-ial",
        "effective_provider": "remote-ial",
        "requested_model": "remote-ial/test-worker",
        "effective_model": "remote-ial/test-worker",
        "direct_provider_fallback": "disabled",
        "execution_endpoint": "/tmp/hermes",
        "process_identity": {
            "hermes_executable": "/tmp/hermes",
            "dispatch_probe": {
                "status": "ready",
                "exit_code": 0,
                "probe_command": ["/tmp/hermes", "chat", "--help"],
                "dispatch_command_preview": [
                    "/tmp/hermes",
                    "chat",
                    "--cli",
                    "--quiet",
                    "--model",
                    "remote-ial/test-worker",
                    "--query",
                    "<amof-contract-probe>",
                ],
            },
        },
        "supported_capabilities": ["read"],
        "writable_root_required": True,
        "cancellation_support": "timeout_process_termination",
        "log_event_support": "stdout_stderr_event_jsonl",
    }


class HermesOpenSandboxRemoteIALTests(unittest.TestCase):
    def test_structured_write_scope_proposal_is_parsed_from_runner_output(self) -> None:
        config = hermes_opensandbox.RemoteIALConfig(
            base_url="https://ial.example.test",
            api_key="unit-test-token",
            model="remote-ial/test-worker",
            timeout_seconds=30,
        )
        stdout = (
            f"{hermes_opensandbox.WRITE_SCOPE_PROPOSAL_START}\n"
            '{"target_id":"github_app:marekhotshot/simple-ai-shop:67f8526b254d8839c025423b6bfda36895881160",'
            '"base_sha":"67f8526b254d8839c025423b6bfda36895881160",'
            '"allowed_roots":["docs/launch-readiness/simple-ai-shop-launch-readiness.md"],'
            '"denied_roots":[],'
            '"reason":"A focused documentation follow-up is justified by the inspected launch-readiness evidence.",'
            '"expected_checks":["git diff --check"],'
            '"docs_only":true,'
            '"source_mutation":false}\n'
            f"{hermes_opensandbox.WRITE_SCOPE_PROPOSAL_END}\n\n"
            "# Launch readiness summary\n\n- Deployment docs are stale.\n"
        )
        with tempfile.TemporaryDirectory(prefix="amof-hermes-write-scope-") as td:
            with (
                patch.dict(os.environ, {"AMOF_HOME": td}, clear=False),
                patch.object(hermes_opensandbox, "runtime_health", return_value=_health()),
                patch.object(hermes_opensandbox, "_remote_ial_config", return_value=config),
                patch.object(
                    hermes_opensandbox,
                    "_remote_ial_health",
                    return_value={"inference_health": "ready"},
                ),
                patch.object(
                    hermes_opensandbox,
                    "_changed_paths",
                    side_effect=[[], []],
                ),
                patch("subprocess.run") as run_process,
            ):
                run_process.return_value = type(
                    "Completed",
                    (),
                    {"stdout": stdout, "stderr": "", "returncode": 0},
                )()
                result = hermes_opensandbox.run(
                    manifest={
                        "repos": [
                            {
                                "path": td,
                                "url": "https://github.com/marekhotshot/simple-ai-shop.git",
                                "target_id": "github_app:marekhotshot/simple-ai-shop:67f8526b254d8839c025423b6bfda36895881160",
                                "sha": "67f8526b254d8839c025423b6bfda36895881160",
                                "branch": "67f8526b254d8839c025423b6bfda36895881160",
                            }
                        ]
                    },
                    goal=(
                        "Inspect launch readiness and return a structured "
                        "write_scope_proposal for docs/launch-readiness/simple-ai-shop-launch-readiness.md."
                    ),
                    request_id="write-scope",
                    studio_session_id=None,
                    selection=_selection(),
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["write_scope_proposal"],
            {
                "target_id": "github_app:marekhotshot/simple-ai-shop:67f8526b254d8839c025423b6bfda36895881160",
                "base_sha": "67f8526b254d8839c025423b6bfda36895881160",
                "allowed_roots": [
                    "docs/launch-readiness/simple-ai-shop-launch-readiness.md"
                ],
                "denied_roots": [],
                "reason": "A focused documentation follow-up is justified by the inspected launch-readiness evidence.",
                "expected_checks": ["git diff --check"],
                "docs_only": True,
                "source_mutation": False,
            },
        )
        self.assertEqual(
            result["task_findings"],
            "# Launch readiness summary\n\n- Deployment docs are stale.",
        )

    def test_prose_only_write_scope_text_does_not_become_structured_proposal(self) -> None:
        config = hermes_opensandbox.RemoteIALConfig(
            base_url="https://ial.example.test",
            api_key="unit-test-token",
            model="remote-ial/test-worker",
            timeout_seconds=30,
        )
        stdout = (
            "Consider docs/launch-readiness/simple-ai-shop-launch-readiness.md for a later bounded write, "
            "but no structured proposal is attached here."
        )
        with tempfile.TemporaryDirectory(prefix="amof-hermes-write-scope-prose-") as td:
            with (
                patch.dict(os.environ, {"AMOF_HOME": td}, clear=False),
                patch.object(hermes_opensandbox, "runtime_health", return_value=_health()),
                patch.object(hermes_opensandbox, "_remote_ial_config", return_value=config),
                patch.object(
                    hermes_opensandbox,
                    "_remote_ial_health",
                    return_value={"inference_health": "ready"},
                ),
                patch.object(
                    hermes_opensandbox,
                    "_changed_paths",
                    side_effect=[[], []],
                ),
                patch("subprocess.run") as run_process,
            ):
                run_process.return_value = type(
                    "Completed",
                    (),
                    {"stdout": stdout, "stderr": "", "returncode": 0},
                )()
                result = hermes_opensandbox.run(
                    manifest={"repos": [{"path": td}]},
                    goal="Inspect launch readiness and return a structured write scope proposal if warranted.",
                    request_id="write-scope-prose-only",
                    studio_session_id=None,
                    selection=_selection(),
                )

        self.assertEqual(result["status"], "completed")
        self.assertNotIn("write_scope_proposal", result)
        self.assertIn("no structured proposal is attached", result["task_findings"])
        self.assertEqual(
            result["proposal_missing_reason"],
            "Consider docs/launch-readiness/simple-ai-shop-launch-readiness.md for a later bounded write, but no structured proposal is attached here.",
        )
        preview_paths = {item["path"] for item in result["evidence_previews"]}
        self.assertIn(str(Path(td) / "share" / "runs" / "hermes-opensandbox" / result["session_id"] / "result.json"), preview_paths)
        self.assertIn(str(Path(td) / "share" / "runs" / "hermes-opensandbox" / result["session_id"] / "events.jsonl"), preview_paths)
        self.assertIn(str(Path(td) / "share" / "runs" / "hermes-opensandbox" / result["session_id"] / "runtime.log"), preview_paths)

    def test_changed_paths_delta_ignores_preexisting_dirtiness(self) -> None:
        before = ["src/components/CookieConsent.tsx", "src/components/PodcastPage.tsx"]
        after = [
            "src/components/CookieConsent.tsx",
            "src/components/PodcastPage.tsx",
            "src/contexts/PodcastPlayerContext.tsx",
        ]
        self.assertEqual(
            hermes_opensandbox._changed_paths_delta(before, after),
            ["src/contexts/PodcastPlayerContext.tsx"],
        )

    def test_read_only_prompt_enforces_workspace_boundary_and_mutation_forbiddance(
        self,
    ) -> None:
        workspace = Path("/tmp/amof-hermes-readonly-boundary")
        prompt = hermes_opensandbox._build_prompt(
            "inspect only",
            _selection(),
            workspace,
        )
        self.assertIn("already materialized", prompt)
        self.assertIn(f"Read-only workspace boundary (exact path): {workspace}", prompt)
        self.assertIn("Do not run git clone, git init, git worktree", prompt)
        self.assertIn("Do not create, modify, or delete files", prompt)

    def test_read_only_first_mutation_triggers_constrained_replan(self) -> None:
        config = hermes_opensandbox.RemoteIALConfig(
            base_url="https://ial.example.test",
            api_key="unit-test-token",
            model="remote-ial/test-worker",
            timeout_seconds=30,
        )

        class _Adapter:
            def __enter__(self) -> "_Adapter":
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

        with tempfile.TemporaryDirectory(prefix="amof-hermes-readonly-replan-") as td:
            with (
                patch.dict(os.environ, {"AMOF_HOME": td}, clear=False),
                patch.object(hermes_opensandbox, "runtime_health", return_value=_health()),
                patch.object(hermes_opensandbox, "_remote_ial_config", return_value=config),
                patch.object(hermes_opensandbox, "_remote_ial_health", return_value={"inference_health": "ready"}),
                patch.object(hermes_opensandbox, "_RemoteIALOpenAIAdapter", return_value=_Adapter()),
                patch.object(hermes_opensandbox, "_base_env", return_value={}),
                patch.object(
                    hermes_opensandbox,
                    "_changed_paths",
                    side_effect=[[], ["scratch.txt"], []],
                ),
                patch.object(
                    hermes_opensandbox,
                    "_restore_read_only_paths",
                    return_value=["scratch.txt"],
                ) as restore_paths,
                patch.object(
                    hermes_opensandbox,
                    "hermes_dispatch_command",
                    side_effect=lambda model, prompt: ["hermes", "chat", "--query", prompt],
                ) as dispatch_command,
                patch(
                    "subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        args=["hermes"],
                        returncode=0,
                        stdout="validation_ok\n",
                        stderr="",
                    ),
                ) as run_process,
            ):
                result = hermes_opensandbox.run(
                    manifest={"repos": [{"path": td}]},
                    goal="inspect only",
                    request_id="readonly-replan",
                    studio_session_id=None,
                    selection=_selection(),
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stop_reason"], "completed")
        self.assertEqual(run_process.call_count, 2)
        restore_paths.assert_called_once_with(Path(td), ["scratch.txt"])
        self.assertEqual(dispatch_command.call_count, 2)
        retry_prompt = dispatch_command.call_args_list[1].kwargs["prompt"]
        self.assertIn("constrained replan", retry_prompt.lower())

    def test_read_only_second_mutation_fails_closed(self) -> None:
        config = hermes_opensandbox.RemoteIALConfig(
            base_url="https://ial.example.test",
            api_key="unit-test-token",
            model="remote-ial/test-worker",
            timeout_seconds=30,
        )

        class _Adapter:
            def __enter__(self) -> "_Adapter":
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

        with tempfile.TemporaryDirectory(prefix="amof-hermes-readonly-replan-fail-") as td:
            with (
                patch.dict(os.environ, {"AMOF_HOME": td}, clear=False),
                patch.object(hermes_opensandbox, "runtime_health", return_value=_health()),
                patch.object(hermes_opensandbox, "_remote_ial_config", return_value=config),
                patch.object(hermes_opensandbox, "_remote_ial_health", return_value={"inference_health": "ready"}),
                patch.object(hermes_opensandbox, "_RemoteIALOpenAIAdapter", return_value=_Adapter()),
                patch.object(hermes_opensandbox, "_base_env", return_value={}),
                patch.object(
                    hermes_opensandbox,
                    "_changed_paths",
                    side_effect=[[], ["first-change.txt"], ["second-change.txt"]],
                ),
                patch.object(
                    hermes_opensandbox,
                    "_restore_read_only_paths",
                    return_value=["first-change.txt"],
                ) as restore_paths,
                patch.object(
                    hermes_opensandbox,
                    "hermes_dispatch_command",
                    side_effect=lambda model, prompt: ["hermes", "chat", "--query", prompt],
                ),
                patch(
                    "subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        args=["hermes"],
                        returncode=0,
                        stdout="validation_ok\n",
                        stderr="",
                    ),
                ) as run_process,
            ):
                result = hermes_opensandbox.run(
                    manifest={"repos": [{"path": td}]},
                    goal="inspect only",
                    request_id="readonly-replan-fail",
                    studio_session_id=None,
                    selection=_selection(),
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "read_only_mutation_detected")
        self.assertEqual(result["changed_paths"], ["second-change.txt"])
        self.assertEqual(run_process.call_count, 2)
        restore_paths.assert_called_once_with(Path(td), ["first-change.txt"])

    def test_read_only_dirty_workspace_blocks_before_subprocess(self) -> None:
        config = hermes_opensandbox.RemoteIALConfig(
            base_url="https://ial.example.test",
            api_key="unit-test-token",
            model="remote-ial/test-worker",
            timeout_seconds=30,
        )
        with tempfile.TemporaryDirectory(prefix="amof-hermes-readonly-dirty-") as td:
            with (
                patch.dict(os.environ, {"AMOF_HOME": td}, clear=False),
                patch.object(hermes_opensandbox, "runtime_health", return_value=_health()),
                patch.object(hermes_opensandbox, "_remote_ial_config", return_value=config),
                patch.object(hermes_opensandbox, "_remote_ial_health", return_value={"inference_health": "ready"}),
                patch.object(
                    hermes_opensandbox,
                    "_changed_paths",
                    side_effect=[["src/components/CookieConsent.tsx"]],
                ),
                patch("subprocess.run") as run_process,
            ):
                result = hermes_opensandbox.run(
                    manifest={"repos": [{"path": td}]},
                    goal="inspect only",
                    request_id="readonly-dirty",
                    studio_session_id=None,
                    selection=_selection(),
                )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stop_reason"], "read_only_workspace_not_clean")
        self.assertEqual(result["changed_paths"], [])
        self.assertEqual(
            (result.get("evidence_refs") or {}).get("preexisting_changed_paths"),
            ["src/components/CookieConsent.tsx"],
        )
        run_process.assert_not_called()

    def test_missing_remote_ial_config_blocks_before_hermes_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-hermes-missing-ial-") as td:
            with (
                patch.dict(
                    os.environ,
                    {
                        "AMOF_HOME": td,
                        "AMOF_REMOTE_IAL_BASE_URL": "",
                        "AMOF_REMOTE_IAL_API_KEY": "",
                        "AMOF_REMOTE_IAL_MODEL": "",
                    },
                    clear=False,
                ),
                patch.object(hermes_opensandbox, "runtime_health", return_value=_health()),
                patch("subprocess.run") as run_process,
            ):
                result = hermes_opensandbox.run(
                    manifest={"repos": [{"path": td}]},
                    goal="inspect only",
                    request_id="missing-ial",
                    studio_session_id=None,
                    selection=_selection(),
                )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stop_reason"], "inference_transport_unavailable")
        self.assertEqual(result["transport"], "remote_ial")
        self.assertFalse(result["fallback_used"])
        run_process.assert_not_called()

    def test_direct_provider_override_is_rejected_for_managed_runner(self) -> None:
        config = hermes_opensandbox.RemoteIALConfig(
            base_url="https://ial.example.test",
            api_key="unit-test-token",
            model="remote-ial/test-worker",
            timeout_seconds=30,
        )
        with tempfile.TemporaryDirectory(prefix="amof-hermes-direct-provider-") as td:
            with (
                patch.dict(os.environ, {"AMOF_HOME": td}, clear=False),
                patch.object(hermes_opensandbox, "runtime_health", return_value=_health()),
                patch.object(hermes_opensandbox, "_remote_ial_config", return_value=config),
                patch.object(hermes_opensandbox, "_remote_ial_health", return_value={"inference_health": "ready"}),
                patch("subprocess.run") as run_process,
            ):
                result = hermes_opensandbox.run(
                    manifest={"repos": [{"path": td}]},
                    goal="inspect only",
                    request_id="direct-provider",
                    studio_session_id=None,
                    selection=_selection(),
                    provider="openrouter",
                    model="remote-ial/test-worker",
                )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stop_reason"], "inference_transport_unavailable")
        self.assertIn("Direct provider override", result["final_text"])
        self.assertEqual(result["requested_provider"], "remote-ial")
        self.assertEqual(result["effective_provider"], "unverified")
        run_process.assert_not_called()

    def test_dispatch_unavailable_returns_typed_failure_truth(self) -> None:
        config = hermes_opensandbox.RemoteIALConfig(
            base_url="https://ial.example.test",
            api_key="unit-test-token",
            model="remote-ial/test-worker",
            timeout_seconds=30,
        )
        health = dict(_health())
        health["dispatch_available"] = False
        health["runtime_health"] = "unavailable"
        health["hermes_runtime"] = "unavailable"
        with tempfile.TemporaryDirectory(prefix="amof-hermes-dispatch-unavailable-") as td:
            with (
                patch.dict(os.environ, {"AMOF_HOME": td}, clear=False),
                patch.object(hermes_opensandbox, "runtime_health", return_value=health),
                patch.object(hermes_opensandbox, "_remote_ial_config", return_value=config),
                patch.object(hermes_opensandbox, "_remote_ial_health", return_value={"inference_health": "ready"}),
                patch("subprocess.run") as run_process,
            ):
                result = hermes_opensandbox.run(
                    manifest={"repos": [{"path": td}]},
                    goal="inspect only",
                    request_id="dispatch-unavailable",
                    studio_session_id=None,
                    selection=_selection(),
                )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stop_reason"], "hermes_dispatch_unavailable")
        self.assertFalse(result["fallback_used"])
        self.assertEqual(
            (result.get("evidence_refs") or {}).get("backend_contract_version"),
            "hermes-cli-remote-ial-v1",
        )
        self.assertEqual(
            ((result.get("evidence_refs") or {}).get("dispatch_probe") or {}).get("status"),
            "ready",
        )
        run_process.assert_not_called()

    def test_runtime_health_reports_missing_hermes_cli_truthfully(self) -> None:
        missing = Path("/tmp/amof-missing-hermes/bin/hermes")
        with patch.object(hermes_opensandbox, "hermes_executable", return_value=missing):
            health = hermes_opensandbox.runtime_health()

        self.assertFalse(health["dispatch_available"])
        self.assertEqual(health["runtime_health"], "unavailable")
        self.assertEqual(health["hermes_runtime"], "unavailable")
        self.assertEqual(health["backend_contract_version"], "hermes-cli-remote-ial-v1")
        self.assertEqual(health["runtime_contract"], "Hermes CLI + Remote IAL")
        self.assertEqual(health["isolation_model"], "runtime_owner_workspace")
        probe = (health["process_identity"] or {}).get("dispatch_probe") or {}
        self.assertEqual(probe.get("status"), "unavailable")
        self.assertEqual(probe.get("probe_command"), [str(missing), "chat", "--help"])

    def test_hermes_runtime_root_uses_amof_home_authority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-hermes-home-root-") as td:
            with patch.dict(os.environ, {"AMOF_HOME": td}, clear=False):
                root = hermes_opensandbox.hermes_runtime_root()

        self.assertEqual(
            root,
            Path(td).resolve(strict=False) / "share" / "runners" / "hermes-agent" / "v2026.6.5",
        )

    def test_probe_and_dispatch_use_same_hermes_command_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-hermes-probe-contract-") as td:
            hermes_bin = Path(td) / "hermes"
            hermes_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hermes_bin.chmod(0o755)
            with patch.object(hermes_opensandbox, "hermes_executable", return_value=hermes_bin):
                probe = hermes_opensandbox._probe_hermes_cli_contract("remote-ial/test-worker")
                dispatch = hermes_opensandbox.hermes_dispatch_command(
                    model="remote-ial/test-worker",
                    prompt="inspect only",
                )

        self.assertEqual(probe["dispatch_command_preview"][:-1], dispatch[:-1])
        self.assertEqual(probe["dispatch_command_preview"][0], dispatch[0])

    def test_base_env_strips_direct_provider_variables(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "unit-test",
                "OPENAI_API_KEY": "unit-test",
                "ANTHROPIC_API_KEY": "unit-test",
            },
            clear=False,
        ):
            env = hermes_opensandbox._base_env()

        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_run_scoped_hermes_config_uses_local_adapter_not_env_key(self) -> None:
        config = hermes_opensandbox.RemoteIALConfig(
            base_url="https://ial.example.test",
            api_key="unit-test-token",
            model="remote-ial/test-worker",
            timeout_seconds=30,
        )
        adapter = type("Adapter", (), {"base_url": "http://127.0.0.1:1/v1", "config": config})()
        with tempfile.TemporaryDirectory(prefix="amof-hermes-config-") as td:
            run_dir = Path(td)
            env = hermes_opensandbox._base_env(adapter, run_dir)
            hermes_home = Path(env["HERMES_HOME"])
            config_exists = (hermes_home / "config.yaml").is_file()
            env_exists = (hermes_home / ".env").is_file()

        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertTrue(config_exists)
        self.assertTrue(env_exists)


if __name__ == "__main__":
    unittest.main()
