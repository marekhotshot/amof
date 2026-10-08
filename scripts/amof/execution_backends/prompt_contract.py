"""Backend-neutral mission prompt and structured proposal instructions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..write_scope_proposals import _normalize_repository_relative_scope_path
from .proposal_contract import WRITE_SCOPE_PROPOSAL_START, WRITE_SCOPE_PROPOSAL_END, _manifest_repo_targets
from .workspace_state import _workspace_repo_roots

def _goal_requests_write_scope_proposal(goal: str) -> bool:
    lowered = goal.lower()
    return "write_scope_proposal" in lowered or "write scope proposal" in lowered


def _explicit_required_proposal_paths(goal: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"\bexactly\s*:?\s*`?([^\s,;`]+)", goal, re.IGNORECASE):
        candidate = match.group(1).rstrip(".'\"),:")
        normalized = _normalize_repository_relative_scope_path(candidate)
        if normalized and "/" in normalized and normalized not in paths:
            paths.append(normalized)
    return paths


def _primary_manifest_target(manifest: dict[str, Any]) -> dict[str, str]:
    targets = _manifest_repo_targets(manifest)
    return targets[0] if targets else {}


def _build_prompt(
    goal: str,
    selection: Any,
    workspace: Path,
    manifest: dict[str, Any] | None = None,
    *,
    read_only_replan: bool = False,
    proposal_replan: bool = False,
    agent_label: str = "Hermes",
    backend_name: str = "hermes_opensandbox",
) -> str:
    proposal_requested = (
        _goal_requests_write_scope_proposal(goal) and not selection.writable_roots
    )
    prompt_targets = _manifest_targets_for_prompt(manifest or {}, workspace)
    lines = [
        f"You are executing as {agent_label} under AMOF authority.",
        f"AMOF runner_id: {selection.runner_id}",
        f"AMOF backend: {backend_name}",
        f"Workspace root: {workspace}",
        f"Approved capabilities: {', '.join(selection.capabilities)}",
        "Denied: Kubernetes mutation, deployment, secrets, unrestricted network, push, promotion, tags, releases.",
        "Tool paths must be repository-relative (example: 00-amof/README.md or README.md).",
        "Do not pass absolute sandbox or host paths to tools; they are rejected.",
    ]
    if len(prompt_targets) > 1:
        lines.extend(
            [
                "",
                f"Target repositories ({len(prompt_targets)}): the workspace root contains one materialized checkout per target. Inspect EVERY target repository relevant to the mission, not only the first.",
                "Use the tool_root value below as the repository-relative prefix for list_dir/read_file/glob.",
            ]
        )
        for index, target in enumerate(prompt_targets, start=1):
            lines.append(
                f"- target {index}: {target.get('name') or target.get('target_id') or 'unknown'}"
                f" tool_root={target.get('tool_root') or 'unknown'}"
                f" (target_id: {target.get('target_id') or 'unknown'}, base_sha: {target.get('base_sha') or 'unknown'})"
            )
    elif prompt_targets:
        lines.append(
            f"Tool root: {prompt_targets[0].get('tool_root') or '.'} (repository-relative; do not pass the workspace absolute path to tools)."
        )
    lines.extend(
        [
            "",
            "Truth domains:",
            "- Agent-observed task findings: report only facts you inspect in the workspace through approved commands/tools.",
            "- AMOF runtime envelope: handoff ID, run ID, Studio Session ID, runner/backend, provider/model/transport, fallback, capabilities, changed paths, status, stop reason, and evidence paths are supplied by AMOF outside your answer.",
            "Do not search the repository for AMOF runtime-envelope field names such as runner_id, backend, transport, studio_session_id, result_path, runtime_log_path, or event_log_path.",
            "If asked for AMOF runtime-envelope fields, state that AMOF will provide them in the runtime envelope; do not treat absent metadata files as blockers.",
            "Use explicit commands when the mission asks for command-derived repository facts, and include command exit codes in your task findings.",
        ]
    )
    if selection.writable_roots:
        roots = ", ".join(selection.writable_roots)
        lines.append(f"Writable roots: {roots}")
        lines.append("Modify files only inside the listed writable roots. Do not commit, push, promote, deploy, tag, or release.")
    else:
        lines.extend(
            [
                "Read-only run: this repository is already materialized and must be inspected in place.",
                f"Read-only workspace boundary (exact path): {workspace}",
                "Do not run git clone, git init, git worktree, or create nested repositories.",
                "Do not create, modify, or delete files anywhere in this workspace.",
            ]
        )
        if read_only_replan:
            lines.append(
                "Read-only mutation was detected once; this constrained replan must remain read-only within the same workspace boundary."
            )
    if proposal_requested:
        target = prompt_targets[0] if prompt_targets else _primary_manifest_target(manifest or {})
        multi_target = len(prompt_targets) > 1
        expected_allowed_roots = _explicit_required_proposal_paths(goal)
        docs_only = bool(expected_allowed_roots) and all(
            root == "docs" or root.startswith("docs/")
            for root in expected_allowed_roots
        )
        proposal_example = {
            "target_id": target.get("target_id") or "",
            "base_sha": target.get("base_sha") or "",
            "allowed_roots": expected_allowed_roots
            or ["<repository-relative-path-your-evidence-justifies>"],
            "denied_roots": [],
            "reason": (
                "bounded write proof artifact"
                if expected_allowed_roots == ["docs/amof-bounded-write-proof.md"]
                else "bounded follow-up justified by inspected evidence"
            ),
            "expected_checks": ["git diff --check"],
            "docs_only": docs_only,
            "source_mutation": not docs_only,
        }
        lines.extend(
            [
                "",
                "Required structured write-scope contract:",
                "This mission requires machine-readable structured write_scope_proposal output. A prose-only answer is a contract failure.",
                (
                    "You MUST emit one non-empty JSON object between these markers for EACH target repository your evidence justifies changing (repeat the marker pair per target), before any human-readable summary."
                    if multi_target
                    else "You MUST emit exactly one non-empty JSON object between these markers before any human-readable summary."
                ),
                WRITE_SCOPE_PROPOSAL_START,
                json.dumps(proposal_example, separators=(",", ":")),
                WRITE_SCOPE_PROPOSAL_END,
                "Use exactly those JSON field names. Do not wrap them in another object.",
                "Populate target_id and base_sha from the canonical target context. Empty or partial proposal objects are invalid.",
                "allowed_roots must list the exact repository-relative file or directory paths your inspected evidence justifies changing; an empty allowed_roots array is invalid.",
                "Keep allowed_roots and denied_roots repository-relative.",
                "Wildcard roots and additional unrequested roots are forbidden.",
                "The proposal may describe a future create_or_update operation; do not perform that operation now and do not include approved_write_scope or any approval claim.",
                "After the JSON block, emit a Markdown summary for humans. Do not restate the JSON block in prose.",
            ]
        )
        if multi_target:
            lines.extend(
                [
                    "Each proposal block covers exactly one target repository: use that target's target_id and base_sha from the target list above, and keep allowed_roots relative to that repository's own root (never prefix them with the checkout directory name).",
                    "Do not emit a proposal block for a target that needs no changes; explain why in the summary instead.",
                ]
            )
        if expected_allowed_roots:
            lines.append(
                (
                    "Required allowed_roots across ALL proposal blocks combined (no additional paths; each block lists only the paths that belong to its own repository): "
                    if multi_target
                    else "Required allowed_roots (exact; no additional paths): "
                )
                + json.dumps(expected_allowed_roots)
            )
        if multi_target:
            lines.append("Canonical proposal target context (one entry per target):")
            for entry in prompt_targets:
                lines.append(
                    f"- target_id: {entry.get('target_id') or 'unknown'} | base_sha: {entry.get('base_sha') or 'unknown'}"
                    f" | repository_url: {entry.get('repository_url') or 'unknown'} | tool_root: {entry.get('tool_root') or 'unknown'}"
                )
        elif target:
            lines.extend(
                [
                    "Canonical proposal target context:",
                    f"- target_id: {target.get('target_id') or 'unknown'}",
                    f"- base_sha: {target.get('base_sha') or 'unknown'}",
                    f"- repository_url: {target.get('repository_url') or 'unknown'}",
                    f"- tool_root: {target.get('tool_root') or '.'}",
                ]
            )
    lines.extend(["", "Mission:", goal])
    if proposal_requested:
        lines.extend(
            [
                "",
                "CURRENT PHASE OVERRIDE — PROPOSAL ONLY:",
                "Any mission instruction to create, update, or output a file is conditional on later operator approval and MUST NOT be executed in this run.",
                "Inspect read-only. Do not create, modify, rename, or delete any file.",
                f"Your final answer MUST begin with {WRITE_SCOPE_PROPOSAL_START}, followed by the required non-empty JSON object and {WRITE_SCOPE_PROPOSAL_END}.",
                "Prose-only output is invalid.",
            ]
        )
        if read_only_replan:
            lines.append(
                "A prior mutation attempt was restored. Do not repeat it; return only the required proposal block and human-readable findings."
            )
        if proposal_replan:
            lines.append(
                "CONTRACT RETRY: your previous answer omitted the required JSON block or its fields were invalid (for example an empty allowed_roots array). "
                "Re-run the inspection conclusion and emit the JSON block again with every required field populated and allowed_roots listing the exact repository-relative paths your evidence justifies."
            )
    elif selection.writable_roots:
        lines.extend(
            [
                "",
                "CURRENT PHASE OVERRIDE — APPROVED BOUNDED WRITE:",
                "AMOF has already validated explicit operator approval for the listed writable roots.",
                "Execute the mission's requested create_or_update operation now. Create missing parent directories when required.",
                "Do not ask for another confirmation and do not emit a write-scope proposal.",
                "The approval remains bounded: do not modify any path outside Writable roots.",
            ]
        )
    return "\n".join(lines)


def _safe_tool_root_segment(value: str) -> str | None:
    """One path segment safe to give tools. Never absolute, never traversal."""
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return None
    name = Path(text).name
    if not name or name in {".", ".."}:
        return None
    if name.startswith("/") or "/" in name or "\\" in name:
        return None
    return name


def _relative_under_workspace(workspace: Path, candidate: Path) -> str | None:
    try:
        ws = workspace.expanduser().resolve(strict=False)
        resolved = candidate.expanduser().resolve(strict=False)
    except OSError:
        return None
    if resolved == ws:
        return "."
    try:
        rel = resolved.relative_to(ws).as_posix()
    except ValueError:
        return None
    if not rel or rel.startswith("..") or Path(rel).is_absolute():
        return None
    return rel


def _tool_visible_relative_root(
    *,
    workspace: Path,
    repo_path: str,
    index: int,
    repo_roots: list[Path],
) -> str:
    """Map a Target Set repo to a repository-relative tool root.

    Manifest ``repos[].path`` may be an execution-Job sandbox absolute
    (``/run-work/files``). Native tools reject absolute paths. Prefer live
    workspace git children in manifest order; never return an absolute path.
    """
    if index < len(repo_roots):
        rel = _relative_under_workspace(workspace, repo_roots[index])
        if rel:
            return rel
        name = _safe_tool_root_segment(repo_roots[index].name)
        if name:
            return name
    raw = str(repo_path or "").strip()
    if raw:
        rel = _relative_under_workspace(workspace, Path(raw))
        if rel:
            return rel
        name = _safe_tool_root_segment(raw)
        if name:
            return name
    return f"target-{index + 1}"


def _manifest_targets_for_prompt(
    manifest: dict[str, Any],
    workspace: Path,
) -> list[dict[str, str]]:
    targets = _manifest_repo_targets(manifest)
    repo_roots = _workspace_repo_roots(workspace)
    annotated: list[dict[str, str]] = []
    for index, target in enumerate(targets):
        item = dict(target)
        item["tool_root"] = _tool_visible_relative_root(
            workspace=workspace,
            repo_path=str(target.get("workspace_path") or ""),
            index=index,
            repo_roots=repo_roots,
        )
        annotated.append(item)
    return annotated
