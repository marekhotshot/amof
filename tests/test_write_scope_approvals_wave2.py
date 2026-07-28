"""Wave 2 WriteScopeApproval + Revocation + TTL tests."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from amof.commands import scope as scope_cmd
from amof.write_scope_approvals import (
    LIFECYCLE_TRANSITIONS,
    STATUS_APPROVED,
    STATUS_CONSUMED,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    WriteScopeApprovalError,
    approve_proposal,
    evaluate_expiry,
    is_approval_active,
    list_approvals,
    load_approval,
    parse_ttl_duration,
    path_is_under_docs,
    reject_worker_minted_approval,
    revoke_approval,
    transition_approval_status,
    validate_approval_body,
)
from amof.write_scope_proposals import (
    persist_write_scope_proposals_from_result,
    proposal_path,
)


BASE_SHA = "67f8526b254d8839c025423b6bfda36895881160"
TARGET_A = f"github_app:marekhotshot/simple-ai-shop:{BASE_SHA}"
OPERATOR = "operator:marek"


def _valid_body(
    *,
    target_id: str = TARGET_A,
    reason: str = "docs-only follow-up",
    allowed_roots: list[str] | None = None,
    denied_roots: list[str] | None = None,
    docs_only: bool = True,
    source_mutation: bool = False,
) -> dict:
    return {
        "target_id": target_id,
        "base_sha": BASE_SHA,
        "allowed_roots": allowed_roots or ["docs/launch-readiness/report.md"],
        "denied_roots": denied_roots if denied_roots is not None else [],
        "reason": reason,
        "expected_checks": ["git diff --check"],
        "docs_only": docs_only,
        "source_mutation": source_mutation,
    }


def _result_envelope(
    *,
    session_id: str = "run-wave2-001",
    proposal: dict | None = None,
) -> dict:
    return {
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
        "write_scope_proposal": proposal,
    }


class WriteScopeApprovalWave2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.home = self._tmpdir.name
        self.proposals = Path(self.home) / "share" / "write-scopes" / "proposals"
        self.approvals = Path(self.home) / "share" / "write-scopes" / "approvals"
        self.revocations = Path(self.home) / "share" / "write-scopes" / "revocations"
        self.events = Path(self.home) / "share" / "write-scopes" / "events"
        self.env = patch.dict(os.environ, {"AMOF_HOME": self.home}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _persist_proposal(self, body: dict | None = None, *, run_id: str = "run-wave2-001") -> dict:
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(session_id=run_id, proposal=body or _valid_body()),
            base_dir=self.proposals,
        )
        self.assertEqual(len(outcome.persisted), 1)
        return outcome.persisted[0]

    def _approve(self, proposal_id: str, *, ttl: str = "2h", **kwargs) -> dict:
        return approve_proposal(
            proposal_id,
            ttl=ttl,
            approved_by=kwargs.pop("approved_by", OPERATOR),
            proposals_dir=self.proposals,
            approvals_dir=self.approvals,
            events_dir=self.events,
            **kwargs,
        )

    def test_approve_valid_proposal(self) -> None:
        proposal = self._persist_proposal()
        approval = self._approve(proposal["proposal_id"], ttl="2h")
        self.assertEqual(approval["status"], STATUS_APPROVED)
        self.assertEqual(approval["proposal_id"], proposal["proposal_id"])
        self.assertEqual(approval["body_hash"], proposal["body_hash"])
        self.assertEqual(approval["provenance"], "operator_asserted")
        self.assertTrue(approval["approval_id"].startswith("wsa-"))
        self.assertTrue(is_approval_active(approval))
        loaded = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        self.assertEqual(loaded["approval_id"], approval["approval_id"])
        events = (self.events / "write-scope-events.jsonl").read_text(encoding="utf-8")
        self.assertIn("write_scope.approved", events)

    def test_unknown_proposal(self) -> None:
        with self.assertRaises(WriteScopeApprovalError) as ctx:
            self._approve("wsp-000000000000000000000000")
        self.assertIn("proposal not found", str(ctx.exception))

    def test_hash_mismatch(self) -> None:
        proposal = self._persist_proposal()
        path = proposal_path(proposal["proposal_id"], base_dir=self.proposals)
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["body"]["allowed_roots"] = ["docs/tampered.md"]
        path.write_text(json.dumps(tampered, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(WriteScopeApprovalError) as ctx:
            self._approve(proposal["proposal_id"])
        self.assertIn("body_hash mismatch", str(ctx.exception))

    def test_invalid_ttl(self) -> None:
        proposal = self._persist_proposal()
        for bad in ("", "0h", "2", "30m1h", "1x", "-2h", "2.5h"):
            with self.assertRaises(WriteScopeApprovalError):
                parse_ttl_duration(bad) if bad != "" else parse_ttl_duration("")
        with self.assertRaises(WriteScopeApprovalError):
            self._approve(proposal["proposal_id"], ttl="2")
        # Accepted forms
        self.assertEqual(parse_ttl_duration("30s"), timedelta(seconds=30))
        self.assertEqual(parse_ttl_duration("30m"), timedelta(minutes=30))
        self.assertEqual(parse_ttl_duration("2h"), timedelta(hours=2))
        self.assertEqual(parse_ttl_duration("1d"), timedelta(days=1))
        self.assertEqual(parse_ttl_duration("1h30m"), timedelta(hours=1, minutes=30))

    def test_immediate_expiry(self) -> None:
        proposal = self._persist_proposal()
        approved_at = "2026-07-28T10:00:00Z"
        # Grant that is already past expiry relative to `now` is refused.
        with self.assertRaises(WriteScopeApprovalError) as ctx:
            self._approve(
                proposal["proposal_id"],
                ttl="1s",
                approved_at=approved_at,
                now=datetime(2026, 7, 28, 10, 0, 5, tzinfo=timezone.utc),
            )
        self.assertIn("expired at grant time", str(ctx.exception))

        approval = self._approve(
            proposal["proposal_id"],
            ttl="1s",
            approved_at=approved_at,
            now=datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        expired = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
            now=datetime(2026, 7, 28, 10, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(expired["status"], STATUS_EXPIRED)
        self.assertFalse(is_approval_active(expired))

    def test_restart_then_expiry(self) -> None:
        proposal = self._persist_proposal()
        approval = self._approve(
            proposal["proposal_id"],
            ttl="1h",
            approved_at="2026-07-28T10:00:00Z",
            now=datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        approval_id = approval["approval_id"]
        # Simulate restart: new load against same durable store with later clock.
        reloaded = load_approval(
            approval_id,
            approvals_dir=self.approvals,
            events_dir=self.events,
            now=datetime(2026, 7, 28, 11, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(reloaded["status"], STATUS_EXPIRED)
        listed = list_approvals(
            status=STATUS_EXPIRED,
            approvals_dir=self.approvals,
            events_dir=self.events,
            now=datetime(2026, 7, 28, 11, 5, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["approval_id"], approval_id)

    def test_revoke_approved(self) -> None:
        proposal = self._persist_proposal()
        approval = self._approve(proposal["proposal_id"])
        updated, revocation, already = revoke_approval(
            approval["approval_id"],
            reason="operator abort",
            revoked_by=OPERATOR,
            approvals_dir=self.approvals,
            revocations_dir=self.revocations,
            events_dir=self.events,
        )
        self.assertFalse(already)
        self.assertEqual(updated["status"], STATUS_REVOKED)
        self.assertEqual(revocation["approval_id"], approval["approval_id"])
        self.assertEqual(revocation["reason"], "operator abort")
        self.assertFalse(is_approval_active(updated))
        events = (self.events / "write-scope-events.jsonl").read_text(encoding="utf-8")
        self.assertIn("write_scope.revoked", events)

    def test_revoke_revoked_idempotent(self) -> None:
        proposal = self._persist_proposal()
        approval = self._approve(proposal["proposal_id"])
        first, rev1, already1 = revoke_approval(
            approval["approval_id"],
            reason="first",
            revoked_by=OPERATOR,
            approvals_dir=self.approvals,
            revocations_dir=self.revocations,
            events_dir=self.events,
            revoked_at="2026-07-28T12:00:00Z",
        )
        second, rev2, already2 = revoke_approval(
            approval["approval_id"],
            reason="second different reason",
            revoked_by=OPERATOR,
            approvals_dir=self.approvals,
            revocations_dir=self.revocations,
            events_dir=self.events,
            revoked_at="2026-07-28T13:00:00Z",
        )
        self.assertFalse(already1)
        self.assertTrue(already2)
        self.assertEqual(rev1["revocation_id"], rev2["revocation_id"])
        self.assertEqual(rev2["reason"], "first")  # original retained
        self.assertEqual(rev2["revoked_at"], "2026-07-28T12:00:00Z")
        self.assertEqual(second["status"], STATUS_REVOKED)
        events = (self.events / "write-scope-events.jsonl").read_text(encoding="utf-8")
        self.assertIn("write_scope.revoke_idempotent", events)

    def test_approve_malformed_body(self) -> None:
        # Hand-written on-disk proposal that fails verification / approve.
        proposal_id = "wsp-" + ("ab" * 12)
        path = self.proposals / f"{proposal_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "kind": "write_scope_proposal",
                    "schema_version": 1,
                    "proposal_id": proposal_id,
                    "run_id": "run-bad",
                    "body": {
                        "target_id": TARGET_A,
                        "base_sha": BASE_SHA,
                        "allowed_roots": [],
                        "denied_roots": [],
                        "reason": "empty roots",
                        "expected_checks": [],
                        "docs_only": True,
                        "source_mutation": False,
                    },
                    "body_hash": "sha256:" + ("0" * 64),
                    "created_at": "2026-07-28T10:00:00Z",
                    "status": "proposed",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(WriteScopeApprovalError):
            self._approve(proposal_id)

    def test_worker_cannot_mint_approval(self) -> None:
        proposal = self._persist_proposal()
        with self.assertRaises(WriteScopeApprovalError):
            reject_worker_minted_approval({"approved_by": "worker"})
        with self.assertRaises(WriteScopeApprovalError) as ctx:
            self._approve(proposal["proposal_id"], approved_by="worker")
        self.assertIn("worker", str(ctx.exception).lower())
        with self.assertRaises(WriteScopeApprovalError):
            approve_proposal(
                proposal["proposal_id"],
                ttl="1h",
                approved_by=OPERATOR,
                provenance="worker_asserted",
                proposals_dir=self.proposals,
                approvals_dir=self.approvals,
                events_dir=self.events,
            )
        self.assertEqual(list_approvals(approvals_dir=self.approvals), [])

    def test_docs_only_conflict(self) -> None:
        body = _valid_body(
            docs_only=True,
            allowed_roots=["src/main.py"],
            source_mutation=False,
        )
        proposal = self._persist_proposal(body)
        with self.assertRaises(WriteScopeApprovalError) as ctx:
            self._approve(proposal["proposal_id"])
        self.assertIn("docs_only", str(ctx.exception))

    def test_path_traversal_rejected(self) -> None:
        with self.assertRaises(WriteScopeApprovalError):
            validate_approval_body(
                _valid_body(allowed_roots=["docs/../../etc/passwd"])
            )
        with self.assertRaises(WriteScopeApprovalError):
            validate_approval_body(_valid_body(allowed_roots=["../secrets"]))
        self.assertTrue(path_is_under_docs("docs/a.md"))
        self.assertFalse(path_is_under_docs("src/a.py"))

    def test_wildcard_roots_rejected(self) -> None:
        with self.assertRaises(WriteScopeApprovalError):
            validate_approval_body(_valid_body(allowed_roots=["docs/*"]))
        with self.assertRaises(WriteScopeApprovalError):
            validate_approval_body(_valid_body(allowed_roots=["**/*.md"]))

    def test_state_transition_integrity(self) -> None:
        # Table shape
        self.assertEqual(LIFECYCLE_TRANSITIONS[None], frozenset({STATUS_APPROVED}))
        self.assertIn(STATUS_REVOKED, LIFECYCLE_TRANSITIONS[STATUS_APPROVED])
        self.assertIn(STATUS_EXPIRED, LIFECYCLE_TRANSITIONS[STATUS_APPROVED])
        self.assertIn(STATUS_CONSUMED, LIFECYCLE_TRANSITIONS[STATUS_APPROVED])
        for terminal in (STATUS_REVOKED, STATUS_EXPIRED, STATUS_CONSUMED):
            self.assertEqual(LIFECYCLE_TRANSITIONS[terminal], frozenset())

        proposal = self._persist_proposal()
        approval = self._approve(proposal["proposal_id"], ttl="1h")

        # consumed rejected in Wave 2
        with self.assertRaises(WriteScopeApprovalError) as ctx:
            transition_approval_status(
                approval["approval_id"],
                STATUS_CONSUMED,
                approvals_dir=self.approvals,
                events_dir=self.events,
            )
        self.assertIn("reserved for Wave 3/4", str(ctx.exception))

        # cannot re-activate
        with self.assertRaises(WriteScopeApprovalError):
            transition_approval_status(
                approval["approval_id"],
                STATUS_APPROVED,
                approvals_dir=self.approvals,
                events_dir=self.events,
            )

        revoke_approval(
            approval["approval_id"],
            reason="stop",
            revoked_by=OPERATOR,
            approvals_dir=self.approvals,
            revocations_dir=self.revocations,
            events_dir=self.events,
        )
        # revoked cannot become approved / expired / consumed
        with self.assertRaises(WriteScopeApprovalError):
            transition_approval_status(
                approval["approval_id"],
                STATUS_APPROVED,
                approvals_dir=self.approvals,
                events_dir=self.events,
            )
        # idempotent revoke returns without illegal transition
        _, _, already = revoke_approval(
            approval["approval_id"],
            reason="again",
            revoked_by=OPERATOR,
            approvals_dir=self.approvals,
            revocations_dir=self.revocations,
            events_dir=self.events,
        )
        self.assertTrue(already)

        # expired cannot reactivate
        proposal2 = self._persist_proposal(
            _valid_body(reason="second proposal"),
            run_id="run-wave2-002",
        )
        approval2 = self._approve(
            proposal2["proposal_id"],
            ttl="1s",
            approved_at="2026-07-28T10:00:00Z",
            now=datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        expired = evaluate_expiry(
            approval2,
            now=datetime(2026, 7, 28, 10, 0, 2, tzinfo=timezone.utc),
            persist=True,
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        self.assertEqual(expired["status"], STATUS_EXPIRED)
        with self.assertRaises(WriteScopeApprovalError):
            transition_approval_status(
                approval2["approval_id"],
                STATUS_APPROVED,
                approvals_dir=self.approvals,
                events_dir=self.events,
                now=datetime(2026, 7, 28, 10, 0, 2, tzinfo=timezone.utc),
            )
        with self.assertRaises(WriteScopeApprovalError) as ctx:
            revoke_approval(
                approval2["approval_id"],
                reason="too late",
                revoked_by=OPERATOR,
                approvals_dir=self.approvals,
                revocations_dir=self.revocations,
                events_dir=self.events,
                now=datetime(2026, 7, 28, 10, 0, 2, tzinfo=timezone.utc),
            )
        self.assertIn("terminal status", str(ctx.exception))

    def test_cli_approve_revoke_show_list(self) -> None:
        proposal = self._persist_proposal()

        approve_args = argparse.Namespace(
            scope_cmd="approve",
            proposal_id=proposal["proposal_id"],
            ttl="2h",
            approved_by=OPERATOR,
            json=True,
        )
        with patch(
            "amof.commands.scope.approve_proposal",
            side_effect=lambda pid, **kwargs: approve_proposal(
                pid,
                proposals_dir=self.proposals,
                approvals_dir=self.approvals,
                events_dir=self.events,
                **kwargs,
            ),
        ):
            self.assertEqual(scope_cmd.cmd_scope(approve_args), 0)

        approvals = list_approvals(approvals_dir=self.approvals, events_dir=self.events)
        self.assertEqual(len(approvals), 1)
        approval_id = approvals[0]["approval_id"]

        show_args = argparse.Namespace(
            scope_cmd="show",
            scope_id=approval_id,
            json=True,
        )
        with patch(
            "amof.commands.scope.load_approval",
            side_effect=lambda aid: load_approval(
                aid, approvals_dir=self.approvals, events_dir=self.events
            ),
        ):
            self.assertEqual(scope_cmd.cmd_scope(show_args), 0)

        list_args = argparse.Namespace(
            scope_cmd="list",
            from_run=None,
            status="approved",
            json=True,
        )
        with patch(
            "amof.commands.scope.list_approvals",
            side_effect=lambda **kwargs: list_approvals(
                approvals_dir=self.approvals, events_dir=self.events, **kwargs
            ),
        ):
            self.assertEqual(scope_cmd.cmd_scope(list_args), 0)

        revoke_args = argparse.Namespace(
            scope_cmd="revoke",
            approval_id=approval_id,
            reason="cli abort",
            revoked_by=OPERATOR,
            json=True,
        )
        with patch(
            "amof.commands.scope.revoke_approval",
            side_effect=lambda aid, **kwargs: revoke_approval(
                aid,
                approvals_dir=self.approvals,
                revocations_dir=self.revocations,
                events_dir=self.events,
                **kwargs,
            ),
        ):
            self.assertEqual(scope_cmd.cmd_scope(revoke_args), 0)

    def test_deny_roots_retained_for_deny_wins(self) -> None:
        body = _valid_body(
            allowed_roots=["docs/"],
            denied_roots=["docs/secret.md"],
            docs_only=True,
        )
        proposal = self._persist_proposal(body)
        approval = self._approve(proposal["proposal_id"])
        self.assertEqual(approval["body"]["denied_roots"], ["docs/secret.md"])
        self.assertEqual(approval["body"]["allowed_roots"], ["docs/"])


if __name__ == "__main__":
    unittest.main()
