"""Wave 5 Write-Scope audit, recover, and migration tests."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from amof.commands import scope as scope_cmd
from amof.write_scope_approvals import (
    STATUS_EXPIRED,
    approve_proposal,
    load_approval,
    revoke_approval,
)
from amof.write_scope_audit import audit_write_scope
from amof.write_scope_bindings import (
    STATUS_ACTIVE,
    STATUS_FAILED,
    STATUS_SUSPENDED,
    WriteScopeBindingError,
    create_binding,
    load_binding,
    transition_binding_status,
)
from amof.write_scope_enforcement import (
    COMPLIANCE_WITHIN_SCOPE,
    compute_receipt_id,
    save_receipt,
)
from amof.write_scope_migration import (
    LEGACY_PATH_ELEVATION_MIGRATION_REFUSAL,
    migrate_nested_proposals_from_result,
    refuse_legacy_path_elevation_as_approval,
    scan_write_scope_store,
)
from amof.write_scope_proposals import persist_write_scope_proposals_from_result, utc_now_iso
from amof.write_scope_recovery import (
    OUTCOME_MANUAL,
    OUTCOME_MARK_FAILED,
    OUTCOME_REQUIRE_NEW_APPROVAL,
    OUTCOME_RESTORE_CONFIRMED,
    recover_binding,
)


OPERATOR = "operator:marek"


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "wave5@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Wave5 Tester"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "docs").mkdir(parents=True, exist_ok=True)
    (path / "docs" / "launch-readiness").mkdir(parents=True, exist_ok=True)
    (path / "docs" / "launch-readiness" / "report.md").write_text(
        "hello\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


def _valid_body(*, target_id: str, base_sha: str) -> dict:
    return {
        "target_id": target_id,
        "base_sha": base_sha,
        "allowed_roots": ["docs/launch-readiness/report.md"],
        "denied_roots": [],
        "reason": "docs-only follow-up",
        "expected_checks": ["git diff --check"],
        "docs_only": True,
        "source_mutation": False,
    }


def _result_envelope(*, session_id: str, proposal: dict) -> dict:
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


class WriteScopeAuditRecoverWave5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.home = Path(self._tmpdir.name)
        self.repo = self.home / "repo"
        self.base_sha = _init_git_repo(self.repo)
        self.target_id = f"github_app:marekhotshot/simple-ai-shop:{self.base_sha}"
        self.proposals = self.home / "share" / "write-scopes" / "proposals"
        self.approvals = self.home / "share" / "write-scopes" / "approvals"
        self.bindings = self.home / "share" / "write-scopes" / "bindings"
        self.revocations = self.home / "share" / "write-scopes" / "revocations"
        self.receipts = self.home / "share" / "write-scopes" / "receipts"
        self.events = self.home / "share" / "write-scopes" / "events"
        self.env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _persist_and_approve(self, *, run_id: str = "run-wave5-001", ttl: str = "2h") -> dict:
        body = _valid_body(target_id=self.target_id, base_sha=self.base_sha)
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(session_id=run_id, proposal=body),
            base_dir=self.proposals,
        )
        self.assertEqual(len(outcome.persisted), 1)
        return approve_proposal(
            outcome.persisted[0]["proposal_id"],
            ttl=ttl,
            approved_by=OPERATOR,
            proposals_dir=self.proposals,
            approvals_dir=self.approvals,
            events_dir=self.events,
        )

    def _bind(self, approval_id: str, *, run_id: str = "exec-wave5-1"):
        return create_binding(
            approval_id,
            run_id=run_id,
            workspace_root=self.repo,
            requested_capabilities=["bounded_write"],
            approvals_dir=self.approvals,
            events_dir=self.events,
            bindings_dir=self.bindings,
        )

    def _emit_receipt(self, binding: dict, *, approval_status: str = "consumed") -> dict:
        evaluated_at = utc_now_iso()
        compliance = COMPLIANCE_WITHIN_SCOPE
        receipt_id = compute_receipt_id(
            binding_id=binding["binding_id"],
            run_id=binding["run_id"],
            evaluated_at=evaluated_at,
            compliance=compliance,
        )
        record = {
            "kind": "mutation_receipt",
            "schema_version": 1,
            "receipt_id": receipt_id,
            "run_id": binding["run_id"],
            "binding_id": binding["binding_id"],
            "approval_id": binding["approval_id"],
            "target_id": binding["target_id"],
            "base_sha": binding["base_sha"],
            "changed_paths": ["docs/launch-readiness/report.md"],
            "in_scope_paths": ["docs/launch-readiness/report.md"],
            "out_of_scope_paths": [],
            "restored_paths": [],
            "compliance": compliance,
            "binding_status": "completed",
            "approval_status": approval_status,
            "created_at": evaluated_at,
            "evaluated_at": evaluated_at,
            "rollback_atomic": False,
            "workspace_root": str(self.repo),
        }
        return save_receipt(
            record,
            receipts_dir=self.receipts,
            events_dir=self.events,
        )

    def test_audit_reconstructs_lineage(self) -> None:
        approval = self._persist_and_approve()
        bound = self._bind(approval["approval_id"])
        receipt = self._emit_receipt(bound.binding)
        audit = audit_write_scope(approval["approval_id"])
        self.assertEqual(audit["kind"], "write_scope_audit")
        self.assertEqual(audit["proposal"]["proposal_id"], approval["proposal_id"])
        self.assertEqual(audit["approval"]["approval_id"], approval["approval_id"])
        self.assertEqual(audit["binding"]["binding_id"], bound.binding["binding_id"])
        self.assertEqual(audit["mutation_receipt"]["receipt_id"], receipt["receipt_id"])
        self.assertIn(
            audit["terminal_authority_state"]["residual_mutation_authority"],
            {"none", "approval_only_requires_new_binding", "binding_requires_recover_or_finalize"},
        )

    def test_audit_cli(self) -> None:
        approval = self._persist_and_approve()
        self._bind(approval["approval_id"])
        args = argparse.Namespace(
            scope_cmd="audit",
            scope_id=approval["approval_id"],
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            code = scope_cmd.cmd_scope(args)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["approval"]["approval_id"], approval["approval_id"])

    def test_recover_active_clean_missing_receipt(self) -> None:
        approval = self._persist_and_approve()
        bound = self._bind(approval["approval_id"])
        result = recover_binding(
            bound.binding["binding_id"],
            decision="auto",
            bindings_dir=self.bindings,
            approvals_dir=self.approvals,
            events_dir=self.events,
            receipts_dir=self.receipts,
        )
        self.assertIn(OUTCOME_MARK_FAILED, result["outcomes"])
        reloaded = load_binding(bound.binding["binding_id"], bindings_dir=self.bindings)
        self.assertEqual(reloaded["status"], STATUS_FAILED)
        # Must not fabricate completed success.
        self.assertNotEqual(reloaded["status"], "completed")

    def test_recover_dirty_requires_explicit_decision(self) -> None:
        approval = self._persist_and_approve()
        bound = self._bind(approval["approval_id"])
        target = self.repo / "docs" / "launch-readiness" / "report.md"
        target.write_text("dirty after crash\n", encoding="utf-8")
        result = recover_binding(
            bound.binding["binding_id"],
            decision="auto",
            bindings_dir=self.bindings,
            approvals_dir=self.approvals,
            events_dir=self.events,
            receipts_dir=self.receipts,
        )
        self.assertEqual(result["outcome"], OUTCOME_MANUAL)
        still = load_binding(bound.binding["binding_id"], bindings_dir=self.bindings)
        self.assertEqual(still["status"], STATUS_ACTIVE)

    def test_recover_restore_confirmed(self) -> None:
        approval = self._persist_and_approve()
        bound = self._bind(approval["approval_id"])
        target = self.repo / "docs" / "launch-readiness" / "report.md"
        target.write_text("dirty after crash\n", encoding="utf-8")
        result = recover_binding(
            bound.binding["binding_id"],
            decision="restore",
            bindings_dir=self.bindings,
            approvals_dir=self.approvals,
            events_dir=self.events,
            receipts_dir=self.receipts,
        )
        self.assertIn(OUTCOME_RESTORE_CONFIRMED, result["outcomes"])
        self.assertIn(OUTCOME_MARK_FAILED, result["outcomes"])
        self.assertIn(OUTCOME_REQUIRE_NEW_APPROVAL, result["outcomes"])
        reloaded = load_binding(bound.binding["binding_id"], bindings_dir=self.bindings)
        self.assertEqual(reloaded["status"], STATUS_FAILED)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
        # No automatic continuation: suspended/active gone; new bind may need new approval
        # after dirty partial — Approval may still be approved but residual notes require_new.
        self.assertEqual(result["residual_mutation_authority"], "none")

    def test_recover_expired_approval(self) -> None:
        from datetime import timedelta

        body = _valid_body(target_id=self.target_id, base_sha=self.base_sha)
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(session_id="run-wave5-exp", proposal=body),
            base_dir=self.proposals,
        )
        # Bind while the Approval is still active under the real clock, then
        # persist expiry via injected later now before recover.
        t0 = datetime.now(timezone.utc).replace(microsecond=0)
        approval = approve_proposal(
            outcome.persisted[0]["proposal_id"],
            ttl="1h",
            approved_by=OPERATOR,
            approved_at=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            now=t0,
            proposals_dir=self.proposals,
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        bound = self._bind(approval["approval_id"])
        expired = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
            now=t0 + timedelta(hours=1),
        )
        self.assertEqual(expired["status"], STATUS_EXPIRED)
        result = recover_binding(
            bound.binding["binding_id"],
            decision="auto",
            bindings_dir=self.bindings,
            approvals_dir=self.approvals,
            events_dir=self.events,
            receipts_dir=self.receipts,
        )
        self.assertIn(OUTCOME_MARK_FAILED, result["outcomes"])
        self.assertIn(OUTCOME_REQUIRE_NEW_APPROVAL, result["outcomes"])

    def test_recover_revoked_approval(self) -> None:
        approval = self._persist_and_approve()
        bound = self._bind(approval["approval_id"])
        revoke_approval(
            approval["approval_id"],
            reason="operator abort",
            revoked_by=OPERATOR,
            approvals_dir=self.approvals,
            revocations_dir=self.revocations,
            events_dir=self.events,
        )
        result = recover_binding(
            bound.binding["binding_id"],
            decision="mark-failed",
            bindings_dir=self.bindings,
            approvals_dir=self.approvals,
            events_dir=self.events,
            receipts_dir=self.receipts,
        )
        self.assertIn(OUTCOME_REQUIRE_NEW_APPROVAL, result["outcomes"])
        self.assertEqual(
            load_binding(bound.binding["binding_id"], bindings_dir=self.bindings)["status"],
            STATUS_FAILED,
        )

    def test_suspended_blocks_new_bind(self) -> None:
        approval = self._persist_and_approve()
        bound = self._bind(approval["approval_id"])
        transition_binding_status(
            bound.binding["binding_id"],
            STATUS_SUSPENDED,
            reason="crash",
            bindings_dir=self.bindings,
            events_dir=self.events,
        )
        with self.assertRaises(WriteScopeBindingError):
            self._bind(approval["approval_id"], run_id="exec-wave5-2")

    def test_legacy_flag_never_becomes_approval(self) -> None:
        refused = refuse_legacy_path_elevation_as_approval(roots=["docs/"])
        self.assertFalse(refused["converted"])
        self.assertIsNone(refused["approval"])
        self.assertIn("MUST NOT", refused["reason"])
        self.assertIn("MUST NOT", LEGACY_PATH_ELEVATION_MIGRATION_REFUSAL)

    def test_migrate_nested_proposals(self) -> None:
        body = _valid_body(target_id=self.target_id, base_sha=self.base_sha)
        outcome = migrate_nested_proposals_from_result(
            _result_envelope(session_id="migrate-run-1", proposal=body),
            base_dir=self.proposals,
        )
        self.assertEqual(len(outcome.persisted), 1)
        # AgentRunResult itself is not rewritten (backwards-readable).
        envelope = _result_envelope(session_id="migrate-run-1", proposal=body)
        self.assertIn("write_scope_proposal", envelope)

    def test_scan_corrupt_fail_closed(self) -> None:
        self.proposals.mkdir(parents=True, exist_ok=True)
        bad = self.proposals / "wsp-corrupt.json"
        bad.write_text("{not-json", encoding="utf-8")
        scan = scan_write_scope_store(
            proposals_dir=self.proposals,
            approvals_dir=self.approvals,
            bindings_dir=self.bindings,
            receipts_dir=self.receipts,
        )
        self.assertEqual(scan.legacy_approvals_fabricated, 0)
        self.assertTrue(any(item["kind"] == "proposal" for item in scan.corrupt))

    def test_recover_cli(self) -> None:
        approval = self._persist_and_approve()
        bound = self._bind(approval["approval_id"])
        args = argparse.Namespace(
            scope_cmd="recover",
            binding_id=bound.binding["binding_id"],
            decision="mark-failed",
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            code = scope_cmd.cmd_scope(args)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["binding"]["status"], STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
