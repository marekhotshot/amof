"""AMOF-RUNTIME-USAGE-TELEMETRY-CONVERGENCE-001 — usage null/zero + Native/Cursor normalize."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from amof.execution_backends import amof_native, cursor_agent, runtime_usage


class RuntimeUsageHelpersTests(unittest.TestCase):
    def test_finite_int_preserves_zero_and_rejects_null(self) -> None:
        self.assertEqual(runtime_usage.finite_int(0), 0)
        self.assertIsNone(runtime_usage.finite_int(None))
        self.assertIsNone(runtime_usage.finite_int(""))
        self.assertIsNone(runtime_usage.finite_int(-1))

    def test_remote_ial_tokens_missing_keys_stay_none(self) -> None:
        prompt, completion = runtime_usage.remote_ial_tokens_from_body({"tokens": {}})
        self.assertIsNone(prompt)
        self.assertIsNone(completion)
        prompt, completion = runtime_usage.remote_ial_tokens_from_body(
            {"tokens": {"input": 0, "output": 12}}
        )
        self.assertEqual(prompt, 0)
        self.assertEqual(completion, 12)

    def test_cursor_sdk_normalization_maps_proven_fields(self) -> None:
        normalized = runtime_usage.normalize_cursor_sdk_usage(
            {
                "input_tokens": 285602,
                "output_tokens": 4554,
                "cache_read_tokens": 250000,
                "cache_write_tokens": 10416,
                "total_tokens": 550572,
                "reasoning_tokens": 100,
            }
        )
        self.assertEqual(normalized["prompt_tokens"], 285602)
        self.assertEqual(normalized["completion_tokens"], 4554)
        self.assertEqual(normalized["cache_tokens"], 260416)
        self.assertEqual(normalized["total_tokens"], 550572)
        self.assertEqual(normalized["reasoning_tokens"], 100)
        self.assertTrue(normalized["saw_authoritative_tokens"])


class NativeUsageProjectionTests(unittest.TestCase):
    def test_openai_compatible_from_remote_ial_does_not_coerce_missing_to_zero(self) -> None:
        body = {
            "text": "hi",
            "model": "x-ai/grok-4.5",
            "request_id": "req-1",
            "stop_reason": "end",
            "tool_calls": [],
        }
        out = amof_native._openai_compatible_from_remote_ial(body, model="x-ai/grok-4.5")
        self.assertIsNone(out["usage"]["prompt_tokens"])
        self.assertIsNone(out["usage"]["completion_tokens"])

    def test_openai_compatible_from_remote_ial_preserves_zero(self) -> None:
        body = {
            "text": "hi",
            "model": "x-ai/grok-4.5",
            "request_id": "req-2",
            "tokens": {"input": 0, "output": 0},
            "stop_reason": "end",
            "tool_calls": [],
        }
        out = amof_native._openai_compatible_from_remote_ial(body, model="x-ai/grok-4.5")
        self.assertEqual(out["usage"]["prompt_tokens"], 0)
        self.assertEqual(out["usage"]["completion_tokens"], 0)

    def test_result_payload_projects_accumulated_usage(self) -> None:
        acc = runtime_usage.empty_usage_accumulator()
        runtime_usage.add_token_field(acc, "prompt_tokens", 100)
        runtime_usage.add_token_field(acc, "completion_tokens", 20)
        acc["model_calls"] = 2
        acc["tool_calls"] = 3
        acc["calls"] = [
            {
                "model_call_id": "t1",
                "actual_model": "x-ai/grok-4.5",
                "input_tokens": 40,
                "output_tokens": 10,
                "status": "ok",
                "provider_receipt_ref": "req-a",
            },
            {
                "model_call_id": "t2",
                "actual_model": "x-ai/grok-4.5",
                "input_tokens": 60,
                "output_tokens": 10,
                "status": "ok",
                "provider_receipt_ref": "req-b",
            },
        ]
        selection = amof_native.AmofNativeBackendSelection(
            runner_id="amof-native-ticket-write",
            capabilities=["ticket_write"],
            writable_roots_relative=[],
            writable_roots_resolved=(),
            timeout_seconds=60,
            readable_root=None,
        )
        result = amof_native._result_payload(
            run_id="run-1",
            status="completed",
            exit_code=0,
            stop_reason="completed",
            final_text="ok",
            studio_session_id=None,
            event_log_path=Path("/tmp/events.jsonl"),
            runtime_log_path=Path("/tmp/runtime.log"),
            changed_paths=[],
            selection=selection,
            health={},
            requested_model="x-ai/grok-4.5",
            effective_model="x-ai/grok-4.5",
            effective_provider="openrouter",
            transport="remote_ial",
            usage_acc=acc,
        )
        self.assertEqual(result["usage"]["prompt_tokens"], 100)
        self.assertEqual(result["usage"]["completion_tokens"], 20)
        self.assertEqual(result["usage"]["total_tokens"], 120)
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(result["usage"]["tool_calls"], 3)
        self.assertEqual(result["usage"]["token_telemetry"], "available")
        self.assertEqual(result["evidence_refs"]["remote_ial_usage"]["prompt_tokens"], 100)
        self.assertEqual(
            result["evidence_refs"]["runtime_usage"]["schema_version"],
            "amof.runtime_usage/v1",
        )

    def test_result_payload_absent_usage_is_unavailable_not_zero(self) -> None:
        selection = amof_native.AmofNativeBackendSelection(
            runner_id="amof-native-ticket-write",
            capabilities=["ticket_write"],
            writable_roots_relative=[],
            writable_roots_resolved=(),
            timeout_seconds=60,
            readable_root=None,
        )
        acc = runtime_usage.empty_usage_accumulator()
        acc["model_calls"] = 2
        result = amof_native._result_payload(
            run_id="run-2",
            status="completed",
            exit_code=0,
            stop_reason="completed",
            final_text="ok",
            studio_session_id=None,
            event_log_path=Path("/tmp/events.jsonl"),
            runtime_log_path=Path("/tmp/runtime.log"),
            changed_paths=[],
            selection=selection,
            health={},
            requested_model="x-ai/grok-4.5",
            effective_model="x-ai/grok-4.5",
            effective_provider="openrouter",
            transport="remote_ial",
            usage_acc=acc,
        )
        self.assertIsNone(result["usage"]["prompt_tokens"])
        self.assertIsNone(result["usage"]["completion_tokens"])
        self.assertEqual(result["usage"]["token_telemetry"], "unavailable")
        self.assertEqual(result["usage"]["model_calls"], 2)

    def test_retry_aggregation_sums_each_attempt_once(self) -> None:
        acc = runtime_usage.empty_usage_accumulator()
        runtime_usage.add_token_field(acc, "prompt_tokens", 10)
        runtime_usage.add_token_field(acc, "completion_tokens", 1)
        runtime_usage.add_token_field(acc, "prompt_tokens", 20)
        runtime_usage.add_token_field(acc, "completion_tokens", 2)
        self.assertEqual(acc["prompt_tokens"], 30)
        self.assertEqual(acc["completion_tokens"], 3)


class CursorUsageProjectionTests(unittest.TestCase):
    def test_result_payload_normalizes_sdk_usage(self) -> None:
        selection = cursor_agent.HermesBackendSelection(
            runner_id="cursor-agent-ticket-write",
            capabilities=["ticket_write"],
            writable_roots=[],
            timeout_seconds=60,
            readable_root=None,
        )
        result = cursor_agent._result_payload(
            run_id="cursor-run-1",
            status="completed",
            exit_code=0,
            stop_reason="completed",
            final_text="ok",
            studio_session_id=None,
            event_log_path=Path("/tmp/events.jsonl"),
            runtime_log_path=Path("/tmp/runtime.log"),
            changed_paths=[],
            selection=selection,
            health={},
            dispatch_probe={},
            requested_model="grok-4.5",
            effective_model="grok-4.5",
            substrate_agent_id="agent-1",
            substrate_run_id="run-1",
            sdk_envelope={
                "status": "finished",
                "usage": {
                    "input_tokens": 285602,
                    "output_tokens": 4554,
                    "total_tokens": 550572,
                },
            },
        )
        self.assertEqual(result["usage"]["prompt_tokens"], 285602)
        self.assertEqual(result["usage"]["completion_tokens"], 4554)
        self.assertEqual(result["usage"]["total_tokens"], 550572)
        self.assertIsNone(result["usage"]["cache_tokens"])  # cache_* not in extract
        self.assertEqual(result["usage"]["token_telemetry"], "partial")
        self.assertEqual(result["usage"]["agent_calls"], 1)
        self.assertIsNone(result["usage"]["model_calls"])
        self.assertIsNone(result["usage"]["tool_calls"])
        self.assertEqual(
            result["usage"]["telemetry_dimensions"]["TOKEN_TELEMETRY"], "PARTIAL"
        )
        self.assertEqual(
            result["usage"]["telemetry_dimensions"]["AGENT_TREE_TELEMETRY"],
            "UNAVAILABLE",
        )

    def test_ambiguous_absent_sdk_usage_stays_unavailable(self) -> None:
        selection = cursor_agent.HermesBackendSelection(
            runner_id="cursor-agent-ticket-write",
            capabilities=["ticket_write"],
            writable_roots=[],
            timeout_seconds=60,
            readable_root=None,
        )
        result = cursor_agent._result_payload(
            run_id="cursor-run-2",
            status="completed",
            exit_code=0,
            stop_reason="completed",
            final_text="ok",
            studio_session_id=None,
            event_log_path=Path("/tmp/events.jsonl"),
            runtime_log_path=Path("/tmp/runtime.log"),
            changed_paths=[],
            selection=selection,
            health={},
            dispatch_probe={},
            requested_model="grok-4.5",
            effective_model="grok-4.5",
            sdk_envelope={"status": "finished", "usage": None},
        )
        self.assertIsNone(result["usage"]["prompt_tokens"])
        self.assertEqual(result["usage"]["token_telemetry"], "unavailable")


if __name__ == "__main__":
    unittest.main()
