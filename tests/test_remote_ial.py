from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pydantic import BaseModel
import requests
try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover - depends on optional API deps.
    TestClient = None


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import agent_cmd, setup
from amof.orchestrator.events import EventLog
from amof.orchestrator.llm.base import ProviderError, stop_reason_for_failure_class
from amof.orchestrator.llm.remote_ial import RemoteIALClient


class _StructuredPlan(BaseModel):
    verdict: str
    steps: list[str]


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


class RemoteIALStructuredTests(unittest.TestCase):
    def test_structured_fallback_sends_schema_instruction_and_parses_json(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                200,
                {
                    "text": '{"verdict":"PASS","steps":["inspect"]}',
                    "provider": "unit-test-upstream",
                    "model": "unit-test/model",
                    "tokens": {"input": 10, "output": 4},
                    "latency_ms": 12,
                    "stop_reason": "stop",
                },
            ),
        ) as post:
            response = client.chat_structured(
                system="system prompt",
                messages=[{"role": "user", "content": "return a plan"}],
                response_model=_StructuredPlan,
            )

        self.assertEqual(response.parsed.verdict, "PASS")
        self.assertEqual(response.parsed.steps, ["inspect"])
        self.assertEqual(response.usage.provider, "remote-ial")
        payload = post.call_args.kwargs["json"]
        self.assertIn("Return ONLY one strict JSON object", payload["system"])
        self.assertIn('"verdict"', payload["system"])
        self.assertEqual(payload["messages"], [{"role": "user", "content": "return a plan"}])
        self.assertEqual(payload["tools"], [])
        self.assertNotIn("Bearer token", str(payload))

    def test_structured_fallback_invalid_json_fails_closed(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                200,
                {
                    "text": "not json",
                    "provider": "unit-test-upstream",
                    "model": "unit-test/model",
                },
            ),
        ):
            with self.assertRaises(ProviderError) as raised:
                client.chat_structured(
                    system="system prompt",
                    messages=[{"role": "user", "content": "return a plan"}],
                    response_model=_StructuredPlan,
                )

        self.assertEqual(raised.exception.provider, "remote-ial")
        self.assertEqual(raised.exception.failure_class, "api_error")
        self.assertIn("failed schema validation", str(raised.exception))

    def test_structured_fallback_preserves_provider_error_classification(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                401,
                {
                    "detail": {
                        "code": "provider_failure",
                        "message": "User not found.",
                        "provider": "unit-test-upstream",
                        "failure_class": "auth",
                        "status_code": 401,
                    }
                },
            ),
        ):
            with self.assertRaises(ProviderError) as raised:
                client.chat_structured(
                    system="system prompt",
                    messages=[{"role": "user", "content": "return a plan"}],
                    response_model=_StructuredPlan,
                )

        self.assertEqual(raised.exception.provider, "remote-ial")
        self.assertEqual(raised.exception.upstream_provider, "unit-test-upstream")
        self.assertEqual(raised.exception.failure_class, "auth")


class RemoteIALEventTests(unittest.TestCase):
    def test_remote_ial_usage_cost_present_is_observed(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                200,
                {
                    "text": "remote-ok",
                    "provider": "openrouter",
                    "model": "openai/gpt-4o-mini",
                    "tokens": {"input": 12, "output": 5},
                    "estimated_cost": 0.000321,
                    "cost_status": "observed",
                    "latency_ms": 34,
                },
            ),
        ):
            response = client.chat(system="", messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(response.usage.cost_status, "observed")
        self.assertTrue(response.usage.cost_observed)
        self.assertAlmostEqual(response.usage.estimated_cost, 0.000321, places=6)

    def test_remote_ial_usage_cost_missing_is_unknown_not_zero_truth(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                200,
                {
                    "text": "remote-ok",
                    "provider": "openrouter",
                    "model": "openai/gpt-4o-mini",
                    "tokens": {"input": 9, "output": 4},
                    "latency_ms": 21,
                },
            ),
        ):
            response = client.chat(system="", messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(response.usage.cost_status, "unknown")
        self.assertFalse(response.usage.cost_observed)
        self.assertEqual(response.usage.estimated_cost, 0.0)

    def test_remote_ial_usage_keeps_provider_generation_references(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                200,
                {
                    "text": "remote-ok",
                    "provider": "openrouter",
                    "model": "openai/gpt-4o-mini",
                    "tokens": {"input": 9, "output": 4},
                    "latency_ms": 21,
                    "provider_generation_id": "gen-123",
                    "provider_generation_ref": "hash-abc",
                },
            ),
        ):
            response = client.chat(system="", messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(response.usage.provider_generation_id, "gen-123")
        self.assertEqual(response.usage.provider_generation_ref, "hash-abc")

    def test_llm_event_marks_unknown_cost_without_zero_fallback(self) -> None:
        client = RemoteIALClient(base_url="http://127.0.0.1:8765", api_key="token")
        with patch(
            "amof.orchestrator.llm.remote_ial.requests.post",
            return_value=_FakeHTTPResponse(
                200,
                {
                    "text": "remote-ok",
                    "provider": "openrouter",
                    "model": "openai/gpt-4o-mini",
                    "tokens": {"input": 4, "output": 3},
                    "latency_ms": 12,
                },
            ),
        ):
            response = client.chat(system="", messages=[{"role": "user", "content": "hi"}])

        with tempfile.TemporaryDirectory(prefix="amof-remote-ial-events-unknown-cost-") as td:
            events = EventLog(session_id="remote-ial-session", runs_dir=Path(td))
            events.llm_call(
                model=response.usage.model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                cost=response.usage.estimated_cost if response.usage.cost_observed else None,
                latency_ms=response.usage.latency_ms,
                provider=response.usage.provider,
                upstream_provider=response.usage.upstream_provider,
                cost_status=response.usage.cost_status,
            )
            event_text = events.log_path.read_text(encoding="utf-8")

        self.assertIn('"cost": null', event_text)
        self.assertIn('"cost_status": "unknown"', event_text)

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
                cost=response.usage.estimated_cost if response.usage.cost_observed else None,
                latency_ms=response.usage.latency_ms,
                provider=response.usage.provider,
                upstream_provider=response.usage.upstream_provider,
                upstream_model=response.usage.upstream_model,
                request_id=response.usage.request_id,
                policy_decision=response.usage.policy_decision,
                input_hash=response.usage.input_hash,
                output_hash=response.usage.output_hash,
                cost_status=response.usage.cost_status,
            )
            event_text = events.log_path.read_text(encoding="utf-8")

        self.assertIn('"provider": "remote-ial"', event_text)
        self.assertIn('"upstream_provider": "bedrock"', event_text)
        self.assertIn('"upstream_model": "eu.anthropic.claude-haiku-4-5-20251001-v1:0"', event_text)
        self.assertIn('"request_id": "req-123"', event_text)
        self.assertIn('"input_hash": "input-hash"', event_text)
        self.assertIn('"output_hash": "output-hash"', event_text)


class PublicRouteTests(unittest.TestCase):
    @unittest.skipIf(TestClient is None, "fastapi is not installed")
    def test_public_api_prefix_does_not_expose_ial_chat(self) -> None:
        from amof.api.main import app

        assert TestClient is not None
        client = TestClient(app)
        response = client.post(
            "/api/v1/ial/chat",
            headers={"Authorization": "Bearer secret-token"},
            json={"system": "", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
