"""Wave 1 WriteScopeProposal identity store tests."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amof.commands import scope as scope_cmd
from amof.write_scope_proposals import (
    WriteScopeProposalError,
    build_proposal_record,
    compute_body_hash,
    compute_proposal_id,
    list_proposals,
    load_proposal,
    normalize_write_scope_body,
    persist_write_scope_proposals_from_result,
    proposal_path,
    save_proposal,
    verify_proposal_record,
)


BASE_SHA = "67f8526b254d8839c025423b6bfda36895881160"
TARGET_A = f"github_app:marekhotshot/simple-ai-shop:{BASE_SHA}"
TARGET_B = f"github_app:marekhotshot/other-repo:{BASE_SHA}"


def _valid_body(*, target_id: str = TARGET_A, reason: str = "docs-only follow-up") -> dict:
    return {
        "target_id": target_id,
        "base_sha": BASE_SHA,
        "allowed_roots": ["docs/launch-readiness/report.md"],
        "denied_roots": [],
        "reason": reason,
        "expected_checks": ["git diff --check"],
        "docs_only": True,
        "source_mutation": False,
    }


def _result_envelope(
    *,
    session_id: str = "run-wave1-001",
    proposal: dict | None = None,
    proposals: list[dict] | None = None,
    extra: dict | None = None,
) -> dict:
    payload: dict = {
        "result_kind": "agent_run_result",
        "contract_version": "agent-run-v1",
        "schema_version": 1,
        "status": "completed",
        "session_id": session_id,
        "exit_code": 0,
        "stop_reason": "completed",
        "final_text": "discovery complete",
        "plan_path": None,
        "checkpoint_path": None,
        "event_log_path": "/tmp/events.jsonl",
        "journal_path": None,
        "budget_summary": {"limit": None, "spent": 0, "remaining": None},
    }
    if proposal is not None:
        payload["write_scope_proposal"] = proposal
    if proposals is not None:
        payload["write_scope_proposals"] = proposals
    if extra:
        payload.update(extra)
    return payload


class WriteScopeProposalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.home = self._tmpdir.name
        self.store = Path(self.home) / "share" / "write-scopes" / "proposals"
        self.env = patch.dict(os.environ, {"AMOF_HOME": self.home}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_valid_single_proposal_persists_and_show(self) -> None:
        body = _valid_body()
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(proposal=body),
            base_dir=self.store,
        )
        self.assertEqual(len(outcome.persisted), 1)
        self.assertFalse(outcome.skipped_prose_only)
        record = outcome.persisted[0]
        self.assertEqual(record["status"], "proposed")
        self.assertEqual(record["run_id"], "run-wave1-001")
        self.assertTrue(record["proposal_id"].startswith("wsp-"))
        self.assertEqual(record["body_hash"], compute_body_hash(body))
        loaded = load_proposal(record["proposal_id"], base_dir=self.store)
        self.assertEqual(loaded["body"]["allowed_roots"], body["allowed_roots"])

        args = argparse.Namespace(
            scope_cmd="show",
            scope_id=record["proposal_id"],
            proposal_id=record["proposal_id"],
            json=True,
        )
        with patch("amof.commands.scope.load_proposal", side_effect=lambda pid: load_proposal(pid, base_dir=self.store)):
            code = scope_cmd.cmd_scope(args)
        self.assertEqual(code, 0)

    def test_multiple_proposals_from_one_run(self) -> None:
        bodies = [
            _valid_body(target_id=TARGET_A, reason="target a"),
            _valid_body(
                target_id=TARGET_B,
                reason="target b",
            ),
        ]
        bodies[1]["allowed_roots"] = ["docs/other.md"]
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(proposals=bodies, proposal=bodies[0]),
            base_dir=self.store,
        )
        self.assertEqual(len(outcome.persisted), 2)
        ids = {item["proposal_id"] for item in outcome.persisted}
        self.assertEqual(len(ids), 2)
        listed = list_proposals(run_id="run-wave1-001", base_dir=self.store)
        self.assertEqual(len(listed), 2)

    def test_malformed_proposal_rejected(self) -> None:
        bad = _valid_body()
        bad["allowed_roots"] = ["*"]  # wildcard — fail closed
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(proposal=bad),
            base_dir=self.store,
        )
        self.assertEqual(outcome.persisted, [])
        self.assertTrue(any(item["reason"] == "malformed_proposal" for item in outcome.rejected))
        self.assertEqual(list_proposals(base_dir=self.store), [])

    def test_prose_only_result_not_stored(self) -> None:
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(
                extra={"proposal_missing_reason": "runner returned prose only"},
            ),
            base_dir=self.store,
        )
        self.assertTrue(outcome.skipped_prose_only)
        self.assertEqual(outcome.persisted, [])
        self.assertEqual(list_proposals(base_dir=self.store), [])

    def test_duplicate_persistence_idempotent(self) -> None:
        body = _valid_body()
        first = persist_write_scope_proposals_from_result(
            _result_envelope(proposal=body),
            base_dir=self.store,
            created_at="2026-07-28T10:00:00Z",
        )
        second = persist_write_scope_proposals_from_result(
            _result_envelope(proposal=body),
            base_dir=self.store,
            created_at="2026-07-28T11:00:00Z",
        )
        self.assertEqual(len(first.persisted), 1)
        self.assertEqual(len(second.persisted), 1)
        self.assertEqual(first.persisted[0]["proposal_id"], second.persisted[0]["proposal_id"])
        self.assertEqual(second.persisted[0]["created_at"], "2026-07-28T10:00:00Z")
        self.assertEqual(len(list_proposals(base_dir=self.store)), 1)

    def test_body_hash_excludes_reason_and_detects_mutation(self) -> None:
        body_a = _valid_body(reason="first rationale")
        body_b = _valid_body(reason="different rationale")
        self.assertEqual(compute_body_hash(body_a), compute_body_hash(body_b))

        record = build_proposal_record(run_id="run-wave1-001", body=body_a)
        saved = save_proposal(record, base_dir=self.store)
        path = proposal_path(saved["proposal_id"], base_dir=self.store)
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["body"]["allowed_roots"] = ["docs/tampered.md"]
        path.write_text(json.dumps(tampered, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(WriteScopeProposalError) as ctx:
            load_proposal(saved["proposal_id"], base_dir=self.store)
        self.assertIn("body_hash mismatch", str(ctx.exception))

    def test_missing_parent_run_rejected(self) -> None:
        body = _valid_body()
        result = _result_envelope(proposal=body)
        result.pop("session_id")
        outcome = persist_write_scope_proposals_from_result(result, base_dir=self.store)
        self.assertEqual(outcome.persisted, [])
        self.assertTrue(
            any(item["reason"] == "missing_parent_run_id" for item in outcome.rejected)
        )

    def test_hostile_approved_write_scope_rejected(self) -> None:
        body = _valid_body()
        hostile_body = dict(body)
        hostile_body["approved_write_scope"] = {
            "allowed_roots": ["docs/"],
            "approved_by": "worker",
        }
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(
                proposal=hostile_body,
                extra={"approved_write_scope": {"allowed_roots": ["docs/"]}},
            ),
            base_dir=self.store,
        )
        self.assertEqual(outcome.persisted, [])
        reasons = {item["reason"] for item in outcome.rejected}
        self.assertIn("untrusted_approved_write_scope_claim", reasons)
        self.assertEqual(list_proposals(base_dir=self.store), [])

    def test_restart_persistence_reload_from_disk(self) -> None:
        body = _valid_body()
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(proposal=body),
            base_dir=self.store,
        )
        proposal_id = outcome.persisted[0]["proposal_id"]
        # Simulate process restart: new list/load against same on-disk store.
        reloaded = list_proposals(base_dir=self.store)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["proposal_id"], proposal_id)
        self.assertEqual(
            load_proposal(proposal_id, base_dir=self.store)["body"]["target_id"],
            TARGET_A,
        )

    def test_list_show_filtering_and_not_found(self) -> None:
        body_a = _valid_body(target_id=TARGET_A)
        body_b = _valid_body(target_id=TARGET_B)
        body_b["allowed_roots"] = ["docs/b.md"]
        persist_write_scope_proposals_from_result(
            _result_envelope(session_id="run-a", proposal=body_a),
            base_dir=self.store,
        )
        persist_write_scope_proposals_from_result(
            _result_envelope(session_id="run-b", proposal=body_b),
            base_dir=self.store,
        )
        only_a = list_proposals(run_id="run-a", base_dir=self.store)
        self.assertEqual(len(only_a), 1)
        self.assertEqual(only_a[0]["run_id"], "run-a")
        proposed = list_proposals(status="proposed", base_dir=self.store)
        self.assertEqual(len(proposed), 2)

        args_list = argparse.Namespace(
            scope_cmd="list",
            from_run="run-a",
            status="proposed",
            json=True,
        )
        with patch(
            "amof.commands.scope.list_proposals",
            side_effect=lambda **kwargs: list_proposals(base_dir=self.store, **kwargs),
        ):
            self.assertEqual(scope_cmd.cmd_scope(args_list), 0)

        args_missing = argparse.Namespace(
            scope_cmd="show",
            scope_id="wsp-000000000000000000000000",
            proposal_id="wsp-000000000000000000000000",
            json=False,
        )
        with patch(
            "amof.commands.scope.load_proposal",
            side_effect=lambda pid: load_proposal(pid, base_dir=self.store),
        ):
            code = scope_cmd.cmd_scope(args_missing)
        self.assertEqual(code, 1)

    def test_proposal_id_deterministic(self) -> None:
        body = normalize_write_scope_body(_valid_body())
        assert body is not None
        body_hash = compute_body_hash(body)
        first = compute_proposal_id(run_id="run-x", body_hash=body_hash)
        second = compute_proposal_id(run_id="run-x", body_hash=body_hash)
        other_run = compute_proposal_id(run_id="run-y", body_hash=body_hash)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other_run)
        self.assertRegex(first, r"^wsp-[0-9a-f]{24}$")

    def test_verify_rejects_mutated_body_in_memory(self) -> None:
        record = build_proposal_record(run_id="run-wave1-001", body=_valid_body())
        record["body"]["allowed_roots"] = ["docs/mutated.md"]
        with self.assertRaises(WriteScopeProposalError):
            verify_proposal_record(record)

    def test_terminal_result_hook_persists_proposals(self) -> None:
        from amof.execution_backends import hermes_opensandbox

        run_dir = Path(self.home) / "share" / "runs" / "hermes-opensandbox" / "hook-run"
        run_dir.mkdir(parents=True)
        result_path = run_dir / "result.json"
        event_log_path = run_dir / "events.jsonl"
        runtime_log_path = run_dir / "runtime.log"
        event_log_path.write_text("", encoding="utf-8")
        body = _valid_body()
        result = _result_envelope(session_id="hook-run", proposals=[body], proposal=body)
        written = hermes_opensandbox._write_terminal_result(
            result_path=result_path,
            event_log_path=event_log_path,
            runtime_log_path=runtime_log_path,
            result=result,
            reason="completed",
            started_at="2026-07-28T12:00:00Z",
        )
        self.assertEqual(written["status"], "completed")
        listed = list_proposals(run_id="hook-run")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["body"]["target_id"], TARGET_A)


if __name__ == "__main__":
    unittest.main()
