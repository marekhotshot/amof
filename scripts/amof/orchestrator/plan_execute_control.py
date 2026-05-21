"""Plan-execute fatal stop, execution readiness, and resume checkpoints."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .planner import ExecutionPlan, Subtask
from .trust_boundary import Capability, TrustState, derive_trusted_intent_caps

# Fatal failures always stop the plan (continue_on_failure cannot override).
FATAL_STOP_REASONS = frozenset(
    {
        "cost_exceeded",
        "provider_auth",
        "provider_quota",
        "provider_rate_limit",
        "provider_payment_required",
        "missing_required_tool",
        "trust_boundary_denied",
        "capability_not_authorized_by_trusted_intent",
        "writable_root_denied",
        "user_interrupt",
        "interrupted",
        "invalid_execution_preconditions",
        "missing_required_secret_access",
    }
)

SKIP_FATAL_PRECONDITION = "skipped_fatal_precondition"
SKIP_BUDGET_BLOCKED = "skipped_budget_blocked"

_POLICY_DENIED_RE = re.compile(
    r"POLICY DENIED \[([^\]]+)\]",
    re.IGNORECASE,
)
_WRITABLE_ROOT_RE = re.compile(
    r"outside writable roots",
    re.IGNORECASE,
)
_ABS_PATH_RE = re.compile(
    r"(?<![\w/.-])(/[\w./-]+\.(?:md|markdown|py|json|yaml|yml|txt|sh|log|csv|html))"
)
_REPORT_DIR_RE = re.compile(
    r"(/[\w./-]*(?:report|reports|matrix)[\w./-]*)",
    re.IGNORECASE,
)

_SHELL_INTENT_RE = re.compile(
    r"\b(shell|bash|kubectl|helm|jenkins|trigger\.sh|git\s+(status|diff|checkout|commit))\b",
    re.IGNORECASE,
)
_SECRET_INTENT_RE = re.compile(
    r"\b(secret|credential|api[_ -]?key|token|password|kubeconfig|\.env\b|jenkins)\b",
    re.IGNORECASE,
)
_NETWORK_INTENT_RE = re.compile(
    r"\b(curl|wget|http|https|jenkins|kubectl|helm|fetch|download|api)\b",
    re.IGNORECASE,
)
_WRITE_INTENT_RE = re.compile(
    r"\b(write|report|output|save to|matrix-reports)\b",
    re.IGNORECASE,
)

_TRUST_BOUNDARY_TO_FATAL = {
    "capability_not_authorized_by_trusted_intent": "capability_not_authorized_by_trusted_intent",
    "secret_access_from_untrusted_context": "missing_required_secret_access",
    "network_access_from_untrusted_context": "trust_boundary_denied",
    "write_not_authorized_by_trusted_intent": "trust_boundary_denied",
}


@dataclass
class ExecutionReadinessIssue:
    kind: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionReadinessResult:
    ok: bool
    failure_type: str = ""
    issues: List[ExecutionReadinessIssue] = field(default_factory=list)

    @property
    def is_fatal(self) -> bool:
        return not self.ok


@dataclass
class PlanExecuteCheckpoint:
    plan_id: str
    session_id: str
    plan_path: Optional[str]
    completed_subtasks: List[str]
    failed_subtask_id: Optional[str]
    failure_type: str
    failure_message: str
    remaining_subtasks: List[str]
    skip_reason: str
    resume_command: str
    continue_on_failure: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "session_id": self.session_id,
            "plan_path": self.plan_path,
            "completed_subtasks": self.completed_subtasks,
            "failed_subtask_id": self.failed_subtask_id,
            "failure_type": self.failure_type,
            "failure_message": self.failure_message,
            "remaining_subtasks": self.remaining_subtasks,
            "skip_reason": self.skip_reason,
            "resume_command": self.resume_command,
            "continue_on_failure": self.continue_on_failure,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


@dataclass
class PlanExecuteStop:
    fatal: bool
    failure_type: str
    failure_message: str
    failed_subtask_id: Optional[str] = None
    skip_status: str = SKIP_FATAL_PRECONDITION


def _plan_text(plan: ExecutionPlan, task_context: Optional[str] = None) -> str:
    parts: List[str] = []
    if task_context:
        parts.append(task_context)
    parts.append(plan.analysis or "")
    for st in plan.subtasks:
        parts.extend([st.title or "", st.description or ""])
    return "\n".join(parts)


def extract_report_paths(text: str) -> List[str]:
    paths: Set[str] = set()
    for match in _ABS_PATH_RE.finditer(text or ""):
        paths.add(match.group(1))
    for match in _REPORT_DIR_RE.finditer(text or ""):
        candidate = match.group(1).rstrip("/")
        if candidate.startswith("/"):
            paths.add(candidate)
    return sorted(paths)


def derive_required_capabilities(text: str) -> Set[Capability]:
    caps = set(derive_trusted_intent_caps(text))
    if _SECRET_INTENT_RE.search(text or ""):
        caps.update({"secret", "write"})
    if _NETWORK_INTENT_RE.search(text or ""):
        caps.add("network")
    if _WRITE_INTENT_RE.search(text or ""):
        caps.add("write")
    return caps  # type: ignore[return-value]


def derive_required_tools(text: str, plan: ExecutionPlan) -> Set[str]:
    required: Set[str] = set()
    if _SHELL_INTENT_RE.search(text or ""):
        required.add("Shell")
    for st in plan.subtasks:
        runner = (st.runner or "code").strip().lower()
        if runner not in {"code", "docs"}:
            required.add(f"runner:{runner}")
    return required


def normalize_subtask_failure(
    stop_reason: Optional[str],
    error: Optional[str] = None,
    *,
    events: Any = None,
) -> str:
    reason = str(stop_reason or error or "tool_failed").strip()
    combined = f"{reason} {error or ''}"

    if reason in FATAL_STOP_REASONS:
        return reason

    policy_match = _POLICY_DENIED_RE.search(combined)
    if policy_match:
        code = policy_match.group(1).strip()
        return _TRUST_BOUNDARY_TO_FATAL.get(code, "trust_boundary_denied")

    if _WRITABLE_ROOT_RE.search(combined):
        return "writable_root_denied"

    if events is not None:
        for event in reversed(getattr(events, "events", []) or []):
            if event.get("type") == "tool_call" and not event.get("success"):
                err = str(event.get("error") or "")
                policy_match = _POLICY_DENIED_RE.search(err)
                if policy_match:
                    code = policy_match.group(1).strip()
                    return _TRUST_BOUNDARY_TO_FATAL.get(code, "trust_boundary_denied")
                if _WRITABLE_ROOT_RE.search(err):
                    return "writable_root_denied"
            if event.get("type") != "policy_gate" or event.get("allowed"):
                continue
            code = str(event.get("reason_code") or "")
            return _TRUST_BOUNDARY_TO_FATAL.get(code, "trust_boundary_denied")

    if reason == "tool_failed":
        return "tool_failed"
    if reason == "completed":
        return "tool_failed"
    return reason or "tool_failed"


def is_fatal_failure(
    failure_type: str,
    *,
    subtask_optional: bool = False,
    continue_on_failure: bool = False,
) -> bool:
    if failure_type in FATAL_STOP_REASONS:
        return True
    if subtask_optional or continue_on_failure:
        return False
    return True


def skip_status_for_failure(failure_type: str) -> str:
    if failure_type == "cost_exceeded":
        return SKIP_BUDGET_BLOCKED
    return SKIP_FATAL_PRECONDITION


def skip_remaining_subtasks(
    plan: ExecutionPlan,
    *,
    skip_status: str,
    skip_message: str,
    after_task_id: Optional[str] = None,
) -> List[str]:
    """Mark pending subtasks as skipped. Returns skipped task ids."""
    del after_task_id  # reserved for checkpoint metadata; all pending tasks are skipped
    skipped: List[str] = []
    for st in plan.subtasks:
        if st.status != "pending":
            continue
        st.status = "skipped"
        st.error = skip_message or skip_status
        skipped.append(st.id)
    return skipped


def assess_execution_readiness(
    goal: str,
    plan: ExecutionPlan,
    *,
    trust_state: Optional[TrustState],
    runner_factory: Any,
    guardrails: Any,
    parent_tool_names: Optional[Set[str]] = None,
) -> ExecutionReadinessResult:
    text = _plan_text(plan, goal)
    issues: List[ExecutionReadinessIssue] = []

    required_caps = derive_required_capabilities(text)
    trusted = set(trust_state.trusted_intent_caps if trust_state else {"read"})
    missing_caps = sorted(set(required_caps) - trusted)
    if missing_caps:
        issues.append(
            ExecutionReadinessIssue(
                kind="missing_capability",
                message=f"Required capability: {', '.join(missing_caps)}",
                detail={
                    "required": sorted(required_caps),
                    "allowed_ceiling": sorted(trusted),
                },
            )
        )
        if "secret" in missing_caps:
            cap_failure = "missing_required_secret_access"
        else:
            cap_failure = "capability_not_authorized_by_trusted_intent"
        return ExecutionReadinessResult(
            ok=False,
            failure_type=cap_failure,
            issues=issues,
        )

    failure_type = "invalid_execution_preconditions"
    required_tools = derive_required_tools(text, plan)
    available_runners = set(getattr(runner_factory, "runner_names", []) or [])
    parent_tools = parent_tool_names or set()
    for tool in sorted(required_tools):
        if tool == "Shell" and "Shell" not in parent_tools:
            issues.append(
                ExecutionReadinessIssue(
                    kind="missing_tool",
                    message="Required tool: Shell (not in runner registry)",
                    detail={"tool": "Shell"},
                )
            )
            failure_type = "missing_required_tool"
        elif tool.startswith("runner:"):
            runner_name = tool.split(":", 1)[1]
            if runner_name not in available_runners:
                issues.append(
                    ExecutionReadinessIssue(
                        kind="missing_runner",
                        message=f"Required runner: {runner_name} (not registered)",
                        detail={"runner": runner_name},
                    )
                )
                failure_type = "missing_required_tool"

    if trust_state and "secret" in required_caps and "secret" not in trusted:
        issues.append(
            ExecutionReadinessIssue(
                kind="trust_boundary",
                message="Shell/tool secret access denied by trust boundary",
                detail={
                    "requested": ["secret"],
                    "allowed_ceiling": sorted(trusted),
                },
            )
        )

    for path in extract_report_paths(text):
        if guardrails is None:
            continue
        err = guardrails.check_write(path)
        if err:
            issues.append(
                ExecutionReadinessIssue(
                    kind="writable_root",
                    message=f"Required report path not writable: {path}",
                    detail={
                        "path": path,
                        "guardrail": err,
                        "writable_roots": [
                            str(root)
                            for root in getattr(guardrails, "writable_roots", []) or []
                        ],
                    },
                )
            )
            failure_type = "writable_root_denied"

    if not issues:
        return ExecutionReadinessResult(ok=True)
    return ExecutionReadinessResult(
        ok=False,
        failure_type=failure_type,
        issues=issues,
    )


def build_checkpoint(
    plan: ExecutionPlan,
    *,
    session_id: str,
    failure_type: str,
    failure_message: str,
    failed_subtask_id: Optional[str],
    goal: str,
) -> PlanExecuteCheckpoint:
    completed = [st.id for st in plan.subtasks if st.status == "completed"]
    remaining = [
        st.id
        for st in plan.subtasks
        if st.status in {"pending", "skipped", "failed", "running"}
        and st.id not in completed
    ]
    plan_path = str(plan.file_path) if plan.file_path else None
    plan_id = plan_path or session_id
    resume_parts = ["amof agent --plan-execute"]
    if plan_path:
        resume_parts.append(f'--plan-file "{plan_path}"')
    resume_parts.append(f'"{goal[:80]}..."' if len(goal) > 80 else f'"{goal}"')
    resume_command = " ".join(resume_parts)

    return PlanExecuteCheckpoint(
        plan_id=plan_id,
        session_id=session_id,
        plan_path=plan_path,
        completed_subtasks=completed,
        failed_subtask_id=failed_subtask_id,
        failure_type=failure_type,
        failure_message=failure_message,
        remaining_subtasks=remaining,
        skip_reason=skip_status_for_failure(failure_type),
        resume_command=resume_command,
        continue_on_failure=getattr(plan, "continue_on_failure", False),
    )


def save_plan_checkpoint(checkpoint: PlanExecuteCheckpoint, checkpoints_dir: Path) -> Path:
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    slug = checkpoint.session_id or "plan"
    path = checkpoints_dir / f"{time.strftime('%Y-%m-%d-%H%M%S')}-plan-execute-{slug}.json"
    path.write_text(json.dumps(checkpoint.to_dict(), indent=2), encoding="utf-8")
    return path


def format_readiness_failure(result: ExecutionReadinessResult) -> str:
    lines = ["Execution readiness failed:"]
    for issue in result.issues:
        lines.append(f"- {issue.message}")
        if issue.kind == "missing_capability" and issue.detail:
            lines.append(
                f"  Allowed ceiling: {', '.join(issue.detail.get('allowed_ceiling', []))}"
            )
        if issue.kind == "writable_root" and issue.detail.get("writable_roots"):
            lines.append(
                f"  Writable roots: {', '.join(issue.detail['writable_roots'])}"
            )
    lines.append("No subtasks executed.")
    return "\n".join(lines)


def format_fatal_stop_summary(
    stop: PlanExecuteStop,
    *,
    skipped_count: int,
    checkpoint_path: Optional[Path] = None,
) -> str:
    lines = [
        f"Fatal stop: {stop.failure_type}",
        f"  {stop.failure_message}",
    ]
    if stop.failed_subtask_id:
        lines.insert(0, f"Subtask {stop.failed_subtask_id} failed: {stop.failure_type}")
    if skipped_count:
        lines.append(f"Remaining {skipped_count} subtask(s) skipped ({stop.skip_status}).")
    if checkpoint_path:
        lines.append(f"Checkpoint saved: {checkpoint_path}")
    lines.extend(
        [
            "Options:",
            "  [c] continue with more budget",
            "  [e] edit plan",
            "  [a] abort",
            "  [r] resume later",
        ]
    )
    return "\n".join(lines)
