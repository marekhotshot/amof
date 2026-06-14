"""Remote-IAL structured-output parsing must tolerate markdown code fences.

Regression for the bounded plan-execute lane: strong planner models (e.g.
claude-sonnet) wrap strict-JSON output in ```json ... ``` fences despite the
"no fences" instruction. The remote-IAL structured contract must parse that, or
the AMOF plan-execute loop cannot use those models as planners.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from pydantic import BaseModel

from amof.orchestrator.llm.base import LLMResponse, ProviderError, Usage
from amof.orchestrator.llm.remote_ial import RemoteIALClient, _strip_code_fences


class _Plan(BaseModel):
    analysis: str
    steps: list[str]


def _usage() -> Usage:
    return Usage(model="m", prompt_tokens=1, completion_tokens=1, latency_ms=1)


class _Client(RemoteIALClient):
    """RemoteIALClient with chat() stubbed to a fixed response text."""

    def __init__(self, text: str) -> None:
        super().__init__(base_url="http://127.0.0.1:18787", model="test/model")
        self._stub_text = text

    def chat(self, *args, **kwargs) -> LLMResponse:  # type: ignore[override]
        return LLMResponse(text=self._stub_text, usage=_usage(), stop_reason="stop", raw={})


class StripCodeFencesTests(unittest.TestCase):
    def test_strips_json_fence(self):
        self.assertEqual(_strip_code_fences('```json\n{"a": 1}\n```'), '{"a": 1}')

    def test_strips_bare_fence(self):
        self.assertEqual(_strip_code_fences('```\n{"a": 1}\n```'), '{"a": 1}')

    def test_passthrough_when_no_fence(self):
        self.assertEqual(_strip_code_fences('{"a": 1}'), '{"a": 1}')

    def test_does_not_corrupt_inner_braces(self):
        self.assertEqual(
            _strip_code_fences('```json\n{"a": {"b": 1}}\n```'), '{"a": {"b": 1}}'
        )


class ChatStructuredFenceToleranceTests(unittest.TestCase):
    def test_fenced_json_parses(self):
        client = _Client('```json\n{"analysis": "ok", "steps": ["one", "two"]}\n```')
        result = client.chat_structured(system="s", messages=[], response_model=_Plan)
        self.assertIsInstance(result.parsed, _Plan)
        self.assertEqual(result.parsed.analysis, "ok")
        self.assertEqual(result.parsed.steps, ["one", "two"])
        self.assertEqual(result.text, '{"analysis": "ok", "steps": ["one", "two"]}')

    def test_clean_json_still_parses(self):
        client = _Client('{"analysis": "ok", "steps": []}')
        result = client.chat_structured(system="s", messages=[], response_model=_Plan)
        self.assertEqual(result.parsed.analysis, "ok")

    def test_genuinely_invalid_json_still_raises(self):
        client = _Client('```json\nnot json at all\n```')
        with self.assertRaises(ProviderError):
            client.chat_structured(system="s", messages=[], response_model=_Plan)

    def test_empty_response_raises(self):
        client = _Client("   ")
        with self.assertRaises(ProviderError):
            client.chat_structured(system="s", messages=[], response_model=_Plan)


if __name__ == "__main__":
    unittest.main()
