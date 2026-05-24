from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import agent_cmd
from amof.orchestrator.session import Session


class _TelemetryStub:
    def to_dict(self) -> dict[str, object]:
        return {"total_cost": 0.0}


class IALEvidenceModeTests(unittest.TestCase):
    def test_default_evidence_policy_is_deterministic(self) -> None:
        self.assertEqual(
            agent_cmd._resolve_evidence_policy({}),
            {"messages": "raw_local", "journal": "enabled"},
        )

    def test_hash_only_mode_avoids_raw_message_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-evidence-hash-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            session = Session(session_id="hash-only-session")
            session.add_user_message("tell me a secret")
            session.add_assistant_message("secret response")

            with patch.dict(os.environ, {"AMOF_HOME": str(temp / "amof-home")}, clear=False):
                session_dir = agent_cmd._save_session(
                    session,
                    telemetry=_TelemetryStub(),
                    events=None,
                    workspace_root=repo,
                    cfg={"evidence": {"messages": "hash_only"}},
                )

            messages_text = (session_dir / "messages.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("tell me a secret", messages_text)
            self.assertNotIn("secret response", messages_text)
            self.assertIn("sha256", messages_text)

    def test_redacted_local_mode_replaces_secrets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-evidence-redacted-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            session = Session(session_id="redacted-session")
            session.add_user_message("Authorization: Bearer secret-token and key unit-test-provider-value")
            session.add_assistant_message("echo unit-test-provider-value back")

            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(temp / "amof-home"),
                    "OPENROUTER_API_KEY": "unit-test-provider-value",
                },
                clear=False,
            ):
                session_dir = agent_cmd._save_session(
                    session,
                    telemetry=_TelemetryStub(),
                    events=None,
                    workspace_root=repo,
                    cfg={"evidence": {"messages": "redacted_local"}},
                )

            messages_text = (session_dir / "messages.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("secret-token", messages_text)
            self.assertNotIn("unit-test-provider-value", messages_text)
            self.assertIn("[REDACTED]", messages_text)

    def test_journal_disabled_skips_file_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-evidence-journal-off-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            session = Session(session_id="journal-off-session")
            session.add_user_message("raw prompt")
            session.add_assistant_message("raw answer")
            manifest = {"ecosystem": "demo-repo", "manifest_source": "appdata"}

            with patch.dict(os.environ, {"AMOF_HOME": str(temp / "amof-home")}, clear=False):
                agent_cmd._generate_journal(
                    session,
                    goal="Inspect this repo",
                    stop_reason="completed",
                    telemetry=object(),
                    events=None,
                    manifest=manifest,
                    workspace_root=repo,
                    cfg={"evidence": {"journal": "disabled"}},
                )
                journal_dir = Path(os.environ["AMOF_HOME"]) / "share" / "journals" / "demo-repo"

            self.assertFalse(journal_dir.exists())

    def test_hash_only_messages_and_disabled_journal_keep_tokens_out_of_local_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-evidence-local-secrets-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            session = Session(session_id="local-evidence-session")
            session.add_user_message("Authorization: Bearer secret-token")
            session.add_assistant_message("provider key is unit-test-provider-value")
            manifest = {"ecosystem": "demo-repo", "manifest_source": "appdata"}

            with patch.dict(
                os.environ,
                {
                    "AMOF_HOME": str(temp / "amof-home"),
                    "OPENROUTER_API_KEY": "unit-test-provider-value",
                },
                clear=False,
            ):
                session_dir = agent_cmd._save_session(
                    session,
                    telemetry=_TelemetryStub(),
                    events=None,
                    workspace_root=repo,
                    cfg={"evidence": {"messages": "hash_only", "journal": "disabled"}},
                )
                agent_cmd._generate_journal(
                    session,
                    goal="Inspect this repo",
                    stop_reason="completed",
                    telemetry=object(),
                    events=None,
                    manifest=manifest,
                    workspace_root=repo,
                    cfg={"evidence": {"messages": "hash_only", "journal": "disabled"}},
                )
                evidence_text = "".join(
                    path.read_text(encoding="utf-8")
                    for path in session_dir.parent.rglob("*")
                    if path.is_file()
                )

            self.assertNotIn("secret-token", evidence_text)
            self.assertNotIn("unit-test-provider-value", evidence_text)


if __name__ == "__main__":
    unittest.main()
