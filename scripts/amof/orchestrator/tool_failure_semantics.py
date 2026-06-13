"""Helpers for read-only tool failure semantics and repo-inspection validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
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
_REPO_FIELD_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?(?P<label>[^:]+?)(?:\*\*)?\s*:\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_PLACEHOLDER_VALUES = {
    "",
    ".git",
    "not explicitly provided",
    "unknown",
    "not recorded",
    "n/a",
}
_REPO_FIELD_ALIASES: Dict[str, tuple[str, ...]] = {
    "repository_path": ("repository path", "repository root"),
    "checkout_state": (
        "checkout state",
        "branch or detached state",
        "branch/detached state",
    ),
    "head_sha": ("head sha", "head commit"),
    "origin_main_sha": ("origin/main sha", "origin main sha"),
    "cleanliness": ("cleanliness", "working tree status", "repository status"),
    "mission_revision_test_paths": (
        "mission revision test paths",
        "mission-revision tests",
    ),
    "hermes_read_only_test_paths": (
        "hermes read-only test paths",
        "hermes tests",
    ),
    "evidence_paths": ("evidence paths", "evidence"),
}
_REQUIRED_REPO_FINDINGS: tuple[str, ...] = (
    "repository_path",
    "checkout_state",
    "head_sha",
    "origin_main_sha",
    "cleanliness",
    "mission_revision_test_paths",
    "hermes_read_only_test_paths",
    "evidence_paths",
)
# Render labels chosen so the canonical rendering re-parses to the same keys
# through _REPO_FIELD_ALIASES (round-trip stable).
_CANONICAL_FINDINGS_LABELS: Dict[str, str] = {
    "repository_path": "Repository Path",
    "checkout_state": "Branch Or Detached State",
    "head_sha": "HEAD SHA",
    "origin_main_sha": "origin/main SHA",
    "cleanliness": "Cleanliness",
    "mission_revision_test_paths": "Mission Revision Test Paths",
    "hermes_read_only_test_paths": "Hermes Read-Only Test Paths",
    "evidence_paths": "Evidence Paths",
}
_TOOLPROPOSAL_OUTPUT_KEYS: Dict[str, str] = {
    "repository path": "repository_path",
    "branch or detached state": "checkout_state",
    "head sha": "head_sha",
    "origin/main sha": "origin_main_sha",
    "cleanliness": "cleanliness",
}


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
    normalized: Dict[str, str]
    conflict: Dict[str, Any] | None = None


def repo_inspection_runner_tools(tool_names: Sequence[str]) -> List[str]:
    allowed = set(READ_ONLY_REPO_INSPECTION_TOOLS)
    return [name for name in tool_names if name in allowed]


def repo_inspection_task_guidance() -> str:
    return (
        "Repository-inspection mode:\n"
        "- Use ToolProposal for git metadata such as branch/detached state, HEAD SHA, "
        "origin/main SHA, and cleanliness.\n"
        "- For branch state, treat `git rev-parse --abbrev-ref HEAD` returning `HEAD` as detached "
        "and report the label `detached`.\n"
        "- Use Read, InspectFiles, Glob, and LS only for file presence or direct file contents.\n"
        "- Do not call ReadLints unless the task explicitly asks for lint diagnostics.\n"
        "- Do not guess pseudo-files like .git/status.\n"
        "- Final answer must include exact labels: Repository Path, Branch Or Detached State, "
        "HEAD SHA, origin/main SHA, Cleanliness, Contract Test Paths, Evidence Paths."
    )


def enrich_repo_inspection_response(final_response: str, *, workspace_root: str | Path | None = None) -> str:
    text = str(final_response or "")
    if not text.strip():
        return text
    if workspace_root:
        repo_path = str(Path(workspace_root))
        path_value = _field_value(text, ("Repository Path", "Repository path"))
        if not path_value or "not explicitly provided" in path_value.lower():
            replacement = f"Repository Path: {repo_path}"
            if re.search(r"Repository Path\s*[:|-].+", text, re.IGNORECASE):
                text = re.sub(r"Repository Path\s*[:|-].+", replacement, text, count=1, flags=re.IGNORECASE)
            else:
                text = f"{replacement}\n{text}"
    text = re.sub(
        r"(Branch Or Detached State\s*[:|-]\s*)(`?HEAD`?\s*(?:\(\s*detached\s*\))?)",
        r"\1detached",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    return text


def validate_repo_inspection_response(
    final_response: str,
    *,
    tool_events: Sequence[Dict[str, Any]] = (),
    workspace_root: str | Path | None = None,
) -> RepoInspectionValidation:
    return normalize_repo_inspection_findings(
        final_response,
        tool_events=tool_events,
        workspace_root=workspace_root,
    )


def normalize_repo_inspection_findings(
    final_response: str,
    *,
    tool_events: Sequence[Dict[str, Any]] = (),
    workspace_root: str | Path | None = None,
) -> RepoInspectionValidation:
    text = str(final_response or "")
    canonical_values, evidence_refs, conflict = _canonical_repo_evidence(tool_events)
    response_values = _parse_repo_inspection_response(text)
    if workspace_root and _is_missing_value("repository_path", response_values.get("repository_path")):
        response_values["repository_path"] = str(Path(workspace_root))

    normalized = dict(canonical_values)
    for key, response_value in response_values.items():
        if _is_missing_value(key, response_value):
            continue
        # Model prose is commentary: it may fill gaps the structured evidence
        # did not cover, but it can never redefine or conflict with canonical
        # structured evidence.
        normalized.setdefault(key, response_value)

    missing = [
        key for key in _REQUIRED_REPO_FINDINGS if _is_missing_value(key, normalized.get(key))
    ]
    return RepoInspectionValidation(
        ok=not missing and conflict is None,
        missing=missing,
        normalized=normalized,
        conflict=conflict,
    )


def render_canonical_findings(normalized: Dict[str, str]) -> str:
    """Render normalized structured findings deterministically.

    Same normalized mapping always produces byte-identical output: fixed key
    order (_REQUIRED_REPO_FINDINGS), fixed labels, one `Label: value` line per
    present finding.
    """
    lines: List[str] = []
    for key in _REQUIRED_REPO_FINDINGS:
        value = str(normalized.get(key) or "").strip()
        if not value:
            continue
        lines.append(f"{_CANONICAL_FINDINGS_LABELS[key]}: {value}")
    return "\n".join(lines)


def strip_canonical_labels(prose: str) -> str:
    """Remove canonical-looking labeled lines from model prose.

    Any line whose label resolves to a canonical repository finding through
    _REPO_FIELD_ALIASES is dropped so demoted commentary can never restate a
    canonical field under any alias.
    """
    kept: List[str] = []
    for raw_line in str(prose or "").splitlines():
        match = _REPO_FIELD_LINE_RE.match(raw_line)
        if match and _label_to_key(match.group("label")):
            continue
        kept.append(raw_line)
    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_terminal_task_findings(validation: RepoInspectionValidation) -> str | None:
    """Terminal task findings for repo-inspection runs.

    Rendered exclusively from the normalized structured findings; model prose
    is carried separately as demoted commentary.
    """
    canonical = render_canonical_findings(validation.normalized)
    return canonical or None


def analyze_tool_call_events(
    tool_events: Sequence[Dict[str, Any]],
    *,
    task_text: str,
    final_response: str = "",
    subtask_id: str | None = None,
) -> Dict[str, Any]:
    failures: List[ToolFailureDetail] = []
    validation = (
        validate_repo_inspection_response(final_response, tool_events=tool_events)
        if is_read_only_repository_inspection(task_text)
        else RepoInspectionValidation(ok=False, missing=[], normalized={})
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
        elif repo_mode and tool_name == "ToolProposal":
            if _has_successful_tool_followup(tool_events, call_index, "ToolProposal"):
                required_or_optional = "alternative_group"
                required_for = "repository metadata helper fallback"
                safe_next_action = "Use the later successful bounded ToolProposal result for repository metadata."
            elif validation.ok:
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
        "repo_inspection_mode": repo_mode,
    }


def classify_tool_failure(tool_name: str, error_summary: str) -> str:
    error = str(error_summary or "")
    if tool_name == "ReadLints" and _NO_PATHS_RE.search(error):
        return "invalid_tool_arguments"
    if tool_name == "ToolProposal" and "allowed_paths" in error:
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
    parsed = _parse_repo_inspection_response(text)
    for key, aliases in _REPO_FIELD_ALIASES.items():
        if any(raw_name.lower() == alias.lower() for alias in aliases for raw_name in names):
            return parsed.get(key)
    return None


def _parse_repo_inspection_response(text: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        match = _REPO_FIELD_LINE_RE.match(raw_line)
        if not match:
            continue
        label = _normalize_label(match.group("label"))
        key = _label_to_key(label)
        if not key:
            continue
        parsed[key] = _normalize_value(key, match.group("value"))
    return parsed


def _canonical_repo_evidence(
    tool_events: Sequence[Dict[str, Any]],
) -> tuple[Dict[str, str], Dict[str, str], Dict[str, Any] | None]:
    values: Dict[str, str] = {}
    refs: Dict[str, str] = {}
    evidence_paths: List[str] = []
    conflict: Dict[str, Any] | None = None

    def _record(key: str, raw_value: str, evidence_ref: str) -> None:
        nonlocal conflict
        normalized_value = _normalize_value(key, raw_value)
        existing = values.get(key)
        if (
            conflict is None
            and existing
            and not _is_missing_value(key, existing)
            and not _is_missing_value(key, normalized_value)
            and not _values_match(key, existing, normalized_value)
        ):
            conflict = {
                "conflicting_field": key,
                "canonical_value": existing,
                "response_value": normalized_value,
                "evidence_ref": refs.get(key) or evidence_ref,
                "conflict_source": "structured_evidence",
                "safe_next_action": "Inspect the conflicting repository evidence and response before retrying.",
            }
            return
        values[key] = normalized_value
        refs[key] = evidence_ref

    for event in tool_events:
        if event.get("success") is not True:
            continue
        tool_name = str(event.get("tool") or "")
        evidence_ref = str(event.get("tool_id") or event.get("event_id") or "")
        metadata = dict(event.get("metadata") or {})
        if tool_name == "ToolProposal":
            for line in str(metadata.get("stdout") or "").splitlines():
                if "=" not in line:
                    continue
                raw_key, raw_value = line.split("=", 1)
                mapped = _TOOLPROPOSAL_OUTPUT_KEYS.get(_normalize_label(raw_key))
                if not mapped:
                    continue
                _record(mapped, raw_value, evidence_ref)
            script_path = str(metadata.get("script_path") or "").strip()
            if script_path:
                evidence_paths.append(script_path)
                refs.setdefault("evidence_paths", evidence_ref)
        elif tool_name == "Glob":
            key = _glob_finding_key(str((event.get("args") or {}).get("glob_pattern") or ""))
            if not key:
                continue
            values[key] = _normalize_glob_output(str(event.get("output_preview") or ""))
            refs[key] = evidence_ref

    if evidence_paths:
        values["evidence_paths"] = ", ".join(dict.fromkeys(evidence_paths))
    return values, refs, conflict


def _normalize_label(label: str) -> str:
    return str(label or "").strip().strip("*`").replace("\\", "").replace("_", " ").lower()


def _label_to_key(label: str) -> str | None:
    normalized_label = _normalize_label(label)
    for key, aliases in _REPO_FIELD_ALIASES.items():
        if normalized_label in aliases:
            return key
    return None


def _normalize_value(key: str, value: str) -> str:
    normalized = str(value or "").strip()
    normalized = re.sub(r"^`(.+)`$", r"\1", normalized)
    normalized = re.sub(r"^\*\*(.+)\*\*$", r"\1", normalized)
    normalized = normalized.strip()
    if key == "checkout_state" and normalized.lower() == "head":
        return "detached"
    return normalized


def _glob_finding_key(glob_pattern: str) -> str | None:
    normalized = str(glob_pattern or "").lower()
    if "mission-revision" in normalized:
        return "mission_revision_test_paths"
    if "hermes" in normalized:
        return "hermes_read_only_test_paths"
    return None


def _normalize_glob_output(output: str) -> str:
    text = str(output or "").strip()
    if not text or "no files matched" in text.lower():
        return "none found"
    return text


def _is_missing_value(key: str, value: str | None) -> bool:
    normalized = str(value or "").strip().strip("`").lower()
    if key in {"mission_revision_test_paths", "hermes_read_only_test_paths"} and normalized == "none found":
        return False
    if key == "evidence_paths" and normalized:
        return False
    if key == "checkout_state" and normalized == "detached":
        return False
    if key in {"head_sha", "origin_main_sha"} and value and _SHA40_RE.search(str(value)):
        return False
    if key == "cleanliness" and value and re.search(r"\b(clean|dirty)\b", str(value), re.IGNORECASE):
        return False
    if (
        "not explicitly provided" in normalized
        or normalized.startswith("unknown")
        or normalized.startswith("not recorded")
    ):
        return True
    return normalized in _PLACEHOLDER_VALUES


def _values_match(key: str, canonical_value: str, response_value: str) -> bool:
    left = _normalize_value(key, canonical_value).strip().lower()
    right = _normalize_value(key, response_value).strip().lower()
    if key in {"head_sha", "origin_main_sha"}:
        left_match = _SHA40_RE.search(left)
        right_match = _SHA40_RE.search(right)
        return bool(left_match and right_match and left_match.group(0) == right_match.group(0))
    return left == right


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


def _has_successful_tool_followup(
    tool_events: Sequence[Dict[str, Any]],
    failed_call_index: int,
    tool_name: str,
) -> bool:
    for later_event in tool_events[failed_call_index:]:
        if later_event.get("tool") != tool_name:
            continue
        if later_event.get("success") is True:
            return True
    return False


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
