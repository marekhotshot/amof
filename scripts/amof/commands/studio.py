"""Additive Studio Session core ledger commands."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..app_paths import runs_dir, studio_dir
from .runs import (
    RunSummary,
    RunsCliError,
    _compute_run_summary,
    _discover_run_summaries,
    _resolve_run,
)


class StudioCliError(RuntimeError):
    """Raised when a Studio Session command cannot complete truthfully."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _studio_root() -> Path:
    root = studio_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _new_studio_session_id() -> str:
    base = datetime.now(timezone.utc).strftime("studio-%Y%m%d-%H%M%S")
    candidate = base
    counter = 1
    while (_studio_root() / candidate).exists():
        counter += 1
        candidate = f"{base}-{counter:02d}"
    return candidate


def _session_dir(studio_session_id: str) -> Path:
    return _studio_root() / studio_session_id


def _manifest_path(studio_session_id: str) -> Path:
    return _session_dir(studio_session_id) / "session.json"


def _events_path(studio_session_id: str) -> Path:
    return _session_dir(studio_session_id) / "events.jsonl"


def _runs_path(studio_session_id: str) -> Path:
    return _session_dir(studio_session_id) / "runs.json"


def _checkpoints_path(studio_session_id: str) -> Path:
    return _session_dir(studio_session_id) / "checkpoints.jsonl"


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _load_json(path: Path, *, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payloads: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _require_manifest(studio_session_id: str) -> dict[str, Any]:
    manifest_path = _manifest_path(studio_session_id)
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise StudioCliError(f"studio session not found: {studio_session_id}")
    return manifest


def _require_active_manifest(studio_session_id: str) -> dict[str, Any]:
    manifest = _require_manifest(studio_session_id)
    if str(manifest.get("status") or "") == "ended":
        raise StudioCliError(f"studio session already ended: {studio_session_id}")
    return manifest


def require_active_studio_session(studio_session_id: str) -> dict[str, Any]:
    """Validate that a Studio Session exists and remains attachable."""
    return _require_active_manifest(studio_session_id)


def _append_event(
    studio_session_id: str,
    event_type: str,
    **payload: Any,
) -> dict[str, Any]:
    events_path = _events_path(studio_session_id)
    event_id = f"{studio_session_id}:{len(_load_jsonl(events_path)) + 1:04d}"
    event = {
        "event_id": event_id,
        "studio_session_id": studio_session_id,
        "timestamp": _now_iso(),
        "event_type": event_type,
        **payload,
    }
    _append_jsonl(events_path, event)
    return event


def read_studio_session_id_from_events(events_path: Path) -> str | None:
    """Return the first non-empty Studio Session correlation from a run log."""
    for event in _load_jsonl(events_path):
        value = str(event.get("studio_session_id") or "").strip()
        if value:
            return value
    return None


def _studio_manifest(studio_session_id: str) -> dict[str, Any]:
    now = _now_iso()
    return {
        "schema_version": 1,
        "studio_session_id": studio_session_id,
        "status": "active",
        "created_at": now,
        "ended_at": None,
        "capture_state": {
            "recording": "not_started",
            "streaming": "not_started",
        },
        "finalization_state": "not_started",
        "redaction_policy": {
            "safe_summary_only": True,
        },
    }


def _studio_paths(studio_session_id: str) -> dict[str, str]:
    return {
        "session_dir": str(_session_dir(studio_session_id)),
        "session_path": str(_manifest_path(studio_session_id)),
        "events_path": str(_events_path(studio_session_id)),
        "runs_path": str(_runs_path(studio_session_id)),
        "checkpoints_path": str(_checkpoints_path(studio_session_id)),
    }


def _surface_for_run(run: RunSummary) -> str:
    session_path = Path(run.session_path)
    root = runs_dir()
    try:
        relative = session_path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return "agent"
    if len(relative.parts) < 2:
        return "agent"
    lane = relative.parts[0]
    if lane.startswith("chat-"):
        return "chat"
    return lane.rstrip("s")


def _load_attached_runs(studio_session_id: str) -> list[dict[str, Any]]:
    payload = _load_json(_runs_path(studio_session_id), default=[])
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _write_attached_runs(studio_session_id: str, runs: list[dict[str, Any]]) -> None:
    _write_json(_runs_path(studio_session_id), runs)


def attach_run_reference(
    *,
    studio_session_id: str,
    run_id: str,
    session_id: str,
    surface: str,
    mode: str | None,
    status: str | None,
    events_path: str,
    session_path: str,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Attach one run reference to a Studio Session ledger idempotently."""
    _require_active_manifest(studio_session_id)
    attached_runs = _load_attached_runs(studio_session_id)
    candidate = {
        "schema_version": 1,
        "run_id": run_id,
        "session_id": session_id,
        "studio_session_id": studio_session_id,
        "surface": surface,
        "mode": mode,
        "status": status,
        "events_path": events_path,
        "session_path": session_path,
        "output_path": output_path,
        "attached_at": _now_iso(),
    }
    for index, existing in enumerate(attached_runs):
        if str(existing.get("run_id") or "") != run_id:
            continue
        preserved_attached_at = str(existing.get("attached_at") or "").strip()
        merged = dict(existing)
        for key, value in candidate.items():
            if value is not None:
                merged[key] = value
        if preserved_attached_at:
            merged["attached_at"] = preserved_attached_at
        attached_runs[index] = merged
        _write_attached_runs(studio_session_id, attached_runs)
        return merged
    attached_runs.append(candidate)
    _write_attached_runs(studio_session_id, attached_runs)
    _append_event(
        studio_session_id,
        "run.attached",
        run_id=run_id,
        session_id=session_id,
        surface=surface,
        mode=mode,
        events_path=events_path,
    )
    return candidate


def _enrich_attached_run(run_ref: dict[str, Any]) -> dict[str, Any]:
    events_path_text = str(run_ref.get("events_path") or "").strip()
    if not events_path_text:
        return dict(run_ref)
    try:
        summary = _compute_run_summary(Path(events_path_text))
    except RunsCliError:
        return dict(run_ref)
    payload = dict(run_ref)
    payload.update(
        {
            "run_id": summary.run_id,
            "session_id": summary.session_id,
            "studio_session_id": payload.get("studio_session_id") or summary.studio_session_id,
            "status": summary.status,
            "mode": payload.get("mode") or summary.planning_mode,
            "events_path": summary.events_path,
            "session_path": summary.session_path,
            "output_path": summary.output_path,
        }
    )
    return payload


def _studio_payload(studio_session_id: str) -> dict[str, Any]:
    manifest = _require_manifest(studio_session_id)
    attached_runs = [
        _enrich_attached_run(item) for item in _load_attached_runs(studio_session_id)
    ]
    checkpoints = _load_jsonl(_checkpoints_path(studio_session_id))
    events = _load_jsonl(_events_path(studio_session_id))
    return {
        "manifest": manifest,
        "paths": _studio_paths(studio_session_id),
        "attached_runs": attached_runs,
        "checkpoints": checkpoints,
        "summary": {
            "attached_runs_count": len(attached_runs),
            "checkpoints_count": len(checkpoints),
            "event_count": len(events),
        },
    }


def _print_studio(payload: dict[str, Any]) -> None:
    manifest = dict(payload.get("manifest") or {})
    summary = dict(payload.get("summary") or {})
    paths = dict(payload.get("paths") or {})
    print(f"studio_session_id: {manifest.get('studio_session_id', '-')}")
    print(f"status: {manifest.get('status', '-')}")
    print(f"created_at: {manifest.get('created_at', '-')}")
    print(f"ended_at: {manifest.get('ended_at') or '-'}")
    print(f"attached_runs: {summary.get('attached_runs_count', 0)}")
    print(f"checkpoints: {summary.get('checkpoints_count', 0)}")
    print(f"events: {summary.get('event_count', 0)}")
    print(f"session_path: {paths.get('session_path', '-')}")
    print(f"events_path: {paths.get('events_path', '-')}")
    attached_runs = list(payload.get("attached_runs") or [])
    if attached_runs:
        print("runs:")
        for run in attached_runs:
            print(
                "  - "
                f"{run.get('run_id', '-')}"
                f" surface={run.get('surface', '-')}"
                f" mode={run.get('mode', '-')}"
            )
    checkpoints = list(payload.get("checkpoints") or [])
    if checkpoints:
        print("checkpoints:")
        for checkpoint in checkpoints:
            print(f"  - {checkpoint.get('checkpoint_id', '-')}: {checkpoint.get('summary', '-')}")


def _create_studio_session() -> dict[str, Any]:
    studio_session_id = _new_studio_session_id()
    session_dir = _session_dir(studio_session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    manifest = _studio_manifest(studio_session_id)
    _write_json(_manifest_path(studio_session_id), manifest)
    _write_attached_runs(studio_session_id, [])
    _checkpoints_path(studio_session_id).touch(exist_ok=True)
    _append_event(studio_session_id, "studio_session_created", status="active")
    return _studio_payload(studio_session_id)


def _add_checkpoint(studio_session_id: str, summary: str) -> dict[str, Any]:
    manifest = _require_active_manifest(studio_session_id)
    checkpoints = _load_jsonl(_checkpoints_path(studio_session_id))
    checkpoint = {
        "schema_version": 1,
        "checkpoint_id": f"{studio_session_id}-checkpoint-{len(checkpoints) + 1:04d}",
        "studio_session_id": studio_session_id,
        "summary": summary,
        "recorded_at": _now_iso(),
    }
    _append_jsonl(_checkpoints_path(studio_session_id), checkpoint)
    _append_event(
        studio_session_id,
        "studio_checkpoint_added",
        checkpoint_id=checkpoint["checkpoint_id"],
        summary=summary,
        status=str(manifest.get("status") or "active"),
    )
    return checkpoint


def _attach_run(studio_session_id: str, run_id: str) -> dict[str, Any]:
    _require_active_manifest(studio_session_id)
    runs = _discover_run_summaries()
    try:
        run = _resolve_run(run_id, runs)
    except RunsCliError as exc:
        raise StudioCliError(str(exc)) from exc
    return attach_run_reference(
        studio_session_id=studio_session_id,
        run_id=run.run_id,
        session_id=run.session_id,
        surface=_surface_for_run(run),
        mode=run.planning_mode,
        status=run.status,
        events_path=run.events_path,
        session_path=run.session_path,
        output_path=run.output_path,
    )


def _end_studio_session(studio_session_id: str) -> dict[str, Any]:
    manifest = _require_manifest(studio_session_id)
    if str(manifest.get("status") or "") == "ended":
        return _studio_payload(studio_session_id)
    manifest["status"] = "ended"
    manifest["ended_at"] = _now_iso()
    _write_json(_manifest_path(studio_session_id), manifest)
    _append_event(studio_session_id, "studio_session_ended", status="ended")
    return _studio_payload(studio_session_id)


def _print_result(payload: Any, *, as_json: bool, operation: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    if isinstance(payload, dict) and "manifest" in payload:
        _print_studio(payload)
        return
    if operation == "checkpoint":
        print(f"CHECKPOINT checkpoint_id={payload['checkpoint_id']} studio_session_id={payload['studio_session_id']}")
        return
    if operation == "attach":
        print(f"ATTACHED studio_session_id={payload['studio_session_id']} run_id={payload['run_id']}")
        return


def cmd_studio(args: argparse.Namespace) -> int:
    try:
        if args.studio_cmd == "create":
            payload = _create_studio_session()
            _print_result(payload, as_json=bool(getattr(args, "json", False)), operation="create")
            return 0
        if args.studio_cmd == "show":
            payload = _studio_payload(str(args.studio_session_id))
            _print_result(payload, as_json=bool(getattr(args, "json", False)), operation="show")
            return 0
        if args.studio_cmd == "end":
            payload = _end_studio_session(str(args.studio_session_id))
            _print_result(payload, as_json=bool(getattr(args, "json", False)), operation="end")
            return 0
        if args.studio_cmd == "attach-run":
            payload = _attach_run(str(args.studio_session_id), str(args.run_id))
            _print_result(payload, as_json=bool(getattr(args, "json", False)), operation="attach")
            return 0
        if args.studio_cmd == "checkpoint" and args.studio_checkpoint_cmd == "add":
            summary = str(getattr(args, "summary", "") or "").strip()
            if not summary:
                raise StudioCliError("checkpoint summary is required")
            payload = _add_checkpoint(str(args.studio_session_id), summary)
            _print_result(payload, as_json=bool(getattr(args, "json", False)), operation="checkpoint")
            return 0
        raise StudioCliError("unsupported studio command")
    except StudioCliError as exc:
        print(f"[studio] {exc}", file=sys.stderr)
        return 1


__all__ = [
    "attach_run_reference",
    "cmd_studio",
    "read_studio_session_id_from_events",
    "require_active_studio_session",
]
