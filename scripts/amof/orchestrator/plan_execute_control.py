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

VALID_CAPABILITIES: frozenset[str] = frozenset(
    {
        "read",
        "write",
        "network",
        "secret",
        "shell_limited",
        "jenkins",
        "k8s",
        "k8s_mutation",
    }
)
CORE_TRUST_CAPABILITIES: frozenset[str] = frozenset(
    {"read", "write", "network", "secret"}
)
TOOL_PACK_SCOPED_CAPABILITIES: frozenset[str] = frozenset(
    {"shell_limited", "jenkins", "k8s", "k8s_mutation"}
)

_CAPABILITY_RATIONALE: Dict[str, str] = {
    "read": "Inspect repository files, configs, and plan context.",
    "write": "Write reports or modify files required by the approved plan.",
    "network": "Reach Jenkins, kubectl, helm, or HTTP endpoints referenced by the plan.",
    "secret": "Use credential env vars, tokens, or kubeconfig required by the plan (values are never logged).",
}

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
_ABS_PATH_RE = re.compile(r"(?<![\w/.-])(/[\w./-]+)")
_REPORT_FILE_RE = re.compile(
    r"(?<![\w/.-])(/[\w./-]*(?:report|reports|matrix)[\w./-]*\.(?:md|markdown))\b",
    re.IGNORECASE,
)
_MARKDOWN_FILE_RE = re.compile(
    r"(?<![\w/.-])(/[\w./-]+\.(?:md|markdown))\b",
    re.IGNORECASE,
)
_REPORT_ROOT_RE = re.compile(
    r"(?<![\w/.-])(/[\w./-]*(?:report|reports|matrix)[\w./-]*)",
    re.IGNORECASE,
)
_REPORT_GLOB_RE = re.compile(
    r"(?<![\w/.-])(/[\w./-]*(?:report|reports|matrix)[\w./-]*)/\*\.(?:md|markdown)\b",
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
_JENKINS_INTENT_RE = re.compile(
    r"\b(jenkins|trigger\.sh|jenkins[_ -]?job|job url)\b|https?://[^\s]+jenkins[^\s]*",
    re.IGNORECASE,
)
_K8S_INTENT_RE = re.compile(
    r"\b(kubectl|kubeconfig|kubernetes|helm)\b",
    re.IGNORECASE,
)
_HELM_RENDER_INTENT_RE = re.compile(
    r"\b(helm\s+(template|lint|diff)|validate\s+helm|chart diff|values validation)\b",
    re.IGNORECASE,
)
_HELM_DEPLOY_INTENT_RE = re.compile(
    r"\b(helm\s+(upgrade|install|uninstall)|upgrade --install|deploy|deployment|"
    r"rollout restart|post-deploy|operation\s*=\s*(install|uninstall))\b",
    re.IGNORECASE,
)
_CODE_EDIT_INTENT_RE = re.compile(
    r"\b(edit|modify|patch|change|write code|update file|strreplace|insert)\b",
    re.IGNORECASE,
)
_READ_ONLY_INSPECTION_RE = re.compile(
    r"\b(read[- ]only|inspect|inspection|audit|review|find|locate|search|"
    r"source code|repository source|literal)\b",
    re.IGNORECASE,
)
_ACTIVE_OPERATION_INTENT_RE = re.compile(
    r"\b(run|execute|use|call|trigger|apply|install|uninstall|upgrade|"
    r"mutate|rotate|read\s+(?:a\s+)?(?:\w+\s+){0,3}(?:secret|token|credential|kubeconfig|\.env)|"
    r"fetch|download|write|edit|modify|patch|update)\b",
    re.IGNORECASE,
)

_TRUST_BOUNDARY_TO_FATAL = {
    "capability_not_authorized_by_trusted_intent": "capability_not_authorized_by_trusted_intent",
    "secret_access_from_untrusted_context": "missing_required_secret_access",
    "network_access_from_untrusted_context": "trust_boundary_denied",
    "write_not_authorized_by_trusted_intent": "trust_boundary_denied",
}


@dataclass(frozen=True)
class ToolPack:
    name: str
    tools: List[str]
    capabilities: List[str]
    readable_roots: List[str] = field(default_factory=list)
    writable_roots: List[str] = field(default_factory=list)
    executable_paths: List[str] = field(default_factory=list)
    command_policy: List[str] = field(default_factory=list)
    approval_required_capabilities: List[str] = field(default_factory=list)
    description: str = ""


@dataclass
class ToolPackRequirements:
    packs: Set[str] = field(default_factory=set)
    capabilities: Set[str] = field(default_factory=set)
    writable_roots: Set[str] = field(default_factory=set)
    executable_paths: Set[str] = field(default_factory=set)
    command_policy: Dict[str, List[str]] = field(default_factory=dict)
    controlled_execution_packs: Set[str] = field(default_factory=set)


CORE_TOOL_PACKS: Dict[str, ToolPack] = {
    "core-read": ToolPack(
        name="core-read",
        tools=["Read", "LS", "Glob", "Grep", "InspectFiles"],
        capabilities=["read"],
        description="Read-only repository inspection tools.",
    ),
    "reports": ToolPack(
        name="reports",
        tools=["Write"],
        capabilities=["write"],
        description="Write report/checkpoint output under approved writable roots.",
    ),
    "code-edit": ToolPack(
        name="code-edit",
        tools=["Write", "StrReplace", "InsertAfter", "ReadLints"],
        capabilities=["read", "write"],
        description="Bounded code editing tools.",
    ),
    "ops-jenkins": ToolPack(
        name="ops-jenkins",
        tools=["JenkinsTrigger", "ShellRestricted"],
        capabilities=["network", "secret", "jenkins", "shell_limited"],
        approval_required_capabilities=["secret"],
        description="Jenkins trigger/check helper execution with limited shell policy.",
    ),
    "ops-k8s": ToolPack(
        name="ops-k8s",
        tools=["K8sInspect", "HelmRelease", "ShellRestricted"],
        capabilities=["network", "secret", "k8s", "shell_limited"],
        command_policy=[
            "kubectl get",
            "kubectl logs",
            "kubectl describe",
            "kubectl top",
            "helm status",
            "helm ls",
        ],
        approval_required_capabilities=["secret"],
        description="Kubernetes/Helm inspection and bounded remediation commands.",
    ),
    "ops-helm-render": ToolPack(
        name="ops-helm-render",
        tools=["HelmTemplate", "HelmLint", "HelmDiff", "ShellRestricted"],
        capabilities=["read", "shell_limited"],
        command_policy=[
            "helm template",
            "helm lint",
            "helm dependency build",
            "helm dependency update",
            "helm diff",
        ],
        description="Non-mutating Helm render, lint, validate, and diff checks.",
    ),
    "ops-helm-deploy": ToolPack(
        name="ops-helm-deploy",
        tools=["HelmDeploy", "K8sMutate", "ShellRestricted"],
        capabilities=["network", "secret", "write", "k8s_mutation", "shell_limited"],
        command_policy=[
            "helm install",
            "helm upgrade --install",
            "helm uninstall",
            "helm status",
        ],
        approval_required_capabilities=["secret"],
        description="Approved Helm/Kubernetes mutation workflow with report output.",
    ),
}

DEFAULT_ENABLED_TOOL_PACKS: frozenset[str] = frozenset(
    {"core-read", "reports", "code-edit"}
)
VALID_TOOL_PACKS: frozenset[str] = frozenset(CORE_TOOL_PACKS)


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
class PlanCapabilityElevation:
    """Scoped capability approval for one plan-execute session (no secret values)."""

    session_id: str
    plan_id: str
    approved_capabilities: List[str]
    base_ceiling: List[str]
    approved_tools: List[str] = field(default_factory=list)
    approved_paths: List[str] = field(default_factory=list)
    rationales: Dict[str, str] = field(default_factory=dict)
    approval_source: str = "interactive"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "approved_capabilities": list(self.approved_capabilities),
            "base_ceiling": list(self.base_ceiling),
            "approved_tools": list(self.approved_tools),
            "approved_paths": list(self.approved_paths),
            "rationales": dict(self.rationales),
            "approval_source": self.approval_source,
            "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


@dataclass
class PlanExecuteCheckpoint:
    plan_id: str
    session_id: str
    plan_path: Optional[str]
    goal: str
    completed_subtasks: List[str]
    failed_subtask_id: Optional[str]
    failure_type: str
    failure_message: str
    remaining_subtasks: List[str]
    skip_reason: str
    resume_command: str
    continue_on_failure: bool = False
    capability_elevation: Optional[Dict[str, Any]] = None
    tool_pack_approvals: List[str] = field(default_factory=list)
    writable_root_approvals: List[str] = field(default_factory=list)
    budget_limit: Optional[float] = None
    spent_cost: Optional[float] = None
    budget_added: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "plan_id": self.plan_id,
            "session_id": self.session_id,
            "plan_path": self.plan_path,
            "goal": self.goal,
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
        if self.capability_elevation is not None:
            payload["capability_elevation"] = self.capability_elevation
        if self.tool_pack_approvals:
            payload["tool_pack_approvals"] = list(self.tool_pack_approvals)
        if self.writable_root_approvals:
            payload["writable_root_approvals"] = list(self.writable_root_approvals)
        if self.budget_limit is not None:
            payload["budget_limit"] = self.budget_limit
        if self.spent_cost is not None:
            payload["spent_cost"] = self.spent_cost
        if self.budget_added:
            payload["budget_added"] = self.budget_added
        return payload


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


def _normalize_write_root(path: str) -> str:
    return str(path).rstrip("/")


def _report_write_roots(text: str) -> Set[str]:
    roots: Set[str] = set()
    blob = text or ""
    for match in _REPORT_GLOB_RE.finditer(blob):
        roots.add(_normalize_write_root(match.group(1)))
    for match in _REPORT_FILE_RE.finditer(blob):
        roots.add(_normalize_write_root(str(Path(match.group(1)).parent)))
    if _WRITE_INTENT_RE.search(blob):
        for match in _MARKDOWN_FILE_RE.finditer(blob):
            roots.add(_normalize_write_root(str(Path(match.group(1)).parent)))
    for match in _REPORT_ROOT_RE.finditer(blob):
        candidate = _normalize_write_root(match.group(1))
        if "." not in Path(candidate).name:
            roots.add(candidate)
    return {root for root in roots if root.startswith("/")}


def _jenkins_executables(text: str) -> Set[str]:
    paths: Set[str] = set()
    if not _JENKINS_INTENT_RE.search(text or ""):
        return paths
    for match in _ABS_PATH_RE.finditer(text or ""):
        path = match.group(1).rstrip('.,;)"\'')
        if Path(path).name in {"trigger.sh", "k8s.sh", "monitor-deployment.sh"}:
            paths.add(path)
    return paths


def _runner_tool_names(runner_factory: Any, runner: str) -> Set[str]:
    if runner_factory is None:
        return set()
    if hasattr(runner_factory, "runner_tool_names"):
        return set(runner_factory.runner_tool_names(runner))
    if hasattr(runner_factory, "_runners"):
        cfg = getattr(runner_factory, "_runners", {}).get(runner)
        if cfg is not None:
            return set(getattr(cfg, "tool_names", []) or [])
    return set()


def tool_pack_scoped_capabilities(approved_tool_packs: Set[str]) -> Set[str]:
    """Capabilities that a tool-pack approval grants only inside that pack."""
    scoped: Set[str] = set()
    for pack_name in approved_tool_packs:
        pack = CORE_TOOL_PACKS.get(pack_name)
        if not pack:
            continue
        scoped.update(
            cap
            for cap in pack.capabilities
            if cap in TOOL_PACK_SCOPED_CAPABILITIES
        )
    return scoped


def _has_namespace_scope(text: str) -> bool:
    return bool(re.search(r"(--namespace|-n)\s+\S+|\bnamespace\b", text or "", re.IGNORECASE))


def _has_release_scope(text: str) -> bool:
    return bool(re.search(r"\brelease\b|helm\s+(?:status|install|uninstall|upgrade)\s+\S+", text or "", re.IGNORECASE))


def _controlled_policy_status(
    pack_name: str,
    *,
    text: str,
    executable_paths: Set[str],
) -> Dict[str, Any]:
    lowered = (text or "").lower()
    dangerous = ["bash -c", "sh -c", "rm -rf", "&& rm", "| sh", "| bash"]
    matched_danger = next((item for item in dangerous if item in lowered), None)
    if matched_danger:
        return {
            "ok": False,
            "reason": f"unbounded shell fragment is not allowed: {matched_danger}",
        }

    if pack_name == "ops-jenkins":
        helpers = {
            path for path in executable_paths
            if Path(path).name == "trigger.sh"
        }
        if not helpers:
            return {
                "ok": False,
                "reason": "expected approved Jenkins helper path ending in trigger.sh",
            }
        return {"ok": True, "reason": "approved Jenkins trigger helper"}

    if pack_name == "ops-k8s":
        allowed = [
            "kubectl get",
            "kubectl logs",
            "kubectl describe",
            "kubectl top",
            "helm status",
            "helm ls",
        ]
        disallowed = [
            "kubectl apply",
            "kubectl delete",
            "kubectl exec",
            "kubectl port-forward",
            "helm upgrade",
            "helm install",
            "helm uninstall",
        ]
        blocked = next((cmd for cmd in disallowed if cmd in lowered), None)
        if blocked:
            return {"ok": False, "reason": f"{blocked} is outside ops-k8s inspection policy"}
        if not any(cmd in lowered for cmd in allowed):
            return {"ok": False, "reason": "no allowed kubectl/helm inspection command found"}
        if not _has_namespace_scope(text):
            return {"ok": False, "reason": "namespace scope is required for ops-k8s"}
        return {"ok": True, "reason": "namespace-scoped K8s/Helm inspection"}

    if pack_name == "ops-helm-deploy":
        allowed = [
            "helm upgrade --install",
            "helm install",
            "helm uninstall",
            "helm status",
        ]
        disallowed = [
            "kubectl delete",
            "kubectl apply",
            "kubectl exec",
            "kubectl port-forward",
        ]
        blocked = next((cmd for cmd in disallowed if cmd in lowered), None)
        if blocked:
            return {"ok": False, "reason": f"{blocked} is outside ops-helm-deploy policy"}
        if not any(cmd in lowered for cmd in allowed):
            return {"ok": False, "reason": "no allowed Helm deploy/status command found"}
        if not _has_namespace_scope(text):
            return {"ok": False, "reason": "namespace scope is required for ops-helm-deploy"}
        if not _has_release_scope(text):
            return {"ok": False, "reason": "release scope is required for ops-helm-deploy"}
        return {"ok": True, "reason": "namespace/release-scoped Helm operation"}

    return {"ok": True, "reason": "no additional controlled shell policy"}


def resolve_controlled_execution(
    plan: ExecutionPlan,
    *,
    runner_factory: Any,
    parent_tool_names: Set[str],
    approved_tool_packs: Set[str],
    effective_capabilities: Set[str],
    requirements: ToolPackRequirements,
    text: str,
) -> Dict[str, Any]:
    controlled_tools = {
        "ControlledShell",
        "ShellRestricted",
        "JenkinsTrigger",
        "K8sInspect",
        "HelmRelease",
        "HelmTemplate",
        "HelmLint",
        "HelmDiff",
        "HelmDeploy",
        "K8sMutate",
    }
    parent_available = bool(controlled_tools & parent_tool_names)
    delegated: Dict[str, bool] = {}
    for st in plan.subtasks:
        runner = (st.runner or "code").strip().lower()
        delegated[runner] = bool(controlled_tools & _runner_tool_names(runner_factory, runner))
    synthesized: Dict[str, Dict[str, Any]] = {}
    for pack_name in sorted(requirements.controlled_execution_packs & approved_tool_packs):
        pack = CORE_TOOL_PACKS[pack_name]
        missing_caps = sorted(set(pack.capabilities) - effective_capabilities)
        policy = _controlled_policy_status(
            pack_name,
            text=text,
            executable_paths=requirements.executable_paths,
        )
        synthesized[pack_name] = {
            "available": not missing_caps and bool(policy.get("ok")),
            "missing_capabilities": missing_caps,
            "policy_ok": bool(policy.get("ok")),
            "policy_reason": policy.get("reason", ""),
        }
    return {
        "parent_available": parent_available,
        "delegated": delegated,
        "available": parent_available or any(delegated.values()) or any(
            item["available"] for item in synthesized.values()
        ),
        "synthesized": synthesized,
    }


def derive_tool_pack_requirements(goal: str, plan: ExecutionPlan) -> ToolPackRequirements:
    """Derive plan-execute readiness requirements as tool packs."""
    text = _plan_text(plan, goal)
    lowered = text.lower()
    req = ToolPackRequirements(packs={"core-read"})
    read_only_inspection = _is_read_only_inspection(text)

    report_roots = _report_write_roots(text)
    if report_roots or "report" in lowered or "matrix" in lowered:
        req.packs.add("reports")
        req.writable_roots.update(report_roots)

    if _CODE_EDIT_INTENT_RE.search(text):
        req.packs.add("code-edit")

    if not read_only_inspection and _JENKINS_INTENT_RE.search(text):
        req.packs.add("ops-jenkins")
        req.controlled_execution_packs.add("ops-jenkins")
        req.executable_paths.update(_jenkins_executables(text))

    if not read_only_inspection and _K8S_INTENT_RE.search(text):
        req.packs.add("ops-k8s")
        req.controlled_execution_packs.add("ops-k8s")
        req.command_policy["ops-k8s"] = list(CORE_TOOL_PACKS["ops-k8s"].command_policy)

    if not read_only_inspection and _HELM_RENDER_INTENT_RE.search(text):
        req.packs.add("ops-helm-render")
        req.controlled_execution_packs.add("ops-helm-render")
        req.command_policy["ops-helm-render"] = list(
            CORE_TOOL_PACKS["ops-helm-render"].command_policy
        )

    if not read_only_inspection and _HELM_DEPLOY_INTENT_RE.search(text):
        req.packs.add("ops-helm-deploy")
        req.controlled_execution_packs.add("ops-helm-deploy")
        req.command_policy["ops-helm-deploy"] = list(
            CORE_TOOL_PACKS["ops-helm-deploy"].command_policy
        )

    if not read_only_inspection and _SECRET_INTENT_RE.search(text):
        req.capabilities.add("secret")

    for st in plan.subtasks:
        runner = (st.runner or "code").strip().lower()
        if runner == "code" and _CODE_EDIT_INTENT_RE.search(f"{st.title}\n{st.description}"):
            req.packs.add("code-edit")

    for pack_name in req.packs:
        req.capabilities.update(CORE_TOOL_PACKS[pack_name].capabilities)
    return req


def extract_report_paths(text: str) -> List[str]:
    """Report write roots only; helper scripts are modeled by tool packs."""
    return sorted(_report_write_roots(text))


def _is_read_only_inspection(text: str) -> bool:
    """Treat dangerous-domain words as data when the task is only source inspection."""
    if not _READ_ONLY_INSPECTION_RE.search(text or ""):
        return False
    if _ACTIVE_OPERATION_INTENT_RE.search(text or ""):
        return False
    return True


def parse_tool_pack_names(names: Optional[List[str]]) -> Set[str]:
    parsed: Set[str] = set()
    if not names:
        return parsed
    for raw in names:
        for part in str(raw).split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name not in VALID_TOOL_PACKS:
                raise ValueError(
                    f"Unknown tool pack {name!r}. "
                    f"Allowed: {', '.join(sorted(VALID_TOOL_PACKS))}"
                )
            parsed.add(name)
    return parsed


def parse_writable_root_paths(raw: Optional[List[str]]) -> List[str]:
    """Parse and validate plan-scoped writable root paths from CLI flags."""
    roots: List[str] = []
    if not raw:
        return roots
    for entry in raw:
        for part in str(entry).split(","):
            path = part.strip()
            if not path:
                continue
            if not path.startswith("/"):
                raise ValueError(
                    f"Writable root must be an absolute path (got {path!r})."
                )
            roots.append(_normalize_write_root(path))
    return roots


def _path_covered_by_writable_roots(path: str, roots: List[Path]) -> bool:
    if not roots:
        return False
    target = Path(path)
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def derive_required_capabilities(text: str) -> Set[Capability]:
    caps = set(derive_trusted_intent_caps(text))
    if _is_read_only_inspection(text or ""):
        return {"read"}  # type: ignore[return-value]
    if _SECRET_INTENT_RE.search(text or ""):
        caps.update({"secret", "write"})
    if _NETWORK_INTENT_RE.search(text or ""):
        caps.add("network")
    if _WRITE_INTENT_RE.search(text or ""):
        caps.add("write")
    return caps  # type: ignore[return-value]


def parse_capability_names(names: List[str]) -> Set[Capability]:
    """Parse and validate capability names from CLI flags."""
    parsed: Set[Capability] = set()
    for raw in names:
        for part in str(raw).split(","):
            cap = part.strip().lower()
            if not cap:
                continue
            if cap not in VALID_CAPABILITIES:
                raise ValueError(
                    f"Unknown capability {cap!r}. "
                    f"Allowed: {', '.join(sorted(VALID_CAPABILITIES))}"
                )
            parsed.add(cap)  # type: ignore[arg-type]
    return parsed


def plan_id_for(plan: ExecutionPlan, session_id: str) -> str:
    if plan.file_path:
        return str(plan.file_path)
    return session_id


def explain_capability_gaps(
    missing_caps: List[str],
    *,
    text: str,
    required_caps: Set[Capability],
) -> Dict[str, str]:
    rationales: Dict[str, str] = {}
    lowered = (text or "").lower()
    for cap in missing_caps:
        reason = _CAPABILITY_RATIONALE.get(cap, "Required by the approved plan.")
        if cap == "secret":
            hints: List[str] = []
            if "jenkins" in lowered or "trigger.sh" in lowered:
                hints.append("Jenkins helper/env references")
            if "kubeconfig" in lowered or "kubectl" in lowered:
                hints.append("kubeconfig for cluster access")
            if ".env" in lowered or "token" in lowered:
                hints.append("environment credential references")
            if hints:
                reason = f"{reason} Needed for: {', '.join(hints)}."
        elif cap == "network" and ("jenkins" in lowered or "kubectl" in lowered or "helm" in lowered):
            reason = f"{reason} Needed for Jenkins/K8s/network actions in the plan."
        rationales[cap] = reason
    return rationales


def readiness_is_capability_only_failure(result: ExecutionReadinessResult) -> bool:
    if result.ok or not result.issues:
        return False
    blocking_issues = [
        issue
        for issue in result.issues
        if issue.kind not in {"capability_summary", "path_summary"}
    ]
    if not blocking_issues:
        return False
    if not all(issue.kind == "missing_capability" for issue in blocking_issues):
        return False
    return result.failure_type in {
        "missing_required_secret_access",
        "capability_not_authorized_by_trusted_intent",
    }


def readiness_is_writable_root_only_failure(result: ExecutionReadinessResult) -> bool:
    if result.ok or not result.issues:
        return False
    return all(issue.kind == "writable_root" for issue in result.issues)


def apply_writable_root_elevation(guardrails: Any, roots: List[str]) -> List[str]:
    """Append plan-scoped writable roots to guardrails for this session only."""
    approved: List[str] = []
    for raw in roots:
        root = Path(raw).resolve()
        existing = [Path(r).resolve() for r in getattr(guardrails, "writable_roots", []) or []]
        if root not in existing:
            guardrails.writable_roots.append(root)
        approved.append(str(root))
    return approved


def apply_tool_pack_approval(plan: ExecutionPlan, approved_tool_packs: Set[str]) -> None:
    plan.tool_pack_approvals = sorted(approved_tool_packs)  # type: ignore[attr-defined]


def apply_capability_elevation(
    trust_state: TrustState,
    elevation: PlanCapabilityElevation,
) -> None:
    """Raise trusted ceiling for this plan/session only (in-memory)."""
    for cap in elevation.approved_capabilities:
        trust_state.trusted_intent_caps.add(cap)  # type: ignore[arg-type]


def build_plan_capability_elevation(
    *,
    session_id: str,
    plan: ExecutionPlan,
    goal: str,
    missing_caps: List[str],
    base_ceiling: Set[Capability],
    approval_source: str,
    parent_tool_names: Optional[Set[str]] = None,
) -> PlanCapabilityElevation:
    text = _plan_text(plan, goal)
    required_caps = derive_required_capabilities(text)
    return PlanCapabilityElevation(
        session_id=session_id,
        plan_id=plan_id_for(plan, session_id),
        approved_capabilities=sorted(missing_caps),
        base_ceiling=sorted(base_ceiling),
        approved_tools=sorted(parent_tool_names or derive_required_tools(text, plan)),
        approved_paths=sorted(extract_report_paths(text)),
        rationales=explain_capability_gaps(
            missing_caps,
            text=text,
            required_caps=required_caps,
        ),
        approval_source=approval_source,
    )


def checkpoint_contains_secret_values(payload: Dict[str, Any]) -> bool:
    """Detect accidental secret values (not capability labels) in checkpoint JSON."""
    blob = json.dumps(payload).lower()
    value_markers = (
        "super-secret-value",
        "password=sk-",
        "api_key=sk-",
        "token=eyj",
        "begin rsa private",
    )
    return any(marker in blob for marker in value_markers)


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
    approved_writable_roots: Optional[Set[str]] = None,
    approved_tool_packs: Optional[Set[str]] = None,
    approved_capabilities: Optional[Set[str]] = None,
    base_capability_ceiling: Optional[Set[str]] = None,
) -> ExecutionReadinessResult:
    text = _plan_text(plan, goal)
    issues: List[ExecutionReadinessIssue] = []
    requirements = derive_tool_pack_requirements(goal, plan)
    approved_packs = set(approved_tool_packs or [])
    enabled_packs = set(DEFAULT_ENABLED_TOOL_PACKS) | approved_packs
    parent_tools = set(parent_tool_names or [])
    plan_approved_caps = set(approved_capabilities or [])
    plan_approved_caps.update(
        getattr(plan, "capability_elevation", {}).get("approved_capabilities", [])
        if isinstance(getattr(plan, "capability_elevation", None), dict)
        else []
    )
    plan_approved_caps.update(tool_pack_scoped_capabilities(approved_packs))

    required_caps = derive_required_capabilities(text)
    required_caps.update(cap for cap in requirements.capabilities if cap in VALID_CAPABILITIES)
    base_ceiling = set(
        base_capability_ceiling
        if base_capability_ceiling is not None
        else (trust_state.trusted_intent_caps if trust_state else {"read"})
    )
    effective_ceiling = base_ceiling | plan_approved_caps
    missing_caps = sorted(set(required_caps) - effective_ceiling)
    failure_type = "invalid_execution_preconditions"
    issues.append(
        ExecutionReadinessIssue(
            kind="capability_summary",
            message="Capability readiness",
            detail={
                "required": sorted(required_caps),
                "base_ceiling": sorted(base_ceiling),
                "approved_capabilities": sorted(plan_approved_caps),
                "effective_ceiling": sorted(effective_ceiling),
                "missing_capabilities": missing_caps,
                "status": "approved" if not missing_caps else "missing",
            },
        )
    )
    if missing_caps:
        issues.append(
            ExecutionReadinessIssue(
                kind="missing_capability",
                message=f"Required capability: {', '.join(missing_caps)}",
                detail={
                    "required": sorted(required_caps),
                    "allowed_ceiling": sorted(effective_ceiling),
                    "base_ceiling": sorted(base_ceiling),
                    "approved_capabilities": sorted(plan_approved_caps),
                    "effective_ceiling": sorted(effective_ceiling),
                    "missing_capabilities": missing_caps,
                    "plan_text": text[:2000],
                },
            )
        )
        if "secret" in missing_caps:
            failure_type = "missing_required_secret_access"
        else:
            failure_type = "capability_not_authorized_by_trusted_intent"

    for pack_name in sorted(requirements.packs - enabled_packs):
        pack = CORE_TOOL_PACKS[pack_name]
        suggested = [f"--approve-tool-pack {pack_name}"]
        if pack.approval_required_capabilities:
            suggested.append(
                "--approve-capabilities "
                + ",".join(sorted(pack.approval_required_capabilities))
            )
        issues.append(
            ExecutionReadinessIssue(
                kind="missing_tool_pack",
                message=f"Required tool pack: {pack_name}",
                detail={
                    "pack": pack_name,
                    "status": "not approved",
                    "needs": sorted(pack.capabilities),
                    "tools": sorted(pack.tools),
                    "suggested_approval": " ".join(suggested),
                },
            )
        )
        failure_type = "missing_required_tool"

    controlled = resolve_controlled_execution(
        plan,
        runner_factory=runner_factory,
        parent_tool_names=parent_tools,
        approved_tool_packs=approved_packs,
        effective_capabilities=set(effective_ceiling),
        requirements=requirements,
        text=text,
    )
    for pack_name in sorted(requirements.controlled_execution_packs & enabled_packs):
        if (
            pack_name == "ops-k8s"
            and "ops-helm-deploy" in enabled_packs
            and _HELM_DEPLOY_INTENT_RE.search(text)
        ):
            continue
        synthesized = controlled["synthesized"].get(pack_name, {})
        mechanism_available = bool(
            controlled["parent_available"]
            or any(controlled["delegated"].values())
            or synthesized.get("available")
        )
        pack_controlled = bool(
            mechanism_available
            and not synthesized.get("missing_capabilities")
            and synthesized.get("policy_ok", True)
        )
        if pack_controlled:
            continue
        issues.append(
            ExecutionReadinessIssue(
                kind="missing_controlled_execution",
                message=f"Controlled execution unavailable for tool pack: {pack_name}",
                detail={
                    "pack": pack_name,
                    "status": "missing",
                    "parent_registry": controlled["parent_available"],
                    "delegated_runners": controlled["delegated"],
                    "synthesized_runner": controlled["synthesized"].get(pack_name, {}),
                },
            )
        )
        if failure_type != "missing_required_secret_access":
            failure_type = "missing_required_tool"

    available_runners = set(getattr(runner_factory, "runner_names", []) or [])
    required_tools = derive_required_tools(text, plan)
    for tool in sorted(required_tools):
        if tool == "Shell":
            continue
        if tool.startswith("runner:"):
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

    base_writable = [
        Path(root).resolve()
        for root in getattr(guardrails, "writable_roots", []) or []
    ]
    extra_writable = [
        Path(root).resolve()
        for root in sorted(approved_writable_roots or [])
    ]
    all_writable = base_writable + extra_writable
    writable_status: Dict[str, str] = {}

    for write_target in sorted(requirements.writable_roots):
        if _path_covered_by_writable_roots(write_target, all_writable):
            writable_status[write_target] = "approved"
            continue
        writable_status[write_target] = "not approved"
        guard_err = guardrails.check_write(write_target) if guardrails is not None else None
        if guard_err:
            suggestion = (
                f"Use a writable report directory under an allowed root, or approve "
                f"writable root with --approve-writable-root {write_target}"
            )
            issues.append(
                ExecutionReadinessIssue(
                    kind="writable_root",
                    message=f"Required write root: {write_target}",
                    detail={
                        "path": write_target,
                        "status": "not approved",
                        "guardrail": guard_err,
                        "writable_roots": [str(root) for root in base_writable],
                        "approved_writable_roots": [str(root) for root in extra_writable],
                        "suggestion": suggestion,
                    },
                )
            )
            failure_type = "writable_root_denied"

    issues.append(
        ExecutionReadinessIssue(
            kind="path_summary",
            message="Path readiness",
            detail={
                "read_paths": sorted(
                    root
                    for pack_name in requirements.packs
                    for root in CORE_TOOL_PACKS[pack_name].readable_roots
                ),
                "executable_paths": sorted(requirements.executable_paths),
                "writable_roots": sorted(requirements.writable_roots),
                "writable_root_status": writable_status,
                "approved_writable_roots": [str(root) for root in extra_writable],
            },
        )
    )

    for path in sorted(requirements.executable_paths):
        issues.append(
            ExecutionReadinessIssue(
                kind="required_executable",
                message=f"Required executable: {path}",
                detail={
                    "path": path,
                    "tool_pack": "ops-jenkins",
                    "status": "approved"
                    if "ops-jenkins" in enabled_packs
                    else "not approved",
                },
            )
        )

    blocking_kinds = {
        "missing_capability",
        "missing_tool_pack",
        "missing_controlled_execution",
        "missing_runner",
        "trust_boundary",
        "writable_root",
    }
    blocking = [issue for issue in issues if issue.kind in blocking_kinds]
    if blocking:
        return ExecutionReadinessResult(
            ok=False,
            failure_type=failure_type,
            issues=issues,
        )
    return ExecutionReadinessResult(ok=True, issues=issues)


def build_checkpoint(
    plan: ExecutionPlan,
    *,
    session_id: str,
    failure_type: str,
    failure_message: str,
    failed_subtask_id: Optional[str],
    goal: str,
    budget_limit: Optional[float] = None,
    spent_cost: Optional[float] = None,
) -> PlanExecuteCheckpoint:
    from .resume_control import build_resume_cli_command

    completed = [st.id for st in plan.subtasks if st.status == "completed"]
    remaining = [
        st.id
        for st in plan.subtasks
        if st.status in {"pending", "skipped", "failed", "running"}
        and st.id not in completed
    ]
    plan_path = str(plan.file_path) if plan.file_path else None
    plan_id = plan_path or session_id
    approve_caps: List[str] = []
    if failure_type in {
        "missing_required_secret_access",
        "capability_not_authorized_by_trusted_intent",
    }:
        approve_caps = ["secret"]
    resume_command = build_resume_cli_command(
        session_id,
        plan_path=plan_path,
        approve_capabilities=approve_caps or None,
        followup_hint=True,
    )

    return PlanExecuteCheckpoint(
        plan_id=plan_id,
        session_id=session_id,
        plan_path=plan_path,
        goal=goal,
        completed_subtasks=completed,
        failed_subtask_id=failed_subtask_id,
        failure_type=failure_type,
        failure_message=failure_message,
        remaining_subtasks=remaining,
        skip_reason=skip_status_for_failure(failure_type),
        resume_command=resume_command,
        continue_on_failure=getattr(plan, "continue_on_failure", False),
        capability_elevation=getattr(plan, "capability_elevation", None),
        tool_pack_approvals=list(getattr(plan, "tool_pack_approvals", []) or []),
        writable_root_approvals=list(getattr(plan, "writable_root_approvals", []) or []),
        budget_limit=budget_limit,
        spent_cost=spent_cost,
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
        if issue.kind == "capability_summary":
            lines.append("- Capabilities:")
            lines.append(
                f"  Base ceiling: {', '.join(issue.detail.get('base_ceiling', [])) or '(none)'}"
            )
            lines.append(
                "  Approved for this plan: "
                + (", ".join(issue.detail.get("approved_capabilities", [])) or "(none)")
            )
            lines.append(
                "  Effective ceiling: "
                + (", ".join(issue.detail.get("effective_ceiling", [])) or "(none)")
            )
            missing = issue.detail.get("missing_capabilities") or []
            lines.append("  Status: " + ("missing " + ", ".join(missing) if missing else "approved"))
            continue
        if issue.kind == "missing_tool_pack":
            lines.append(f"- Required tool pack: {issue.detail.get('pack', issue.message)}")
            lines.append(f"  Status: {issue.detail.get('status', 'not approved')}")
            if issue.detail.get("needs"):
                lines.append(f"  Needs: {', '.join(issue.detail['needs'])}")
            if issue.detail.get("suggested_approval"):
                lines.append(
                    f"  Suggested approval: {issue.detail['suggested_approval']}"
                )
            continue
        if issue.kind == "missing_controlled_execution":
            lines.append(f"- Tool pack: {issue.detail.get('pack', issue.message)}")
            lines.append("  Requires: controlled execution")
            lines.append("  Status: Shell/controlled runner missing")
            lines.append(
                "  Shell available in parent registry: "
                + ("yes" if issue.detail.get("parent_registry") else "no")
            )
            delegated = issue.detail.get("delegated_runners") or {}
            if delegated:
                delegated_text = ", ".join(
                    f"{name}={'yes' if available else 'no'}"
                    for name, available in sorted(delegated.items())
                )
                lines.append(f"  Shell available in delegated runners: {delegated_text}")
            synthesized = issue.detail.get("synthesized_runner") or {}
            if synthesized:
                lines.append(
                    "  Controlled tool-pack runner: "
                    + ("yes" if synthesized.get("available") else "no")
                )
                if synthesized.get("missing_capabilities"):
                    lines.append(
                        "  Missing capabilities: "
                        + ", ".join(synthesized["missing_capabilities"])
                    )
                if synthesized.get("policy_reason"):
                    lines.append(f"  Policy: {synthesized['policy_reason']}")
            continue
        if issue.kind == "writable_root":
            lines.append(f"- Required writable root: {issue.detail.get('path', issue.message)}")
            lines.append(f"  Status: {issue.detail.get('status', 'not approved')}")
            if issue.detail.get("suggestion"):
                lines.append(f"  Suggested approval: {issue.detail['suggestion']}")
            if issue.detail.get("writable_roots"):
                lines.append(
                    f"  Allowed writable roots: {', '.join(issue.detail['writable_roots'])}"
                )
            continue
        if issue.kind == "required_executable":
            lines.append(f"- Required executable: {issue.detail.get('path', '')}")
            lines.append(f"  Tool pack: {issue.detail.get('tool_pack', '-')}")
            continue
        if issue.kind == "path_summary":
            lines.append("- Paths:")
            read_paths = issue.detail.get("read_paths") or []
            executable_paths = issue.detail.get("executable_paths") or []
            writable_roots = issue.detail.get("writable_roots") or []
            lines.append(f"  Read paths: {', '.join(read_paths) or '(none)'}")
            lines.append(
                f"  Executable paths: {', '.join(executable_paths) or '(none)'}"
            )
            if writable_roots:
                status = issue.detail.get("writable_root_status") or {}
                lines.append(
                    "  Writable roots: "
                    + ", ".join(f"{root} ({status.get(root, 'unknown')})" for root in writable_roots)
                )
            else:
                lines.append("  Writable roots: (none)")
            continue
        lines.append(f"- {issue.message}")
        if issue.kind == "missing_capability" and issue.detail:
            lines.append(
                f"  Base ceiling: {', '.join(issue.detail.get('base_ceiling', []))}"
            )
            lines.append(
                "  Approved for this plan: "
                + (", ".join(issue.detail.get("approved_capabilities", [])) or "(none)")
            )
            lines.append(
                f"  Effective ceiling: {', '.join(issue.detail.get('effective_ceiling', []))}"
            )
            required = issue.detail.get("required") or []
            missing = issue.detail.get("missing_capabilities") or sorted(
                set(required) - set(issue.detail.get("effective_ceiling", []))
            )
            if missing:
                text_detail = issue.detail.get("plan_text", "")
                for cap, reason in explain_capability_gaps(
                    missing,
                    text=text_detail,
                    required_caps=set(required),
                ).items():
                    lines.append(f"  Why {cap} is needed: {reason}")
    lines.append("No subtasks executed.")
    return "\n".join(lines)


def format_readiness_success(
    *,
    approved_tool_packs: Set[str],
    approved_capabilities: Set[str],
    approved_writable_roots: Set[str],
    base_capability_ceiling: Optional[Set[str]] = None,
) -> str:
    packs = sorted(DEFAULT_ENABLED_TOOL_PACKS | set(approved_tool_packs))
    scoped_caps = tool_pack_scoped_capabilities(set(approved_tool_packs))
    plan_caps = set(approved_capabilities) | scoped_caps
    base_caps = set(base_capability_ceiling or set())
    effective_caps = base_caps | plan_caps
    lines = [
        "Execution readiness passed:",
        f"- Approved tool packs: {', '.join(packs) or '(none)'}",
        f"- Base capability ceiling: {', '.join(sorted(base_caps)) or '(none)'}",
        f"- Approved capabilities for this plan: {', '.join(sorted(plan_caps)) or '(none)'}",
        f"- Effective capability ceiling: {', '.join(sorted(effective_caps)) or '(none)'}",
        (
            "- Approved writable roots: "
            + (", ".join(sorted(approved_writable_roots)) or "(none)")
        ),
        "- Shell mode: shell_limited only"
        if any(p in approved_tool_packs for p in {"ops-jenkins", "ops-k8s", "ops-helm-deploy"})
        else "- Shell mode: none",
        "- No unrestricted shell enabled",
    ]
    return "\n".join(lines)


def format_capability_elevation_prompt(
    result: ExecutionReadinessResult,
    *,
    goal: str,
    plan: ExecutionPlan,
) -> str:
    text = _plan_text(plan, goal)
    issue = next((i for i in result.issues if i.kind == "missing_capability"), None)
    detail = issue.detail if issue else {}
    missing = sorted(
        set(detail.get("required") or []) - set(detail.get("effective_ceiling") or detail.get("allowed_ceiling") or [])
    )
    lines = [
        "Execution readiness requires capability elevation for this plan only:",
        f"  Required: {', '.join(missing)}",
        f"  Base ceiling: {', '.join(detail.get('base_ceiling', []))}",
        "  Approved for this plan: "
        + (", ".join(detail.get("approved_capabilities", [])) or "(none)"),
        f"  Effective ceiling: {', '.join(detail.get('effective_ceiling', detail.get('allowed_ceiling', [])))}",
    ]
    for cap, reason in explain_capability_gaps(
        missing,
        text=text,
        required_caps=set(detail.get("required") or []),
    ).items():
        lines.append(f"  Why {cap}: {reason}")
    lines.extend(
        [
            "Options:",
            "  [y] approve elevation for this plan/session only",
            "  [n] reject",
            "  [r] save checkpoint and exit",
            "  [e] edit plan",
        ]
    )
    return "\n".join(lines)


def format_elevation_scope(elevation: PlanCapabilityElevation) -> str:
    lines = [
        "[plan-execute] Capability elevation approved (plan/session scope only):",
        f"  Session: {elevation.session_id}",
        f"  Plan: {elevation.plan_id}",
        f"  Approved capabilities: {', '.join(elevation.approved_capabilities)}",
        f"  Base ceiling: {', '.join(elevation.base_ceiling)}",
    ]
    if elevation.approved_tools:
        lines.append(f"  Approved tools/runners: {', '.join(elevation.approved_tools)}")
    if elevation.approved_paths:
        lines.append(f"  Approved report paths: {', '.join(elevation.approved_paths)}")
    for cap, reason in elevation.rationales.items():
        lines.append(f"  Scope note ({cap}): {reason}")
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
