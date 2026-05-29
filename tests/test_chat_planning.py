from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import chat


class _FakeHTTPResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, object]:
        return self._payload


def _write_remote_ial_profile(amof_home: Path, *, extra_agent_yaml: str | None = None) -> None:
    config_root = amof_home / "config"
    profiles_dir = config_root / "provider-profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (config_root / "config.yaml").write_text("current_context: local\n", encoding="utf-8")
    (config_root / "contexts.yaml").write_text(
        (
            "contexts:\n"
            "  local:\n"
            "    credentials:\n"
            "      provider_profile_refs:\n"
            "        - remote-ial-default\n"
        ),
        encoding="utf-8",
    )
    (profiles_dir / "remote-ial-default.yaml").write_text(
        (
            "name: remote-ial-default\n"
            "provider: remote-ial\n"
            "default_model: openai/gpt-4o-mini\n"
            "timeout_seconds: 30\n"
            "credential_refs:\n"
            "  api_key_env: AMOF_REMOTE_IAL_API_KEY\n"
            "  base_url_env: AMOF_REMOTE_IAL_BASE_URL\n"
        ),
        encoding="utf-8",
    )
    if extra_agent_yaml is not None:
        (config_root / "agent.yaml").write_text(extra_agent_yaml, encoding="utf-8")


def _remote_ial_success_payload(*, ticket_id: str = "AMOF-CHAT-001") -> dict[str, object]:
    return {
        "text": json.dumps(
            {
                "ticket_id": ticket_id,
                "proposed_ticket_id": None,
                "proposed_steps": [
                    "Read the bounded repo files already selected by the operator.",
                    "Draft a Director-ready plan proposal without executing anything.",
                ],
                "risks": [
                    "The bounded file set may omit runtime or integration context.",
                ],
                "validation_plan": [
                    "Review the PlanPacket fields and confirm scope before any execution.",
                    "Require explicit user approval before Director receives the prompt.",
                ],
                "execution_prompt_for_director": (
                    "Prepare a Director intake proposal from this PlanPacket only. "
                    "Await user approval before any execution flow."
                ),
                "execution_allowed": False,
            }
        ),
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "request_id": "req-chat-123",
        "policy_decision": {"decision": "allow"},
        "input_hash": "chat-input-hash",
        "output_hash": "chat-output-hash",
        "tokens": {"input": 120, "output": 55},
        "latency_ms": 41,
        "estimated_cost": 0.0123,
    }


def _remote_ial_unknown_cost_payload(*, ticket_id: str = "AMOF-CHAT-UNKNOWN") -> dict[str, object]:
    return {
        "text": json.dumps(
            {
                "ticket_id": ticket_id,
                "proposed_ticket_id": None,
                "proposed_steps": [
                    "Read only the supplied bounded files.",
                ],
                "risks": [
                    "Bounded context can miss cross-repo dependencies.",
                ],
                "validation_plan": [
                    "Confirm this stays proposal-only and requires explicit approval.",
                ],
                "execution_prompt_for_director": (
                    "Prepare a Director intake proposal from this PlanPacket only. "
                    "Await user approval before any execution flow."
                ),
                "execution_allowed": False,
            }
        ),
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "request_id": "req-chat-unknown",
        "tokens": {"input": 60, "output": 20},
        "latency_ms": 27,
    }


def _fake_planning_context(repo: Path) -> object:
    receipt_payload = {
        "receipt_kind": "planning_context_receipt",
        "planning_repo_path": str(repo),
        "merkle_root": "abc123def4567890",
        "freshness": "fresh",
        "files_to_inspect": ["README.md", "app.py"],
    }

    class _Receipt:
        def to_dict(self) -> dict[str, object]:
            return dict(receipt_payload)

    return SimpleNamespace(
        receipt=_Receipt(),
        context_prompt="# Codebase Index\n- `README.md`: project overview\n- `app.py`: app entrypoint\n",
    )


class ChatPlanningTests(unittest.TestCase):
    def test_plan_packet_preserves_remote_ial_attribution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-plan-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            _write_remote_ial_profile(amof_home)

            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(amof_home),
                    "AMOF_REMOTE_IAL_BASE_URL": "https://ial.example.test",
                    "AMOF_REMOTE_IAL_API_KEY": "unit-test-token",
                },
                clear=False,
            ):
                with patch.object(chat, "build_canonical_planning_context", return_value=_fake_planning_context(repo)):
                    with patch(
                        "amof.orchestrator.llm.remote_ial.requests.post",
                        return_value=_FakeHTTPResponse(200, _remote_ial_success_payload()),
                    ):
                        result = chat.plan_read_only_chat(
                            objective="Plan AMOF-CHAT-001 for this repo.",
                            repo=repo,
                            ticket_id="AMOF-CHAT-001",
                            files=["README.md", "app.py"],
                        )

            self.assertEqual(result.plan_packet.ticket_id, "AMOF-CHAT-001")
            self.assertTrue(result.plan_packet.requires_user_approval)
            self.assertFalse(result.plan_packet.execution_allowed)
            self.assertEqual(result.inference.transport_provider, "remote-ial")
            self.assertEqual(result.inference.upstream_provider, "openrouter")
            self.assertEqual(result.inference.upstream_model, "openai/gpt-4o-mini")
            self.assertEqual(result.inference.request_id, "req-chat-123")
            self.assertEqual(result.inference.input_hash, "chat-input-hash")
            self.assertEqual(result.inference.output_hash, "chat-output-hash")

            events_text = Path(result.evidence["events_path"]).read_text(encoding="utf-8")
            self.assertIn('"provider": "remote-ial"', events_text)
            self.assertIn('"upstream_provider": "openrouter"', events_text)
            self.assertIn('"request_id": "req-chat-123"', events_text)
            self.assertIn('"input_hash": "chat-input-hash"', events_text)
            self.assertIn('"output_hash": "chat-output-hash"', events_text)

            session_dir = Path(result.evidence["session_dir"])
            journal_path = Path(result.evidence["journal_path"])
            self.assertEqual(journal_path.parent, session_dir)

    def test_chat_plan_does_not_invoke_shell_or_mutate_repo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-plan-shell-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            target = repo / "README.md"
            target.write_text("# Initial\n", encoding="utf-8")
            before_paths = sorted(str(path.relative_to(repo)) for path in repo.rglob("*"))
            before_text = target.read_text(encoding="utf-8")
            amof_home = temp / "amof-home"
            _write_remote_ial_profile(amof_home)

            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(amof_home),
                    "AMOF_REMOTE_IAL_BASE_URL": "https://ial.example.test",
                    "AMOF_REMOTE_IAL_API_KEY": "unit-test-token",
                },
                clear=False,
            ):
                with patch.object(chat, "build_canonical_planning_context", return_value=_fake_planning_context(repo)):
                    with patch(
                        "subprocess.run",
                        side_effect=AssertionError("chat planning must not invoke subprocess.run"),
                    ):
                        with patch(
                            "amof.orchestrator.llm.remote_ial.requests.post",
                            return_value=_FakeHTTPResponse(200, _remote_ial_success_payload()),
                        ):
                            result = chat.plan_read_only_chat(
                                objective="Plan a read-only change review.",
                                repo=repo,
                                files=["README.md"],
                            )

            after_paths = sorted(str(path.relative_to(repo)) for path in repo.rglob("*"))
            self.assertEqual(before_paths, after_paths)
            self.assertEqual(before_text, target.read_text(encoding="utf-8"))
            self.assertFalse(Path(result.evidence["session_dir"]).is_relative_to(repo))
            self.assertFalse(Path(result.evidence["plan_result_path"]).is_relative_to(repo))
            if result.evidence["journal_path"] is not None:
                self.assertFalse(Path(result.evidence["journal_path"]).is_relative_to(repo))

    def test_hash_only_evidence_and_disabled_journal_keep_local_artifacts_redacted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-plan-evidence-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            _write_remote_ial_profile(
                amof_home,
                extra_agent_yaml=(
                    "evidence:\n"
                    "  messages: hash_only\n"
                    "  journal: disabled\n"
                ),
            )
            secret_objective = "Plan with Authorization: Bearer secret-token"

            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(amof_home),
                    "AMOF_REMOTE_IAL_BASE_URL": "https://ial.example.test",
                    "AMOF_REMOTE_IAL_API_KEY": "unit-test-token",
                },
                clear=False,
            ):
                with patch.object(chat, "build_canonical_planning_context", return_value=_fake_planning_context(repo)):
                    with patch(
                        "amof.orchestrator.llm.remote_ial.requests.post",
                        return_value=_FakeHTTPResponse(200, _remote_ial_success_payload(ticket_id="AMOF-CHAT-777")),
                    ):
                        result = chat.plan_read_only_chat(
                            objective=secret_objective,
                            repo=repo,
                            ticket_id="AMOF-CHAT-777",
                            files=["README.md"],
                        )

            messages_text = Path(result.evidence["messages_path"]).read_text(encoding="utf-8")
            artifact_text = Path(result.evidence["plan_result_path"]).read_text(encoding="utf-8")

            self.assertNotIn("secret-token", messages_text)
            self.assertNotIn(secret_objective, messages_text)
            self.assertIn("sha256", messages_text)
            self.assertNotIn("secret-token", artifact_text)
            self.assertNotIn(secret_objective, artifact_text)
            self.assertIn("sha256", artifact_text)
            self.assertIsNone(result.evidence["journal_path"])

    def test_minimal_context_mode_skips_canonical_index_builder(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-plan-minimal-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "context.md").write_text("# Context\nbounded\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            _write_remote_ial_profile(amof_home)

            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(amof_home),
                    "AMOF_REMOTE_IAL_BASE_URL": "https://ial.example.test",
                    "AMOF_REMOTE_IAL_API_KEY": "unit-test-token",
                    "AMOF_REMOTE_IAL_CONTEXT_LIMIT_CHARS": "20000",
                },
                clear=False,
            ):
                with patch.object(
                    chat,
                    "build_canonical_planning_context",
                    side_effect=AssertionError("canonical planning context should be skipped"),
                ):
                    with patch(
                        "amof.orchestrator.llm.remote_ial.requests.post",
                        return_value=_FakeHTTPResponse(200, _remote_ial_success_payload(ticket_id="AMOF-CHAT-MINIMAL")),
                    ):
                        result = chat.plan_read_only_chat(
                            objective="Return a bounded next-action checklist.",
                            repo=repo,
                            ticket_id="AMOF-CHAT-MINIMAL",
                            files=["context.md"],
                            minimal_context=True,
                        )

            receipt_payload = json.loads(Path(result.evidence["planning_context_receipt_path"]).read_text(encoding="utf-8"))
            self.assertEqual(receipt_payload["mode"], "minimal_context_no_index")
            self.assertEqual(receipt_payload["files_to_inspect"], ["context.md"])

    def test_unknown_cost_does_not_render_zero_in_plan_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-plan-unknown-cost-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "context.md").write_text("# Context\nbounded\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            _write_remote_ial_profile(amof_home)

            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(amof_home),
                    "AMOF_REMOTE_IAL_BASE_URL": "https://ial.example.test",
                    "AMOF_REMOTE_IAL_API_KEY": "unit-test-token",
                    "AMOF_REMOTE_IAL_CONTEXT_LIMIT_CHARS": "20000",
                },
                clear=False,
            ):
                with patch.object(
                    chat,
                    "build_canonical_planning_context",
                    side_effect=AssertionError("canonical planning context should be skipped"),
                ):
                    with patch(
                        "amof.orchestrator.llm.remote_ial.requests.post",
                        return_value=_FakeHTTPResponse(200, _remote_ial_unknown_cost_payload()),
                    ):
                        result = chat.plan_read_only_chat(
                            objective="Return bounded checklist with unknown provider cost.",
                            repo=repo,
                            ticket_id="AMOF-CHAT-UNKNOWN",
                            files=["context.md"],
                            minimal_context=True,
                        )

            self.assertIsNone(result.inference.estimated_cost)
            plan_result_text = Path(result.evidence["plan_result_path"]).read_text(encoding="utf-8")
            self.assertIn('"estimated_cost": null', plan_result_text)
            self.assertNotIn('"estimated_cost": 0.0', plan_result_text)


if __name__ == "__main__":
    unittest.main()
