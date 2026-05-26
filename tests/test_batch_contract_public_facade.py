from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"

if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.api.run_manager import RUN_STATUS_PAUSED, RUN_STATUS_QUEUED, RunManager
from amof.orchestrator import batch_contract
from amof.orchestrator.batch_contract import (
    BatchItemContract,
    BatchItemEvaluationResult,
    BatchManifestContract,
    FORCED_CHOICE_OPTIONS,
    MeaningfulDeltaResult,
    ScopeContract,
    ScopeGateResult,
    evaluate_batch_item,
    scope_gate_item,
)
from amof.queue import QUEUE_STATUS_PAUSED, QUEUE_STATUS_PENDING, QueueDispatcher, QueueStore
from amof.queue import dispatcher as queue_dispatcher_module


def _manifest_payload() -> dict:
    return {
        "batch_id": "batch-1",
        "runtime_mode": "batch",
        "max_items": 2,
        "max_exhausted_items": 1,
        "pause_on_exhaustion": True,
        "items": [
            {
                "id": "item-1",
                "prompt": "Do bounded work",
                "max_attempts": 2,
                "scope": {
                    "read_files": ["README.md"],
                    "write_files": ["scratch/result.txt"],
                    "allowed_commands": ["python3 -m unittest"],
                },
            }
        ],
    }


def _run_payload(**loop_state: object) -> dict:
    return {
        "loop_state": {
            "latest_evidence": {},
            "worker_state": {},
            **loop_state,
        }
    }


class BatchContractPublicFacadeTests(unittest.TestCase):
    def test_dispatcher_imports_batch_contract_facade_symbols(self) -> None:
        self.assertIs(queue_dispatcher_module.BatchManifestContract, BatchManifestContract)
        self.assertIs(queue_dispatcher_module.evaluate_batch_item, evaluate_batch_item)
        self.assertIs(queue_dispatcher_module.scope_gate_item, scope_gate_item)
        self.assertEqual(
            tuple(queue_dispatcher_module.FORCED_CHOICE_OPTIONS),
            ("resume_next", "retry_current", "handoff", "stop"),
        )
        for name in (
            "BatchManifestContract",
            "BatchItemContract",
            "ScopeContract",
            "ScopeGateResult",
            "MeaningfulDeltaResult",
            "BatchItemEvaluationResult",
            "evaluate_batch_item",
            "scope_gate_item",
        ):
            self.assertTrue(hasattr(batch_contract, name), name)

    def test_manifest_parsing_and_envelope_keys_are_stable(self) -> None:
        manifest = BatchManifestContract.from_dict(_manifest_payload())

        self.assertEqual(manifest.batch_id, "batch-1")
        self.assertEqual(manifest.runtime_mode, "headless_batch")
        self.assertEqual(manifest.max_items, 2)
        self.assertEqual(len(manifest.items), 1)

        manifest_payload = manifest.to_dict()
        self.assertEqual(
            set(manifest_payload),
            {"batch_id", "runtime_mode", "max_items", "max_exhausted_items", "pause_on_exhaustion", "items"},
        )
        self.assertEqual(
            set(manifest_payload["items"][0]),
            {"id", "prompt", "scope", "max_attempts"},
        )
        self.assertEqual(
            set(manifest_payload["items"][0]["scope"]),
            {"read_files", "write_files", "allowed_commands"},
        )

    def test_manifest_validation_errors_remain_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing batch_id"):
            BatchManifestContract.from_dict({"items": []})
        with self.assertRaisesRegex(ValueError, "at least one item"):
            BatchManifestContract.from_dict({"batch_id": "batch-1", "items": []})
        with self.assertRaisesRegex(ValueError, "more items than max_items"):
            payload = _manifest_payload()
            payload["items"] = [payload["items"][0], {**payload["items"][0], "id": "item-2"}]
            payload["max_items"] = 1
            BatchManifestContract.from_dict(payload)
        with self.assertRaisesRegex(ValueError, "Unsupported runtime mode"):
            BatchManifestContract.from_dict({**_manifest_payload(), "runtime_mode": "private-mode"})

    def test_scope_gate_normalizes_and_blocks_public_boundary_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            scope = ScopeContract(
                read_files=["src/../README.md", str(root / "src" / "input.py")],
                write_files=["scratch/output.txt"],
                allowed_commands=["python3 -m unittest"],
            )

            allowed = scope_gate_item(scope, workspace_root=root)
            self.assertTrue(allowed.allowed)
            self.assertIsNone(allowed.reason)
            self.assertEqual(
                set(allowed.to_dict()),
                {"allowed", "reason", "normalized_scope", "blocked_paths"},
            )
            self.assertEqual(allowed.normalized_scope["read_files"], ["README.md", "src/input.py"])

            blocked = scope_gate_item(
                scope,
                workspace_root=root,
                no_touch_paths=["scratch"],
            )
            self.assertFalse(blocked.allowed)
            self.assertEqual(blocked.reason, "no_touch_path")
            self.assertEqual(blocked.blocked_paths[0], {"path": "scratch/output.txt", "reason": "no_touch_path"})

            with self.assertRaisesRegex(ValueError, "resolves outside workspace root"):
                scope_gate_item(
                    ScopeContract(read_files=["../outside.txt"], write_files=["scratch/output.txt"], allowed_commands=[]),
                    workspace_root=root,
                )

    def test_batch_evaluation_result_keys_and_fail_closed_pause_behavior(self) -> None:
        item = BatchItemContract.from_dict(_manifest_payload()["items"][0])

        retry_result = evaluate_batch_item(
            item,
            attempts_used=1,
            last_child_run_id="child-1",
            run_payload=_run_payload(),
        )
        self.assertIsInstance(retry_result, BatchItemEvaluationResult)
        self.assertEqual(retry_result.decision, "pause_for_choice")
        self.assertEqual(retry_result.pause_mode, "forced_choice")
        self.assertEqual(retry_result.required_choice, "retry_current")
        self.assertEqual(tuple(retry_result.forced_choices), FORCED_CHOICE_OPTIONS)
        self.assertFalse(retry_result.exhausted)
        self.assertEqual(
            set(retry_result.to_dict()),
            {"decision", "pause_mode", "required_choice", "forced_choices", "exhausted", "meaningful_delta", "handoff"},
        )
        self.assertEqual(
            set(retry_result.meaningful_delta.to_dict()),
            {"meaningful", "reason", "signals"},
        )

        handoff_result = evaluate_batch_item(
            item,
            attempts_used=2,
            last_child_run_id="child-2",
            run_payload=_run_payload(stop_reason="no_progress"),
        )
        self.assertEqual(handoff_result.decision, "pause_for_choice")
        self.assertEqual(handoff_result.required_choice, "handoff")
        self.assertTrue(handoff_result.exhausted)
        self.assertIsInstance(handoff_result.meaningful_delta, MeaningfulDeltaResult)
        self.assertEqual(handoff_result.handoff["recommended_next_choice"], "handoff")
        self.assertEqual(handoff_result.handoff["last_child_run_id"], "child-2")

    def test_batch_evaluation_continues_only_on_meaningful_delta(self) -> None:
        item = BatchItemContract.from_dict(_manifest_payload()["items"][0])

        result = evaluate_batch_item(
            item,
            attempts_used=1,
            last_child_run_id="child-1",
            run_payload=_run_payload(worker_state={"files_touched": ["scratch/result.txt"]}),
        )

        self.assertEqual(result.decision, "continue")
        self.assertIsNone(result.pause_mode)
        self.assertIsNone(result.required_choice)
        self.assertEqual(result.forced_choices, [])
        self.assertFalse(result.exhausted)
        self.assertTrue(result.meaningful_delta.meaningful)
        self.assertIsNone(result.handoff)

    def test_dispatcher_resume_and_handoff_status_fields_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_store = QueueStore(Path(tmpdir) / "queue")
            run_manager = RunManager(queue_store=queue_store)
            dispatcher = QueueDispatcher(run_manager=run_manager, queue_store=queue_store)

            run_id = run_manager.create_run(
                "amof-dev",
                "batch",
                ["amof", "batch", "..."],
                queue_payload={"kind": "batch", "batch_manifest": _manifest_payload()},
            )
            paused_state = {
                "run_id": run_id,
                "task_id": run_id,
                "queue_state": "paused",
                "loop_state": "running",
                "loop_step": 1,
                "current_goal": "batch-1",
                "decision": "STOP",
                "stop_reason": "batch_item_not_meaningful",
                "runtime_mode": "headless_batch",
                "batch_id": "batch-1",
                "batch_state": "paused_for_choice",
                "batch_cursor": 0,
                "current_item_id": "item-1",
                "current_child_run_id": "child-1",
                "child_run_ids": ["child-1"],
                "completed_items": [],
                "pause_mode": "forced_choice",
                "required_choice": "handoff",
                "forced_choices": list(FORCED_CHOICE_OPTIONS),
                "handoff": {"item_id": "item-1", "recommended_next_choice": "handoff"},
            }
            run_manager.update_loop_state(run_id, paused_state)
            run_manager.update_status(run_id, RUN_STATUS_PAUSED)

            handoff_item = dispatcher.resume_task(run_id, choice="handoff")
            handoff_run = run_manager.get_run(run_id)
            self.assertEqual(handoff_item.status, QUEUE_STATUS_PAUSED)
            self.assertEqual(handoff_run.status, RUN_STATUS_PAUSED)
            self.assertEqual(handoff_run.loop_state["batch_state"], "handoff_requested")
            self.assertEqual(handoff_run.loop_state["selected_choice"], "handoff")
            self.assertEqual(handoff_run.loop_state["forced_choices"], [])
            self.assertEqual(handoff_run.loop_state["latest_evidence"]["handoff"]["item_id"], "item-1")

            retry_run_id = run_manager.create_run(
                "amof-dev",
                "batch",
                ["amof", "batch", "..."],
                queue_payload={"kind": "batch", "batch_manifest": _manifest_payload()},
            )
            retry_state = {**paused_state, "run_id": retry_run_id, "task_id": retry_run_id}
            run_manager.update_loop_state(retry_run_id, retry_state)
            run_manager.update_status(retry_run_id, RUN_STATUS_PAUSED)

            retry_item = dispatcher.resume_task(retry_run_id, choice="retry_current")
            retry_run = run_manager.get_run(retry_run_id)
            self.assertEqual(retry_item.status, QUEUE_STATUS_PENDING)
            self.assertEqual(retry_item.control["resume_choice"], "retry_current")
            self.assertEqual(retry_run.status, RUN_STATUS_QUEUED)
            self.assertEqual(retry_run.loop_state["batch_state"], "resume_requested")
            self.assertEqual(retry_run.loop_state["selected_choice"], "retry_current")
            self.assertIsNone(retry_run.loop_state["pause_mode"])
            self.assertEqual(retry_run.loop_state["forced_choices"], [])


if __name__ == "__main__":
    unittest.main()
