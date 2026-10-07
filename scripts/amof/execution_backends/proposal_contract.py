"""AMOF-owned structured write-scope proposal parsing for execution backends."""

from __future__ import annotations

import json
import re
from typing import Any

from ..write_scope_proposals import _normalize_repository_relative_scope_path

WRITE_SCOPE_PROPOSAL_START = "AMOF_WRITE_SCOPE_PROPOSAL_JSON_START"
WRITE_SCOPE_PROPOSAL_END = "AMOF_WRITE_SCOPE_PROPOSAL_JSON_END"
WRITE_SCOPE_PROPOSAL_REQUIRED = "WRITE_SCOPE_PROPOSAL_REQUIRED"
WRITE_SCOPE_PROPOSAL_FIELDS = (
    "target_id", "base_sha", "allowed_roots", "denied_roots", "reason",
    "expected_checks", "docs_only", "source_mutation",
)


def _proposal_missing_reason(task_findings: str, runtime_detail: str) -> str:
    detail = task_findings or runtime_detail
    for line in detail.splitlines():
        text = line.strip()
        if text:
            return text[:500]
    return "structured write_scope_proposal was requested but the runner did not emit one"


def _manifest_repo_targets(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Every manifest repo as canonical proposal target context, in order."""
    repos = manifest.get("repos")
    if not isinstance(repos, list):
        return []
    targets: list[dict[str, str]] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        base_sha = str(repo.get("sha") or repo.get("branch") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
            base_sha = ""
        targets.append({
            "target_id": str(repo.get("target_id") or "").strip(),
            "base_sha": base_sha,
            "repository_url": str(repo.get("url") or "").strip(),
            "workspace_path": str(repo.get("path") or "").strip(),
            "name": str(repo.get("name") or "").strip(),
        })
    return targets


def _normalize_write_scope_proposal(
    value: Any, *, expected_allowed_roots: list[str] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    proposal = dict(value)
    required = set(WRITE_SCOPE_PROPOSAL_FIELDS)
    if not required.issubset(proposal):
        return None
    target_id = str(proposal.get("target_id") or "").strip()
    base_sha = str(proposal.get("base_sha") or "").strip().lower()
    reason = str(proposal.get("reason") or "").strip()
    if not target_id or not re.fullmatch(r"[0-9a-f]{40}", base_sha) or not reason:
        return None

    def _string_list(name: str) -> list[str] | None:
        raw = proposal.get(name)
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            return None
        values = [item.strip() for item in raw]
        return None if any(not item for item in values) else values

    raw_allowed_roots = _string_list("allowed_roots")
    raw_denied_roots = _string_list("denied_roots")
    expected_checks = _string_list("expected_checks")
    allowed_roots = (
        [_normalize_repository_relative_scope_path(item) for item in raw_allowed_roots]
        if raw_allowed_roots is not None else None
    )
    denied_roots = (
        [_normalize_repository_relative_scope_path(item) for item in raw_denied_roots]
        if raw_denied_roots is not None else None
    )
    docs_only = proposal.get("docs_only")
    source_mutation = proposal.get("source_mutation")
    if (allowed_roots is None or not allowed_roots or any(item is None for item in allowed_roots)
            or denied_roots is None or any(item is None for item in denied_roots)
            or expected_checks is None or not isinstance(docs_only, bool)
            or not isinstance(source_mutation, bool)):
        return None
    normalized_allowed_roots = [str(item) for item in allowed_roots]
    normalized_denied_roots = [str(item) for item in denied_roots]
    if expected_allowed_roots and not set(normalized_allowed_roots).issubset(set(expected_allowed_roots)):
        return None
    proposal.update(target_id=target_id, base_sha=base_sha, reason=reason,
                    allowed_roots=normalized_allowed_roots, denied_roots=normalized_denied_roots,
                    expected_checks=expected_checks, docs_only=docs_only,
                    source_mutation=source_mutation)
    return proposal


def _extract_write_scope_proposal_outputs(
    text: str, *, expected_allowed_roots: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    pattern = re.compile(
        rf"{WRITE_SCOPE_PROPOSAL_START}\s*(\{{.*?\}})\s*{WRITE_SCOPE_PROPOSAL_END}", re.DOTALL,
    )
    proposals: list[dict[str, Any]] = []
    seen_target_ids: set[str] = set()
    summary_parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        summary_parts.append(text[cursor:match.start()])
        cursor = match.end()
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            parsed = None
        proposal = _normalize_write_scope_proposal(parsed, expected_allowed_roots=expected_allowed_roots)
        if proposal is None:
            continue
        target_id = str(proposal.get("target_id") or "")
        if target_id in seen_target_ids:
            continue
        seen_target_ids.add(target_id)
        proposals.append(proposal)
    summary_parts.append(text[cursor:])
    return proposals, "".join(summary_parts).strip()


def _extract_write_scope_proposal_output(
    text: str, *, expected_allowed_roots: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    proposals, summary = _extract_write_scope_proposal_outputs(
        text, expected_allowed_roots=expected_allowed_roots,
    )
    return (proposals[0] if proposals else None), summary
