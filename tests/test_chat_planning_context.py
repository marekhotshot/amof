from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import chat as chat_cmd
from amof.orchestrator.llm.base import LLMResponse, Usage
from amof.orchestrator.planning_context import build_canonical_planning_context


def _commit_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "AMOF Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "AMOF Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return env


def _git(path: Path | None, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(path) if path is not None else None,
        check=True,
        capture_output=True,
        text=True,
        env=_commit_env(),
    )
    return completed.stdout.strip()


def _create_remote_with_source() -> tuple[Path, Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="amof-chat-planning-"))
    remote = root / "source.git"
    seed = root / "seed"
    source = root / "source"
    _git(None, "init", "--bare", str(remote))
    _git(None, "init", "-b", "main", str(seed))
    (seed / "README.md").write_text("# amof\n", encoding="utf-8")
    commands_dir = seed / "scripts" / "amof" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / "chat.py").write_text("def plan():\n    return 'plan'\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "test: seed repo")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(None, "clone", str(remote), str(source))
    return root, seed, source


def _advance_remote(seed: Path, *, filename: str, content: str) -> None:
    (seed / filename).write_text(content, encoding="utf-8")
    _git(seed, "add", filename)
    _git(seed, "commit", "-m", "test: advance remote")
    _git(seed, "push", "origin", "main")


class _FakeRemoteIALClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def model_name(self) -> str:
        return "remote-ial/test-model"

    def chat_structured(self, *args, **kwargs):
        raise NotImplementedError()

    def chat(self, system: str, messages: list[dict[str, object]], **kwargs) -> LLMResponse:
        self.calls.append(system)
        if "incremental" in system.lower():
            payload = {
                "files": {
                    "repos/source/README.md": {
                        "purpose": "project overview",
                        "classes": [],
                        "functions": [],
                        "complexity": "low",
                    }
                },
                "dependency_graph_updates": {
                    "repos/source/scripts/amof/commands/chat.py": ["repos/source/README.md"],
                    "repos/source/README.md": [],
                },
            }
            text = json.dumps(payload)
        elif "codebase analyst" in system.lower():
            payload = {
                "summary": "AMOF planning context for chat planning.",
                "architecture": "CLI commands and orchestration modules.",
                "files": {
                    "repos/source/scripts/amof/commands/chat.py": {
                        "purpose": "chat planning command",
                        "classes": [],
                        "functions": [{"name": "plan_read_only_chat", "description": "plans chat work"}],
                        "complexity": "medium",
                    },
                    "repos/source/README.md": {
                        "purpose": "project overview",
                        "classes": [],
                        "functions": [],
                        "complexity": "low",
                    },
                },
                "dependency_graph": {
                    "repos/source/scripts/amof/commands/chat.py": ["repos/source/README.md"],
                },
                "entry_points": ["repos/source/scripts/amof/commands/chat.py"],
                "key_abstractions": [{"name": "PlanPacket", "description": "proposal-only output"}],
            }
            text = json.dumps(payload)
        else:
            payload = {
                "ticket_id": "AMOF-281",
                "proposed_ticket_id": None,
                "proposed_steps": ["Build indexed planning context", "Generate a proposal-only plan packet"],
                "risks": ["Index could drift if origin/main changes"],
                "validation_plan": ["Run focused chat planning context tests"],
                "execution_prompt_for_director": "Proposal only. Review indexed context before any execution handoff.",
                "execution_allowed": False,
            }
            text = json.dumps(payload)
        return LLMResponse(
            text=text,
            usage=Usage(
                model="remote-ial/test-model",
                prompt_tokens=100,
                completion_tokens=40,
                latency_ms=5,
                estimated_cost=0.01,
                provider="remote-ial",
            ),
        )


class ChatPlanningContextTests(unittest.TestCase):
    def test_build_planning_context_bootstraps_missing_index(self) -> None:
        root, _seed, source = _create_remote_with_source()
        amof_home = root / ".amof-home"
        fake_client = _FakeRemoteIALClient()
        with patch.dict(os.environ, {"AMOF_HOME": str(amof_home), "AMOF_WORKSPACE_ROOT": str(source)}, clear=False):
            result = build_canonical_planning_context(
                repo=source,
                objective="chat planning",
                indexer_llm=fake_client,
                planner_provenance={"resolved_model": "remote-ial/test-model"},
                max_files=4,
            )
        self.assertTrue(result.receipt.index_refreshed)
        self.assertEqual(result.receipt.refresh_reason, "missing")
        self.assertTrue(Path(result.receipt.index_path).exists())
        self.assertTrue(Path(result.receipt.tree_path).exists())
        self.assertTrue(Path(result.receipt.planning_repo_path).exists())
        self.assertIn("repos/source/scripts/amof/commands/chat.py", result.receipt.files_to_inspect)

    def test_build_planning_context_refreshes_when_remote_main_changes(self) -> None:
        root, seed, source = _create_remote_with_source()
        amof_home = root / ".amof-home"
        fake_client = _FakeRemoteIALClient()
        with patch.dict(os.environ, {"AMOF_HOME": str(amof_home), "AMOF_WORKSPACE_ROOT": str(source)}, clear=False):
            first = build_canonical_planning_context(
                repo=source,
                objective="chat planning",
                indexer_llm=fake_client,
                planner_provenance={"resolved_model": "remote-ial/test-model"},
                max_files=4,
            )
            _advance_remote(seed, filename="README.md", content="# amof\n\nupdated\n")
            second = build_canonical_planning_context(
                repo=source,
                objective="chat planning",
                indexer_llm=fake_client,
                planner_provenance={"resolved_model": "remote-ial/test-model"},
                max_files=4,
            )
        self.assertEqual(first.receipt.refresh_reason, "missing")
        self.assertEqual(second.receipt.refresh_reason, "stale")
        self.assertTrue(second.receipt.index_refreshed)
        self.assertNotEqual(first.receipt.origin_main_sha, second.receipt.origin_main_sha)

    def test_plan_read_only_chat_writes_planning_context_receipt(self) -> None:
        root, _seed, source = _create_remote_with_source()
        amof_home = root / ".amof-home"
        fake_client = _FakeRemoteIALClient()
        session_dir = amof_home / "share" / "runs" / "chat-plans" / "session-1"
        session_dir.mkdir(parents=True, exist_ok=True)

        def _fake_save_session(*args, **kwargs):
            return session_dir

        with patch.dict(os.environ, {"AMOF_HOME": str(amof_home), "AMOF_WORKSPACE_ROOT": str(source)}, clear=False):
            with patch.object(chat_cmd, "_active_provider_profile", return_value={"name": "test", "provider": "remote-ial"}):
                with patch.object(chat_cmd, "_profile_model", return_value="remote-ial/test-model"):
                    with patch.object(chat_cmd, "_build_remote_ial_client", return_value=fake_client):
                        with patch.object(chat_cmd, "_load_agent_config", return_value={}):
                            with patch.object(
                                chat_cmd,
                                "_resolve_evidence_policy",
                                return_value={"messages": "full", "journal": "full"},
                            ):
                                with patch.object(chat_cmd, "_save_session", side_effect=_fake_save_session):
                                    result = chat_cmd.plan_read_only_chat(
                                        objective="chat planning",
                                        repo=source,
                                        ticket_id="AMOF-281",
                                        max_files=4,
                                    )
        receipt_path = Path(result.evidence["planning_context_receipt_path"])
        self.assertTrue(receipt_path.exists())
        receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt_payload["receipt_kind"], "planning_context_receipt")
        self.assertEqual(result.plan_packet.files_to_inspect, receipt_payload["files_to_inspect"])


if __name__ == "__main__":
    unittest.main()
