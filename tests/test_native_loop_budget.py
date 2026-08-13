"""Deterministic tests for amof.native_loop_budget/v1."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.execution_backends import native_loop_budget as lb
from amof.execution_backends.native_loop_budget import (
    LoopBudgetPolicy,
    LoopBudgetState,
    ProgressFingerprint,
    decide_extension,
    decide_readonly_synthesis,
    evaluate_progress,
    note_turn_complete,
    observe_tool_result,
    snapshot_fingerprint,
    termination_after_denied_extension,
)


def _fp(**kwargs) -> ProgressFingerprint:
    return ProgressFingerprint(**kwargs)


class NativeLoopBudgetPolicyTests(unittest.TestCase):
    def test_defaults_are_bounded(self) -> None:
        policy = lb.default_policy()
        self.assertEqual(policy.base_turn_limit, 12)
        self.assertEqual(policy.extension_increment, 3)
        self.assertEqual(policy.max_extension_count, 2)
        self.assertEqual(policy.absolute_turn_limit, 18)
        self.assertGreater(policy.absolute_turn_limit, policy.base_turn_limit)
        self.assertEqual(policy.policy_version, "native-loop-budget-v1.1")
        policy.validate()

    def test_unbounded_absolute_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LoopBudgetPolicy(base_turn_limit=12, absolute_turn_limit=100).validate()


class ProgressEvaluationTests(unittest.TestCase):
    def test_identical_is_no_progress(self) -> None:
        fp = _fp(write_digest="a", successful_write_count=1)
        ev = evaluate_progress(fp, copy.deepcopy(fp))
        self.assertEqual(ev.verdict, "NO_PROGRESS")

    def test_unknown_without_baseline(self) -> None:
        ev = evaluate_progress(_fp(write_digest="a"), None)
        self.assertEqual(ev.verdict, "UNKNOWN")

    def test_path_noise_only_is_no_progress(self) -> None:
        base = _fp(write_digest="w1", successful_write_count=1, path_noise_count=0)
        cur = _fp(
            write_digest="w1",
            successful_write_count=1,
            path_noise_count=3,
            failure_signature="noise",
            tool_outcome_signature="t2",
        )
        ev = evaluate_progress(cur, base)
        self.assertEqual(ev.verdict, "NO_PROGRESS")
        self.assertIn("path_noise_without_material_state_change", ev.evidence)

    def test_write_plus_shell_ok_is_material(self) -> None:
        base = _fp(write_digest="w0", successful_write_count=0, successful_shell_count=0)
        cur = _fp(
            write_digest="w1",
            successful_write_count=1,
            successful_shell_count=1,
            shell_exit_fingerprint="ok",
            phase="validation_ok",
        )
        ev = evaluate_progress(cur, base)
        self.assertEqual(ev.verdict, "MATERIAL_PROGRESS")

    def test_write_only_is_partial(self) -> None:
        base = _fp(write_digest="w0", successful_write_count=0)
        cur = _fp(write_digest="w1", successful_write_count=1, phase="implementation")
        ev = evaluate_progress(cur, base)
        self.assertEqual(ev.verdict, "PARTIAL_PROGRESS")

    def test_oscillation_is_no_progress(self) -> None:
        a = _fp(write_digest="a", successful_write_count=1)
        b = _fp(write_digest="b", successful_write_count=2)
        digests = [a.digest(), b.digest(), a.digest()]
        ev = evaluate_progress(a, b, recent_digests=digests)
        self.assertEqual(ev.verdict, "NO_PROGRESS")
        self.assertIn("oscillating_state_digest", ev.evidence)

    def test_failure_churn_without_write_is_no_progress(self) -> None:
        base = _fp(failure_signature="f1", failed_shell_count=1)
        cur = _fp(failure_signature="f2", failed_shell_count=2)
        ev = evaluate_progress(cur, base)
        self.assertEqual(ev.verdict, "NO_PROGRESS")


class EvidenceProgressTests(unittest.TestCase):
    def test_new_reads_across_targets_are_evidence_progress(self) -> None:
        keys: set[str] = set()
        base = ProgressFingerprint()
        cur = ProgressFingerprint()
        observe_tool_result(
            cur,
            name="read_file",
            arguments={"path": "00-amof/README.md"},
            output="# public",
            evidence_keys=keys,
        )
        observe_tool_result(
            cur,
            name="list_dir",
            arguments={"path": "01-amof-private"},
            output="README.md\n",
            evidence_keys=keys,
        )
        ev = evaluate_progress(cur, base)
        self.assertEqual(ev.verdict, "EVIDENCE_PROGRESS")
        self.assertEqual(cur.successful_evidence_count, 2)
        self.assertIn("new_successful_observation_coverage", ev.evidence)

    def test_repeated_same_reads_are_no_progress(self) -> None:
        keys: set[str] = set()
        fp = ProgressFingerprint()
        observe_tool_result(
            fp,
            name="read_file",
            arguments={"path": "00-amof/README.md"},
            output="# public",
            evidence_keys=keys,
        )
        observe_tool_result(
            fp,
            name="list_dir",
            arguments={"path": "00-amof"},
            output="README.md\n",
            evidence_keys=keys,
        )
        after_first = snapshot_fingerprint(fp)
        observe_tool_result(
            fp,
            name="read_file",
            arguments={"path": "00-amof/README.md"},
            output="# public",
            evidence_keys=keys,
        )
        observe_tool_result(
            fp,
            name="list_dir",
            arguments={"path": "00-amof"},
            output="README.md\n",
            evidence_keys=keys,
        )
        ev = evaluate_progress(fp, after_first)
        self.assertEqual(ev.verdict, "NO_PROGRESS")
        self.assertEqual(fp.successful_evidence_count, 2)
        self.assertEqual(fp.evidence_coverage_digest, after_first.evidence_coverage_digest)

    def test_tool_errors_are_not_evidence_progress(self) -> None:
        keys: set[str] = set()
        base = ProgressFingerprint()
        cur = ProgressFingerprint()
        observe_tool_result(
            cur,
            name="read_file",
            arguments={"path": "missing.md"},
            output="ERROR: read_file: not a file: missing.md",
            error="read_file: not a file: missing.md",
            evidence_keys=keys,
        )
        observe_tool_result(
            cur,
            name="list_dir",
            arguments={"path": "/run-work/files"},
            output="ERROR: absolute_path",
            error="list_dir: absolute_path",
            evidence_keys=keys,
        )
        ev = evaluate_progress(cur, base)
        self.assertEqual(ev.verdict, "NO_PROGRESS")
        self.assertEqual(cur.successful_evidence_count, 0)
        self.assertEqual(len(keys), 0)

    def test_readonly_base_with_evidence_requires_synthesis(self) -> None:
        state = LoopBudgetState(policy=lb.default_policy())
        keys = state.observed_evidence_keys
        observe_tool_result(
            state.fingerprint,
            name="read_file",
            arguments={"path": "00-amof/README.md"},
            output="# public",
            evidence_keys=keys,
        )
        outcome = decide_readonly_synthesis(state, at_turn=12)
        self.assertEqual(outcome, lb.SYNTHESIS_REQUIRED)
        self.assertTrue(state.synthesis_required)
        self.assertEqual(state.effective_turn_limit, 13)
        self.assertEqual(state.extension_count, 0)

    def test_readonly_base_without_evidence_fails_no_progress(self) -> None:
        state = LoopBudgetState(policy=lb.default_policy())
        observe_tool_result(
            state.fingerprint,
            name="read_file",
            arguments={"path": "package.json"},
            output="ERROR: read_file: not a file: package.json",
            error="read_file: not a file: package.json",
            evidence_keys=state.observed_evidence_keys,
        )
        outcome = decide_readonly_synthesis(state, at_turn=12)
        self.assertEqual(outcome, lb.STOP_BASE_NO_PROGRESS)
        self.assertFalse(state.synthesis_required)
        self.assertEqual(state.effective_turn_limit, 12)


class ExtensionDecisionTests(unittest.TestCase):
    def test_material_grants_bounded_extension(self) -> None:
        state = LoopBudgetState(policy=lb.default_policy())
        state.checkpoint_fingerprint = _fp(
            write_digest="w0", successful_write_count=0, successful_shell_count=0
        )
        state.fingerprint = _fp(
            write_digest="w1",
            successful_write_count=1,
            successful_shell_count=1,
            shell_exit_fingerprint="ok",
            phase="validation_ok",
        )
        decision = decide_extension(state, at_turn=12)
        self.assertTrue(decision.granted)
        self.assertEqual(decision.granted_turns, 3)
        self.assertEqual(state.effective_turn_limit, 15)
        self.assertEqual(state.extension_count, 1)

    def test_partial_alone_does_not_grant(self) -> None:
        state = LoopBudgetState(policy=lb.default_policy())
        state.checkpoint_fingerprint = _fp(write_digest="w0", successful_write_count=0)
        state.fingerprint = _fp(write_digest="w1", successful_write_count=1)
        decision = decide_extension(state, at_turn=12)
        self.assertFalse(decision.granted)
        self.assertEqual(state.effective_turn_limit, 12)
        self.assertEqual(
            termination_after_denied_extension(state),
            lb.STOP_BASE_NO_PROGRESS,
        )

    def test_unknown_does_not_grant(self) -> None:
        state = LoopBudgetState(policy=lb.default_policy())
        state.checkpoint_fingerprint = None
        state.fingerprint = _fp(write_digest="w1")
        decision = decide_extension(state, at_turn=12)
        self.assertFalse(decision.granted)
        self.assertEqual(decision.progress_verdict, "UNKNOWN")

    def test_stale_progress_reuse_denied(self) -> None:
        state = LoopBudgetState(policy=lb.default_policy())
        shared = _fp(
            write_digest="w1",
            successful_write_count=1,
            successful_shell_count=1,
            shell_exit_fingerprint="ok",
            phase="validation_ok",
        )
        state.checkpoint_fingerprint = copy.deepcopy(shared)
        state.fingerprint = copy.deepcopy(shared)
        decision = decide_extension(state, at_turn=12)
        self.assertFalse(decision.granted)

    def test_second_extension_requires_new_progress(self) -> None:
        state = LoopBudgetState(policy=lb.default_policy())
        state.checkpoint_fingerprint = _fp(
            write_digest="w0", successful_write_count=0, successful_shell_count=0
        )
        state.fingerprint = _fp(
            write_digest="w1",
            successful_write_count=1,
            successful_shell_count=1,
            shell_exit_fingerprint="ok1",
            phase="validation_ok",
        )
        first = decide_extension(state, at_turn=12)
        self.assertTrue(first.granted)
        # Same fingerprint as post-extension checkpoint → deny.
        second = decide_extension(state, at_turn=15)
        self.assertFalse(second.granted)
        # Fresh material progress → grant second extension.
        state.fingerprint = _fp(
            write_digest="w2",
            successful_write_count=2,
            successful_shell_count=2,
            shell_exit_fingerprint="ok2",
            phase="validation_ok",
        )
        third = decide_extension(state, at_turn=15)
        self.assertTrue(third.granted)
        self.assertEqual(state.effective_turn_limit, 18)
        self.assertEqual(state.extension_count, 2)
        # Max extensions exhausted.
        state.fingerprint = _fp(
            write_digest="w3",
            successful_write_count=3,
            successful_shell_count=3,
            shell_exit_fingerprint="ok3",
            phase="validation_ok",
        )
        fourth = decide_extension(state, at_turn=18)
        self.assertFalse(fourth.granted)
        self.assertIn("max_extension_count", fourth.reason)

    def test_absolute_ceiling_metadata(self) -> None:
        state = LoopBudgetState(policy=lb.default_policy())
        self.assertEqual(state.policy.absolute_turn_limit, 18)
        state.turns_used = 18
        state.extension_count = 2
        self.assertEqual(
            termination_after_denied_extension(state),
            lb.STOP_ABSOLUTE_TURN_LIMIT,
        )


class ObserveAndReplayTests(unittest.TestCase):
    def test_observe_write_and_shell(self) -> None:
        fp = ProgressFingerprint()
        observe_tool_result(
            fp,
            name="write_file",
            arguments={"path": "tests/a.mjs", "content": "ok"},
            output="wrote tests/a.mjs",
            grant_paths_digest="g1",
        )
        self.assertEqual(fp.successful_write_count, 1)
        observe_tool_result(
            fp,
            name="run_shell",
            arguments={"command": "node test.mjs"},
            output="exit_code=0\npass",
            grant_paths_digest="g1",
        )
        self.assertEqual(fp.successful_shell_count, 1)
        self.assertEqual(fp.phase, "validation_ok")

    def test_observe_path_noise(self) -> None:
        fp = ProgressFingerprint()
        observe_tool_result(
            fp,
            name="read_file",
            arguments={"path": "package.json"},
            output="ERROR: read_file: not a file: package.json",
            error="read_file: not a file: package.json",
        )
        self.assertGreaterEqual(fp.path_noise_count, 1)

    def test_deterministic_replay_same_decisions(self) -> None:
        def run_once() -> list[bool]:
            state = LoopBudgetState(policy=lb.default_policy())
            state.checkpoint_fingerprint = _fp(
                write_digest="w0", successful_write_count=0, successful_shell_count=0
            )
            decisions = []
            state.fingerprint = _fp(
                write_digest="w1",
                successful_write_count=1,
                successful_shell_count=1,
                shell_exit_fingerprint="ok",
                phase="validation_ok",
            )
            decisions.append(decide_extension(state, at_turn=12).granted)
            state.fingerprint = _fp(
                write_digest="w1",
                successful_write_count=1,
                successful_shell_count=1,
                shell_exit_fingerprint="ok",
                phase="validation_ok",
            )
            decisions.append(decide_extension(state, at_turn=15).granted)
            return decisions

        self.assertEqual(run_once(), run_once())
        self.assertEqual(run_once(), [True, False])

    def test_note_turn_sets_checkpoint_near_base(self) -> None:
        state = LoopBudgetState(policy=lb.default_policy())
        state.fingerprint = _fp(write_digest="near")
        note_turn_complete(state, 11)
        self.assertIsNotNone(state.checkpoint_fingerprint)
        self.assertEqual(state.checkpoint_fingerprint.write_digest, "near")


class HistoricalB5OfflineReplayTests(unittest.TestCase):
    """Offline fixture: late path-noise after earlier writes must not extend."""

    def test_b5_like_late_path_noise_denies_extension(self) -> None:
        state = LoopBudgetState(policy=lb.default_policy())
        # Near base: implementation present but focused test never green.
        state.checkpoint_fingerprint = _fp(
            write_digest="panel+test",
            successful_write_count=2,
            successful_shell_count=0,
            failed_shell_count=0,
            path_noise_count=0,
            phase="implementation",
        )
        # At turn 12: more path noise / exploratory reads, no validation gain.
        state.fingerprint = _fp(
            write_digest="panel+test",
            successful_write_count=2,
            successful_shell_count=0,
            failed_shell_count=0,
            path_noise_count=4,
            failure_signature="path",
            tool_outcome_signature="noise",
            phase="implementation",
        )
        decision = decide_extension(state, at_turn=12)
        self.assertFalse(decision.granted)
        self.assertEqual(
            termination_after_denied_extension(state),
            lb.STOP_BASE_NO_PROGRESS,
        )


class ModelSelfReportNotAuthorityTests(unittest.TestCase):
    def test_prose_claims_do_not_create_progress(self) -> None:
        # Fingerprint ignores model prose; empty tool evidence → NO_PROGRESS vs baseline empty.
        base = ProgressFingerprint()
        cur = ProgressFingerprint()
        ev = evaluate_progress(cur, base)
        self.assertEqual(ev.verdict, "NO_PROGRESS")


class IntegrationMockLoopTests(unittest.TestCase):
    """Drive _run_model_loop with mocked chat completions."""

    def _tool_message(self, name: str, arguments: dict, call_id: str = "c1") -> dict:
        import json

        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        }

    def test_completes_below_base_without_extension(self) -> None:
        from amof.execution_backends import amof_native

        responses = [
            {
                "choices": [{"message": {"role": "assistant", "content": "done", "tool_calls": None}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "x-ai/grok-4.5",
            }
        ]

        class Tools:
            enforcer = type("E", (), {"grant_roots": []})()
            repo_root = Path(".")

            def dispatch_tool(self, name, arguments):  # noqa: ANN001
                raise AssertionError("no tools expected")

        with patch.object(amof_native, "_chat_completion", side_effect=responses):
            out: dict = {}
            status, stop, _text = amof_native._run_model_loop(
                goal="finish quickly",
                tools=Tools(),  # type: ignore[arg-type]
                model="x-ai/grok-4.5",
                writable=False,
                event_log_path=Path("/tmp/amof-loop-budget-events.jsonl"),
                deadline=None,
                run_id="t-below-base",
                loop_budget_out=out,
            )
        self.assertEqual(status, "completed")
        self.assertEqual(stop, "completed")
        self.assertEqual(out.get("extension_count"), 0)
        self.assertEqual(out.get("turns_used"), 1)

    def test_no_progress_stops_at_base(self) -> None:
        from amof.execution_backends import amof_native

        # 12 tool turns with identical path-noise failure, then would want more.
        def make_noise(i: int) -> dict:
            return {
                "choices": [
                    {
                        "message": self._tool_message(
                            "read_file",
                            {"path": "package.json"},
                            call_id=f"c{i}",
                        )
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "x-ai/grok-4.5",
            }

        responses = [make_noise(i) for i in range(1, 13)]

        class Tools:
            enforcer = type(
                "E",
                (),
                {"grant_roots": []},
            )()
            repo_root = Path(".")

            def dispatch_tool(self, name, arguments):  # noqa: ANN001
                raise amof_native.AmofNativeBackendError(
                    "read_file: not a file: package.json"
                )

        event_path = Path("/tmp/amof-loop-budget-noprog.jsonl")
        event_path.write_text("", encoding="utf-8")
        with patch.object(amof_native, "_chat_completion", side_effect=responses):
            with patch.object(amof_native, "_grant_tree_digest", return_value="g0"):
                out: dict = {}
                status, stop, _text = amof_native._run_model_loop(
                    goal="loop noise",
                    tools=Tools(),  # type: ignore[arg-type]
                    model="x-ai/grok-4.5",
                    writable=False,
                    event_log_path=event_path,
                    deadline=None,
                    run_id="t-noprog",
                    loop_budget_out=out,
                )
        self.assertEqual(status, "failed")
        self.assertEqual(stop, lb.STOP_BASE_NO_PROGRESS)
        self.assertEqual(out.get("turns_used"), 12)
        self.assertEqual(out.get("extension_count"), 0)

    def test_material_progress_grants_extension_then_completes(self) -> None:
        from amof.execution_backends import amof_native

        responses = []
        for i in range(1, 12):
            responses.append(
                {
                    "choices": [
                        {
                            "message": self._tool_message(
                                "write_file",
                                {"path": "tests/t.mjs", "content": f"v{i}"},
                                call_id=f"w{i}",
                            )
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "model": "x-ai/grok-4.5",
                }
            )
        # Turn 12: write + successful shell (material vs turn-11 checkpoint).
        responses.append(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "w12",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": '{"path":"tests/t.mjs","content":"final"}',
                                    },
                                },
                                {
                                    "id": "s12",
                                    "type": "function",
                                    "function": {
                                        "name": "run_shell",
                                        "arguments": '{"command":"node tests/t.mjs"}',
                                    },
                                },
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "x-ai/grok-4.5",
            }
        )
        # Turn 13 (extension): complete.
        responses.append(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "fixed", "tool_calls": None}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "x-ai/grok-4.5",
            }
        )

        class Tools:
            enforcer = type("E", (), {"grant_roots": []})()
            repo_root = Path(".")

            def dispatch_tool(self, name, arguments):  # noqa: ANN001
                if name == "write_file":
                    return f"wrote {arguments.get('path')}"
                if name == "run_shell":
                    return "exit_code=0\nok"
                return "ok"

        event_path = Path("/tmp/amof-loop-budget-ext.jsonl")
        event_path.write_text("", encoding="utf-8")
        digests = [f"g{i}" for i in range(20)]

        with patch.object(amof_native, "_chat_completion", side_effect=responses):
            with patch.object(
                amof_native, "_grant_tree_digest", side_effect=digests
            ):
                out: dict = {}
                status, stop, _text = amof_native._run_model_loop(
                    goal="extend then finish",
                    tools=Tools(),  # type: ignore[arg-type]
                    model="x-ai/grok-4.5",
                    writable=True,
                    event_log_path=event_path,
                    deadline=None,
                    run_id="t-ext",
                    loop_budget_out=out,
                )
        self.assertEqual(status, "completed")
        self.assertEqual(stop, "completed")
        self.assertEqual(out.get("extension_count"), 1)
        self.assertEqual(out.get("turns_used"), 13)
        self.assertEqual(out.get("effective_turn_limit"), 15)

    def test_readonly_evidence_reaches_synthesis_then_completes(self) -> None:
        from amof.execution_backends import amof_native

        responses = []
        for i in range(1, 13):
            responses.append(
                {
                    "choices": [
                        {
                            "message": self._tool_message(
                                "read_file",
                                {"path": f"00-amof/file-{i}.md"},
                                call_id=f"r{i}",
                            )
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "model": "x-ai/grok-4.5",
                }
            )
        responses.append(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "bounded report from collected evidence",
                            "tool_calls": None,
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "x-ai/grok-4.5",
            }
        )

        class Tools:
            enforcer = type("E", (), {"grant_roots": []})()
            repo_root = Path(".")

            def dispatch_tool(self, name, arguments):  # noqa: ANN001
                return f"contents of {arguments.get('path')}"

        seen_tools: list[object] = []

        def _capture_chat(**kwargs):  # noqa: ANN003
            seen_tools.append(kwargs.get("tools"))
            return responses.pop(0)

        event_path = Path("/tmp/amof-loop-budget-synth.jsonl")
        event_path.write_text("", encoding="utf-8")
        with patch.object(amof_native, "_chat_completion", side_effect=_capture_chat):
            with patch.object(amof_native, "_grant_tree_digest", return_value="g0"):
                out: dict = {}
                status, stop, text = amof_native._run_model_loop(
                    goal="inspect both targets",
                    tools=Tools(),  # type: ignore[arg-type]
                    model="x-ai/grok-4.5",
                    writable=False,
                    event_log_path=event_path,
                    deadline=None,
                    run_id="t-synth",
                    loop_budget_out=out,
                )
        self.assertEqual(status, "completed")
        self.assertEqual(stop, "completed")
        self.assertIn("bounded report", text)
        self.assertTrue(out.get("synthesis_required"))
        self.assertTrue(out.get("synthesis_consumed"))
        self.assertEqual(out.get("extension_count"), 0)
        self.assertEqual(out.get("turns_used"), 13)
        self.assertEqual(out.get("effective_turn_limit"), 13)
        self.assertEqual(out.get("successful_evidence_count"), 12)
        self.assertEqual(seen_tools[-1], [])

    def test_readonly_synthesis_without_final_answer_fails_closed(self) -> None:
        from amof.execution_backends import amof_native

        responses = []
        for i in range(1, 13):
            responses.append(
                {
                    "choices": [
                        {
                            "message": self._tool_message(
                                "read_file",
                                {"path": f"00-amof/file-{i}.md"},
                                call_id=f"r{i}",
                            )
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "model": "x-ai/grok-4.5",
                }
            )
        responses.append(
            {
                "choices": [
                    {
                        "message": self._tool_message(
                            "read_file",
                            {"path": "00-amof/extra.md"},
                            call_id="r13",
                        )
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "x-ai/grok-4.5",
            }
        )

        class Tools:
            enforcer = type("E", (), {"grant_roots": []})()
            repo_root = Path(".")

            def dispatch_tool(self, name, arguments):  # noqa: ANN001
                return f"contents of {arguments.get('path')}"

        event_path = Path("/tmp/amof-loop-budget-synth-fail.jsonl")
        event_path.write_text("", encoding="utf-8")
        with patch.object(amof_native, "_chat_completion", side_effect=responses):
            with patch.object(amof_native, "_grant_tree_digest", return_value="g0"):
                out: dict = {}
                status, stop, _text = amof_native._run_model_loop(
                    goal="inspect then refuse to stop",
                    tools=Tools(),  # type: ignore[arg-type]
                    model="x-ai/grok-4.5",
                    writable=False,
                    event_log_path=event_path,
                    deadline=None,
                    run_id="t-synth-fail",
                    loop_budget_out=out,
                )
        self.assertEqual(status, "failed")
        self.assertEqual(stop, lb.STOP_SYNTHESIS_NOT_COMPLETED)
        self.assertNotEqual(stop, lb.STOP_BASE_NO_PROGRESS)
        self.assertTrue(out.get("synthesis_required"))
        self.assertEqual(out.get("extension_count"), 0)


if __name__ == "__main__":
    unittest.main()
