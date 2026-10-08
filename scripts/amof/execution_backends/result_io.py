"""Backend-independent canonical result and evidence persistence."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..commands.studio import attach_run_reference, require_active_studio_session
from ..write_scope_proposals import persist_write_scope_proposals_from_result

SECRET_LIKE_TEXT_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._-]+|"
    r"(?:token|secret|password|authorization|api[_-]?key)\s*[:=]\s*['\"]?[^\s,'\"]+|"
    r"(?:ghp|github_pat|sk|xoxb|xoxp|xoxs|xoxa)-[A-Za-z0-9._-]+)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_ms_from_timestamps(started_at: Any, completed_at: Any) -> int | None:
    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        return None
    try:
        duration_ms = int(
            (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)).total_seconds()
            * 1000
        )
    except ValueError:
        return None
    return duration_ms if duration_ms >= 0 else None


def _append_event(path: Path, event: str, **payload: Any) -> None:
    record = {"timestamp": _now_iso(), "event": event, "event_type": event, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _write_runtime_log(path: Path, message: str) -> None:
    path.write_text(message if message.endswith("\n") else f"{message}\n", encoding="utf-8")


def _redact_secret_like_text(text: str) -> str:
    return SECRET_LIKE_TEXT_RE.sub("[redacted]", text)


def _preview_kind(path: str, text: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".json"):
        return "json"
    if lowered.endswith(".jsonl"):
        return "jsonl"
    if lowered.endswith(".log"):
        return "log"
    if re.search(r"^\s*#{1,6}\s+", text, re.MULTILINE) or re.search(
        r"^\s*[-*+]\s+", text, re.MULTILINE
    ):
        return "markdown"
    return "text"


def _truncate_preview(text: str, limit: int = 16000) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[truncated {len(text) - limit} chars]"


def _preview_payload(*, path: str, raw: str, load_error: str | None = None) -> dict[str, Any]:
    sanitized = _truncate_preview(_redact_secret_like_text(raw))
    kind = _preview_kind(path, sanitized)
    preview_text = sanitized
    if kind == "json":
        try:
            preview_text = json.dumps(json.loads(sanitized), indent=2)
        except json.JSONDecodeError:
            preview_text = sanitized
    elif kind == "jsonl":
        lines: list[str] = []
        for line in sanitized.splitlines():
            if not line.strip():
                continue
            try:
                lines.append(json.dumps(json.loads(line), indent=2))
            except json.JSONDecodeError:
                lines.append(line)
            if len(lines) >= 80:
                break
        preview_text = "\n\n".join(lines)
    return {
        "path": path,
        "title": Path(path).stem,
        "kind": kind,
        "preview_text": preview_text,
        "raw_text": sanitized,
        "load_error": load_error,
    }


def _build_evidence_previews(
    *,
    result: dict[str, Any],
    result_path: Path,
    event_log_path: Path,
    runtime_log_path: Path,
) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    result_snapshot = dict(result)
    result_snapshot.pop("evidence_previews", None)
    previews.append(
        _preview_payload(
            path=str(result_path),
            raw=json.dumps(result_snapshot, indent=2) + "\n",
        )
    )
    for artifact_path in (event_log_path, runtime_log_path):
        if not artifact_path.exists():
            continue
        previews.append(
            _preview_payload(
                path=str(artifact_path),
                raw=artifact_path.read_text(encoding="utf-8"),
            )
        )
    return previews


def _write_terminal_result(
    *,
    result_path: Path,
    event_log_path: Path,
    runtime_log_path: Path,
    result: dict[str, Any],
    reason: str,
    started_at: str | None = None,
) -> dict[str, Any]:
    if started_at is not None:
        result.setdefault("started_at", started_at)
    result.setdefault("completed_at", _now_iso())
    duration_ms = _duration_ms_from_timestamps(
        result.get("started_at"),
        result.get("completed_at"),
    )
    if duration_ms is not None:
        result.setdefault("duration_ms", duration_ms)
    result.setdefault("result_path", str(result_path))
    result.setdefault("runtime_log_unavailable_reason", None)
    result.setdefault("failure_classification", reason if result.get("status") != "completed" else None)
    if not runtime_log_path.exists():
        _write_runtime_log(
            runtime_log_path,
            result.get("final_text") or result.get("stop_reason") or "terminal result written",
        )
    result["evidence_previews"] = _build_evidence_previews(
        result=result,
        result_path=result_path,
        event_log_path=event_log_path,
        runtime_log_path=runtime_log_path,
    )
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    # Wave 1: persist validated proposal evidence only. Never creates authority.
    # Backends may emit write_scope_proposals[]; singular write_scope_proposal
    # remains for compatibility. Prose-only / hostile approval claims are rejected.
    # Intentionally does not mutate AgentRunResult fields (additionalProperties=false).
    persist_write_scope_proposals_from_result(result)
    _append_event(
        event_log_path,
        "run_finished",
        run_id=str(result.get("session_id") or ""),
        session_id=str(result.get("session_id") or ""),
        studio_session_id=result.get("studio_session_id"),
        status=str(result.get("status") or "failed"),
        exit_code=result.get("exit_code"),
        stop_reason=str(result.get("stop_reason") or reason),
        failure_classification=reason,
        result_path=str(result_path),
        runtime_log_path=str(runtime_log_path),
    )
    return result


def _attach_studio_run(
    *,
    studio_session_id: str | None,
    run_id: str,
    event_log_path: Path,
    run_dir: Path,
    result_path: Path,
    status: str,
) -> None:
    if studio_session_id is None:
        return
    require_active_studio_session(studio_session_id)
    attach_run_reference(
        studio_session_id=studio_session_id,
        run_id=run_id,
        session_id=run_id,
        surface="agent",
        mode="execute",
        status=status,
        events_path=str(event_log_path),
        session_path=str(run_dir),
        output_path=str(result_path),
    )
