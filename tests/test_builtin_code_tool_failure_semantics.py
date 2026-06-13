from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands.agent_cmd import _agent_plans_dir, _resolved_manifest_artifact_root
from amof.contracts_runtime import AgentRunResult
from amof.orchestrator.tool_failure_semantics import (
    _REPO_FIELD_ALIASES,
    analyze_tool_call_events,
    enrich_repo_inspection_response,
    normalize_repo_inspection_findings,
    render_canonical_findings,
    render_terminal_task_findings,
    repo_inspection_runner_tools,
    repo_inspection_task_guidance,
    strip_canonical_labels,
    validate_repo_inspection_response,
)
from amof.orchestrator.tools.glob_tool import GlobTool
from amof.orchestrator.tools.tool_proposal import ToolProposalTool
AGENT_RUN_RESULT_SCHEMA_PATH = ROOT / "contracts" / "agent-run-result.schema.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate(schema_path: Path, payload: dict) -> None:
    try:
        import jsonschema  # type: ignore
    except Exception:
        return
    jsonschema.validate(instance=payload, schema=_load(schema_path))


class BuiltinCodeToolFailureSemanticsTests(unittest.TestCase):
    def test_repo_inspection_uses_minimal_read_only_tool_set(self) -> None:
        tool_names = [
            "Read",
            "InspectFiles",
            "ToolProposal",
            "Write",
            "StrReplace",
            "InsertAfter",
            "Glob",
            "LS",
            "ReadLints",
        ]
        self.assertEqual(
            repo_inspection_runner_tools(tool_names),
            ["Read", "InspectFiles", "ToolProposal", "Glob", "LS"],
        )
        self.assertIn("Do not call ReadLints", repo_inspection_task_guidance())
        self.assertIn("ToolProposal", repo_inspection_task_guidance())

    def test_readlints_failure_is_diagnostic_for_repo_inspection(self) -> None:
        events = [
            {
                "event_id": "run:0001",
                "tool": "ReadLints",
                "args": {},
                "success": False,
                "error": "No paths provided. Specify files or directories to lint.",
            }
        ]
        analysis = analyze_tool_call_events(
            events,
            task_text=_repo_inspection_task(),
            final_response=_valid_repo_inspection_response(),
            subtask_id="1",
        )
        self.assertEqual(len(analysis["fatal_failures"]), 0)
        self.assertEqual(analysis["failures"][0].required_or_optional, "diagnostic")

    def test_required_read_failure_keeps_exact_tool_detail(self) -> None:
        events = [
            {
                "event_id": "run:0001",
                "tool": "Read",
                "args": {"path": ".git/status"},
                "success": False,
                "error": "File not found: .git/status",
            }
        ]
        analysis = analyze_tool_call_events(
            events,
            task_text=_repo_inspection_task(),
            final_response="Repository Path: /tmp/repo",
            subtask_id="1",
        )
        self.assertEqual(len(analysis["fatal_failures"]), 1)
        failure = analysis["fatal_failures"][0].to_failure_dict()
        self.assertEqual(failure["failing_tool_name"], "Read")
        self.assertEqual(failure["failing_tool_call_index"], 1)
        self.assertEqual(failure["tool_failure_class"], "missing_file")
        self.assertTrue(failure["tool_failure_required"])
        self.assertIn("git status", failure["safe_next_action"])

    def test_failed_read_can_be_recovered_by_alternative_path(self) -> None:
        events = [
            {
                "event_id": "run:0001",
                "tool": "Read",
                "args": {"path": "/.git/HEAD"},
                "success": False,
                "error": "File not found: /.git/HEAD",
            },
            {
                "event_id": "run:0002",
                "tool": "Glob",
                "args": {"glob_pattern": "**/.git/HEAD"},
                "success": True,
                "output_preview": ".git/HEAD",
            },
            {
                "event_id": "run:0003",
                "tool": "Read",
                "args": {"path": ".git/HEAD"},
                "success": True,
                "output_preview": "1|4c686d6d038607e925bc7ac18a10c52cceadbda5",
            },
        ]
        analysis = analyze_tool_call_events(
            events,
            task_text=_repo_inspection_task(),
            final_response=_valid_repo_inspection_response(),
            subtask_id="1",
        )
        self.assertEqual(len(analysis["fatal_failures"]), 0)
        self.assertEqual(analysis["failures"][0].required_or_optional, "alternative_group")

    def test_failed_toolproposal_can_be_recovered_by_later_success(self) -> None:
        events = [
            {
                "event_id": "run:0001",
                "tool": "ToolProposal",
                "args": {"allowed_paths": ["."]},
                "success": False,
                "error": "invalid_tool_proposal_static_gate: broad or absolute allowed_paths are not allowed",
            },
            {
                "event_id": "run:0002",
                "tool": "ToolProposal",
                "args": {"allowed_paths": [".git/"]},
                "success": True,
                "output_preview": "detached\n4c686d6d038607e925bc7ac18a10c52cceadbda5\n",
            },
        ]
        analysis = analyze_tool_call_events(
            events,
            task_text=_repo_inspection_task(),
            final_response=_valid_repo_inspection_response(),
            subtask_id="1",
        )
        self.assertEqual(len(analysis["fatal_failures"]), 0)
        self.assertEqual(analysis["failures"][0].failure_class, "invalid_tool_arguments")
        self.assertEqual(analysis["failures"][0].required_or_optional, "alternative_group")

    def test_generated_dispatch_artifacts_live_outside_target_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            workspace_root = base / "workspace" / "00-amof"
            workspace_root.mkdir(parents=True)
            alt_root = base / "share"
            (alt_root / "ecosystems" / "dispatch-abc").mkdir(parents=True)
            (alt_root / "ecosystems" / "dispatch-abc" / "ecosystem.yaml").write_text(
                "name: dispatch-abc\nrepos: []\n",
                encoding="utf-8",
            )
            original = os.environ.get("AMOF_WORKSPACE_ROOT")
            os.environ["AMOF_WORKSPACE_ROOT"] = str(alt_root)
            try:
                plans_dir = _agent_plans_dir({"ecosystem": "dispatch-abc"}, workspace_root)
            finally:
                if original is None:
                    os.environ.pop("AMOF_WORKSPACE_ROOT", None)
                else:
                    os.environ["AMOF_WORKSPACE_ROOT"] = original
            self.assertEqual(
                plans_dir,
                alt_root / "ecosystems" / "dispatch-abc" / "plans",
            )

    def test_materialized_workspace_falls_back_to_outer_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_container = Path(tmpdir) / "share" / "workspaces" / "ws-123456"
            workspace_root = workspace_container / "00-amof"
            workspace_root.mkdir(parents=True)
            artifact_root = _resolved_manifest_artifact_root({"ecosystem": "default"}, workspace_root)
        self.assertEqual(artifact_root, workspace_container)

    def test_repo_inspection_response_is_enriched_with_repo_path_and_detached_label(self) -> None:
        enriched = enrich_repo_inspection_response(
            "Repository Path: Not explicitly provided in the output.\n"
            "Branch Or Detached State: `HEAD` (detached)\n"
            "HEAD SHA: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n"
            "origin/main SHA: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n"
            "Cleanliness: clean\n"
            "Mission-revision tests: none found\n"
            "Hermes tests: none found\n"
            "Evidence Paths: /tmp/events.jsonl\n",
            workspace_root="/tmp/ws-123/00-amof",
        )
        self.assertIn("Repository Path: /tmp/ws-123/00-amof", enriched)
        self.assertIn("Branch Or Detached State: detached", enriched)
        self.assertTrue(validate_repo_inspection_response(enriched).ok)

    def test_repo_inspection_markdown_bullets_normalize_successfully(self) -> None:
        validation = validate_repo_inspection_response(
            "- **Repository Path**: `/tmp/ws-1/00-amof`\n"
            "- **Branch or Detached State**: detached\n"
            "- **HEAD SHA**: `4c686d6d038607e925bc7ac18a10c52cceadbda5`\n"
            "- **origin/main SHA**: `4c686d6d038607e925bc7ac18a10c52cceadbda5`\n"
            "- **Cleanliness**: clean\n"
            "- **Mission-revision tests**: none found\n"
            "- **Hermes tests**: none found\n"
            "- **Evidence Paths**: /tmp/events.jsonl\n"
        )
        self.assertTrue(validation.ok)
        self.assertEqual(validation.normalized["checkout_state"], "detached")

    def test_repo_inspection_mixed_format_matches_live_shape(self) -> None:
        validation = validate_repo_inspection_response(
            "Repository Path: /tmp/ws-1/00-amof\n"
            "- **Branch or Detached State**: detached\n"
            "- **HEAD SHA**: `4c686d6d038607e925bc7ac18a10c52cceadbda5`\n"
            "- **origin/main SHA**: `4c686d6d038607e925bc7ac18a10c52cceadbda5`\n"
            "- **Cleanliness**: clean\n"
            "- **Mission-revision tests**: none found\n"
            "- **Hermes tests**: none found\n"
            "- **Evidence Paths**: /tmp/events.jsonl\n"
        )
        self.assertTrue(validation.ok)
        self.assertEqual(validation.normalized["repository_path"], "/tmp/ws-1/00-amof")

    def test_repo_inspection_supported_aliases_normalize_to_canonical_keys(self) -> None:
        validation = validate_repo_inspection_response(
            "- **Repository root**: /tmp/ws-1/00-amof\n"
            "- **Checkout state**: detached\n"
            "- **Head commit**: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n"
            "- **Origin Main SHA**: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n"
            "- **Working tree status**: clean\n"
            "- **Mission-revision tests**: none found\n"
            "- **Hermes tests**: none found\n"
            "- **Evidence**: /tmp/events.jsonl\n"
        )
        self.assertTrue(validation.ok)
        self.assertEqual(validation.normalized["origin_main_sha"], "4c686d6d038607e925bc7ac18a10c52cceadbda5")

    def test_repo_inspection_response_without_workspace_root_stays_incomplete(self) -> None:
        enriched = enrich_repo_inspection_response(
            "Repository Path: Not explicitly provided in the output.\n"
            "Branch Or Detached State: `HEAD` (detached)\n"
            "HEAD SHA: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n"
            "origin/main SHA: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n"
            "Cleanliness: clean\n"
            "Mission-revision tests: none found\n"
            "Hermes tests: none found\n"
            "Evidence Paths: /tmp/events.jsonl\n",
        )
        validation = validate_repo_inspection_response(enriched)
        self.assertFalse(validation.ok)
        self.assertIn("repository_path", validation.missing)

    def test_repo_inspection_placeholder_values_remain_incomplete(self) -> None:
        validation = validate_repo_inspection_response(
            "Repository Path: unknown\n"
            "Checkout State: not recorded\n"
            "HEAD SHA: n/a\n"
            "origin/main SHA: unknown\n"
            "Cleanliness: not recorded\n"
            "Mission-revision tests: none found\n"
            "Hermes tests: none found\n"
            "Evidence Paths: /tmp/events.jsonl\n"
        )
        self.assertFalse(validation.ok)
        self.assertIn("repository_path", validation.missing)
        self.assertIn("checkout_state", validation.missing)

    def test_repo_inspection_response_does_not_overwrite_explicit_conflicting_repo_path(self) -> None:
        enriched = enrich_repo_inspection_response(
            "Repository Path: /different/path\n"
            "Branch Or Detached State: detached\n"
            "HEAD SHA: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n",
            workspace_root="/tmp/ws-123/00-amof",
        )
        self.assertIn("Repository Path: /different/path", enriched)
        self.assertNotIn("Repository Path: /tmp/ws-123/00-amof", enriched)

    def test_repo_inspection_truthful_empty_test_matches_count_as_complete(self) -> None:
        tool_events = [
            {
                "event_id": "run:0001",
                "tool": "Glob",
                "args": {"glob_pattern": "**/tests/mission-revision/**"},
                "success": True,
                "output_preview": "No files matched the pattern.",
            },
            {
                "event_id": "run:0002",
                "tool": "Glob",
                "args": {"glob_pattern": "**/tests/hermes/**"},
                "success": True,
                "output_preview": "No files matched the pattern.",
            },
        ]
        validation = validate_repo_inspection_response(
            "Repository Path: /tmp/ws-1/00-amof\n"
            "Checkout State: detached\n"
            "HEAD SHA: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n"
            "origin/main SHA: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n"
            "Cleanliness: clean\n"
            "Evidence Paths: /tmp/events.jsonl\n",
            tool_events=tool_events,
        )
        self.assertTrue(validation.ok)
        self.assertEqual(validation.normalized["mission_revision_test_paths"], "none found")
        self.assertEqual(validation.normalized["hermes_read_only_test_paths"], "none found")

    def test_repo_inspection_prose_contradiction_completes_with_canonical_findings(self) -> None:
        # Reference live-defect shape: prose contradicts canonical evidence.
        # Prose is commentary only; it cannot conflict with canonical fields.
        validation = validate_repo_inspection_response(
            "Repository Path: /tmp/ws-1/00-amof\n"
            "Checkout State: detached\n"
            "HEAD SHA: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
            "origin/main SHA: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "Cleanliness: clean\n"
            "Evidence Paths: /tmp/evidence/proposal.sh\n",
            tool_events=_canonical_repo_tool_events(),
        )
        self.assertTrue(validation.ok)
        self.assertIsNone(validation.conflict)
        self.assertEqual(
            validation.normalized["head_sha"],
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        rendered = render_terminal_task_findings(validation)
        self.assertIn("HEAD SHA: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", rendered)
        self.assertNotIn("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", rendered)

    def test_repo_inspection_prose_placeholder_contradiction_completes(self) -> None:
        # The exact live failure: prose claimed the repository path was not
        # explicitly provided while canonical evidence proved it.
        validation = validate_repo_inspection_response(
            "Repository Path: Not explicitly provided in the output.\n"
            "Evidence Paths: some mislabeled prose value\n"
            "The inspection completed; see canonical metadata.\n",
            tool_events=_canonical_repo_tool_events(),
        )
        self.assertIsNone(validation.conflict)
        self.assertEqual(validation.normalized["repository_path"], "/tmp/ws-1/00-amof")

    def test_genuine_structured_evidence_conflict_still_returns_conflict(self) -> None:
        # Two successful canonical evidence sources disagreeing is a genuine
        # findings conflict and must not be weakened.
        tool_events = list(_canonical_repo_tool_events())
        tool_events.append(
            {
                "event_id": "run:0010",
                "tool": "ToolProposal",
                "success": True,
                "metadata": {
                    "stdout": "HEAD_SHA=cccccccccccccccccccccccccccccccccccccccc\n",
                    "script_path": "/tmp/evidence/proposal-2.sh",
                },
            }
        )
        validation = validate_repo_inspection_response(
            "Repository Path: /tmp/ws-1/00-amof\n"
            "Checkout State: detached\n"
            "Cleanliness: clean\n"
            "Evidence Paths: /tmp/evidence/proposal.sh\n",
            tool_events=tool_events,
        )
        self.assertFalse(validation.ok)
        self.assertEqual(validation.conflict["conflicting_field"], "head_sha")
        self.assertEqual(validation.conflict["conflict_source"], "structured_evidence")

    def test_normalized_findings_render_is_deterministic(self) -> None:
        first = normalize_repo_inspection_findings(
            _valid_repo_inspection_response(),
            tool_events=_canonical_repo_tool_events(),
        )
        second = normalize_repo_inspection_findings(
            _valid_repo_inspection_response(),
            tool_events=_canonical_repo_tool_events(),
        )
        rendered_first = render_canonical_findings(first.normalized)
        rendered_second = render_canonical_findings(second.normalized)
        self.assertEqual(
            rendered_first.encode("utf-8"),
            rendered_second.encode("utf-8"),
        )
        reparsed = validate_repo_inspection_response(
            rendered_first,
            tool_events=_canonical_repo_tool_events(),
        )
        self.assertTrue(reparsed.ok)

    def test_label_stripping_covers_all_repo_field_aliases(self) -> None:
        lines = ["Commentary that must survive label stripping."]
        for aliases in _REPO_FIELD_ALIASES.values():
            for alias in aliases:
                lines.append(f"{alias.title()}: prose-injected value")
                lines.append(f"- **{alias}**: prose-injected bullet value")
        stripped = strip_canonical_labels("\n".join(lines))
        self.assertIn("Commentary that must survive label stripping.", stripped)
        self.assertNotIn("prose-injected", stripped)
        # Stripped commentary can no longer state any canonical field.
        self.assertEqual(validate_repo_inspection_response(stripped).normalized, {})

    def test_glob_ignores_cursor_style_ignore_globs_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "tests").mkdir()
            (root / "tests" / "test_runner_registration.py").write_text("pass\n", encoding="utf-8")
            result = GlobTool().execute(
                glob_pattern="**/test_runner_registration.py",
                target_directory=str(root),
                ignore_globs=[],
            )
        self.assertTrue(result.success)
        self.assertIn("tests/test_runner_registration.py", result.output)

    def test_toolproposal_executes_python_scripts_for_repo_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            git_dir = root / ".git"
            git_dir.mkdir()
            cwd = os.getcwd()
            os.chdir(root)
            try:
                result = ToolProposalTool().execute(
                    purpose="Read repository metadata.",
                    mutation_intent=False,
                    allowed_paths=[".git/"],
                    allow_network=False,
                    timeout_seconds=30,
                    inputs=[],
                    outputs=["repository_path"],
                    rollback="No rollback needed.",
                    script="import os\nprint(os.path.abspath('.'))\n",
                )
            finally:
                os.chdir(cwd)
        self.assertTrue(result.success)
        self.assertIn(str(root), result.output)

    def test_agent_run_result_schema_accepts_legacy_and_detailed_failures(self) -> None:
        legacy = AgentRunResult(
            status="failed",
            session_id="20260612-121744",
            exit_code=1,
            stop_reason="tool_failed",
            final_text="Subtask failed.",
            plan_path=None,
            checkpoint_path=None,
            event_log_path="/tmp/events.jsonl",
            journal_path=None,
            budget_summary={"limit": None, "spent": 0.0, "remaining": None},
        ).to_dict()
        detailed = AgentRunResult(
            status="failed",
            session_id="20260612-121744",
            exit_code=1,
            stop_reason="tool_failed",
            final_text="Subtask failed.",
            task_findings="Repository Path: /tmp/repo",
            plan_path=None,
            checkpoint_path=None,
            event_log_path="/tmp/events.jsonl",
            journal_path=None,
            budget_summary={"limit": None, "spent": 0.0, "remaining": None},
            changed_paths=[],
            failure={
                "failure_class": "tool_failed",
                "safe_next_action": "Use ToolProposal for git status.",
                "failing_tool_id": "run:0001",
                "failing_tool_name": "Read",
                "failing_tool_call_index": 1,
                "tool_failure_class": "missing_file",
                "tool_failure_summary": "File not found: .git/status",
                "tool_failure_required": True,
                "tool_failure_evidence_ref": "run:0001",
                "required_for": "repository cleanliness",
                "required_or_optional": "required",
                "subtask_id": "1",
            },
        ).to_dict()
        _validate(AGENT_RUN_RESULT_SCHEMA_PATH, legacy)
        _validate(AGENT_RUN_RESULT_SCHEMA_PATH, detailed)


def _canonical_repo_tool_events() -> list[dict]:
    return [
        {
            "event_id": "run:0007",
            "tool": "ToolProposal",
            "success": True,
            "metadata": {
                "stdout": (
                    "repository_path=/tmp/ws-1/00-amof\n"
                    "branch_or_detached_state=detached\n"
                    "HEAD_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                    "origin/main_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                    "cleanliness=clean\n"
                ),
                "script_path": "/tmp/evidence/proposal.sh",
            },
        },
        {
            "event_id": "run:0008",
            "tool": "Glob",
            "args": {"glob_pattern": "**/tests/mission-revision/**"},
            "success": True,
            "output_preview": "No files matched the pattern.",
        },
        {
            "event_id": "run:0009",
            "tool": "Glob",
            "args": {"glob_pattern": "**/tests/hermes/**"},
            "success": True,
            "output_preview": "No files matched the pattern.",
        },
    ]


def _repo_inspection_task() -> str:
    return (
        "Inspect the canonical AMOF public repository.\n"
        "Report repository path, branch or detached state, HEAD SHA, origin/main SHA, "
        "clean or dirty status, and mission-revision plus Hermes read-only contract tests."
    )


def _valid_repo_inspection_response() -> str:
    return (
        "Repository Path: /tmp/workspaces/ws-1/00-amof\n"
        "Branch Or Detached State: detached\n"
        "HEAD SHA: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n"
        "origin/main SHA: 4c686d6d038607e925bc7ac18a10c52cceadbda5\n"
        "Cleanliness: clean\n"
        "Contract Test Paths: tests/test_handoff_agent_dispatch.py (mission-revision), "
        "tests/test_runner_registration.py (Hermes read-only)\n"
        "Evidence Paths: /tmp/events.jsonl, /tmp/result.json\n"
    )


if __name__ == "__main__":
    unittest.main()
