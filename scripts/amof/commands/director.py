"""Minimal Director dry-run planning commands."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from ..app_config import get_current_context_name
from ..app_paths import director_prepare_runs_dir, director_run_local_dir, materialized_runs_dir
from .workspace import materialize_from_intake_envelope
from ..runtime_workspace import RuntimeWorkspaceError
from ..run_scope import build_ad_hoc_run_scope, parse_ad_hoc_repo_spec

EXECUTION_COMMAND_PROFILES: dict[str, dict[str, Any]] = {
    "validation.git-status": {
        "argv": ["git", "status", "--short"],
        "description": "Verify that the prepared workspace stays clean after a bounded git status check.",
    },
    "validation.unit-test": {
        "argv": ["python3", "-B", "-m", "unittest", "tests.test_director_plan_materialization_command"],
        "description": "Run one focused Python unit-test module without writing bytecode into the materialized workspace.",
    },
    "validation.read-only-lint": {
        "argv": [
            "python3",
            "-B",
            "-c",
            (
                "import ast, pathlib, sys; "
                "[ast.parse(pathlib.Path(path).read_text(encoding='utf-8')) for path in sys.argv[1:]]"
            ),
            "scripts/amof/cli.py",
            "scripts/amof/commands/director.py",
            "tests/test_director_plan_materialization_command.py",
        ],
        "description": "Parse selected Python files as a read-only static sanity check without creating workspace artifacts.",
    },
}
DEFAULT_VALIDATION_COMMAND_ARGV = list(EXECUTION_COMMAND_PROFILES["validation.git-status"]["argv"])
ALLOWLISTED_EXECUTION_COMMANDS = (
    ["git", "status", "--short"],
    ["git", "rev-parse", "HEAD"],
    ["git", "diff", "--stat"],
)
DIRECTOR_RUN_LIFECYCLE_STATES = (
    "planned",
    "workspace_materialized",
    "execution_started",
    "execution_succeeded",
    "execution_failed",
    "summary_written",
    "ready_for_promotion",
)
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PROMOTION_READINESS_CLASSIFICATIONS = (
    "ready_for_promotion",
    "blocked",
    "failed",
    "needs_review",
)


class DirectorPlannerError(RuntimeError):
    """Raised when a Director dry-run plan cannot be produced truthfully."""


class ValidationStepFailedError(DirectorPlannerError):
    """Raised when the bounded execution step emitted a receipt but failed validation."""

    def __init__(self, message: str, receipt_path: Path) -> None:
        super().__init__(message)
        self.receipt_path = receipt_path


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise DirectorPlannerError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise DirectorPlannerError(f"{field_name} is required.")
    return normalized


def _require_mapping(value: Any, field_name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise DirectorPlannerError(f"{field_name} must be an object.")
    return value


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise DirectorPlannerError(f"{field_name} must be an array of strings.")
    result = [str(item).strip() for item in value if str(item).strip()]
    if not result:
        raise DirectorPlannerError(f"{field_name} must not be empty.")
    return result


def build_approved_plan_handoff_envelope(
    *,
    approval: Dict[str, Any],
    run_id: str,
    target_base_dir: str,
) -> Dict[str, Any]:
    approval_payload = _require_mapping(approval, "approval")
    _require_text(approval_payload.get("approval_id"), "approval.approval_id")
    approval_state = _require_text(approval_payload.get("approval_state"), "approval.approval_state")
    if approval_state != "approved":
        raise DirectorPlannerError("approval.approval_state must be 'approved' before handoff.")
    approval_artifact_path = _require_text(
        approval_payload.get("approval_artifact_path"),
        "approval.approval_artifact_path",
    )
    source_session = _require_mapping(approval_payload.get("source_session"), "approval.source_session")
    repo_truth = _require_mapping(approval_payload.get("repo_truth"), "approval.repo_truth")
    context_truth = _require_mapping(approval_payload.get("context_truth"), "approval.context_truth")
    plan_bundle = _require_mapping(
        approval_payload.get("plan_bundle") or approval_payload.get("plan_packet"),
        "approval.plan_bundle",
    )

    repo_source = (
        str(repo_truth.get("canonical_remote_url") or "").strip()
        or str(repo_truth.get("source_remote_url") or "").strip()
        or str(repo_truth.get("source_git_root") or "").strip()
        or str(repo_truth.get("source_repo_path") or "").strip()
    )
    repo_source = _require_text(repo_source, "approval.repo_truth.repo_source")
    expected_sha = _require_git_sha(
        str(repo_truth.get("origin_main_sha") or ""),
        field_name="approval.repo_truth.origin_main_sha",
    )
    objective = _require_text(plan_bundle.get("objective"), "approval.plan_bundle.objective")
    files_to_inspect = _string_list(
        plan_bundle.get("files_to_inspect") or [],
        "approval.plan_bundle.files_to_inspect",
    )
    ticket_id = (
        str(plan_bundle.get("ticket_id") or "").strip()
        or str(plan_bundle.get("proposed_ticket_id") or "").strip()
        or None
    )
    session_id = _require_text(source_session.get("session_id"), "approval.source_session.session_id")
    planning_receipt_path = _require_text(
        context_truth.get("planning_context_receipt_path"),
        "approval.context_truth.planning_context_receipt_path",
    )
    plan_result_path = _require_text(
        source_session.get("plan_result_path"),
        "approval.source_session.plan_result_path",
    )
    indexed_context_path = str(context_truth.get("indexed_context_path") or "").strip()
    planning_branch_ref = str(repo_truth.get("planning_branch_ref") or "").strip() or "origin/main"
    risk_reasons = [str(item).strip() for item in plan_bundle.get("risks") or [] if str(item).strip()]

    paths_read = [approval_artifact_path, plan_result_path, planning_receipt_path]
    if indexed_context_path:
        paths_read.append(indexed_context_path)

    source_summary = (
        f"Approved PlanBundle from session {session_id} targets {repo_source} at exact SHA {expected_sha}."
    )
    return {
        "result_kind": "director_intake_execution_contract",
        "ticket_summary": {
            "ticket_id": ticket_id,
            "rough_intent": objective,
            "bounded_goal": (
                "Convert one approved proposal-only PlanBundle into a workspace-materialization "
                "handoff without invoking agent execution."
            ),
            "task_kind": "other",
        },
        "inspection_scope": {
            "repos": [repo_source],
            "paths_read": paths_read,
            "runtime_sources": [],
            "notes": (
                f"Approved from chat session {session_id}; bounded indexed context files: "
                + ", ".join(files_to_inspect)
            ),
        },
        "source_truth": {
            "status": "confirmed",
            "summary": source_summary,
            "evidence": [
                {
                    "kind": "file",
                    "path": approval_artifact_path,
                    "summary": "Explicit operator approval artifact for the finalized PlanBundle.",
                    "freshness": "fresh",
                },
                {
                    "kind": "file",
                    "path": plan_result_path,
                    "summary": "Finalized proposal-only PlanBundle emitted by bounded chat.",
                    "freshness": "fresh",
                },
                {
                    "kind": "file",
                    "path": planning_receipt_path,
                    "summary": (
                        "Canonical planning-context receipt capturing source repo truth, planning clone "
                        "truth, and indexed context freshness."
                    ),
                    "freshness": "fresh",
                },
            ],
        },
        "runtime_truth": {
            "status": "confirmed",
            "summary": "This approved chat handoff does not authorize runtime execution, deployment, or agent launch.",
            "evidence": [
                {
                    "kind": "operator_statement",
                    "summary": (
                        "Approval is limited to writing an intake envelope that can be used only by an "
                        "explicit workspace materialization command."
                    ),
                    "freshness": "fresh",
                }
            ],
        },
        "workspace_truth": {
            "status": "confirmed",
            "summary": (
                "Workspace materialization remains owned by the existing workspace intake boundary and "
                "may occur only through explicit operator action."
            ),
            "evidence": [
                {
                    "kind": "contract",
                    "path": "contracts/director-intake-execution-contract.schema.json",
                    "summary": "Existing Director intake execution contract reused without schema churn.",
                    "freshness": "fresh",
                },
                {
                    "kind": "contract",
                    "path": "contracts/execution-handoff-result.schema.json",
                    "summary": "Existing workspace materialization handoff result contract remains authoritative.",
                    "freshness": "fresh",
                },
            ],
        },
        "allowed_mutations": [
            "materialize_per_run_workspace",
            "write_execution_handoff_result",
        ],
        "forbidden_mutations": [
            "agent_execution",
            "ticket_checkpoint",
            "promote_main",
            "deploy",
            "helm",
            "kubernetes",
            "image_build",
            "image_push",
            "runtime_sync",
        ],
        "validation_gates": [
            {
                "name": "approved_plan_artifact",
                "requirement": "approval.approval_state must remain approved before any workspace materialization.",
                "failure_action": "stop",
            },
            {
                "name": "exact_sha_checkout",
                "requirement": "Materialized workspace must resolve to approval.repo_truth.origin_main_sha exactly.",
                "failure_action": "stop",
            },
        ],
        "stop_conditions": [
            "stop on approval artifact state mismatch",
            "stop on workspace materialization failure",
            "stop on exact SHA mismatch",
        ],
        "risk_classification": {
            "level": "low",
            "reasons": risk_reasons
            or [
                "Approved chat handoff remains bounded to workspace materialization only.",
                "Agent execution and delivery operations stay explicitly forbidden from this contract.",
            ],
        },
        "executor_disposition": "replay_later",
        "next_executor_prompt": (
            "If the operator explicitly requests materialization, call the existing workspace intake boundary "
            "to materialize execution_handoff.workspace_materialization. Do not invoke agent execution from this contract."
        ),
        "execution_handoff": {
            "handoff_kind": "workspace_materialization_dry_run",
            "workspace_materialization": {
                "repo": repo_source,
                "expected_sha": expected_sha,
                "run_id": _require_text(run_id, "run_id"),
                "target_base_dir": _require_text(target_base_dir, "target_base_dir"),
                "branch_or_ref": planning_branch_ref,
                "candidate_sha": expected_sha,
            },
        },
        "ambiguities": [],
        "contract_version": "chat-approved-handoff-v1",
    }


def _build_plan_materialization_envelope(
    *,
    repo: str,
    expected_sha: str,
    run_id: str,
    target_base_dir: str,
) -> Dict[str, Any]:
    repo = _require_text(repo, "repo")
    expected_sha = _require_text(expected_sha, "expected_sha")
    run_id = _require_text(run_id, "run_id")
    target_base_dir = _require_text(target_base_dir, "target_base_dir")

    return {
        "result_kind": "director_intake_execution_contract",
        "ticket_summary": {
            "rough_intent": "Plan a dry-run per-run workspace materialization",
            "bounded_goal": "Produce one intake envelope that can drive isolated workspace materialization",
            "task_kind": "other",
        },
        "inspection_scope": {
            "repos": [repo],
            "paths_read": [
                "contracts/director-intake-execution-contract.schema.json",
                "contracts/per-run-workspace-materialization.md",
            ],
            "runtime_sources": [],
        },
        "source_truth": {
            "status": "confirmed",
            "summary": f"Materialize repo {repo} at exact SHA {expected_sha}.",
            "evidence": [
                {
                    "kind": "other",
                    "summary": "Planner inputs provided explicitly on the command line.",
                }
            ],
        },
        "runtime_truth": {
            "status": "confirmed",
            "summary": "This planner command does not deploy or mutate runtime surfaces.",
            "evidence": [
                {
                    "kind": "operator_statement",
                    "summary": "Dry-run planner only; no deploy, Kubernetes, or image activity.",
                }
            ],
        },
        "workspace_truth": {
            "status": "confirmed",
            "summary": "Execution should materialize an isolated per-run workspace from the handoff payload.",
            "evidence": [
                {
                    "kind": "contract",
                    "summary": "Use the per-run workspace materialization bridge.",
                }
            ],
        },
        "allowed_mutations": [
            "materialize_per_run_workspace",
            "write_execution_handoff_result",
        ],
        "forbidden_mutations": [
            "deploy",
            "helm",
            "kubernetes",
            "image_build",
            "image_push",
            "opensandbox",
            "runtime_sync",
        ],
        "validation_gates": [
            {
                "name": "exact_sha_checkout",
                "requirement": "Materialized workspace must resolve to the requested expected_sha.",
                "failure_action": "stop",
            }
        ],
        "stop_conditions": [
            "stop on materialization failure",
            "stop on exact SHA mismatch",
        ],
        "risk_classification": {
            "level": "low",
            "reasons": [
                "dry-run planner only",
                "execution remains bounded to isolated workspace materialization",
            ],
        },
        "executor_disposition": "replay_now",
        "next_executor_prompt": (
            "Materialize the per-run workspace from execution_handoff.workspace_materialization "
            "and emit the handoff result."
        ),
        "execution_handoff": {
            "handoff_kind": "workspace_materialization_dry_run",
            "workspace_materialization": {
                "repo": repo,
                "expected_sha": expected_sha,
                "run_id": run_id,
                "target_base_dir": target_base_dir,
            },
        },
    }


def plan_materialization_envelope(
    *,
    repo: str,
    expected_sha: str,
    run_id: str,
    target_base_dir: str,
    output_path: str | Path,
) -> Path:
    output = Path(output_path).resolve(strict=False)
    payload = _build_plan_materialization_envelope(
        repo=repo,
        expected_sha=expected_sha,
        run_id=run_id,
        target_base_dir=target_base_dir,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_status_dirty(path: Path) -> bool:
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(path),
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "git status --short failed"
        raise DirectorPlannerError(f"Failed to inspect git status for {path}: {message}")
    return bool(proc.stdout.strip())


def _run_git_capture(args: list[str], *, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise DirectorPlannerError(f"git {' '.join(args)} failed: {message}")
    return proc.stdout.strip()


def _require_git_sha(value: str, *, field_name: str) -> str:
    normalized = _require_text(value, field_name).lower()
    if not GIT_SHA_PATTERN.fullmatch(normalized):
        raise DirectorPlannerError(f"{field_name} must resolve to a 40-character lowercase git SHA.")
    return normalized


def _resolve_repo_ref_to_sha(repo: str, ref: str) -> str:
    normalized_repo = _require_text(repo, "repo")
    normalized_ref = _require_text(ref, "ref")
    repo_path = Path(normalized_repo).expanduser()
    if repo_path.exists():
        if not repo_path.is_dir():
            raise DirectorPlannerError("repo must point to a directory when using a local repository path.")
        resolved = _run_git_capture(
            ["rev-parse", "--verify", f"{normalized_ref}^{{commit}}"],
            cwd=repo_path,
        )
        return _require_git_sha(resolved, field_name="resolved_sha")

    if GIT_SHA_PATTERN.fullmatch(normalized_ref.lower()):
        return normalized_ref.lower()

    remote_refs = _run_git_capture(
        [
            "ls-remote",
            normalized_repo,
            normalized_ref,
            f"refs/heads/{normalized_ref}",
            f"refs/tags/{normalized_ref}",
            f"refs/tags/{normalized_ref}^{{}}",
        ]
    )
    if not remote_refs:
        raise DirectorPlannerError(f"Unable to resolve ref {normalized_ref!r} for repo {normalized_repo!r}.")

    ref_to_sha: dict[str, str] = {}
    for line in remote_refs.splitlines():
        sha, _, name = line.partition("\t")
        if not name:
            continue
        ref_to_sha[name] = _require_git_sha(sha, field_name="resolved_sha")
    for candidate in (
        normalized_ref,
        f"refs/heads/{normalized_ref}",
        f"refs/tags/{normalized_ref}^{{}}",
        f"refs/tags/{normalized_ref}",
    ):
        if candidate in ref_to_sha:
            return ref_to_sha[candidate]
    raise DirectorPlannerError(f"Unable to resolve ref {normalized_ref!r} for repo {normalized_repo!r}.")


def _normalize_command_argv(command_argv: list[str] | tuple[str, ...] | None) -> list[str]:
    if command_argv is None:
        return list(DEFAULT_VALIDATION_COMMAND_ARGV)
    if not isinstance(command_argv, (list, tuple)):
        raise DirectorPlannerError("command_argv must be a list or tuple of strings.")
    normalized: list[str] = []
    for item in command_argv:
        if not isinstance(item, str):
            raise DirectorPlannerError("command_argv entries must be strings.")
        value = item.strip()
        if not value:
            raise DirectorPlannerError("command_argv entries must be non-empty strings.")
        normalized.append(value)
    if not normalized:
        raise DirectorPlannerError("command_argv must not be empty.")
    return normalized


def _parse_execute_command_json(value: str | None) -> list[str] | None:
    if value is None:
        return None
    try:
        payload = json.loads(_require_text(value, "execute_command"))
    except json.JSONDecodeError as exc:
        raise DirectorPlannerError(f"execute_command must be valid JSON: {exc}") from exc
    return _normalize_command_argv(payload)


def _resolve_execute_profile(profile_name: str | None) -> list[str] | None:
    if profile_name is None:
        return None
    normalized = _require_text(profile_name, "execute_profile")
    profile = EXECUTION_COMMAND_PROFILES.get(normalized)
    if profile is None:
        available = ", ".join(sorted(EXECUTION_COMMAND_PROFILES))
        raise DirectorPlannerError(
            f"execute_profile must name a known validation profile. Available profiles: {available}"
        )
    return _normalize_command_argv(profile["argv"])


def _ensure_allowlisted_command(command_argv: list[str]) -> list[str]:
    if any(command_argv == allowed for allowed in ALLOWLISTED_EXECUTION_COMMANDS):
        return command_argv
    allowed_text = ", ".join(json.dumps(list(allowed)) for allowed in ALLOWLISTED_EXECUTION_COMMANDS)
    raise DirectorPlannerError(
        f"execute_command must match an allowlisted argv exactly. Allowed commands: {allowed_text}"
    )


def _write_execution_step_receipt(
    *,
    output_path: str | Path,
    payload: dict[str, Any],
) -> Path:
    receipt_path = Path(output_path).resolve(strict=False)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return receipt_path


def _load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _json_bool(payload: dict[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _json_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _lifecycle_event(state: str) -> dict[str, str]:
    if state not in DIRECTOR_RUN_LIFECYCLE_STATES:
        raise DirectorPlannerError(f"Unknown lifecycle state: {state}")
    return {
        "state": state,
        "timestamp": _now_iso(),
    }


def write_run_summary(
    *,
    output_path: str | Path,
    run_id: str,
    repo: str,
    expected_sha: str,
    output_dir: str | Path,
    intake_path: str | Path,
    plan_result_path: str | Path,
    workspace_receipt_path: str | Path,
    execution_handoff_result_path: str | Path,
    execution_step_receipt_path: str | Path | None = None,
    final_status: str,
    lifecycle_state: str,
    lifecycle_events: list[dict[str, str]],
    failure_message: str | None = None,
) -> Path:
    workspace_receipt = _load_json(workspace_receipt_path)
    execution_handoff_result = _load_json(execution_handoff_result_path)
    execution_step_receipt = (
        _load_json(execution_step_receipt_path) if execution_step_receipt_path is not None else None
    )
    payload = {
        "run_id": _require_text(run_id, "run_id"),
        "repo": _require_text(repo, "repo"),
        "expected_sha": _require_text(expected_sha, "expected_sha"),
        "output_dir": str(Path(output_dir).resolve(strict=False)),
        "intake_path": str(Path(intake_path).resolve(strict=False)),
        "receipt_paths": {
            "director_plan_result_path": str(Path(plan_result_path).resolve(strict=False)),
            "workspace_receipt_path": str(Path(workspace_receipt_path).resolve(strict=False)),
            "execution_handoff_result_path": str(Path(execution_handoff_result_path).resolve(strict=False)),
            "execution_step_receipt_path": (
                str(Path(execution_step_receipt_path).resolve(strict=False))
                if execution_step_receipt_path is not None
                else None
            ),
        },
        "receipts": {
            "director_plan_result": _load_json(plan_result_path),
            "workspace_receipt": workspace_receipt,
            "execution_handoff_result": execution_handoff_result,
            "execution_step_receipt": execution_step_receipt,
        },
        "workspace_path": workspace_receipt["workspace_path"],
        "actual_sha": workspace_receipt["actual_sha"],
        "final_status": _require_text(final_status, "final_status"),
        "lifecycle_state": _require_text(lifecycle_state, "lifecycle_state"),
        "lifecycle_events": lifecycle_events,
        "validation_summary": {
            "actual_sha_matches_expected": workspace_receipt["actual_sha"] == expected_sha,
            "workspace_clean_after_materialization": not workspace_receipt["dirty"],
            "execution_exit_code": (
                execution_step_receipt["exit_code"] if execution_step_receipt is not None else None
            ),
            "source_repo_mutated": (
                execution_step_receipt["source_repo_mutated"]
                if execution_step_receipt is not None
                else None
            ),
        },
        "failure_message": failure_message,
        "timestamp": _now_iso(),
    }
    summary_path = Path(output_path).resolve(strict=False)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return summary_path


def execute_validation_step(
    *,
    run_id: str,
    workspace_path: str | Path,
    source_repo_path: str,
    output_path: str | Path,
    command_argv: list[str] | tuple[str, ...] | None = None,
) -> Path:
    workspace = Path(workspace_path).resolve(strict=False)
    source_repo = Path(source_repo_path).resolve(strict=False)
    normalized_command_argv = _normalize_command_argv(command_argv)
    dirty_before = _git_status_dirty(workspace)
    source_dirty_before = _git_status_dirty(source_repo) if source_repo.exists() else False
    started_at = _now_iso()
    proc = subprocess.run(
        normalized_command_argv,
        cwd=str(workspace),
        check=False,
        capture_output=True,
        text=True,
    )
    finished_at = _now_iso()
    dirty_after = _git_status_dirty(workspace)
    source_dirty_after = _git_status_dirty(source_repo) if source_repo.exists() else source_dirty_before
    source_repo_mutated = source_dirty_before != source_dirty_after
    receipt_path = _write_execution_step_receipt(
        output_path=output_path,
        payload={
            "run_id": _require_text(run_id, "run_id"),
            "workspace_path": str(workspace),
            "command_argv": normalized_command_argv,
            "cwd": str(workspace),
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "started_at": started_at,
            "finished_at": finished_at,
            "dirty_status_before": dirty_before,
            "dirty_status_after": dirty_after,
            "source_repo_mutated": source_repo_mutated,
        },
    )

    if proc.returncode != 0:
        raise ValidationStepFailedError(
            f"Bounded validation command failed with exit code {proc.returncode}.",
            receipt_path,
        )
    if dirty_before:
        raise ValidationStepFailedError(
            "Prepared workspace was unexpectedly dirty before bounded execution.",
            receipt_path,
        )
    if dirty_after:
        raise ValidationStepFailedError(
            "Prepared workspace became dirty after bounded execution.",
            receipt_path,
        )
    if source_repo_mutated:
        raise ValidationStepFailedError(
            "Source repo dirty state changed during bounded execution.",
            receipt_path,
        )

    return receipt_path


def write_plan_materialization_result(
    *,
    repo: str,
    expected_sha: str,
    run_id: str,
    intake_path: str | Path,
    target_base_dir: str,
    output_path: str | Path,
) -> Path:
    result_path = Path(output_path).resolve(strict=False)
    payload = {
        "result_kind": "director_plan_materialization_result",
        "status": "planned",
        "run_id": _require_text(run_id, "run_id"),
        "repo": _require_text(repo, "repo"),
        "expected_sha": _require_text(expected_sha, "expected_sha"),
        "intake_path": str(Path(intake_path).resolve(strict=False)),
        "target_base_dir": _require_text(target_base_dir, "target_base_dir"),
        "timestamp": _now_iso(),
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return result_path


def prepare_run_artifacts(
    *,
    repo: str,
    expected_sha: str,
    run_id: str,
    output_dir: str | Path,
    target_base_dir: str | Path | None = None,
    execute_noop: bool = False,
    validation_command_argv: list[str] | tuple[str, ...] | None = None,
) -> dict[str, str]:
    output_root = Path(output_dir).resolve(strict=False)
    output_root.mkdir(parents=True, exist_ok=True)
    materialization_root = (
        Path(target_base_dir).resolve(strict=False)
        if target_base_dir is not None
        else (output_root / "materialized-runs").resolve(strict=False)
    )
    intake_path = plan_materialization_envelope(
        repo=repo,
        expected_sha=expected_sha,
        run_id=run_id,
        target_base_dir=str(materialization_root),
        output_path=output_root / "director-intake.json",
    )
    plan_result_path = write_plan_materialization_result(
        repo=repo,
        expected_sha=expected_sha,
        run_id=run_id,
        intake_path=intake_path,
        target_base_dir=str(materialization_root),
        output_path=output_root / "director-plan-result.json",
    )
    receipt, handoff_result_path = materialize_from_intake_envelope(
        intake_path=str(intake_path),
    )
    result = {
        "output_dir": str(output_root),
        "intake_path": str(intake_path),
        "director_plan_result_path": str(plan_result_path),
        "workspace_receipt_path": str(receipt.receipt_path),
        "execution_handoff_result_path": str(handoff_result_path),
    }
    lifecycle_events = [
        _lifecycle_event("planned"),
        _lifecycle_event("workspace_materialized"),
    ]
    execution_step_receipt_path: Path | None = None
    final_status = "prepared"
    lifecycle_state = "ready_for_promotion"
    failure_message: str | None = None
    if execute_noop:
        lifecycle_events.append(_lifecycle_event("execution_started"))
        try:
            execution_step_receipt_path = execute_validation_step(
                run_id=run_id,
                workspace_path=receipt.workspace_path,
                source_repo_path=repo,
                output_path=output_root / "execution-step-receipt.json",
                command_argv=validation_command_argv,
            )
        except ValidationStepFailedError as exc:
            execution_step_receipt_path = exc.receipt_path
            lifecycle_events.append(_lifecycle_event("execution_failed"))
            final_status = "failed"
            lifecycle_state = "execution_failed"
            failure_message = str(exc)
        else:
            lifecycle_events.append(_lifecycle_event("execution_succeeded"))
            final_status = "executed"
        result["execution_step_receipt_path"] = str(execution_step_receipt_path)
    lifecycle_events.append(_lifecycle_event("summary_written"))
    if lifecycle_state != "execution_failed":
        lifecycle_events.append(_lifecycle_event("ready_for_promotion"))
    run_summary_path = write_run_summary(
        output_path=output_root / "run-summary.json",
        run_id=run_id,
        repo=repo,
        expected_sha=expected_sha,
        output_dir=output_root,
        intake_path=intake_path,
        plan_result_path=plan_result_path,
        workspace_receipt_path=receipt.receipt_path,
        execution_handoff_result_path=handoff_result_path,
        execution_step_receipt_path=execution_step_receipt_path,
        final_status=final_status,
        lifecycle_state=lifecycle_state,
        lifecycle_events=lifecycle_events,
        failure_message=failure_message,
    )
    result["run_summary_path"] = str(run_summary_path)
    if lifecycle_state == "execution_failed":
        raise DirectorPlannerError(_require_text(failure_message, "failure_message"))
    return result


def run_local(
    *,
    repo: str,
    ref: str | None,
    run_id: str,
    output_dir: str | Path,
    execute_profile: str,
) -> dict[str, Any]:
    repo_spec = parse_ad_hoc_repo_spec(repo, ref)
    normalized_repo = _require_text(repo_spec["source"], "repo")
    normalized_ref = _require_text(repo_spec["ref"], "ref")
    normalized_run_id = _require_text(run_id, "run_id")
    execute_command_argv = _resolve_execute_profile(execute_profile)
    if execute_command_argv is None:
        raise DirectorPlannerError("run-local requires --execute-profile.")
    resolved_sha = _resolve_repo_ref_to_sha(normalized_repo, normalized_ref)
    output_root = Path(output_dir).resolve(strict=False) / normalized_run_id
    scope = build_ad_hoc_run_scope(
        scope_id=normalized_run_id,
        context_name=get_current_context_name(),
        repo_name=repo_spec["name"],
        repo_source=normalized_repo,
        source_kind=repo_spec["source_kind"],
        ref=normalized_ref,
        resolved_sha=resolved_sha,
    )
    result = prepare_run_artifacts(
        repo=normalized_repo,
        expected_sha=resolved_sha,
        run_id=normalized_run_id,
        output_dir=output_root,
        target_base_dir=materialized_runs_dir(),
        execute_noop=True,
        validation_command_argv=execute_command_argv,
    )
    return {
        "repo": normalized_repo,
        "repo_name": repo_spec["name"],
        "ref": normalized_ref,
        "resolved_sha": resolved_sha,
        "scope": scope.to_dict(),
        "execute_profile": _require_text(execute_profile, "execute_profile"),
        **result,
    }


def classify_promotion_readiness(
    *,
    run_summary_path: str | Path,
) -> dict[str, Any]:
    summary_path = Path(run_summary_path).resolve(strict=False)
    summary = _load_json(summary_path)
    validation_summary = summary.get("validation_summary")
    if not isinstance(validation_summary, dict):
        raise DirectorPlannerError("run_summary.validation_summary must be an object.")

    final_status = summary.get("final_status")
    lifecycle_state = summary.get("lifecycle_state")
    failure_message = summary.get("failure_message")
    actual_sha_matches_expected = _json_bool(validation_summary, "actual_sha_matches_expected")
    workspace_clean = _json_bool(validation_summary, "workspace_clean_after_materialization")
    source_repo_mutated = _json_bool(validation_summary, "source_repo_mutated")
    execution_exit_code = _json_int(validation_summary, "execution_exit_code")

    reasons: list[str] = []
    classification = "needs_review"

    if final_status == "failed":
        reasons.append("run_summary.final_status is failed.")
    if lifecycle_state == "execution_failed":
        reasons.append("run_summary.lifecycle_state is execution_failed.")
    if isinstance(failure_message, str) and failure_message.strip():
        reasons.append("run_summary.failure_message is present.")
    if execution_exit_code is not None and execution_exit_code != 0:
        reasons.append(f"validation_summary.execution_exit_code is {execution_exit_code}.")
    if reasons:
        classification = "failed"
    else:
        blocked_reasons: list[str] = []
        if actual_sha_matches_expected is False:
            blocked_reasons.append("validation_summary.actual_sha_matches_expected is false.")
        if workspace_clean is False:
            blocked_reasons.append("validation_summary.workspace_clean_after_materialization is false.")
        if source_repo_mutated is True:
            blocked_reasons.append("validation_summary.source_repo_mutated is true.")
        if blocked_reasons:
            classification = "blocked"
            reasons = blocked_reasons
        else:
            ready_reasons = [
                final_status == "executed",
                lifecycle_state == "ready_for_promotion",
                actual_sha_matches_expected is True,
                workspace_clean is True,
                execution_exit_code == 0,
                source_repo_mutated is False,
                failure_message is None,
            ]
            if all(ready_reasons):
                classification = "ready_for_promotion"
                reasons = ["Run summary satisfies the bounded local promotion-readiness gates."]
            else:
                if final_status != "executed":
                    reasons.append(f"run_summary.final_status is {final_status!r}, not 'executed'.")
                if lifecycle_state != "ready_for_promotion":
                    reasons.append(
                        f"run_summary.lifecycle_state is {lifecycle_state!r}, not 'ready_for_promotion'."
                    )
                if actual_sha_matches_expected is not True:
                    reasons.append(
                        "validation_summary.actual_sha_matches_expected is not explicitly true."
                    )
                if workspace_clean is not True:
                    reasons.append(
                        "validation_summary.workspace_clean_after_materialization is not explicitly true."
                    )
                if execution_exit_code != 0:
                    reasons.append(
                        "validation_summary.execution_exit_code does not confirm a clean executed run."
                    )
                if source_repo_mutated is not False:
                    reasons.append(
                        "validation_summary.source_repo_mutated does not confirm the source repo stayed unchanged."
                    )
                if failure_message is not None:
                    reasons.append("run_summary.failure_message is not null.")
                if not reasons:
                    reasons.append("Run summary requires operator review.")

    if classification not in PROMOTION_READINESS_CLASSIFICATIONS:
        raise DirectorPlannerError(f"Unknown promotion readiness classification: {classification}")

    expected_sha = summary.get("expected_sha")
    actual_sha = summary.get("actual_sha")
    return {
        "result_kind": "promotion_readiness_result",
        "classification": classification,
        "reasons": reasons,
        "run_summary_path": str(summary_path),
        "run_id": _require_text(summary.get("run_id"), "run_summary.run_id"),
        "expected_sha": _require_git_sha(str(expected_sha), field_name="run_summary.expected_sha"),
        "actual_sha": _require_git_sha(str(actual_sha), field_name="run_summary.actual_sha"),
        "timestamp": _require_text(summary.get("timestamp"), "run_summary.timestamp"),
    }


def write_promotion_readiness_result(
    *,
    output_path: str | Path,
    payload: dict[str, Any],
) -> Path:
    result_path = Path(output_path).resolve(strict=False)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return result_path


def _default_promotion_readiness_output_path(run_summary_path: str | Path) -> Path:
    summary_path = Path(run_summary_path).resolve(strict=False)
    return summary_path.parent / "promotion-readiness-result.json"


def _build_promote_main_command_hint(
    *,
    repo_name: str,
    ticket_id: str | None,
    candidate_branch: str | None,
    source_sha: str,
    expected_main_sha: str | None,
    promotion_reason: str | None,
    promotion_readiness_result_path: str | Path,
    promote_mode: str,
) -> tuple[str, list[str]]:
    repo_text = _require_text(repo_name, "repo_name")
    mode_text = _require_text(promote_mode, "promote_mode")
    if mode_text not in {"dry-run", "push"}:
        raise DirectorPlannerError("promote_mode must be one of: dry-run, push.")

    missing_inputs: list[str] = []

    def _value_or_placeholder(value: str | None, *, field_name: str, placeholder: str) -> str:
        if value is None:
            missing_inputs.append(field_name)
            return placeholder
        return _require_text(value, field_name)

    ticket_text = _value_or_placeholder(ticket_id, field_name="ticket_id", placeholder="<ticket-id>")
    candidate_branch_text = _value_or_placeholder(
        candidate_branch,
        field_name="candidate_branch",
        placeholder="<candidate-branch>",
    )
    expected_main_sha_text = _value_or_placeholder(
        expected_main_sha,
        field_name="expected_main_sha",
        placeholder="<expected-main-sha>",
    )
    promotion_reason_text = _value_or_placeholder(
        promotion_reason,
        field_name="promotion_reason",
        placeholder="<promotion-reason>",
    )
    command_parts = [
        "python3",
        "scripts/amof.py",
        "promote-main",
        "--repo",
        repo_text,
        "--ticket-id",
        ticket_text,
        "--candidate-branch",
        candidate_branch_text,
        "--source-sha",
        _require_git_sha(source_sha, field_name="source_sha"),
        "--expected-main-sha",
        expected_main_sha_text,
        "--promotion-reason",
        promotion_reason_text,
        "--require-promotion-readiness-result",
        str(Path(promotion_readiness_result_path).resolve(strict=False)),
        "--" + mode_text,
    ]
    return " ".join(shlex.quote(part) for part in command_parts), missing_inputs


def readiness_report(
    *,
    run_summary_path: str | Path,
    promotion_readiness_output: str | Path | None = None,
    repo_name: str = "amof",
    ticket_id: str | None = None,
    candidate_branch: str | None = None,
    expected_main_sha: str | None = None,
    promotion_reason: str | None = None,
    promote_mode: str = "dry-run",
) -> dict[str, Any]:
    summary_path = Path(run_summary_path).resolve(strict=False)
    readiness_payload = classify_promotion_readiness(run_summary_path=summary_path)
    readiness_output_path = (
        Path(promotion_readiness_output).resolve(strict=False)
        if promotion_readiness_output is not None
        else _default_promotion_readiness_output_path(summary_path)
    )
    write_promotion_readiness_result(output_path=readiness_output_path, payload=readiness_payload)
    command_hint, missing_inputs = _build_promote_main_command_hint(
        repo_name=repo_name,
        ticket_id=ticket_id,
        candidate_branch=candidate_branch,
        source_sha=readiness_payload["actual_sha"],
        expected_main_sha=expected_main_sha,
        promotion_reason=promotion_reason,
        promotion_readiness_result_path=readiness_output_path,
        promote_mode=promote_mode,
    )
    promotion_readiness = _require_text(readiness_payload["classification"], "classification")
    return {
        "result_kind": "director_readiness_report",
        "run_summary_path": str(summary_path),
        "promotion_readiness_result_path": str(readiness_output_path),
        "promotion_readiness": promotion_readiness,
        "candidate_source_sha": readiness_payload["actual_sha"],
        "expected_sha": readiness_payload["expected_sha"],
        "actual_sha": readiness_payload["actual_sha"],
        "run_id": readiness_payload["run_id"],
        "ready_for_promotion": promotion_readiness == "ready_for_promotion",
        "ready_for_promote_main_command": command_hint,
        "missing_promote_main_inputs": missing_inputs,
    }


def cmd_director(args: argparse.Namespace) -> int:
    action = getattr(args, "director_cmd", None)
    try:
        if action == "plan-materialization":
            output_path = plan_materialization_envelope(
                repo=getattr(args, "repo", None),
                expected_sha=getattr(args, "expected_sha", None),
                run_id=getattr(args, "run_id", None),
                target_base_dir=getattr(args, "target_base_dir", str(materialized_runs_dir())),
                output_path=getattr(args, "output", None),
            )
            print(output_path)
            return 0
        if action == "prepare-run":
            execute_noop = bool(getattr(args, "execute_noop", False))
            execute_profile_argv = _resolve_execute_profile(getattr(args, "execute_profile", None))
            raw_execute_command_argv = _parse_execute_command_json(getattr(args, "execute_command", None))
            allow_raw_execute_command = bool(getattr(args, "allow_raw_execute_command", False))
            selected_execute_modes = sum(
                (
                    1 if execute_noop else 0,
                    1 if execute_profile_argv is not None else 0,
                    1 if raw_execute_command_argv is not None else 0,
                )
            )
            if selected_execute_modes > 1:
                raise DirectorPlannerError(
                    "Use only one of --execute-noop, --execute-profile, or --execute-command."
                )
            if allow_raw_execute_command and raw_execute_command_argv is None:
                raise DirectorPlannerError(
                    "--allow-raw-execute-command requires --execute-command."
                )
            execute_command_argv: list[str] | None = None
            if execute_profile_argv is not None:
                execute_noop = True
                execute_command_argv = execute_profile_argv
            if raw_execute_command_argv is not None:
                if not allow_raw_execute_command:
                    raise DirectorPlannerError(
                        "--execute-command is restricted to developer use. "
                        "Pass --allow-raw-execute-command or prefer --execute-profile."
                    )
                execute_noop = True
                execute_command_argv = _ensure_allowlisted_command(raw_execute_command_argv)
            output_root = Path(
                getattr(args, "output_dir", str(director_prepare_runs_dir()))
            ).resolve(strict=False) / _require_text(getattr(args, "run_id", None), "run_id")
            result = prepare_run_artifacts(
                repo=getattr(args, "repo", None),
                expected_sha=getattr(args, "expected_sha", None),
                run_id=getattr(args, "run_id", None),
                output_dir=output_root,
                target_base_dir=materialized_runs_dir(),
                execute_noop=execute_noop,
                validation_command_argv=execute_command_argv,
            )
            print(json.dumps(result, indent=2))
            return 0
        if action == "run-local":
            result = run_local(
                repo=getattr(args, "repo", None),
                ref=getattr(args, "ref", None),
                run_id=getattr(args, "run_id", None),
                output_dir=getattr(args, "output_dir", str(director_run_local_dir())),
                execute_profile=getattr(args, "execute_profile", None),
            )
            print(json.dumps(result, indent=2))
            return 0
        if action == "classify-promotion-readiness":
            result = classify_promotion_readiness(
                run_summary_path=getattr(args, "run_summary", None),
            )
            output_path = getattr(args, "output", None)
            if output_path:
                write_promotion_readiness_result(output_path=output_path, payload=result)
            print(json.dumps(result, indent=2))
            return 0
        if action == "readiness-report":
            result = readiness_report(
                run_summary_path=getattr(args, "run_summary", None),
                promotion_readiness_output=getattr(args, "promotion_readiness_output", None),
                repo_name=getattr(args, "repo_name", "amof"),
                ticket_id=getattr(args, "ticket_id", None),
                candidate_branch=getattr(args, "candidate_branch", None),
                expected_main_sha=getattr(args, "expected_main_sha", None),
                promotion_reason=getattr(args, "promotion_reason", None),
                promote_mode=getattr(args, "promote_mode", "dry-run"),
            )
            print(json.dumps(result, indent=2))
            return 0
    except (DirectorPlannerError, RuntimeWorkspaceError) as exc:
        sys.stderr.write(f"[director] {exc}\n")
        return 1

    sys.stderr.write(
        "Usage: amof director <plan-materialization|prepare-run|run-local|classify-promotion-readiness|readiness-report> ...\n"
    )
    return 1


__all__ = [
    "build_approved_plan_handoff_envelope",
    "cmd_director",
    "classify_promotion_readiness",
    "execute_validation_step",
    "plan_materialization_envelope",
    "prepare_run_artifacts",
    "readiness_report",
    "run_local",
    "write_promotion_readiness_result",
    "write_run_summary",
    "write_plan_materialization_result",
]
