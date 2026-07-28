"""Wave 4 write-scope runtime enforcement + MutationReceipt tests."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from amof.contracts_runtime import AgentRunResult
from amof.orchestrator.tools.base import Guardrails
from amof.write_scope_approvals import (
    STATUS_APPROVED,
    STATUS_CONSUMED,
    STATUS_REVOKED,
    approve_proposal,
    load_approval,
    revoke_approval,
)
from amof.write_scope_bindings import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    create_binding,
    git_rev_parse_head,
    load_binding,
)
from amof.write_scope_enforcement import (
    COMPLIANCE_BASE_SHA_MISMATCH,
    COMPLIANCE_NO_MUTATION,
    COMPLIANCE_PARTIAL,
    COMPLIANCE_SCOPE_EXCEEDED,
    COMPLIANCE_WITHIN_SCOPE,
    RECEIPT_KIND,
    apply_enforcement_to_result,
    classify_changed_paths,
    enforce_write_scope_mutations,
    guardrail_write_allowed,
    load_scope_roots_for_binding,
    normalize_workspace_path,
    path_is_within_effective_scope,
    verify_receipt_record,
)
from amof.write_scope_proposals import persist_write_scope_proposals_from_result


OPERATOR = "operator:marek"


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "wave4@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Wave4 Tester"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "docs").mkdir(parents=True, exist_ok=True)
    (path / "docs" / "launch-readiness").mkdir(parents=True, exist_ok=True)
    (path / "docs" / "launch-readiness" / "report.md").write_text(
        "hello\n", encoding="utf-8"
    )
    (path / "docs" / "secrets").mkdir(parents=True, exist_ok=True)
    (path / "docs" / "secrets" / "token.txt").write_text("secret\n", encoding="utf-8")
    (path / "src").mkdir(parents=True, exist_ok=True)
    (path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
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
    denied_roots: list[str] | None = None,
    docs_only: bool = True,
) -> dict:
    return {
        "target_id": target_id,
        "base_sha": base_sha,
        "allowed_roots": allowed_roots or ["docs/launch-readiness/report.md"],
        "denied_roots": denied_roots or [],
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


class WriteScopeEnforcementWave4Tests(unittest.TestCase):
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

    def _persist_and_approve(self, *, run_id: str = "run-wave4-001", **body_kwargs) -> dict:
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

    def _bind(self, approval_id: str, *, run_id: str = "exec-wave4-1", **kwargs):
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

    def _enforce(self, binding_id: str, changed_paths: list[str], **kwargs):
        return enforce_write_scope_mutations(
            binding_id,
            changed_paths=changed_paths,
            workspace_root=self.repo,
            approvals_dir=self.approvals,
            bindings_dir=self.bindings,
            revocations_dir=self.revocations,
            events_dir=self.events,
            receipts_dir=self.receipts,
            **kwargs,
        )

    def test_exact_allowed_file_within_scope(self) -> None:
        approval = self._persist_and_approve()
        gate = self._bind(approval["approval_id"])
        target = self.repo / "docs" / "launch-readiness" / "report.md"
        target.write_text("updated\n", encoding="utf-8")
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/launch-readiness/report.md"],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_WITHIN_SCOPE)
        self.assertEqual(outcome.receipt["in_scope_paths"], ["docs/launch-readiness/report.md"])
        self.assertEqual(outcome.receipt["out_of_scope_paths"], [])
        self.assertFalse(outcome.run_failed)
        binding = load_binding(gate.binding["binding_id"], bindings_dir=self.bindings)
        self.assertEqual(binding["status"], STATUS_COMPLETED)
        loaded = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        self.assertEqual(loaded["status"], STATUS_CONSUMED)

    def test_allowed_directory_descendant(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/launch-readiness/"],
            docs_only=True,
        )
        gate = self._bind(approval["approval_id"])
        new_file = self.repo / "docs" / "launch-readiness" / "extra.md"
        new_file.write_text("extra\n", encoding="utf-8")
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/launch-readiness/extra.md"],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_WITHIN_SCOPE)
        self.assertIn("docs/launch-readiness/extra.md", outcome.receipt["in_scope_paths"])

    def test_denied_path_inside_allowed_directory(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/"],
            denied_roots=["docs/secrets/"],
            docs_only=True,
        )
        gate = self._bind(approval["approval_id"])
        secret = self.repo / "docs" / "secrets" / "token.txt"
        secret.write_text("leaked\n", encoding="utf-8")
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/secrets/token.txt"],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_SCOPE_EXCEEDED)
        self.assertTrue(outcome.run_failed)
        self.assertIn("docs/secrets/token.txt", outcome.receipt["out_of_scope_paths"])
        self.assertIn("docs/secrets/token.txt", outcome.receipt["restored_paths"])
        self.assertEqual(secret.read_text(encoding="utf-8"), "secret\n")
        binding = load_binding(gate.binding["binding_id"], bindings_dir=self.bindings)
        self.assertEqual(binding["status"], STATUS_FAILED)
        loaded = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        self.assertEqual(loaded["status"], STATUS_REVOKED)
        self.assertIsNotNone(outcome.receipt.get("revocation_id"))

    def test_sibling_escape(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/launch-readiness/"],
        )
        gate = self._bind(approval["approval_id"])
        sibling = self.repo / "docs" / "other.md"
        sibling.write_text("nope\n", encoding="utf-8")
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/other.md"],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_SCOPE_EXCEEDED)
        self.assertIn("docs/other.md", outcome.receipt["out_of_scope_paths"])

    def test_dotdot_traversal(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/launch-readiness/"],
        )
        gate = self._bind(approval["approval_id"])
        scope = load_scope_roots_for_binding(
            gate.binding,
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        ok, _rel, err = path_is_within_effective_scope(
            "docs/launch-readiness/../../src/app.py",
            scope=scope,
        )
        self.assertFalse(ok)
        self.assertEqual(err, "path_traversal")
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/launch-readiness/../../src/app.py"],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_SCOPE_EXCEEDED)

    def test_symlink_escape(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/launch-readiness/"],
        )
        gate = self._bind(approval["approval_id"])
        outside = self.home / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.repo / "docs" / "launch-readiness" / "escape-link"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation not supported")
        abs_path, rel, err = normalize_workspace_path(
            "docs/launch-readiness/escape-link",
            workspace_root=self.repo,
        )
        self.assertIsNone(abs_path)
        self.assertIn(err, {"symlink_escape", "symlink_or_escape_outside_workspace"})
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/launch-readiness/escape-link"],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_SCOPE_EXCEEDED)

    def test_rename_allowed_to_denied(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/"],
            denied_roots=["docs/secrets/"],
            docs_only=True,
        )
        gate = self._bind(approval["approval_id"])
        src = self.repo / "docs" / "launch-readiness" / "report.md"
        dest = self.repo / "docs" / "secrets" / "moved.md"
        src.rename(dest)
        # Recreate source as deleted + dest as created for classification.
        outcome = self._enforce(
            gate.binding["binding_id"],
            [
                "docs/launch-readiness/report.md",
                "docs/secrets/moved.md",
            ],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_SCOPE_EXCEEDED)
        self.assertIn("docs/secrets/moved.md", outcome.receipt["out_of_scope_paths"])
        loaded = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        self.assertEqual(loaded["status"], STATUS_REVOKED)

    def test_create_delete_modify(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/launch-readiness/"],
        )
        gate = self._bind(approval["approval_id"])
        created = self.repo / "docs" / "launch-readiness" / "new.md"
        created.write_text("new\n", encoding="utf-8")
        modified = self.repo / "docs" / "launch-readiness" / "report.md"
        modified.write_text("mod\n", encoding="utf-8")
        doomed = self.repo / "docs" / "launch-readiness" / "tmp.md"
        doomed.write_text("tmp\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "docs/launch-readiness/tmp.md"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "tmp"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        # Re-approve on new HEAD for delete case — use classification only here.
        scope = load_scope_roots_for_binding(
            gate.binding,
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        in_scope, out = classify_changed_paths(
            [
                "docs/launch-readiness/new.md",
                "docs/launch-readiness/report.md",
            ],
            scope=scope,
        )
        self.assertEqual(sorted(in_scope), sorted([
            "docs/launch-readiness/new.md",
            "docs/launch-readiness/report.md",
        ]))
        self.assertEqual(out, [])
        doomed.unlink()
        in_scope2, out2 = classify_changed_paths(
            ["docs/launch-readiness/tmp.md"],
            scope=scope,
        )
        self.assertEqual(in_scope2, ["docs/launch-readiness/tmp.md"])
        self.assertEqual(out2, [])

    def test_mixed_in_and_out_of_scope(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/launch-readiness/"],
        )
        gate = self._bind(approval["approval_id"])
        (self.repo / "docs" / "launch-readiness" / "report.md").write_text(
            "ok\n", encoding="utf-8"
        )
        (self.repo / "src" / "app.py").write_text("bad\n", encoding="utf-8")
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/launch-readiness/report.md", "src/app.py"],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_SCOPE_EXCEEDED)
        self.assertIn("docs/launch-readiness/report.md", outcome.receipt["in_scope_paths"])
        self.assertIn("src/app.py", outcome.receipt["out_of_scope_paths"])
        self.assertIn("src/app.py", outcome.receipt["restored_paths"])
        self.assertEqual(
            (self.repo / "src" / "app.py").read_text(encoding="utf-8"),
            "print('hi')\n",
        )

    def test_no_mutation(self) -> None:
        approval = self._persist_and_approve()
        gate = self._bind(approval["approval_id"])
        outcome = self._enforce(gate.binding["binding_id"], [])
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_NO_MUTATION)
        self.assertFalse(outcome.run_failed)
        binding = load_binding(gate.binding["binding_id"], bindings_dir=self.bindings)
        self.assertEqual(binding["status"], STATUS_COMPLETED)
        loaded = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        self.assertEqual(loaded["status"], STATUS_APPROVED)

    def test_expiry_during_execution(self) -> None:
        approval = self._persist_and_approve()
        gate = self._bind(approval["approval_id"])
        # Simulate TTL elapsing mid-run: evaluate expiry with a future clock
        # and persist, then enforce with real clock against expired Approval.
        future = datetime.now(timezone.utc) + timedelta(hours=3)
        expired = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
            now=future,
            evaluate_ttl=True,
            persist_expiry=True,
        )
        self.assertEqual(expired["status"], "expired")
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/launch-readiness/report.md"],
            runner_failed=False,
        )
        self.assertTrue(outcome.run_failed)
        self.assertEqual(outcome.stop_reason, "write_scope_expired")
        self.assertEqual(outcome.receipt["stop_reason"], "write_scope_expired")
        binding = load_binding(gate.binding["binding_id"], bindings_dir=self.bindings)
        self.assertEqual(binding["status"], STATUS_FAILED)

    def test_revoke_during_execution(self) -> None:
        approval = self._persist_and_approve()
        gate = self._bind(approval["approval_id"])
        revoke_approval(
            approval["approval_id"],
            reason="operator kill switch mid-run",
            revoked_by=OPERATOR,
            approvals_dir=self.approvals,
            revocations_dir=self.revocations,
            events_dir=self.events,
        )
        (self.repo / "docs" / "launch-readiness" / "report.md").write_text(
            "mid\n", encoding="utf-8"
        )
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/launch-readiness/report.md"],
        )
        self.assertTrue(outcome.run_failed)
        self.assertEqual(outcome.stop_reason, "write_scope_revoked")
        binding = load_binding(gate.binding["binding_id"], bindings_dir=self.bindings)
        self.assertEqual(binding["status"], STATUS_FAILED)

    def test_base_sha_change_during_execution(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/launch-readiness/"],
        )
        gate = self._bind(approval["approval_id"])
        # Advance HEAD after bind.
        (self.repo / "docs" / "launch-readiness" / "bump.md").write_text(
            "bump\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "add", "."], cwd=self.repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "bump"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/launch-readiness/report.md"],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_BASE_SHA_MISMATCH)
        self.assertTrue(outcome.run_failed)
        binding = load_binding(gate.binding["binding_id"], bindings_dir=self.bindings)
        self.assertEqual(binding["status"], STATUS_FAILED)
        # Approval remains (sha-bound; not silently reused on new HEAD).
        loaded = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        self.assertEqual(loaded["status"], STATUS_APPROVED)

    def test_partial_runner_failure(self) -> None:
        approval = self._persist_and_approve()
        gate = self._bind(approval["approval_id"])
        (self.repo / "docs" / "launch-readiness" / "report.md").write_text(
            "partial\n", encoding="utf-8"
        )
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/launch-readiness/report.md"],
            runner_failed=True,
            runner_stop_reason="runner_crashed",
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_PARTIAL)
        self.assertTrue(outcome.run_failed)
        binding = load_binding(gate.binding["binding_id"], bindings_dir=self.bindings)
        self.assertEqual(binding["status"], STATUS_FAILED)
        loaded = load_approval(
            approval["approval_id"],
            approvals_dir=self.approvals,
            events_dir=self.events,
        )
        self.assertEqual(loaded["status"], STATUS_CONSUMED)

    def test_receipt_schema_integrity(self) -> None:
        approval = self._persist_and_approve()
        gate = self._bind(approval["approval_id"])
        outcome = self._enforce(gate.binding["binding_id"], [])
        verified = verify_receipt_record(outcome.receipt)
        self.assertEqual(verified["kind"], RECEIPT_KIND)
        self.assertTrue(verified["receipt_id"].startswith("wmr-"))
        self.assertIs(verified["rollback_atomic"], False)
        # Persist path exists.
        path = self.receipts / f"{verified['receipt_id']}.json"
        self.assertTrue(path.is_file())
        # Schema package mirror exists in repo.
        schema = (
            Path(__file__).resolve().parents[1]
            / "contracts"
            / "mutation-receipt.schema.json"
        )
        self.assertTrue(schema.is_file())

    def test_agent_run_result_linkage(self) -> None:
        approval = self._persist_and_approve()
        gate = self._bind(approval["approval_id"])
        (self.repo / "docs" / "launch-readiness" / "report.md").write_text(
            "linked\n", encoding="utf-8"
        )
        result = {
            "result_kind": "agent_run_result",
            "contract_version": "agent-run-v1",
            "schema_version": 1,
            "status": "completed",
            "session_id": gate.binding["run_id"],
            "exit_code": 0,
            "stop_reason": "completed",
            "final_text": "ok",
            "plan_path": None,
            "checkpoint_path": None,
            "event_log_path": "/tmp/events.jsonl",
            "journal_path": None,
            "budget_summary": {"limit": None, "spent": 0, "remaining": None},
            "changed_paths": ["docs/launch-readiness/report.md"],
        }
        updated = apply_enforcement_to_result(
            result,
            binding=gate.binding,
            workspace_root=self.repo,
            approvals_dir=self.approvals,
            bindings_dir=self.bindings,
            revocations_dir=self.revocations,
            events_dir=self.events,
            receipts_dir=self.receipts,
        )
        self.assertEqual(updated["write_scope_binding_id"], gate.binding["binding_id"])
        self.assertEqual(updated["write_scope_approval_id"], approval["approval_id"])
        self.assertEqual(updated["mutation_receipt"]["compliance"], COMPLIANCE_WITHIN_SCOPE)
        envelope = AgentRunResult(
            status=updated["status"],
            session_id=updated["session_id"],
            exit_code=updated["exit_code"],
            stop_reason=updated["stop_reason"],
            final_text=updated["final_text"],
            plan_path=None,
            checkpoint_path=None,
            event_log_path=updated["event_log_path"],
            journal_path=None,
            budget_summary=updated["budget_summary"],
            changed_paths=updated.get("changed_paths"),
            write_scope_binding_id=updated.get("write_scope_binding_id"),
            write_scope_approval_id=updated.get("write_scope_approval_id"),
            mutation_receipt=updated.get("mutation_receipt"),
        )
        serialized = envelope.to_dict()
        self.assertIn("mutation_receipt", serialized)
        self.assertEqual(serialized["mutation_receipt"]["kind"], RECEIPT_KIND)

    def test_approval_consumed_after_in_scope_success(self) -> None:
        approval = self._persist_and_approve()
        gate = self._bind(approval["approval_id"])
        (self.repo / "docs" / "launch-readiness" / "report.md").write_text(
            "done\n", encoding="utf-8"
        )
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["docs/launch-readiness/report.md"],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_WITHIN_SCOPE)
        self.assertEqual(outcome.receipt["approval_status"], STATUS_CONSUMED)

    def test_approval_revoked_by_breach_after_scope_exceeded(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/launch-readiness/"],
        )
        gate = self._bind(approval["approval_id"])
        (self.repo / "src" / "app.py").write_text("breach\n", encoding="utf-8")
        outcome = self._enforce(
            gate.binding["binding_id"],
            ["src/app.py"],
        )
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_SCOPE_EXCEEDED)
        self.assertEqual(outcome.receipt["approval_status"], STATUS_REVOKED)
        self.assertTrue(str(outcome.receipt.get("revocation_id") or "").startswith("wsr-"))

    def test_guardrails_deny_wins_tool_layer(self) -> None:
        approval = self._persist_and_approve(
            allowed_roots=["docs/"],
            denied_roots=["docs/secrets/"],
            docs_only=True,
        )
        gate = self._bind(approval["approval_id"])
        g = Guardrails(
            writable_roots=[Path(p) for p in gate.writable_roots],
            denied_roots=[Path(p) for p in gate.denied_roots],
        )
        allowed_path = str(self.repo / "docs" / "launch-readiness" / "report.md")
        denied_path = str(self.repo / "docs" / "secrets" / "token.txt")
        self.assertIsNone(g.check_write(allowed_path))
        err = g.check_write(denied_path)
        self.assertIsNotNone(err)
        self.assertIn("denied", err.lower())
        # Direct helper.
        msg = guardrail_write_allowed(
            denied_path,
            writable_roots=gate.writable_roots,
            denied_roots=gate.denied_roots,
        )
        self.assertIsNotNone(msg)


if __name__ == "__main__":
    unittest.main()
