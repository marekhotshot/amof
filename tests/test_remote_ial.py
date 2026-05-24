from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import agent_cmd, setup
from amof.orchestrator.events import EventLog
from amof.orchestrator.llm.base import ProviderError, stop_reason_for_failure_class
from amof.orchestrator.llm.remote_ial import RemoteIALClient


class _FakeHTTPResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, object]:
        return self._payload


class RemoteIALProfileTests(unittest.TestCase):
    def test_remote_ial_template_uses_private_env_refs(self) -> None:
        self.assertIn("remote-ial", setup.PROVIDER_TEMPLATE_ORDER)
        template = setup.PROVIDER_TEMPLATES["remote-ial"]
        self.assertEqual(template["provider"], "remote-ial")
        self.assertEqual(template["credential_refs"]["api_key_env"], "AMOF_REMOTE_IAL_API_KEY")
        self.assertEqual(template["credential_refs"]["base_url_env"], "AMOF_REMOTE_IAL_BASE_URL")


class RemoteIALFailureTests(unittest.TestCase):
    def test_auth_failure_surfaces_as_fatal_local_failure(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="bad-token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                401,
                {"detail": {"message": "Invalid remote IAL bearer token."}},
            ),
        ):
            with self.assertRaises(ProviderError) as raised:
                client.chat(system="", messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(raised.exception.provider, "remote-ial")
        self.assertEqual(raised.exception.failure_class, "auth")
        stop_reason = stop_reason_for_failure_class(raised.exception.failure_class)
        self.assertEqual(agent_cmd._agent_stop_reason_exit_code(stop_reason), 1)

    def test_network_failure_surfaces_as_fatal_local_failure(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            side_effect=requests.exceptions.Timeout("connection timed out"),
        ):
            with self.assertRaises(ProviderError) as raised:
                client.chat(system="", messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(raised.exception.provider, "remote-ial")
        self.assertEqual(raised.exception.failure_class, "network")

    def test_upstream_provider_failure_preserves_provider_identity(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                502,
                {
                    "detail": {
                        "message": "[bedrock/server_error] upstream failed",
                        "provider": "bedrock",
                        "failure_class": "server_error",
                        "status_code": 503,
                    }
                },
            ),
        ):
            with self.assertRaises(ProviderError) as raised:
                client.chat(system="", messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(raised.exception.provider, "remote-ial")
        self.assertEqual(raised.exception.upstream_provider, "bedrock")
        self.assertEqual(raised.exception.failure_class, "server_error")

    def test_upstream_auth_failure_preserves_gateway_correlation_fields(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                401,
                {
                    "detail": {
                        "code": "provider_failure",
                        "message": "User not found.",
                        "provider": "openrouter",
                        "upstream_provider": "openrouter",
                        "model": "openai/gpt-4o-mini",
                        "upstream_model": "openai/gpt-4o-mini",
                        "failure_class": "auth",
                        "status_code": 401,
                        "request_id": "req-live-401",
                        "policy_decision": {"decision": "allow"},
                        "input_hash": "live-input-hash",
                        "output_hash": "live-output-hash",
                    }
                },
            ),
        ):
            with self.assertRaises(ProviderError) as raised:
                client.chat(system="", messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(raised.exception.provider, "remote-ial")
        self.assertEqual(raised.exception.upstream_provider, "openrouter")
        self.assertEqual(raised.exception.upstream_model, "openai/gpt-4o-mini")
        self.assertEqual(raised.exception.request_id, "req-live-401")
        self.assertEqual(raised.exception.input_hash, "live-input-hash")
        self.assertEqual(raised.exception.output_hash, "live-output-hash")
        self.assertEqual(raised.exception.failure_class, "auth")


class RemoteIALEventTests(unittest.TestCase):
    def test_llm_call_records_remote_transport_and_upstream_provider(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                200,
                {
                    "text": "remote-ok",
                    "provider": "bedrock",
                    "model": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
                    "request_id": "req-123",
                    "policy_decision": {"allowed": True},
                    "input_hash": "input-hash",
                    "output_hash": "output-hash",
                    "tokens": {"input": 12, "output": 5},
                    "latency_ms": 34,
                },
            ),
        ):
            response = client.chat(system="", messages=[{"role": "user", "content": "hi"}])

        with tempfile.TemporaryDirectory(prefix="amof-remote-ial-events-") as td:
            events = EventLog(session_id="remote-ial-session", runs_dir=Path(td))
            events.llm_call(
                model=response.usage.model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                cost=response.usage.estimated_cost,
                latency_ms=response.usage.latency_ms,
                provider=response.usage.provider,
                upstream_provider=response.usage.upstream_provider,
                upstream_model=response.usage.upstream_model,
                request_id=response.usage.request_id,
                policy_decision=response.usage.policy_decision,
                input_hash=response.usage.input_hash,
                output_hash=response.usage.output_hash,
            )
            event_text = events.log_path.read_text(encoding="utf-8")

        self.assertIn('"provider": "remote-ial"', event_text)
        self.assertIn('"upstream_provider": "bedrock"', event_text)
        self.assertIn('"upstream_model": "eu.anthropic.claude-haiku-4-5-20251001-v1:0"', event_text)
        self.assertIn('"request_id": "req-123"', event_text)
        self.assertIn('"input_hash": "input-hash"', event_text)
        self.assertIn('"output_hash": "output-hash"', event_text)


class PublicRouteTests(unittest.TestCase):
    def test_public_api_prefix_does_not_expose_ial_chat(self) -> None:
        from amof.api.main import app

        client = TestClient(app)
        response = client.post(
            "/api/v1/ial/chat",
            headers={"Authorization": "Bearer secret-token"},
            json={"system": "", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
