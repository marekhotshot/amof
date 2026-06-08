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
from amof.orchestrator.llm.base import LLMResponse, Usage


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


class _FakeQueuedClient:
    def __init__(
        self,
        payloads: list[dict[str, object]],
        *,
        usage_payloads: list[dict[str, object]] | None = None,
    ) -> None:
        self.payloads = list(payloads)
        self.usage_payloads = list(usage_payloads or [])
        self.calls = 0

    def chat(self, *args, **kwargs) -> LLMResponse:
        if not self.payloads:
            raise AssertionError("no more queued chat responses")
        self.calls += 1
        payload = self.payloads.pop(0)
        usage_payload = self.usage_payloads.pop(0) if self.usage_payloads else {}
        usage_kwargs = {
            "model": "remote-ial/test-model",
            "prompt_tokens": 50,
            "completion_tokens": 20,
            "latency_ms": 5,
            "estimated_cost": 0.01,
            "provider": "remote-ial",
        }
        usage_kwargs.update(usage_payload)
        return LLMResponse(
            text=json.dumps(payload),
            usage=Usage(**usage_kwargs),
        )


class ChatIntakeSessionTests(unittest.TestCase):
    def test_start_creates_session_artifact_and_first_question(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-session-start-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            fake_client = _FakeQueuedClient(
                [
                    {
                        "state": "ask_user",
                        "assistant_message": "One clarification is needed before finalizing.",
                        "question": "Which operator workflow should this session optimize first?",
                        "rationale": "Need one bounded clarification.",
                    }
                ]
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                with patch.object(chat, "build_canonical_planning_context", return_value=_fake_planning_context(repo)):
                    with patch.object(chat, "_active_provider_profile", return_value={"name": "test", "provider": "remote-ial"}):
                        with patch.object(chat, "_profile_model", return_value="remote-ial/test-model"):
                            with patch.object(chat, "_build_remote_ial_client", return_value=fake_client):
                                with patch.object(chat, "_load_agent_config", return_value={}):
                                    with patch.object(chat, "_resolve_evidence_policy", return_value={"messages": "full", "journal": "full"}):
                                        result = chat.start_bounded_chat_session(
                                            objective="Clarify AMOF-282 session flow",
                                            repo=repo,
                                            ticket_id="AMOF-282",
                                        )
            self.assertEqual(result.status, "active")
            self.assertEqual(result.questions_asked, 1)
            self.assertEqual(result.pending_question, "Which operator workflow should this session optimize first?")
            self.assertTrue(Path(result.evidence["session_state_path"]).exists())
            self.assertTrue(Path(result.evidence["planning_context_receipt_path"]).exists())
            self.assertTrue(Path(result.evidence["indexed_context_path"]).exists())

    def test_ask_respects_question_budget_and_marks_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-session-budget-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            fake_client = _FakeQueuedClient(
                [
                    {
                        "state": "ask_user",
                        "assistant_message": "One clarification is needed before finalizing.",
                        "question": "Which operator workflow should this session optimize first?",
                        "rationale": "Need one bounded clarification.",
                    }
                ]
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                with patch.object(chat, "build_canonical_planning_context", return_value=_fake_planning_context(repo)):
                    with patch.object(chat, "_active_provider_profile", return_value={"name": "test", "provider": "remote-ial"}):
                        with patch.object(chat, "_profile_model", return_value="remote-ial/test-model"):
                            with patch.object(chat, "_build_remote_ial_client", return_value=fake_client):
                                with patch.object(chat, "_load_agent_config", return_value={}):
                                    with patch.object(chat, "_resolve_evidence_policy", return_value={"messages": "full", "journal": "full"}):
                                        started = chat.start_bounded_chat_session(
                                            objective="Clarify AMOF-282 session flow",
                                            repo=repo,
                                            ticket_id="AMOF-282",
                                            max_questions=1,
                                        )
                                        updated = chat.ask_bounded_chat_session(
                                            session_id=started.session_id,
                                            message="Optimize the operator-facing CLI path first.",
                                        )
            self.assertEqual(updated.status, "ready_to_finalize")
            self.assertIsNone(updated.pending_question)
            self.assertEqual(updated.questions_asked, 1)
            self.assertEqual(fake_client.calls, 1)

    def test_finalize_emits_proposal_only_plan_packet(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-session-finalize-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            fake_client = _FakeQueuedClient(
                [
                    {
                        "state": "ready_to_finalize",
                        "assistant_message": "Ready to finalize now.",
                        "question": None,
                        "rationale": "Sufficient context already exists.",
                    },
                    {
                        "ticket_id": "AMOF-282",
                        "proposed_ticket_id": None,
                        "proposed_steps": [
                            "Start a bounded chat intake session.",
                            "Collect a small number of clarifications before finalizing.",
                        ],
                        "risks": ["Too many session turns could blur the boundary with execution."],
                        "validation_plan": ["Run focused AMOF-282 intake session tests."],
                        "execution_prompt_for_director": "Proposal only. Wait for explicit approval before any execution handoff.",
                        "execution_allowed": False,
                    },
                ]
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                with patch.object(chat, "build_canonical_planning_context", return_value=_fake_planning_context(repo)):
                    with patch.object(chat, "_active_provider_profile", return_value={"name": "test", "provider": "remote-ial"}):
                        with patch.object(chat, "_profile_model", return_value="remote-ial/test-model"):
                            with patch.object(chat, "_build_remote_ial_client", return_value=fake_client):
                                with patch.object(chat, "_load_agent_config", return_value={}):
                                    with patch.object(chat, "_resolve_evidence_policy", return_value={"messages": "full", "journal": "full"}):
                                        started = chat.start_bounded_chat_session(
                                            objective="Clarify AMOF-282 session flow",
                                            repo=repo,
                                            ticket_id="AMOF-282",
                                        )
                                        finalized = chat.finalize_bounded_chat_session(session_id=started.session_id)
            self.assertEqual(finalized.status, "finalized")
            self.assertIsNotNone(finalized.plan_packet)
            self.assertTrue(finalized.non_executable_until_user_approval)
            self.assertEqual(finalized.to_dict()["plan_bundle"]["result_kind"], "plan_bundle")
            self.assertFalse(finalized.plan_packet["execution_allowed"])
            self.assertTrue(finalized.plan_packet["requires_user_approval"])
            self.assertTrue(Path(finalized.evidence["plan_result_path"]).exists())

    def test_finalize_unknown_cost_stays_truthful_in_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-session-finalize-unknown-cost-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            fake_client = _FakeQueuedClient(
                [
                    {
                        "state": "ready_to_finalize",
                        "assistant_message": "Ready to finalize now.",
                        "question": None,
                        "rationale": "Sufficient context already exists.",
                    },
                    {
                        "ticket_id": "AMOF-282",
                        "proposed_ticket_id": None,
                        "proposed_steps": ["Finalize one bounded proposal."],
                        "risks": ["Provider cost truth is unavailable."],
                        "validation_plan": ["Verify runtime artifacts keep null cost."],
                        "execution_prompt_for_director": "Proposal only.",
                        "execution_allowed": False,
                    },
                ],
                usage_payloads=[
                    {"estimated_cost": 0.0, "cost_status": "unknown", "cost_observed": False},
                    {"estimated_cost": 0.0, "cost_status": "unknown", "cost_observed": False},
                ],
            )
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                with patch.object(chat, "build_canonical_planning_context", return_value=_fake_planning_context(repo)):
                    with patch.object(chat, "_active_provider_profile", return_value={"name": "test", "provider": "remote-ial"}):
                        with patch.object(chat, "_profile_model", return_value="remote-ial/test-model"):
                            with patch.object(chat, "_build_remote_ial_client", return_value=fake_client):
                                with patch.object(chat, "_load_agent_config", return_value={}):
                                    with patch.object(chat, "_resolve_evidence_policy", return_value={"messages": "full", "journal": "full"}):
                                        started = chat.start_bounded_chat_session(
                                            objective="Clarify AMOF-282 session flow",
                                            repo=repo,
                                            ticket_id="AMOF-282",
                                        )
                                        finalized = chat.finalize_bounded_chat_session(session_id=started.session_id)

            telemetry_path = Path(finalized.evidence["session_dir"]) / "telemetry.json"
            telemetry_payload = json.loads(telemetry_path.read_text(encoding="utf-8"))
            self.assertIsNone(telemetry_payload["total_cost"])
            self.assertEqual(telemetry_payload["cost_status"], "unknown")
            self.assertEqual(telemetry_payload["unknown_cost_calls"], 1)

            events_path = Path(finalized.evidence["events_path"])
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(events[-1]["event_type"], "run_finished")
            self.assertEqual(events[-1]["cost_status"], "unknown")
            self.assertIsNone(events[-1]["estimated_cost"])
            self.assertEqual(events[-2]["event_type"], "session_end")
            self.assertIsNone(events[-2]["telemetry"]["total_cost"])
            self.assertEqual(events[-2]["telemetry"]["cost_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
