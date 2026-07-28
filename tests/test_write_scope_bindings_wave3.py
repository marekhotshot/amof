"""Wave 3 WriteScopeBinding + execution bind gate tests."""

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
    STATUS_APPROVED,
    STATUS_CONSUMED,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    approve_proposal,
    load_approval,
    revoke_approval,
    transition_approval_status,
)
from amof.write_scope_bindings import (
    LEGACY_PATH_ELEVATION_WARNING,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    WriteScopeBindingError,
    create_binding,
    emit_legacy_path_elevation_warning,
    finalize_binding,
    git_rev_parse_head,
    list_bindings,
    load_binding,
    prepare_execution_write_scope,
    resolve_writable_roots,
)
from amof.write_scope_proposals import persist_write_scope_proposals_from_result


OPERATOR = "operator:marek"


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "wave3@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Wave3 Tester"],
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
    return git_rev_parse_head(path)


def _valid_body(
    *,
    target_id: str,
    base_sha: str,
    allowed_roots: list[str] | None = None,
    docs_only: bool = True,
) -> dict:
    return {
        "target_id": target_id,
        "base_sha": base_sha,
        "allowed_roots": allowed_roots or ["docs/launch-readiness/report.md"],
        "denied_roots": [],
        "reason": "docs-only follow-up",
        "expected_checks": ["git diff --check"],
        "docs_only": docs_only,
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


class WriteScopeBindingWave3Tests(unittest.TestCase):
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
        self.events = self.home / "share" / "write-scopes" / "events"
        self.env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _persist_and_approve(self, *, run_id: str = "run-wave3-001", **body_kwargs) -> dict:
        body = _valid_body(
            target_id=self.target_id,
            base_sha=self.base_sha,
            **body_kwargs,
        )
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(session_id=run_id, proposal=body),
            base_dir=self.proposals,
        )
        self.assertEqual(len(outcome.persisted), 1)
        return approve_proposal(
            outcome.persisted[0]["proposal_id"],
            ttl="2h",
            approved_by=OPERATOR,
            proposals_dir=self.proposals,
            approvals_dir=self.approvals,
            events_dir=self.events,
        )

    def _bind(self, approval_id: str, *, run_id: str = "exec-1", **kwargs):
        return create_binding(
            approval_id,
            run_id=run_id,
            workspace_root=self.repo,
            requested_capabilities=kwargs.pop(
                "requested_capabilities", ["bounded_write"]
            ),
            approvals_dir=self.approvals,
            events_dir=self.events,
            bindings_dir=self.bindings,
            **kwargs,
        )

    def test_valid_bind(self) -> None:
        approval = self._persist_and_approve()
        result = self._bind(approval["approval_id"])
        self.assertEqual(result.binding["status"], STATUS_ACTIVE)
        self.assertEqual(result.binding["approval_id"], approval["approval_id"])
        self.assertEqual(result.binding["target_id"], self.target_id)
        self.assertEqual(result.binding["base_sha"], self.base_sha)
        self.assertTrue(result.binding["binding_id"].startswith("wsb-"))
        self.assertEqual(len(result.writable_roots), 1)
        self.assertTrue(result.writable_roots[0].startswith(str(self.repo)))
        self.assertTrue(result.writable_roots[0].endswith("docs/launch-readiness/report.md"))
        # Approval not consumed on bind.
        loaded = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        self.assertEqual(loaded["status"], STATUS_APPROVED)
        events = (self.events / "write-scope-events.jsonl").read_text(encoding="utf-8")
        self.assertIn("write_scope.bound", events)

    def test_missing_approval(self) -> None:
        with self.assertRaises(WriteScopeBindingError) as ctx:
            self._bind("wsa-000000000000000000000000")
        self.assertIn("approval not found", str(ctx.exception))

    def test_expired_approval(self) -> None:
        approval = self._persist_and_approve()
        # Force expiry via TTL evaluation with future clock on a short TTL grant.
        proposal_body = _valid_body(target_id=self.target_id, base_sha=self.base_sha)
        outcome = persist_write_scope_proposals_from_result(
            _result_envelope(session_id="run-exp", proposal=proposal_body),
            base_dir=self.proposals,
        )
        short = approve_proposal(
            outcome.persisted[0]["proposal_id"],
            ttl="1s",
            approved_by=OPERATOR,
            approved_at="2026-07-28T10:00:00Z",
            now=datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc),
            proposals_dir=self.proposals,
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        expired = load_approval(
            short["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
            now=datetime(2026, 7, 28, 10, 0, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(expired["status"], STATUS_EXPIRED)
        with self.assertRaises(WriteScopeBindingError) as ctx:
            self._bind(short["approval_id"])
        self.assertIn("expired", str(ctx.exception).lower())

    def test_revoked_approval(self) -> None:
        approval = self._persist_and_approve()
        revoke_approval(
            approval["approval_id"],
            reason="operator halt",
            revoked_by=OPERATOR,
            approvals_dir=self.approvals,
            revocations_dir=self.revocations,
            events_dir=self.events,
        )
        with self.assertRaises(WriteScopeBindingError) as ctx:
            self._bind(approval["approval_id"])
        self.assertIn("revoked", str(ctx.exception).lower())

    def test_consumed_approval(self) -> None:
        approval = self._persist_and_approve()
        transition_approval_status(
            approval["approval_id"],
            STATUS_CONSUMED,
            approvals_dir=self.approvals,
            events_dir=self.events,
            allow_consumed=True,
        )
        loaded = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
            evaluate_ttl=False,
        )
        self.assertEqual(loaded["status"], STATUS_CONSUMED)
        with self.assertRaises(WriteScopeBindingError) as ctx:
            self._bind(approval["approval_id"])
        self.assertIn("consumed", str(ctx.exception).lower())

    def test_base_sha_mismatch(self) -> None:
        approval = self._persist_and_approve()
        (self.repo / "docs" / "extra.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "drift"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        with self.assertRaises(WriteScopeBindingError) as ctx:
            self._bind(approval["approval_id"])
        self.assertIn("base_sha mismatch", str(ctx.exception))

    def test_wrong_target(self) -> None:
        approval = self._persist_and_approve()
        with self.assertRaises(WriteScopeBindingError) as ctx:
            self._bind(
                approval["approval_id"],
                execution_target_id="github_app:other/repo:" + self.base_sha,
            )
        self.assertIn("target_id mismatch", str(ctx.exception))

    def test_concurrent_bind_conflict(self) -> None:
        approval_a = self._persist_and_approve(run_id="run-a")
        # Second approval same target, different run.
        approval_b = self._persist_and_approve(run_id="run-b")
        self._bind(approval_a["approval_id"], run_id="exec-a")
        with self.assertRaises(WriteScopeBindingError) as ctx:
            self._bind(approval_b["approval_id"], run_id="exec-b")
        self.assertIn("active mutating binding already exists", str(ctx.exception))

    def test_duplicate_bind(self) -> None:
        approval = self._persist_and_approve()
        self._bind(approval["approval_id"], run_id="exec-1")
        with self.assertRaises(WriteScopeBindingError) as ctx:
            self._bind(approval["approval_id"], run_id="exec-2")
        self.assertIn("approval already bound", str(ctx.exception))

    def test_missing_bounded_write_capability(self) -> None:
        approval = self._persist_and_approve()
        with self.assertRaises(WriteScopeBindingError) as ctx:
            self._bind(
                approval["approval_id"],
                requested_capabilities=["read"],
            )
        self.assertIn("bounded_write", str(ctx.exception))

    def test_restart_with_active_binding(self) -> None:
        approval = self._persist_and_approve()
        result = self._bind(approval["approval_id"], run_id="exec-restart")
        binding_id = result.binding["binding_id"]
        # Simulate restart: new process loads from disk.
        reloaded = load_binding(binding_id, bindings_dir=self.bindings)
        self.assertEqual(reloaded["status"], STATUS_ACTIVE)
        listed = list_bindings(
            approval_id=approval["approval_id"],
            bindings_dir=self.bindings,
        )
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["binding_id"], binding_id)
        # Still blocks re-bind after reload.
        with self.assertRaises(WriteScopeBindingError):
            self._bind(approval["approval_id"], run_id="exec-restart-2")

    def test_legacy_compatibility_warning(self) -> None:
        stream = io.StringIO()
        emit_legacy_path_elevation_warning(stream=stream)
        text = stream.getvalue()
        self.assertIn("deprecated", text.lower())
        self.assertIn("--approve-writable-root", text)
        self.assertIn("does NOT create", text)
        self.assertIn(LEGACY_PATH_ELEVATION_WARNING, text)

        warn = io.StringIO()
        roots, binding = prepare_execution_write_scope(
            write_scope_approval=None,
            approve_writable_roots=["/tmp/legacy-out"],
            run_id="legacy-1",
            warn_stream=warn,
            legacy_path_elevation=True,
        )
        self.assertIsNone(binding)
        self.assertEqual(roots, ["/tmp/legacy-out"])
        self.assertIn("deprecated", warn.getvalue().lower())
        self.assertIn("does NOT create", warn.getvalue())
        # Never fabricated Approval/Binding files.
        self.assertFalse(any(self.approvals.glob("wsa-*.json")))
        self.assertFalse(any(self.bindings.glob("wsb-*.json")))

    def test_read_only_unaffected_without_approval(self) -> None:
        roots, binding = prepare_execution_write_scope(
            write_scope_approval=None,
            approve_writable_roots=None,
            run_id="ro-1",
            workspace_root=self.repo,
        )
        self.assertEqual(roots, [])
        self.assertIsNone(binding)
        self.assertFalse(any(self.bindings.glob("wsb-*.json")))

    def test_finalize_completed_does_not_consume_approval(self) -> None:
        approval = self._persist_and_approve()
        result = self._bind(approval["approval_id"])
        finalized = finalize_binding(
            result.binding["binding_id"],
            success=True,
            bindings_dir=self.bindings,
            events_dir=self.events,
        )
        self.assertEqual(finalized["status"], STATUS_COMPLETED)
        still = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        self.assertEqual(still["status"], STATUS_APPROVED)
        # Completed binding still blocks re-bind (single-use).
        with self.assertRaises(WriteScopeBindingError) as ctx:
            self._bind(approval["approval_id"], run_id="exec-after-complete")
        self.assertIn("already bound", str(ctx.exception))

    def test_failed_binding_allows_retry_bind(self) -> None:
        approval = self._persist_and_approve()
        result = self._bind(approval["approval_id"], run_id="exec-fail")
        finalize_binding(
            result.binding["binding_id"],
            success=False,
            reason="runner crashed",
            bindings_dir=self.bindings,
            events_dir=self.events,
        )
        retry = self._bind(approval["approval_id"], run_id="exec-retry")
        self.assertEqual(retry.binding["status"], STATUS_ACTIVE)

    def test_resolve_roots_reject_traversal(self) -> None:
        with self.assertRaises(WriteScopeBindingError):
            resolve_writable_roots(["../outside"], workspace_root=self.repo)
        with self.assertRaises(WriteScopeBindingError):
            resolve_writable_roots(["/abs/path"], workspace_root=self.repo)

    def test_mixed_authority_refused(self) -> None:
        approval = self._persist_and_approve()
        with self.assertRaises(WriteScopeBindingError) as ctx:
            prepare_execution_write_scope(
                write_scope_approval=approval["approval_id"],
                approve_writable_roots=["/tmp/x"],
                run_id="mixed",
                workspace_root=self.repo,
                requested_capabilities=["bounded_write"],
                approvals_dir=self.approvals,
                events_dir=self.events,
                bindings_dir=self.bindings,
            )
        self.assertIn("mixed authority", str(ctx.exception))

    def test_cli_show_list_bindings(self) -> None:
        approval = self._persist_and_approve()
        result = self._bind(approval["approval_id"])
        show_args = argparse.Namespace(
            scope_cmd="show",
            scope_id=result.binding["binding_id"],
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            code = scope_cmd.cmd_scope(show_args)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["binding_id"], result.binding["binding_id"])
        self.assertEqual(payload["status"], STATUS_ACTIVE)

        list_args = argparse.Namespace(
            scope_cmd="list",
            from_run="exec-1",
            status="active",
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            code = scope_cmd.cmd_scope(list_args)
        self.assertEqual(code, 0)
        rows = json.loads(out.getvalue())
        self.assertTrue(any(r.get("binding_id") == result.binding["binding_id"] for r in rows))


if __name__ == "__main__":
    unittest.main()
