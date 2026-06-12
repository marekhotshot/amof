"""Helpers for read-only tool failure semantics and repo-inspection validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from .plan_execute_control import is_read_only_repository_inspection

READ_ONLY_REPO_INSPECTION_TOOLS: tuple[str, ...] = (
    "Read",
    "InspectFiles",
    "ToolProposal",
    "Glob",
    "LS",
)

_SHA40_RE = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)
_LINT_INTENT_RE = re.compile(r"\b(lint|linter|diagnostic(?:s)?)\b", re.IGNORECASE)
_FILE_NOT_FOUND_RE = re.compile(r"\bfile not found\b|\bdoesn't exist\b", re.IGNORECASE)
_NOT_A_FILE_RE = re.compile(r"\bnot a file\b|\bis a directory\b", re.IGNORECASE)
_NO_PATHS_RE = re.compile(r"\bno paths provided\b", re.IGNORECASE)
_LINTER_UNAVAILABLE_RE = re.compile(r"\blinter not configured\b|\bnot found in \$PATH\b", re.IGNORECASE)
_INVALID_ARGS_RE = re.compile(r"\binvalid\b.*\b(argument|args?)\b|\bmust be\b", re.IGNORECASE)


@dataclass(frozen=True)
class ToolFailureDetail:
    tool_id: str
    tool_name: str
    call_index: int
    arguments_redacted: Dict[str, Any]
    failure_class: str
    exit_code: int | None
    exception_type: str | None
    error_summary: str
    required_or_optional: str
    required_for: str
    safe_next_action: str
    evidence_ref: str
    subtask_id: str | None = None

    @property
    def required(self) -> bool:
        return self.required_or_optional == "required"

    def to_failure_dict(self) -> Dict[str, Any]:
        return {
            "failure_class": "tool_failed",
            "failure_type": "tool_failed",
            "failure_reason": self.error_summary,
            "retry_eligible": True,
            "safe_next_action": self.safe_next_action,
            "evidence_ref": self.evidence_ref,
            "raw_error_excerpt": self.error_summary,
            "failing_tool_id": self.tool_id,
            "failing_tool_name": self.tool_name,
            "failing_tool_call_index": self.call_index,
            "tool_failure_class": self.failure_class,
            "tool_failure_summary": self.error_summary,
            "tool_failure_required": self.required,
            "tool_failure_evidence_ref": self.evidence_ref,
            "required_for": self.required_for,
            "required_or_optional": self.required_or_optional,
            "subtask_id": self.subtask_id,
        }


@dataclass(frozen=True)
class RepoInspectionValidation:
    ok: bool
    missing: List[str]


def repo_inspection_runner_tools(tool_names: Sequence[str]) -> List[str]:
    allowed = set(READ_ONLY_REPO_INSPECTION_TOOLS)
    return [name for name in tool_names if name in allowed]


def repo_inspection_task_guidance() -> str:
    return (
        "Repository-inspection mode:\n"
        "- Use ToolProposal for git metadata such as branch/detached state, HEAD SHA, "
        "origin/main SHA, and cleanliness.\n"
        "- Use Read, InspectFiles, Glob, and LS only for file presence or direct file contents.\n"
        "- Do not call ReadLints unless the task explicitly asks for lint diagnostics.\n"
        "- Do not guess pseudo-files like .git/status.\n"
        "- Final answer must include exact labels: Repository Path, Branch Or Detached State, "
        "HEAD SHA, origin/main SHA, Cleanliness, Contract Test Paths, Evidence Paths."
    )


def validate_repo_inspection_response(final_response: str) -> RepoInspectionValidation:
    text = str(final_response or "")
    missing: List[str] = []

    path_match = _field_value(
        text,
        (
            "Repository Path",
            "Repository path",
        ),
    )
    if not path_match or path_match.strip() in {".git", ""}:
        missing.append("repository_path")

    branch_value = _field_value(
        text,
        (
            "Branch Or Detached State",
            "Branch or Detached State",
            "Branch",
        ),
    )
    if not branch_value:
        missing.append("branch_or_detached_state")
    else:
        lowered = branch_value.strip().lower()
        if lowered != "detached" and _SHA40_RE.fullmatch(branch_value.strip()):
            missing.append("branch_or_detached_state")

    head_value = _field_value(text, ("HEAD SHA", "HEAD"))
    if not head_value or not _SHA40_RE.search(head_value):
        missing.append("head_sha")

    origin_value = _field_value(text, ("origin/main SHA", "Origin/Main SHA", "origin/main"))
    if not origin_value or not _SHA40_RE.search(origin_value):
        missing.append("origin_main_sha")

    clean_value = _field_value(text, ("Cleanliness", "Clean or Dirty Status", "Clean Status"))
    if not clean_value or not re.search(r"\b(clean|dirty)\b", clean_value, re.IGNORECASE):
        missing.append("cleanliness")

    lower_text = text.lower()
    if "mission-revision" not in lower_text:
        missing.append("mission_revision_tests")
    if "hermes" not in lower_text:
        missing.append("hermes_tests")
    if "evidence path" not in lower_text:
        missing.append("evidence_paths")

    return RepoInspectionValidation(ok=not missing, missing=missing)


def analyze_tool_call_events(
    tool_events: Sequence[Dict[str, Any]],
    *,
    task_text: str,
    final_response: str = "",
    subtask_id: str | None = None,
) -> Dict[str, Any]:
    failures: List[ToolFailureDetail] = []
    validation = (
        validate_repo_inspection_response(final_response)
        if is_read_only_repository_inspection(task_text)
        else RepoInspectionValidation(ok=False, missing=[])
    )
    repo_mode = is_read_only_repository_inspection(task_text)

    for call_index, event in enumerate(tool_events, start=1):
        if event.get("success") is not False:
            continue
        tool_name = str(event.get("tool") or "")
        args = dict(event.get("args") or {})
        error_summary = str(event.get("error") or "tool execution failed").strip()
        failure_class = classify_tool_failure(tool_name, error_summary)
        required_or_optional = "required"
        required_for = f"{tool_name} evidence needed by the active subtask"
        safe_next_action = "Inspect the tool arguments and retry with a valid bounded read-only input."

        if tool_name == "ReadLints" and not _LINT_INTENT_RE.search(task_text or ""):
            required_or_optional = "diagnostic"
            required_for = "optional lint diagnostics"
            safe_next_action = (
                "Skip lint diagnostics for this repository inspection or rerun with explicit lint scope."
            )
        elif repo_mode and tool_name == "Read" and _FILE_NOT_FOUND_RE.search(error_summary):
            path = str(args.get("path") or "").strip()
            basename = path.rsplit("/", 1)[-1]
            if _has_successful_followup(tool_events, call_index, basename):
                required_or_optional = "alternative_group"
                required_for = "path discovery fallback"
                safe_next_action = "Use the later successful discovery/read result for this repository fact."
            elif path.endswith(".git/status"):
                required_or_optional = "required"
                required_for = "repository cleanliness"
                safe_next_action = (
                    "Use ToolProposal to collect git status metadata instead of reading a nonexistent .git/status path."
                )
            elif repo_mode and validation.ok:
                required_or_optional = "alternative_group"
                required_for = "repository metadata fallback"
                safe_next_action = "Use the completed repository findings already collected through alternate read-only evidence."

        failures.append(
            ToolFailureDetail(
                tool_id=str(event.get("tool_id") or event.get("event_id") or f"tool-call-{call_index}"),
                tool_name=tool_name,
                call_index=call_index,
                arguments_redacted=args,
                failure_class=failure_class,
                exit_code=_int_or_none((event.get("metadata") or {}).get("rc")),
                exception_type=None,
                error_summary=error_summary,
                required_or_optional=required_or_optional,
                required_for=required_for,
                safe_next_action=safe_next_action,
                evidence_ref=str(event.get("event_id") or f"tool_call:{call_index}"),
                subtask_id=subtask_id,
            )
        )

    fatal = [failure for failure in failures if failure.required]
    nonfatal = [failure for failure in failures if not failure.required]
    diagnostic_warnings = [
        f"{failure.tool_name} call {failure.call_index}: {failure.error_summary}"
        for failure in nonfatal
    ]
    return {
        "failures": failures,
        "fatal_failures": fatal,
        "nonfatal_failures": nonfatal,
        "diagnostic_warnings": diagnostic_warnings,
        "repo_validation": validation,
    }


def classify_tool_failure(tool_name: str, error_summary: str) -> str:
    error = str(error_summary or "")
    if tool_name == "ReadLints" and _NO_PATHS_RE.search(error):
        return "invalid_tool_arguments"
    if _FILE_NOT_FOUND_RE.search(error):
        return "missing_file"
    if _NOT_A_FILE_RE.search(error):
        return "unsupported_path"
    if _LINTER_UNAVAILABLE_RE.search(error):
        return "unavailable_linter"
    if _INVALID_ARGS_RE.search(error):
        return "invalid_tool_arguments"
    return "tool_implementation"


def _field_value(text: str, names: Sequence[str]) -> str | None:
    for raw_name in names:
        pattern = re.compile(
            rf"{re.escape(raw_name)}\s*[:|-]\s*(.+)",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def _has_successful_followup(
    tool_events: Sequence[Dict[str, Any]],
    failed_call_index: int,
    basename: str,
) -> bool:
    if not basename:
        return False
    for later_event in tool_events[failed_call_index:]:
        if later_event.get("success") is not True:
            continue
        combined = json.dumps(later_event.get("args") or {}, sort_keys=True)
        combined += " "
        combined += str(later_event.get("output_preview") or "")
        if basename in combined:
            return True
    return False


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
