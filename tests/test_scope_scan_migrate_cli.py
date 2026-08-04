"""CLI wiring for amof scope scan|migrate around write_scope_migration helpers."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amof.commands import scope as scope_cmd
from amof.write_scope_proposals import list_proposals


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "scope-cli@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Scope CLI Tester"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "docs").mkdir(parents=True, exist_ok=True)
    (path / "docs" / "note.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _valid_body(*, target_id: str, base_sha: str) -> dict:
    return {
        "target_id": target_id,
        "base_sha": base_sha,
        "allowed_roots": ["docs/note.md"],
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


class ScopeScanMigrateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.home = Path(self._tmpdir.name)
        self.repo = self.home / "repo"
        self.base_sha = _init_git_repo(self.repo)
        self.target_id = f"github_app:marekhotshot/simple-ai-shop:{self.base_sha}"
        self.store = self.home / "share" / "write-scopes"
        self.proposals = self.store / "proposals"
        self.approvals = self.store / "approvals"
        self.bindings = self.store / "bindings"
        self.receipts = self.store / "receipts"
        for path in (
            self.proposals,
            self.approvals,
            self.bindings,
            self.receipts,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run_amof(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SCRIPTS_ROOT)
        env["AMOF_HOME"] = str(self.home)
        return subprocess.run(
            [sys.executable, "-m", "amof", *args],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_scope_help_lists_scan_and_migrate(self) -> None:
        result = self._run_amof("scope", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scan", result.stdout)
        self.assertIn("migrate", result.stdout)

    def test_scan_without_store_fails_closed(self) -> None:
        # argparse required=True should reject missing --store before handler.
        result = self._run_amof("scope", "scan", "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--store", (result.stderr + result.stdout))

        # Handler also fails closed if store is empty/missing.
        args = argparse.Namespace(
            scope_cmd="scan",
            store="",
            events_path=None,
            json=True,
        )
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            code = scope_cmd.cmd_scope(args)
        self.assertEqual(code, 1)
        self.assertIn("--store", err.getvalue())
        self.assertIn("fail-closed", err.getvalue())

    def test_scan_with_store_reports_ok_and_corrupt(self) -> None:
        bad = self.proposals / "wsp-corrupt.json"
        bad.write_text("{not-json", encoding="utf-8")
        args = argparse.Namespace(
            scope_cmd="scan",
            store=str(self.store),
            events_path=None,
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            code = scope_cmd.cmd_scope(args)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["legacy_approvals_fabricated"], 0)
        self.assertTrue(any(item["kind"] == "proposal" for item in payload["corrupt"]))

    def test_migrate_default_is_dry_run_safe(self) -> None:
        body = _valid_body(target_id=self.target_id, base_sha=self.base_sha)
        result_path = self.home / "result.json"
        result_path.write_text(
            json.dumps(_result_envelope(session_id="migrate-cli-1", proposal=body)),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            scope_cmd="migrate",
            result_path=str(result_path),
            run_id=None,
            base_dir=str(self.proposals),
            apply=False,
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            code = scope_cmd.cmd_scope(args)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["mode"], "dry-run")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["persisted_count"], 0)
        self.assertEqual(list_proposals(base_dir=self.proposals), [])

    def test_migrate_apply_persists_proposals(self) -> None:
        body = _valid_body(target_id=self.target_id, base_sha=self.base_sha)
        result_path = self.home / "result-apply.json"
        result_path.write_text(
            json.dumps(_result_envelope(session_id="migrate-cli-2", proposal=body)),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            scope_cmd="migrate",
            result_path=str(result_path),
            run_id=None,
            base_dir=str(self.proposals),
            apply=True,
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            code = scope_cmd.cmd_scope(args)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["mode"], "apply")
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["persisted_count"], 1)
        records = list_proposals(base_dir=self.proposals)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["run_id"], "migrate-cli-2")


if __name__ == "__main__":
    unittest.main()
