"""Thin intake endpoints for ticket build-write flows."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from amof.app_paths import get_app_paths, ticket_worktrees_dir
from amof.api.command_builder import get_workspace_root
from amof.api.dependencies import require_step_up_user
from amof.api.routers.release import (
    _read_environment_profiles,
    _reconcile_environment_dns_or_raise,
    _slug,
    _write_environment_profiles,
    build_lifecycle_environment_index,
)
from amof.cli import get_available_ecosystems
from amof.intake.amof_commit import build_amof_commit_event, decide_amof_commit_build_write
from amof.intake.build_write import RUNTIME_DECISION_STATE
from amof.intake.draft_compiler import compile_intake_draft
from amof.intake.github_push import decide_github_push_payload
from amof.state import get_all_tickets

router = APIRouter(prefix="/intake", tags=["intake"])


@router.post("/draft", dependencies=[Depends(require_step_up_user)])
def intake_draft(body: dict[str, Any]) -> dict[str, Any]:
    raw_text = str(body.get("raw_text") or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="raw_text is required")
    try:
        return compile_intake_draft(raw_text).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _legacy_env_path(workspace_root: Path) -> Path:
    return workspace_root / ".env"


def _appdata_intake_env_path() -> Path:
    return get_app_paths().config_root / "intake.env"


def _resolve_intake_env_path(workspace_root: Path) -> Path:
    app_env_path = _appdata_intake_env_path()
    legacy_env_path = _legacy_env_path(workspace_root)
    if app_env_path.exists() or not legacy_env_path.exists():
        return app_env_path
    return legacy_env_path


def _legacy_ticket_worktree_base(workspace_root: Path, ticket_id: str) -> Path:
    return workspace_root / ".amof-worktrees" / ticket_id


def _default_ticket_worktree_base(workspace_root: Path, ticket_id: str) -> Path:
    appdata_worktree_base = ticket_worktrees_dir() / ticket_id
    legacy_worktree_base = _legacy_ticket_worktree_base(workspace_root, ticket_id)
    if appdata_worktree_base.exists() or not legacy_worktree_base.exists():
        return appdata_worktree_base
    return legacy_worktree_base


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _proof_mode_default() -> bool:
    # Safe by default: intake only returns a would-run decision unless explicitly enabled.
    return not _env_flag("AMOF_GITHUB_PUSH_REAL_RUN_ENABLED", default=False)


def _load_workspace_env(workspace_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env_path = _resolve_intake_env_path(workspace_root)
    if not env_path.exists():
        return env
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and value and key not in env:
            env[key] = value
    return env


def _require_intake_key(header_value: str | None) -> None:
    expected = str(os.environ.get("AMOF_GITHUB_PUSH_INTAKE_KEY") or "").strip()
    if not expected:
        return
    provided = str(header_value or "").strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid intake key")


def _git_output(repo_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip() or f"git {' '.join(args)} failed"
        raise HTTPException(status_code=409, detail=detail)
    return completed.stdout.strip()


def _resolve_ticket_repo_path(
    *,
    workspace_root: Path,
    ticket_id: str,
    repo_name: str,
    ticket_info: dict[str, Any],
) -> Path:
    worktree_base = Path(str(ticket_info.get("worktree_base") or _default_ticket_worktree_base(workspace_root, ticket_id)))
    repo_path = worktree_base / repo_name
    if not repo_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Ticket worktree for {ticket_id}/{repo_name} was not found at {repo_path}",
        )
    return repo_path


def _collect_ticket_head_evidence(
    *,
    workspace_root: Path,
    ticket_id: str,
    repo_name: str,
    ticket_info: dict[str, Any],
) -> dict[str, Any]:
    repo_path = _resolve_ticket_repo_path(
        workspace_root=workspace_root,
        ticket_id=ticket_id,
        repo_name=repo_name,
        ticket_info=ticket_info,
    )
    branch = _git_output(repo_path, "branch", "--show-current")
    sha = _git_output(repo_path, "rev-parse", "--short=16", "HEAD")
    message = _git_output(repo_path, "show", "-s", "--format=%s", "HEAD")
    changed_files_raw = _git_output(repo_path, "show", "--format=", "--name-only", "HEAD")
    changed_files = [line.strip() for line in changed_files_raw.splitlines() if line.strip()]
    return {
        "repo_path": str(repo_path),
        "branch": branch,
        "sha": sha,
        "commit_message": message,
        "changed_files": changed_files,
    }


def _run_ticket_env_writer(
    *,
    workspace_root: Path,
    repo_path: Path,
    ticket_id: str,
    branch: str,
    sha: str,
    host_mode: str,
    owner_id: str,
    owner_slug: str,
    owner_type: str,
    target_revision: str,
    output: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    writer = repo_path / "scripts" / "gitops" / "update-ticket-env-from-head.py"
    if not writer.exists():
        raise HTTPException(status_code=404, detail=f"Ticket env writer was not found at {writer}")
    command = [
        "python3",
        str(writer),
        "--ticket-id",
        ticket_id,
        "--branch",
        branch,
        "--commit-sha",
        sha,
        "--host-mode",
        host_mode,
        "--owner-id",
        owner_id,
        "--owner-slug",
        owner_slug,
        "--owner-type",
        owner_type,
        "--target-revision",
        target_revision,
        "--summary-json",
    ]
    if output:
        command.extend(["--output", output])
    if dry_run:
        command.append("--dry-run")
    completed = subprocess.run(
        command,
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
        env=_load_workspace_env(workspace_root),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip() or "ticket env writer failed"
        raise HTTPException(status_code=409, detail=detail)
    try:
        return json.loads((completed.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Ticket env writer did not return valid JSON") from exc


def _upsert_linked_environment_profile(
    *,
    ecosystem: str,
    ticket_id: str,
    repo_name: str,
    ticket_info: dict[str, Any],
    writer_summary: dict[str, Any],
) -> dict[str, Any]:
    environment_id = str(ticket_info.get("environment_id") or _slug(ticket_id)).strip() or _slug(ticket_id)
    stage_id = str(ticket_info.get("stage_id") or "dev").strip() or "dev"
    branch = str(writer_summary.get("branch") or ticket_info.get("repos", {}).get(repo_name) or "").strip()
    hostname = str(writer_summary.get("hostname") or "").strip()
    namespace = str(writer_summary.get("namespace") or environment_id).strip() or environment_id
    url = f"https://{hostname}" if hostname else None
    profiles = _read_environment_profiles(ecosystem)
    profile = {
        "id": environment_id,
        "stage_id": stage_id,
        "title": ticket_id,
        "summary": f"Ticket environment synced from {branch or ticket_id} head.",
        "label": ticket_id,
        "ticket_label": ticket_id,
        "ticket_id": ticket_id,
        "workspace": f"{ecosystem} / {ticket_id}",
        "repo_summary": f"{repo_name}: {branch}" if branch else repo_name,
        "status": "healthy",
        "namespace": namespace,
        "helm_release": namespace,
        "public_base_url": url,
        "hostname": hostname or None,
        "endpoints": [{"label": "Ticket Environment", "url": url}] if url else [],
        "promotion_target": None,
        "ai_log_chunks": ["Ticket environment synced from AMOF internal commit intake."],
        "environment_mode": "ticket_local",
        "environment_target": environment_id,
        "source_sha": str(writer_summary.get("source_commit_sha") or "").strip() or None,
        "candidate_branch": branch or None,
        "gitops_commit_sha": str(writer_summary.get("gitops_commit_sha") or "").strip() or None,
    }
    replaced = False
    for index, existing in enumerate(profiles):
        if str(existing.get("id") or "").strip() == environment_id:
            merged = dict(existing)
            merged.update(profile)
            _reconcile_environment_dns_or_raise(existing, merged)
            profiles[index] = merged
            replaced = True
            break
    if not replaced:
        _reconcile_environment_dns_or_raise(None, profile)
        profiles.append(profile)
    _write_environment_profiles(ecosystem, profiles)
    environment_index = build_lifecycle_environment_index(ecosystem)
    return environment_index.get(environment_id) or environment_index.get(stage_id) or profile


@router.post("/github/push")
async def github_push_intake(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_amof_intake_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Accept a raw GitHub push payload and return the normalized build-write decision."""
    _require_intake_key(x_amof_intake_key)

    event_type = str(x_github_event or "push").strip().lower()
    if event_type and event_type != "push":
        raise HTTPException(status_code=400, detail=f"Unsupported GitHub event '{event_type}'")

    try:
        payload = await request.json()
    except Exception as exc:  # pragma: no cover - FastAPI already validates common cases.
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="GitHub push payload must be a JSON object")

    proof_mode = _proof_mode_default()
    decision = decide_github_push_payload(payload, proof_mode=proof_mode)

    return {
        "repo": decision.repo,
        "normalized_repo": decision.normalized_repo,
        "branch": decision.branch,
        "sha": decision.source_sha,
        "ticket_id": decision.ticket_id,
        "decision": decision.action,
        "reason": decision.reason,
        "command": decision.command,
        "changed_files": decision.changed_files,
        "env_only_commit": decision.env_only_commit,
        "actor": decision.actor,
        "event_source": decision.event_source,
        "amof_created": decision.amof_created,
        "dedupe_key": decision.dedupe_key,
        "already_processed": decision.already_processed,
        "amof_origin_replay": decision.amof_origin_replay,
        "proof_mode": proof_mode,
        "execution_enabled": False,
    }


@router.post("/amof/commit", dependencies=[Depends(require_step_up_user)])
def amof_commit_intake(body: dict[str, Any]) -> dict[str, Any]:
    ecosystem = str(body.get("ecosystem") or "").strip()
    if not ecosystem:
        raise HTTPException(status_code=400, detail="ecosystem is required")
    if ecosystem not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {ecosystem} not found")
    ticket_id = str(body.get("ticket_id") or "").strip().upper()
    if not ticket_id or ticket_id == "MAIN":
        raise HTTPException(status_code=400, detail="ticket_id must be a non-main ticket")
    repo_name = str(body.get("repo") or "amof").strip() or "amof"
    host_mode = str(body.get("host_mode") or "cloud").strip() or "cloud"
    if host_mode not in {"local", "cloud"}:
        raise HTTPException(status_code=400, detail="host_mode must be 'local' or 'cloud'")
    dry_run = bool(body.get("dry_run", False))
    tickets = get_all_tickets(ecosystem=ecosystem)
    ticket_info = tickets.get(ticket_id)
    if not isinstance(ticket_info, dict):
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} was not found for ecosystem {ecosystem}")
    workspace_root = get_workspace_root()
    evidence = _collect_ticket_head_evidence(
        workspace_root=workspace_root,
        ticket_id=ticket_id,
        repo_name=repo_name,
        ticket_info=ticket_info,
    )
    event = build_amof_commit_event(
        repo=repo_name,
        branch=str(evidence["branch"]),
        sha=str(evidence["sha"]),
        actor=str(body.get("actor") or "amof"),
        changed_files=list(evidence.get("changed_files") or []),
        commit_message=str(evidence.get("commit_message") or body.get("commit_message") or "").strip(),
    )
    decision = decide_amof_commit_build_write(
        event,
        proof_mode=dry_run,
        state=RUNTIME_DECISION_STATE,
    )
    response = {
        "repo": decision.repo,
        "normalized_repo": decision.normalized_repo,
        "branch": decision.branch,
        "sha": decision.source_sha,
        "ticket_id": decision.ticket_id,
        "decision": decision.action,
        "reason": decision.reason,
        "command": decision.command,
        "changed_files": decision.changed_files,
        "env_only_commit": decision.env_only_commit,
        "actor": decision.actor,
        "event_source": decision.event_source,
        "amof_created": decision.amof_created,
        "dedupe_key": decision.dedupe_key,
        "already_processed": decision.already_processed,
        "amof_origin_replay": decision.amof_origin_replay,
        "proof_mode": dry_run,
        "execution_enabled": not dry_run and decision.action == "build_write",
        "worktree_path": str(evidence["repo_path"]),
        "writer_result": None,
        "linked_environment": None,
    }
    if decision.action != "build_write":
        return response
    writer_summary = _run_ticket_env_writer(
        workspace_root=workspace_root,
        repo_path=Path(str(evidence["repo_path"])),
        ticket_id=ticket_id,
        branch=str(evidence["branch"]),
        sha=str(evidence["sha"]),
        host_mode=host_mode,
        owner_id=str(body.get("owner_id") or "operator@amof.dev"),
        owner_slug=str(body.get("owner_slug") or "operator-amof-dev"),
        owner_type=str(body.get("owner_type") or "team"),
        target_revision=str(body.get("target_revision") or evidence["branch"]).strip() or str(evidence["branch"]),
        output=(str(body.get("output")).strip() if body.get("output") else None),
        dry_run=dry_run,
    )
    response["writer_result"] = writer_summary
    if not dry_run:
        response["linked_environment"] = _upsert_linked_environment_profile(
            ecosystem=ecosystem,
            ticket_id=ticket_id,
            repo_name=repo_name,
            ticket_info=ticket_info,
            writer_summary=writer_summary,
        )
    return response
