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


def _proposal_output(
    allowed_roots: list[str],
    *,
    reason: str = "bounded write proof artifact",
) -> str:
    proposal = {
        "target_id": "github_app:marekhotshot/simple-ai-shop:67f8526b254d8839c025423b6bfda36895881160",
        "base_sha": "67f8526b254d8839c025423b6bfda36895881160",
        "allowed_roots": allowed_roots,
        "denied_roots": [],
        "reason": reason,
        "expected_checks": ["git diff --check"],
        "docs_only": True,
        "source_mutation": False,
    }
    return (
        f"{hermes_opensandbox.WRITE_SCOPE_PROPOSAL_START}\n"
        f"{json.dumps(proposal)}\n"
        f"{hermes_opensandbox.WRITE_SCOPE_PROPOSAL_END}\n\n"
        "# Bounded write proof\n"
    )


class HermesOpenSandboxRemoteIALTests(unittest.TestCase):
    def test_result_payload_reports_observed_remote_ial_usage_and_cost(self) -> None:
        result = hermes_opensandbox._result_payload(
            run_id="hermes-usage",
            status="completed",
            exit_code=0,
            stop_reason="completed",
            final_text="done",
            studio_session_id=None,
            event_log_path=Path("/tmp/events.jsonl"),
            runtime_log_path=Path("/tmp/runtime.log"),
            changed_paths=[],
            selection=_selection(),
            health=_health(),
            dispatch_probe={},
            usage={
                "prompt_tokens": 120,
                "completion_tokens": 45,
                "estimated_cost_usd": 0.0123,
                "chat_calls": 3,
            },
        )

        usage = result["evidence_refs"]["remote_ial_usage"]
        self.assertEqual(usage["prompt_tokens"], 120)
        self.assertEqual(usage["completion_tokens"], 45)
        self.assertEqual(usage["estimated_cost_usd"], 0.0123)
        self.assertEqual(usage["chat_calls"], 3)
        self.assertEqual(usage["cost_status"], "observed")
        self.assertEqual(result["budget_summary"]["spent"], 0.0123)
        self.assertEqual(result["budget_summary"]["cost_status"], "observed")
        self.assertEqual(result["num_turns"], 3)

    def test_result_payload_marks_token_only_usage_unpriced(self) -> None:
        result = hermes_opensandbox._result_payload(
            run_id="hermes-token-only",
            status="completed",
            exit_code=0,
            stop_reason="completed",
            final_text="done",
            studio_session_id=None,
            event_log_path=Path("/tmp/events.jsonl"),
            runtime_log_path=Path("/tmp/runtime.log"),
            changed_paths=[],
            selection=_selection(),
            health=_health(),
            dispatch_probe={},
            usage={
                "prompt_tokens": 120,
                "completion_tokens": 45,
                "estimated_cost_usd": None,
                "chat_calls": 1,
            },
        )

        self.assertIsNone(result["budget_summary"]["spent"])
        self.assertEqual(result["budget_summary"]["cost_status"], "tokens_only")

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
                        "write_scope_proposal for exactly "
                        "docs/launch-readiness/simple-ai-shop-launch-readiness.md."
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
        self.assertNotIn("approved_write_scope", result)

    def test_exact_required_proposal_path_is_preserved(self) -> None:
        expected = "docs/amof-bounded-write-proof.md"
        proposal, summary = hermes_opensandbox._extract_write_scope_proposal_output(
            _proposal_output([expected]),
            expected_allowed_roots=[expected],
        )

        self.assertIsNotNone(proposal)
        self.assertEqual(proposal["allowed_roots"], [expected])
        self.assertEqual(proposal["reason"], "bounded write proof artifact")
        self.assertEqual(summary, "# Bounded write proof")
        self.assertNotIn("approved_write_scope", proposal)

    def test_partial_empty_and_wildcard_proposals_are_rejected(self) -> None:
        expected = "docs/amof-bounded-write-proof.md"
        partial, _ = hermes_opensandbox._extract_write_scope_proposal_output(
            _proposal_output([]),
            expected_allowed_roots=[expected],
        )
        wildcard, _ = hermes_opensandbox._extract_write_scope_proposal_output(
            _proposal_output(["docs/*"]),
            expected_allowed_roots=[expected],
        )

        self.assertIsNone(partial)
        self.assertIsNone(wildcard)

    def test_bracketed_dynamic_route_paths_are_accepted(self) -> None:
        # Next.js dynamic-route directories are literal path segments; a real
        # repository proposal like commerce/app/[locale]/about/page.tsx must
        # not be rejected as glob syntax.
        roots = [
            "commerce/app/[locale]/about/page.tsx",
            "commerce/app/[...slug]/page.tsx",
            "commerce/components/layout/footer.tsx",
        ]
        proposal, _ = hermes_opensandbox._extract_write_scope_proposal_output(
            _proposal_output(roots),
        )

        self.assertIsNotNone(proposal)
        self.assertEqual(proposal["allowed_roots"], roots)

    def test_wildcards_and_traversal_remain_rejected(self) -> None:
        for bad in ["commerce/app/*", "commerce/app/?", "commerce/{a,b}", "../etc"]:
            proposal, _ = hermes_opensandbox._extract_write_scope_proposal_output(
                _proposal_output([bad]),
            )
            self.assertIsNone(proposal, bad)

    def test_required_proposal_with_extra_path_is_rejected(self) -> None:
        expected = "docs/amof-bounded-write-proof.md"
        proposal, _ = hermes_opensandbox._extract_write_scope_proposal_output(
            _proposal_output([expected, "docs/unrequested.md"]),
            expected_allowed_roots=[expected],
        )

        self.assertIsNone(proposal)

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
                # One corrective proposal replan is allowed before blocking,
                # so the loop inspects changed paths for two iterations.
                patch.object(
                    hermes_opensandbox,
                    "_changed_paths",
                    side_effect=[[], [], []],
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
                self.assertEqual(
                    run_process.call_count,
                    2,
                    "prose-only output must trigger exactly one corrective proposal replan",
                )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            result["stop_reason"],
            hermes_opensandbox.WRITE_SCOPE_PROPOSAL_REQUIRED,
        )
        self.assertEqual(result["exit_code"], 1)
        self.assertNotIn("write_scope_proposal", result)
        self.assertNotIn("approved_write_scope", result)
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

    def test_changed_paths_expands_untracked_directories_to_exact_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-hermes-exact-untracked-") as td:
            workspace = Path(td)
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=workspace,
                check=True,
            )
            proof = workspace / "docs" / "amof-bounded-write-proof.md"
            proof.parent.mkdir()
            proof.write_text("# proof\n", encoding="utf-8")

            self.assertEqual(
                hermes_opensandbox._changed_paths(workspace),
                ["docs/amof-bounded-write-proof.md"],
            )

    def test_selection_keeps_nonexistent_exact_file_scope_inside_readable_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-hermes-file-scope-") as td:
            workspace = Path(td).resolve()
            exact_path = workspace / "docs" / "amof-bounded-write-proof.md"
            selection = hermes_opensandbox.build_selection(
                runner_id="hermes-local-ticket-write",
                requested_capabilities=["bounded_write"],
                approve_writable_roots=[str(exact_path)],
                timeout_seconds=30,
                readable_root=str(workspace),
            )

            self.assertEqual(selection.writable_roots, [str(exact_path)])
            self.assertEqual(
                hermes_opensandbox._workspace_for(selection, {}),
                workspace,
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
        self.assertNotIn(hermes_opensandbox.WRITE_SCOPE_PROPOSAL_START, prompt)

    def test_required_proposal_prompt_forbids_prose_only_and_extra_paths(self) -> None:
        expected = "docs/amof-bounded-write-proof.md"
        prompt = hermes_opensandbox._build_prompt(
            f"Return structured_write_scope_proposal for exactly {expected}.",
            _selection(),
            Path("/tmp/amof-hermes-required-proposal"),
        )

        self.assertIn("A prose-only answer is a contract failure", prompt)
        self.assertIn(json.dumps([expected]), prompt)
        self.assertIn('"reason":"bounded write proof artifact"', prompt)
        self.assertIn('"expected_checks":["git diff --check"]', prompt)
        self.assertIn('"docs_only":true', prompt)
        self.assertIn("Wildcard roots and additional unrequested roots are forbidden", prompt)
        self.assertIn("do not include approved_write_scope", prompt)
        self.assertGreater(
            prompt.rfind("CURRENT PHASE OVERRIDE"),
            prompt.rfind("Mission:"),
        )
        self.assertIn("MUST NOT be executed in this run", prompt)

    def test_approved_bounded_write_prompt_executes_without_reproposing(self) -> None:
        expected = "docs/amof-bounded-write-proof.md"
        selection = hermes_opensandbox.HermesBackendSelection(
            runner_id="hermes-local-ticket-write",
            capabilities=["read", "write"],
            writable_roots=[expected],
            timeout_seconds=30,
            readable_root=None,
        )
        prompt = hermes_opensandbox._build_prompt(
            (
                f"Earlier discovery returned structured_write_scope_proposal for {expected}. "
                f"Execution phase: approved_write_scope is exactly {expected}."
            ),
            selection,
            Path("/tmp/amof-hermes-approved-write"),
        )

        self.assertNotIn(hermes_opensandbox.WRITE_SCOPE_PROPOSAL_START, prompt)
        self.assertIn("CURRENT PHASE OVERRIDE — APPROVED BOUNDED WRITE", prompt)
        self.assertIn("already validated explicit operator approval", prompt)
        self.assertIn("Create missing parent directories", prompt)
        self.assertIn("Do not ask for another confirmation", prompt)

    def test_required_proposal_is_evaluated_after_read_only_mutation_replan(
        self,
    ) -> None:
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
                    side_effect=[
                        subprocess.CompletedProcess(
                            args=["hermes"],
                            returncode=0,
                            stdout="prose only after an attempted mutation\n",
                            stderr="",
                        ),
                        subprocess.CompletedProcess(
                            args=["hermes"],
                            returncode=0,
                            stdout=_proposal_output(
                                ["docs/amof-bounded-write-proof.md"]
                            ),
                            stderr="",
                        ),
                    ],
                ) as run_process,
            ):
                result = hermes_opensandbox.run(
                    manifest={"repos": [{"path": td}]},
                    goal=(
                        "Return structured_write_scope_proposal for exactly "
                        "docs/amof-bounded-write-proof.md."
                    ),
                    request_id="readonly-replan",
                    studio_session_id=None,
                    selection=_selection(),
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stop_reason"], "completed")
        self.assertEqual(
            result["write_scope_proposal"]["allowed_roots"],
            ["docs/amof-bounded-write-proof.md"],
        )
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
                    side_effect=[
                        ["first-change.txt"],
                        ["second-change.txt"],
                    ],
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
        self.assertEqual(result["changed_paths"], [])
        self.assertEqual(run_process.call_count, 2)
        self.assertEqual(
            restore_paths.call_args_list,
            [
                unittest.mock.call(Path(td), ["first-change.txt"]),
                unittest.mock.call(Path(td), ["second-change.txt"]),
            ],
        )

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
