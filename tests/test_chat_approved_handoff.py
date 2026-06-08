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

from amof.commands import chat, director, workspace
from amof.orchestrator.llm.base import LLMResponse, Usage


def _fake_planning_context(repo: Path) -> object:
    receipt_payload = {
        "receipt_kind": "planning_context_receipt",
        "recorded_at": "2026-05-25T10:00:00+00:00",
        "source_repo_path": str(repo),
        "source_git_root": str(repo),
        "source_remote_url": "https://github.com/marekhotshot/amof.git",
        "canonical_remote_url": "https://github.com/marekhotshot/amof.git",
        "planning_workspace_root": str(repo.parent / "planning"),
        "planning_repo_path": str(repo.parent / "planning" / "repos" / "amof"),
        "planning_branch_ref": "origin/main",
        "origin_main_sha": "0123456789abcdef0123456789abcdef01234567",
        "index_dir": str(repo.parent / "index"),
        "index_path": str(repo.parent / "index" / "codebase-index.json"),
        "tree_path": str(repo.parent / "index" / "tree.json"),
        "merkle_root": "abc123def4567890",
        "indexed_at": "2026-05-25T10:00:01+00:00",
        "freshness": "fresh",
        "refresh_reason": None,
        "index_refreshed": False,
        "repo_scope": ["repos/amof"],
        "files_to_inspect": ["README.md", "app.py"],
        "planner_provenance": {"profile_name": "test", "resolved_model": "remote-ial/test-model"},
    }

    class _Receipt:
        def to_dict(self) -> dict[str, object]:
            return dict(receipt_payload)

    return SimpleNamespace(
        receipt=_Receipt(),
        context_prompt="# Codebase Index\n- `README.md`: project overview\n- `app.py`: app entrypoint\n",
    )


class _FakeQueuedClient:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = list(payloads)

    def chat(self, *args, **kwargs) -> LLMResponse:
        if not self.payloads:
            raise AssertionError("no more queued chat responses")
        payload = self.payloads.pop(0)
        return LLMResponse(
            text=json.dumps(payload),
            usage=Usage(
                model="remote-ial/test-model",
                prompt_tokens=50,
                completion_tokens=20,
                latency_ms=5,
                estimated_cost=0.01,
                provider="remote-ial",
            ),
        )


def _finalized_session(repo: Path, amof_home: Path) -> chat.IntakeSessionResult:
    fake_client = _FakeQueuedClient(
        [
            {
                "state": "ready_to_finalize",
                "assistant_message": "Ready to finalize now.",
                "question": None,
                "rationale": "Sufficient context already exists.",
            },
            {
                "ticket_id": "AMOF-283",
                "proposed_ticket_id": None,
                "proposed_steps": [
                    "Write an explicit approval artifact for finalized PlanBundles.",
                    "Convert approved artifacts into Director intake envelopes without execution side effects.",
                ],
                "risks": ["Crossing the chat boundary into execution would violate ownership rules."],
                "validation_plan": ["Run focused AMOF-283 approved handoff tests."],
                "execution_prompt_for_director": (
                    "Proposal only. Wait for explicit approval before any execution handoff."
                ),
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
                            with patch.object(
                                chat,
                                "_resolve_evidence_policy",
                                return_value={"messages": "full", "journal": "full"},
                            ):
                                started = chat.start_bounded_chat_session(
                                    objective="Bridge approved PlanBundles into existing workspace handoff contracts.",
                                    repo=repo,
                                    ticket_id="AMOF-283",
                                )
                                return chat.finalize_bounded_chat_session(session_id=started.session_id)


class ChatApprovedHandoffTests(unittest.TestCase):
    def test_approve_writes_explicit_artifact_without_execution_calls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-approve-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            finalized = _finalized_session(repo, amof_home)

            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                with patch.object(director, "prepare_run_artifacts") as prepare_run:
                    with patch.object(workspace, "materialize_from_intake_envelope") as materialize:
                        approval = chat.approve_finalized_chat_session(session_id=finalized.session_id)

            self.assertEqual(approval.approval_state, "approved")
            self.assertTrue(approval.non_executable_until_workspace_handoff)
            self.assertTrue(Path(approval.evidence["approval_artifact_path"]).exists())
            self.assertEqual(approval.approval_artifact.source_session["session_id"], finalized.session_id)
            prepare_run.assert_not_called()
            materialize.assert_not_called()

    def test_handoff_rejects_unapproved_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-handoff-reject-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            finalized = _finalized_session(repo, amof_home)
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                approval = chat.approve_finalized_chat_session(session_id=finalized.session_id)
                artifact_path = Path(approval.evidence["approval_artifact_path"])
                payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                payload["approval_state"] = "pending"
                artifact_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

                with self.assertRaises(chat.ChatPlanError):
                    chat.handoff_approved_chat_plan(approval_id_or_path=str(artifact_path))

    def test_handoff_writes_intake_without_materialization(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-chat-handoff-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            amof_home = temp / "amof-home"
            finalized = _finalized_session(repo, amof_home)
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                approval = chat.approve_finalized_chat_session(session_id=finalized.session_id)

                with patch.object(director, "prepare_run_artifacts") as prepare_run:
                    with patch.object(workspace, "materialize_from_intake_envelope") as materialize:
                        handoff = chat.handoff_approved_chat_plan(approval_id_or_path=approval.approval_id)

            self.assertTrue(Path(handoff.intake_path).exists())
            self.assertTrue(handoff.explicit_workspace_command_required)
            self.assertIn("workspace materialize-from-intake", handoff.materialization_command_hint)
            intake_payload = json.loads(Path(handoff.intake_path).read_text(encoding="utf-8"))
            self.assertEqual(intake_payload["result_kind"], "director_intake_execution_contract")
            self.assertEqual(intake_payload["executor_disposition"], "replay_later")
            self.assertIn("agent_execution", intake_payload["forbidden_mutations"])
            self.assertIn("ticket_checkpoint", intake_payload["forbidden_mutations"])
            self.assertIn("promote_main", intake_payload["forbidden_mutations"])
            self.assertEqual(
                intake_payload["execution_handoff"]["workspace_materialization"]["expected_sha"],
                "0123456789abcdef0123456789abcdef01234567",
            )
            prepare_run.assert_not_called()
            materialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
