"""Minimal bounded batch execution contract helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

RUNTIME_MODE_INTERACTIVE = "interactive"
RUNTIME_MODE_HEADLESS_SINGLE = "headless_single"
RUNTIME_MODE_HEADLESS_BATCH = "headless_batch"

ALLOWED_RUNTIME_MODES = {
    RUNTIME_MODE_INTERACTIVE,
    RUNTIME_MODE_HEADLESS_SINGLE,
    RUNTIME_MODE_HEADLESS_BATCH,
}

FORCED_CHOICE_OPTIONS = (
    "resume_next",
    "retry_current",
    "handoff",
    "stop",
)

NON_MEANINGFUL_STOP_REASONS = {
    "cancelled",
    "max_steps_exceeded",
    "max_tokens_exceeded",
    "max_cost_exceeded",
    "max_errors_exceeded",
    "no_progress",
    "worker_max_iterations_no_progress",
    "validation_command_failed",
    "promotion_apply_failed",
    "scratch_artifacts_pending_review",
}

EXHAUSTION_STOP_REASONS = {
    "no_progress",
    "worker_max_iterations_no_progress",
    "max_steps_exceeded",
    "max_errors_exceeded",
    "verifier_rejected_terminal",
}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, Iterable):
        items = list(value)
    else:
        raise ValueError(f"Expected list of strings, got {type(value).__name__}")
    normalized: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def normalize_runtime_mode(
    value: Optional[str],
    *,
    default: str = RUNTIME_MODE_HEADLESS_BATCH,
) -> str:
    raw = (value or default).strip().lower().replace("-", "_")
    aliases = {
        "single": RUNTIME_MODE_HEADLESS_SINGLE,
        "headless": RUNTIME_MODE_HEADLESS_SINGLE,
        "batch": RUNTIME_MODE_HEADLESS_BATCH,
    }
    normalized = aliases.get(raw, raw)
    if normalized not in ALLOWED_RUNTIME_MODES:
        allowed = ", ".join(sorted(ALLOWED_RUNTIME_MODES))
        raise ValueError(f"Unsupported runtime mode '{value}'. Expected one of: {allowed}")
    return normalized


def _normalize_workspace_path(path: str, workspace_root: Path) -> str:
    root = workspace_root.resolve()
    raw = Path(path)
    resolved = raw.resolve(strict=False) if raw.is_absolute() else (root / raw).resolve(strict=False)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Path '{path}' resolves outside workspace root") from exc


def _is_relative_to(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


@dataclass(frozen=True)
class ScopeContract:
    read_files: list[str]
    write_files: list[str]
    allowed_commands: list[str]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScopeContract":
        read_files = _string_list(data.get("read_files"))
        write_files = _string_list(data.get("write_files"))
        allowed_commands = _string_list(data.get("allowed_commands"))
        if not read_files:
            raise ValueError("Batch item scope is missing read_files")
        if not write_files:
            raise ValueError("Batch item scope is missing write_files")
        return cls(
            read_files=read_files,
            write_files=write_files,
            allowed_commands=allowed_commands,
        )

    def normalize(self, workspace_root: Path) -> "ScopeContract":
        return ScopeContract(
            read_files=[_normalize_workspace_path(path, workspace_root) for path in self.read_files],
            write_files=[_normalize_workspace_path(path, workspace_root) for path in self.write_files],
            allowed_commands=list(self.allowed_commands),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "read_files": list(self.read_files),
            "write_files": list(self.write_files),
            "allowed_commands": list(self.allowed_commands),
        }


@dataclass(frozen=True)
class BatchItemContract:
    id: str
    prompt: str
    scope: ScopeContract
    max_attempts: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BatchItemContract":
        item_id = str(data.get("id") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        if not item_id:
            raise ValueError("Batch item is missing id")
        if not prompt:
            raise ValueError(f"Batch item '{item_id}' is missing prompt")
        max_attempts = int(data.get("max_attempts") or 1)
        if max_attempts < 1:
            raise ValueError(f"Batch item '{item_id}' must set max_attempts >= 1")
        return cls(
            id=item_id,
            prompt=prompt,
            scope=ScopeContract.from_dict(data.get("scope") or {}),
            max_attempts=max_attempts,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "scope": self.scope.to_dict(),
            "max_attempts": self.max_attempts,
        }


@dataclass(frozen=True)
class BatchManifestContract:
    batch_id: str
    runtime_mode: str
    items: list[BatchItemContract]
    max_items: int
    max_exhausted_items: int = 1
    pause_on_exhaustion: bool = True

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BatchManifestContract":
        batch_id = str(data.get("batch_id") or "").strip()
        if not batch_id:
            raise ValueError("Batch manifest is missing batch_id")
        items = [BatchItemContract.from_dict(item) for item in list(data.get("items") or [])]
        if not items:
            raise ValueError("Batch manifest must include at least one item")
        max_items = int(data.get("max_items") or len(items))
        if max_items < 1:
            raise ValueError("Batch manifest must set max_items >= 1")
        if len(items) > max_items:
            raise ValueError("Batch manifest contains more items than max_items")
        max_exhausted_items = int(data.get("max_exhausted_items") or 1)
        if max_exhausted_items < 1:
            raise ValueError("Batch manifest must set max_exhausted_items >= 1")
        return cls(
            batch_id=batch_id,
            runtime_mode=normalize_runtime_mode(str(data.get("runtime_mode") or "")),
            items=items,
            max_items=max_items,
            max_exhausted_items=max_exhausted_items,
            pause_on_exhaustion=bool(data.get("pause_on_exhaustion", True)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "runtime_mode": self.runtime_mode,
            "max_items": self.max_items,
            "max_exhausted_items": self.max_exhausted_items,
            "pause_on_exhaustion": self.pause_on_exhaustion,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ScopeGateResult:
    allowed: bool
    reason: Optional[str]
    normalized_scope: Dict[str, Any]
    blocked_paths: list[Dict[str, str]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "normalized_scope": dict(self.normalized_scope),
            "blocked_paths": [dict(item) for item in self.blocked_paths],
        }


@dataclass(frozen=True)
class MeaningfulDeltaResult:
    meaningful: bool
    reason: str
    signals: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "meaningful": self.meaningful,
            "reason": self.reason,
            "signals": dict(self.signals),
        }


@dataclass(frozen=True)
class BatchItemEvaluationResult:
    decision: str
    pause_mode: Optional[str]
    required_choice: Optional[str]
    forced_choices: list[str]
    exhausted: bool
    meaningful_delta: MeaningfulDeltaResult
    handoff: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "pause_mode": self.pause_mode,
            "required_choice": self.required_choice,
            "forced_choices": list(self.forced_choices),
            "exhausted": self.exhausted,
            "meaningful_delta": self.meaningful_delta.to_dict(),
            "handoff": dict(self.handoff) if isinstance(self.handoff, dict) else None,
        }


def scope_gate_item(
    scope: ScopeContract,
    *,
    workspace_root: Path,
    no_touch_paths: Optional[Iterable[str]] = None,
    readonly_repos: Optional[Mapping[str, Path]] = None,
) -> ScopeGateResult:
    normalized_scope = scope.normalize(workspace_root).to_dict()
    blocked_paths: list[Dict[str, str]] = []

    protected_paths = [
        _normalize_workspace_path(path, workspace_root)
        for path in _string_list(no_touch_paths or [])
    ]
    readonly_roots = {
        name: _normalize_workspace_path(str(path), workspace_root)
        for name, path in dict(readonly_repos or {}).items()
    }

    for path in normalized_scope["read_files"] + normalized_scope["write_files"]:
        for protected_path in protected_paths:
            if _is_relative_to(path, protected_path):
                blocked_paths.append({"path": path, "reason": "no_touch_path"})
        for repo_name, repo_root in readonly_roots.items():
            if _is_relative_to(path, repo_root):
                blocked_paths.append({"path": path, "reason": f"readonly_repo:{repo_name}"})

    if blocked_paths:
        first_reason = blocked_paths[0]["reason"]
        return ScopeGateResult(
            allowed=False,
            reason=first_reason,
            normalized_scope=normalized_scope,
            blocked_paths=blocked_paths,
        )

    return ScopeGateResult(
        allowed=True,
        reason=None,
        normalized_scope=normalized_scope,
        blocked_paths=[],
    )


def evaluate_meaningful_delta(run_payload: Mapping[str, Any]) -> MeaningfulDeltaResult:
    loop_state = dict(run_payload.get("loop_state") or {})
    latest_evidence = dict(run_payload.get("latest_evidence") or loop_state.get("latest_evidence") or {})
    worker_state = dict(loop_state.get("worker_state") or {})
    stop_reason = str(latest_evidence.get("stop_reason") or loop_state.get("stop_reason") or "").strip()
    terminal_condition = str(latest_evidence.get("terminal_condition") or "").strip()
    state_changed = bool(latest_evidence.get("state_changed") or worker_state.get("state_changed"))
    definition_of_done_met = bool(latest_evidence.get("definition_of_done_met"))
    promotion_applied = bool(latest_evidence.get("promotion_applied") or worker_state.get("promotion_applied"))
    files_touched = _string_list(worker_state.get("files_touched"))
    canonical_write_targets = _string_list(
        latest_evidence.get("canonical_write_targets") or worker_state.get("canonical_write_targets")
    )
    blocker = str(latest_evidence.get("blocker") or worker_state.get("blocker") or "").strip() or None
    signals = {
        "stop_reason": stop_reason or None,
        "terminal_condition": terminal_condition or None,
        "state_changed": state_changed,
        "definition_of_done_met": definition_of_done_met,
        "promotion_applied": promotion_applied,
        "files_touched": files_touched,
        "canonical_write_targets": canonical_write_targets,
        "blocker": blocker,
        "same_result_count": int(loop_state.get("same_result_count") or 0),
        "same_error_count": int(loop_state.get("same_error_count") or 0),
        "no_change_steps": int(loop_state.get("no_change_steps") or 0),
    }

    if definition_of_done_met:
        return MeaningfulDeltaResult(True, "definition_of_done_met", signals)
    if stop_reason in NON_MEANINGFUL_STOP_REASONS:
        return MeaningfulDeltaResult(False, f"stop_reason:{stop_reason}", signals)
    if terminal_condition == "definition_of_done_met":
        return MeaningfulDeltaResult(True, "terminal_condition:definition_of_done_met", signals)
    if state_changed or promotion_applied or files_touched or canonical_write_targets:
        return MeaningfulDeltaResult(True, "durable_repo_delta_detected", signals)
    return MeaningfulDeltaResult(False, "no_durable_repo_delta", signals)


def build_exhaustion_handoff(
    item: BatchItemContract,
    *,
    attempts_used: int,
    last_child_run_id: Optional[str],
    run_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    loop_state = dict(run_payload.get("loop_state") or {})
    latest_evidence = dict(run_payload.get("latest_evidence") or loop_state.get("latest_evidence") or {})
    return {
        "item_id": item.id,
        "attempts_used": attempts_used,
        "last_child_run_id": last_child_run_id,
        "last_stop_reason": latest_evidence.get("stop_reason") or loop_state.get("stop_reason"),
        "last_blocker": latest_evidence.get("blocker"),
        "last_summary": latest_evidence.get("summary") or loop_state.get("last_result_summary"),
        "recommended_next_choice": "handoff",
    }


def evaluate_batch_item(
    item: BatchItemContract,
    *,
    attempts_used: int,
    last_child_run_id: Optional[str],
    run_payload: Mapping[str, Any],
) -> BatchItemEvaluationResult:
    meaningful_delta = evaluate_meaningful_delta(run_payload)
    signals = meaningful_delta.signals
    exhausted = (
        attempts_used >= item.max_attempts
        or str(signals.get("stop_reason") or "") in EXHAUSTION_STOP_REASONS
        or int(signals.get("same_result_count") or 0) >= 3
        or int(signals.get("same_error_count") or 0) >= 3
        or int(signals.get("no_change_steps") or 0) >= 3
    )
    if meaningful_delta.meaningful:
        return BatchItemEvaluationResult(
            decision="continue",
            pause_mode=None,
            required_choice=None,
            forced_choices=[],
            exhausted=False,
            meaningful_delta=meaningful_delta,
            handoff=None,
        )

    required_choice = "handoff" if exhausted else "retry_current"
    handoff = (
        build_exhaustion_handoff(
            item,
            attempts_used=attempts_used,
            last_child_run_id=last_child_run_id,
            run_payload=run_payload,
        )
        if exhausted
        else None
    )
    return BatchItemEvaluationResult(
        decision="pause_for_choice",
        pause_mode="forced_choice",
        required_choice=required_choice,
        forced_choices=list(FORCED_CHOICE_OPTIONS),
        exhausted=exhausted,
        meaningful_delta=meaningful_delta,
        handoff=handoff,
    )
