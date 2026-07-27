"""Minimal trust-boundary helpers for tool execution.

The first slice is intentionally small:
- classify the top-level task into trusted capability intent
- classify tool calls into capabilities
- prevent untrusted tool output from expanding capabilities
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import shlex
from typing import Any, Dict, List, Literal, Optional, Set


Capability = Literal["read", "write", "network", "secret"]
TRUSTED_INSTRUCTION = "trusted_instruction"
UNTRUSTED_DATA = "untrusted_data"

_READ_TOOL_NAMES = {"Read", "InspectFiles", "ToolProposal", "Grep", "Glob", "LS", "ReadLints", "MemorySearch"}
_WRITE_TOOL_NAMES = {"Write", "StrReplace", "InsertAfter", "Delete", "GitCheckpoint"}

_WRITE_INTENT_RE = re.compile(
    r"\b(add|append|change|edit|fix|update|patch|refactor|write|rewrite|overwrite|regenerate|create|delete|modify|implement|restore|rewire)\b",
    re.IGNORECASE,
)
_NETWORK_INTENT_RE = re.compile(
    r"\b(fetch|download|install|call\b.*api|call endpoint|network|http|https|curl|wget|probe|verify endpoint|test against)\b",
    re.IGNORECASE,
)
_SECRET_INTENT_RE = re.compile(
    r"\b(secret|credential|api[_ -]?key|token|password|env(?:ironment)? variable|secretref|secret ref|rotate key|provider key)\b",
    re.IGNORECASE,
)
_FULL_REWRITE_INTENT_RE = re.compile(
    r"\b(rewrite|replace|overwrite|regenerate)\b.{0,40}\b(entire|whole|file|from scratch)\b|"
    r"\b(entire|whole)\b.{0,40}\b(file)\b",
    re.IGNORECASE,
)

# Docs-only / bounded-write packets enumerate prohibited domains; strip those
# clauses before trusted-intent classification (BL-073 / BL-044 residual).
_PROHIBITION_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don't|never)\s+"
    r"(?:run|execute|use|call|trigger|apply|install|uninstall|upgrade|"
    r"mutate|rotate|fetch|download|write|edit|modify|patch|update|change)\b"
    r"[^.!\n]*[.!]?",
    re.IGNORECASE,
)
_NEGATED_OPERATION_RE = re.compile(
    r"\b(?:do\s+not|don't|never|without)\s+"
    r"(?:run|execute|use|call|trigger|apply|install|uninstall|upgrade|"
    r"mutate|rotate|fetch|download|write|edit|modify|patch|update|change)\b",
    re.IGNORECASE,
)
_NO_SLASH_PROHIBITION_RE = re.compile(
    r"\bno\s+[\w-]+(?:\s*/\s*[\w-]+)+",
    re.IGNORECASE,
)
_FORBIDDEN_MUTATIONS_CLAUSE_RE = re.compile(
    r"\bForbidden mutations:\s*[^.!\n]*[.!]?",
    re.IGNORECASE,
)
_INERT_DEPLOY_CONFIG_NOUN_RE = re.compile(
    r"\bdeploy(?:ment)?\s+configs?\b",
    re.IGNORECASE,
)
_INERT_DEPLOYMENT_CREDENTIALS_RE = re.compile(
    r"\bdeployment[_ -]?credentials\b",
    re.IGNORECASE,
)


def strip_prohibited_intent_framing(text: str) -> str:
    """Remove prohibition / governance framing before capability intent scans."""
    sanitized = _PROHIBITION_CLAUSE_RE.sub(" ", text or "")
    sanitized = _NEGATED_OPERATION_RE.sub(" ", sanitized)
    sanitized = _NO_SLASH_PROHIBITION_RE.sub(" ", sanitized)
    sanitized = _FORBIDDEN_MUTATIONS_CLAUSE_RE.sub(" ", sanitized)
    sanitized = _INERT_DEPLOY_CONFIG_NOUN_RE.sub(" ", sanitized)
    sanitized = _INERT_DEPLOYMENT_CREDENTIALS_RE.sub(" ", sanitized)
    return sanitized

_SHELL_WRITE_PREFIXES = (
    "mkdir",
    "touch",
    "rm ",
    "mv ",
    "cp ",
    "sed -i",
    "git add",
    "git commit",
    "kubectl apply",
    "kubectl delete",
    "helm upgrade",
    "helm install",
)
_SHELL_NETWORK_PREFIXES = (
    "curl",
    "wget",
    "pip install",
    "npm install",
    "uv pip install",
    "git fetch",
    "git pull",
    "git push",
    "docker pull",
)
_SHELL_SECRET_PREFIXES = (
    "printenv",
    "env",
    "kubectl get secret",
    "kubectl describe secret",
    "cat ~/.aws",
    "cat ~/.ssh",
)
_SECRET_TOKEN_RE = re.compile(r"(api[_-]?key|token|secret|password)", re.IGNORECASE)


@dataclass
class TrustState:
    trusted_intent_caps: Set[Capability]
    full_rewrite_authorized: bool = False
    untrusted_context_present: bool = False
    untrusted_sources: List[str] = field(default_factory=list)


@dataclass
class PolicyInput:
    run_id: str = ""
    session_id: str = ""
    source: str = "master"
    tool_name: str = ""
    tool_args: Dict[str, Any] = field(default_factory=dict)
    requested_caps: Set[Capability] = field(default_factory=set)
    trust_state: Optional[TrustState] = None
    mode: str = ""
    ecosystem: Optional[str] = None
    runtime_profile: Optional[str] = None


@dataclass
class PolicyDecision:
    allowed: bool
    reason_code: str
    message: str
    matched_rule: str


def derive_trusted_intent_caps(user_prompt: str) -> Set[Capability]:
    text = strip_prohibited_intent_framing(user_prompt or "").strip()
    caps: Set[Capability] = {"read"}
    if not text:
        return caps
    if _WRITE_INTENT_RE.search(text):
        caps.add("write")
    if _NETWORK_INTENT_RE.search(text):
        caps.add("network")
    if _SECRET_INTENT_RE.search(text):
        caps.update({"secret", "write"})
    return caps


def create_trust_state(user_prompt: str) -> TrustState:
    sanitized = strip_prohibited_intent_framing(user_prompt or "")
    return TrustState(
        trusted_intent_caps=derive_trusted_intent_caps(user_prompt),
        full_rewrite_authorized=bool(_FULL_REWRITE_INTENT_RE.search(sanitized)),
    )


def classify_tool_capabilities(tool_name: str, tool_args: Dict[str, Any]) -> Set[Capability]:
    if tool_name in _READ_TOOL_NAMES:
        return {"read"}
    if tool_name in _WRITE_TOOL_NAMES:
        return {"write"}
    if tool_name == "Delegate":
        return set()
    if tool_name == "Shell":
        return _classify_shell_capabilities(str(tool_args.get("command", "") or ""))
    # Fail closed for unknown tools: they must not gain capability silently.
    return {"write", "network", "secret"}


def record_untrusted_tool_output(
    tool_name: str,
    trust_state: Optional[TrustState],
) -> None:
    if trust_state is None:
        return
    trust_state.untrusted_context_present = True
    if tool_name not in trust_state.untrusted_sources:
        trust_state.untrusted_sources.append(tool_name)


class MinimalToolPolicyGate:
    """Minimal gate enforcing trusted-intent capability ceilings."""

    def evaluate(self, policy_input: PolicyInput) -> PolicyDecision:
        requested = set(policy_input.requested_caps or set())
        trust_state = policy_input.trust_state
        trusted = set(trust_state.trusted_intent_caps if trust_state else {"read"})

        if not requested or requested == {"read"}:
            return PolicyDecision(
                allowed=True,
                reason_code="allowed_read_only",
                message="Read-only capability stays within trusted intent.",
                matched_rule="allow_read_only",
            )

        if trust_state and trust_state.untrusted_context_present:
            if "secret" in requested and "secret" not in trusted:
                return PolicyDecision(
                    allowed=False,
                    reason_code="secret_access_from_untrusted_context",
                    message="Untrusted data cannot trigger secret access unless the trusted task explicitly authorized secret work.",
                    matched_rule="deny_untrusted_secret_access",
                )
            if "network" in requested and "network" not in trusted:
                return PolicyDecision(
                    allowed=False,
                    reason_code="network_access_from_untrusted_context",
                    message="Untrusted data cannot trigger network access unless the trusted task explicitly authorized network work.",
                    matched_rule="deny_untrusted_network_access",
                )
            if "write" in requested and "write" not in trusted:
                return PolicyDecision(
                    allowed=False,
                    reason_code="write_not_authorized_by_trusted_intent",
                    message="Untrusted data cannot expand the run into write capability the trusted task did not authorize.",
                    matched_rule="deny_untrusted_write_escalation",
                )

        if not requested.issubset(trusted):
            missing = sorted(requested - trusted)
            return PolicyDecision(
                allowed=False,
                reason_code="capability_not_authorized_by_trusted_intent",
                message=f"Requested capabilities {missing} are outside the trusted top-level task ceiling {sorted(trusted)}.",
                matched_rule="deny_capability_outside_trusted_intent",
            )

        return PolicyDecision(
            allowed=True,
            reason_code="allowed_within_trusted_intent",
            message="Requested capabilities stay within the trusted task ceiling.",
            matched_rule="allow_within_trusted_intent",
        )


def _classify_shell_capabilities(command: str) -> Set[Capability]:
    text = (command or "").strip()
    if not text:
        return {"read"}

    lowered = text.lower()
    caps: Set[Capability] = set()

    if any(lowered.startswith(prefix) or f"&& {prefix}" in lowered for prefix in _SHELL_WRITE_PREFIXES):
        caps.add("write")
    if any(lowered.startswith(prefix) or f"&& {prefix}" in lowered for prefix in _SHELL_NETWORK_PREFIXES):
        caps.add("network")
    if any(lowered.startswith(prefix) or f"&& {prefix}" in lowered for prefix in _SHELL_SECRET_PREFIXES):
        caps.add("secret")

    if _SECRET_TOKEN_RE.search(lowered) and ("printenv" in lowered or "env" in lowered):
        caps.add("secret")

    try:
        argv = shlex.split(text)
    except ValueError:
        argv = []
    if argv:
        cmd0 = argv[0].lower()
        if cmd0 in {"ls", "pwd"}:
            caps.add("read")
        elif cmd0 == "git" and len(argv) >= 2 and argv[1] in {"status", "diff", "show", "log"}:
            caps.add("read")
        elif cmd0 == "kubectl" and len(argv) >= 2 and argv[1] == "get":
            caps.add("read")
        elif cmd0 == "helm" and len(argv) >= 2 and argv[1] in {"list", "history", "get"}:
            caps.add("read")

    if not caps:
        # Unknown shell commands fail closed.
        return {"write", "network", "secret"}
    return caps
