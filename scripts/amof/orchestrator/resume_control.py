"""Resume follow-up and explicit budget controls for agent/plan-execute."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .planner import ExecutionPlan
from .telemetry import SessionTelemetry

_SECRET_VALUE_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|kubeconfig)\s*[:=]\s*\S+"
)


@dataclass(frozen=True)
class BudgetOptions:
    budget: Optional[float] = None
    cost_limit: Optional[float] = None
    subtask_budget: Optional[float] = None
    add_budget: Optional[float] = None
    require_budget_approval: bool = False
    budget_strict: bool = False
    budget_status: bool = False


@dataclass
class ResumeFollowup:
    text: str
    source: str  # inline | file
    sha256: str
    char_count: int
    preview: str

    def to_event_dict(self, session_id: str) -> Dict[str, Any]:
        return {
            "session_id": session_id,
            "source": self.source,
            "chars": self.char_count,
            "sha256": self.sha256,
            "preview": self.preview,
        }


def parse_positive_budget(value: Any, *, flag: str) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{flag} must be a positive number (got {value!r})") from exc
    if amount <= 0:
        raise ValueError(f"{flag} must be greater than zero (got {amount})")
    return amount


def resolve_run_budget(options: BudgetOptions) -> Tuple[Optional[float], Optional[str]]:
    """Return (hard_budget, error_message)."""
    if options.budget is not None and options.cost_limit is not None:
        if abs(options.budget - options.cost_limit) > 1e-9:
            return None, (
                "Cannot use both --budget and --cost-limit with different values. "
                "Use one canonical flag."
            )
    if options.budget is not None:
        return options.budget, None
    if options.cost_limit is not None:
        return options.cost_limit, None
    return None, None


def redact_sensitive_preview(text: str, *, max_len: int = 200) -> str:
    redacted = _SECRET_VALUE_RE.sub(r"\1=***", text or "")
    redacted = redacted.replace("\n", " ")
    if len(redacted) > max_len:
        return redacted[: max_len - 3] + "..."
    return redacted


def load_resume_followup(
    *,
    inline: Optional[str],
    file_path: Optional[str],
    readable_roots: Optional[List[Path]] = None,
) -> Tuple[Optional[ResumeFollowup], Optional[str]]:
    if inline and file_path:
        return None, "Use only one of --follow-up or --follow-up-file."
    if not inline and not file_path:
        return None, None

    if inline:
        text = inline.strip()
        source = "inline"
    else:
        path = Path(file_path).expanduser()
        if not path.is_file():
            return None, f"Follow-up file not found: {path}"
        if readable_roots:
            resolved = path.resolve()
            allowed = False
            for root in readable_roots:
                try:
                    resolved.relative_to(root.resolve())
                    allowed = True
                    break
                except ValueError:
                    continue
            if not allowed:
                roots = ", ".join(str(r) for r in readable_roots)
                return None, f"Follow-up file is outside readable roots: {roots}"
        text = path.read_text(encoding="utf-8").strip()
        source = "file"

    if not text:
        return None, "Follow-up text is empty."

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return (
        ResumeFollowup(
            text=text,
            source=source,
            sha256=digest,
            char_count=len(text),
            preview=redact_sensitive_preview(text),
        ),
        None,
    )


def find_latest_plan_checkpoint(checkpoints_dir: Path, session_id: str) -> Optional[Dict[str, Any]]:
    if not checkpoints_dir.is_dir():
        return None
    matches = sorted(
        checkpoints_dir.glob(f"*-plan-execute-{session_id}.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in matches:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("session_id") or "") == session_id:
            data["_checkpoint_path"] = str(path)
            return data
    return None


def prepare_plan_for_resume(
    plan: ExecutionPlan,
    checkpoint: Dict[str, Any],
    *,
    retry_failed: bool = True,
) -> str:
    """Restore plan subtask statuses from checkpoint; return next subtask id."""
    completed = set(checkpoint.get("completed_subtasks") or [])
    failed_id = checkpoint.get("failed_subtask_id")
    for st in plan.subtasks:
        if st.id in completed:
            st.status = "completed"
            st.error = None
        elif st.status == "skipped":
            st.status = "pending"
            st.error = None

    if failed_id and retry_failed:
        failed = plan.get_subtask(str(failed_id))
        if failed is not None:
            failed.status = "pending"
            failed.error = None

    for task_id in plan.execution_order:
        st = plan.get_subtask(task_id)
        if st and st.status == "pending":
            return st.id
    for st in plan.subtasks:
        if st.status == "pending":
            return st.id
    return ""


def append_followup_to_context(base: str, followup: Optional[ResumeFollowup]) -> str:
    if not followup:
        return base
    parts = [base, "", "## Operator follow-up (resume)", followup.text]
    return "\n".join(parts)


def estimate_plan_cost(plan: ExecutionPlan, *, per_subtask: float = 0.05) -> float:
    pending = sum(1 for st in plan.subtasks if st.status == "pending")
    return float(plan.planning_cost or 0.0) + pending * per_subtask


def check_budget_before_execution(
    telemetry: SessionTelemetry,
    plan: ExecutionPlan,
    options: BudgetOptions,
    *,
    noninteractive: bool,
) -> Optional[str]:
    if telemetry.max_cost is None:
        return None
    estimate = estimate_plan_cost(plan)
    remaining = max(0.0, telemetry.max_cost - telemetry.total_cost)
    if estimate <= remaining:
        return None
    msg = (
        f"Estimated plan cost ${estimate:.4f} exceeds remaining budget "
        f"${remaining:.4f} (limit ${telemetry.max_cost:.2f}, "
        f"spent ${telemetry.total_cost:.4f})."
    )
    if options.budget_strict:
        return msg + " (--budget-strict)"
    if options.require_budget_approval:
        if noninteractive:
            return msg + " Re-run with --add-budget or higher --budget."
        return "__prompt__"
    return None


def format_budget_status(
    session_id: str,
    telemetry: SessionTelemetry,
    checkpoint: Optional[Dict[str, Any]],
    *,
    subtask_budget: Optional[float] = None,
) -> str:
    lines = [
        f"Budget status for session {session_id}:",
        f"  Spent: ${telemetry.total_cost:.4f}",
    ]
    if telemetry.max_cost is not None:
        remaining = max(0.0, telemetry.max_cost - telemetry.total_cost)
        lines.append(f"  Limit: ${telemetry.max_cost:.2f}")
        lines.append(f"  Remaining: ${remaining:.4f}")
    else:
        lines.append("  Limit: (none)")
    if subtask_budget is not None:
        lines.append(f"  Subtask limit: ${subtask_budget:.2f}")
    if checkpoint:
        lines.append(f"  Checkpoint: {checkpoint.get('_checkpoint_path', '-')}")
        lines.append(f"  Last failure: {checkpoint.get('failure_type', '-')}")
        failed = checkpoint.get("failed_subtask_id")
        if failed:
            lines.append(f"  Failed subtask: {failed}")
        remaining_tasks = checkpoint.get("remaining_subtasks") or []
        if remaining_tasks:
            lines.append(f"  Remaining subtasks: {', '.join(remaining_tasks)}")
        resume_cmd = checkpoint.get("resume_command")
        if resume_cmd:
            lines.append(f"  Resume command: {resume_cmd}")
    return "\n".join(lines)


def format_resume_summary(
    *,
    session_id: str,
    checkpoint: Optional[Dict[str, Any]],
    followup: Optional[ResumeFollowup],
    add_budget: Optional[float],
    telemetry: SessionTelemetry,
    next_subtask_id: str,
    completed_count: int,
    remaining_count: int,
) -> str:
    lines = [f"Resuming session {session_id}"]
    if checkpoint:
        lines.append(f"Loaded checkpoint: {checkpoint.get('_checkpoint_path', '(unknown)')}")
    else:
        lines.append("Loaded checkpoint: (none)")
    if add_budget:
        lines.append(f"Additional budget approved: +${add_budget:.2f} (limit now ${telemetry.max_cost:.2f})")
    if followup:
        lines.append(f"Follow-up: {followup.source}, {followup.char_count} chars")
        lines.append(f"Follow-up preview: {followup.preview}")
    else:
        lines.append("Follow-up: none")
    if next_subtask_id:
        lines.append(f"Next action: execute subtask {next_subtask_id}")
    else:
        lines.append("Next action: (no pending subtasks)")
    lines.append(f"Completed subtasks preserved: {completed_count}")
    lines.append(f"Remaining subtasks to run: {remaining_count}")
    return "\n".join(lines)


def build_resume_cli_command(
    session_id: str,
    *,
    plan_path: Optional[str] = None,
    add_budget: Optional[float] = None,
    approve_capabilities: Optional[List[str]] = None,
    followup_hint: bool = False,
) -> str:
    parts = ["amof agent", f"--resume {session_id}"]
    if add_budget is not None:
        parts.append(f"--add-budget {add_budget:.2f}")
    if approve_capabilities:
        for cap in approve_capabilities:
            parts.append(f"--approve-capabilities {cap}")
    if followup_hint:
        parts.append('--follow-up "..."')
    if plan_path:
        parts.append(f'--plan-file "{plan_path}"')
    return " ".join(parts)


def update_checkpoint_budget(
    checkpoint_path: Path,
    checkpoint: Dict[str, Any],
    *,
    add_budget: float,
    new_limit: float,
) -> None:
    checkpoint["budget_limit"] = new_limit
    checkpoint["budget_added"] = float(checkpoint.get("budget_added") or 0.0) + add_budget
    checkpoint["resume_command"] = build_resume_cli_command(
        str(checkpoint.get("session_id") or ""),
        plan_path=checkpoint.get("plan_path"),
        add_budget=None,
        approve_capabilities=None,
        followup_hint=True,
    )
    checkpoint_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
