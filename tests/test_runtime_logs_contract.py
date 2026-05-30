from __future__ import annotations

import json
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

from amof.commands import chat


class _FakeHTTPResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, object]:
        return self._payload


def _write_remote_ial_profile(amof_home: Path) -> None:
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


def _load_events(path: Path) -> list[dict[str, object]]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


class RuntimeLogsContractTests(unittest.TestCase):
    def test_minimal_context_run_emits_required_lifecycle_contract(self) -> None:
        payload = {
            "text": json.dumps(
                {
                    "ticket_id": "AMOF-RUNTIME-LOGS-CONTRACT-001",
                    "proposed_ticket_id": None,
                    "proposed_steps": ["Step 1", "Step 2"],
                    "risks": ["Risk 1"],
                    "validation_plan": ["Validation 1"],
                    "execution_prompt_for_director": "Proposal only.",
                    "execution_allowed": False,
                }
            ),
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "request_id": "req-runtime-logs-1",
            "tokens": {"input": 42, "output": 18},
            "latency_ms": 25,
            "estimated_cost": 0.0012,
            "provider_generation_id": "gen-raw-should-not-leak",
            "provider_generation_ref": "hash-safe-ref",
        }
        with tempfile.TemporaryDirectory(prefix="amof-runtime-logs-contract-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "context.md").write_text("bounded context\n", encoding="utf-8")
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
                with patch(
                    "amof.orchestrator.llm.remote_ial.requests.post",
                    return_value=_FakeHTTPResponse(200, payload),
                ):
                    result = chat.plan_read_only_chat(
                        objective="Return runtime logs checklist.",
                        repo=repo,
                        ticket_id="AMOF-RUNTIME-LOGS-CONTRACT-001",
                        files=["context.md"],
                        minimal_context=True,
                    )

            events = _load_events(Path(result.evidence["events_path"]))
            event_types = [str(event.get("event_type") or event.get("type")) for event in events]
            for required in (
                "run_created",
                "planning_mode_selected",
                "context_file_loaded",
                "ial_request_started",
                "ial_request_finished",
                "planning_context_receipt_written",
                "run_finished",
            ):
                self.assertIn(required, event_types)

            self.assertLess(event_types.index("run_created"), event_types.index("ial_request_started"))
            self.assertLess(event_types.index("ial_request_started"), event_types.index("ial_request_finished"))
            self.assertEqual(event_types[-1], "run_finished")

            for event in events:
                for key in ("event_id", "run_id", "session_id", "timestamp", "event_type", "severity", "actor"):
                    self.assertIn(key, event)
                self.assertEqual(event.get("run_id"), result.session_id)
                self.assertEqual(event.get("planning_mode"), "minimal_context")
                self.assertEqual(event.get("context"), "local")
                self.assertNotIn("provider_generation_id", event)

            finish_events = [e for e in events if (e.get("event_type") or e.get("type")) == "ial_request_finished"]
            self.assertEqual(len(finish_events), 1)
            self.assertEqual(finish_events[0].get("cost_status"), "observed")
            self.assertEqual(finish_events[0].get("estimated_cost"), 0.0012)
            self.assertEqual(finish_events[0].get("tokens_in"), 42)
            self.assertEqual(finish_events[0].get("tokens_out"), 18)

    def test_unknown_cost_serializes_unknown_without_fake_zero(self) -> None:
        payload = {
            "text": json.dumps(
                {
                    "ticket_id": "AMOF-RUNTIME-LOGS-CONTRACT-001",
                    "proposed_ticket_id": None,
                    "proposed_steps": ["Step 1"],
                    "risks": ["Risk 1"],
                    "validation_plan": ["Validation 1"],
                    "execution_prompt_for_director": "Proposal only.",
                    "execution_allowed": False,
                }
            ),
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "request_id": "req-runtime-logs-2",
            "tokens": {"input": 11, "output": 9},
            "latency_ms": 12,
        }
        with tempfile.TemporaryDirectory(prefix="amof-runtime-logs-contract-unknown-") as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            (repo / "context.md").write_text("bounded context\n", encoding="utf-8")
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
                with patch(
                    "amof.orchestrator.llm.remote_ial.requests.post",
                    return_value=_FakeHTTPResponse(200, payload),
                ):
                    result = chat.plan_read_only_chat(
                        objective="Return runtime logs checklist.",
                        repo=repo,
                        ticket_id="AMOF-RUNTIME-LOGS-CONTRACT-001",
                        files=["context.md"],
                        minimal_context=True,
                    )

            events = _load_events(Path(result.evidence["events_path"]))
            finish_events = [e for e in events if (e.get("event_type") or e.get("type")) == "ial_request_finished"]
            self.assertEqual(len(finish_events), 1)
            finish = finish_events[0]
            self.assertEqual(finish.get("cost_status"), "unknown")
            self.assertIsNone(finish.get("estimated_cost"))

            run_finished = [e for e in events if (e.get("event_type") or e.get("type")) == "run_finished"][0]
            self.assertEqual(run_finished.get("cost_status"), "unknown")
            self.assertIsNone(run_finished.get("estimated_cost"))


if __name__ == "__main__":
    unittest.main()
