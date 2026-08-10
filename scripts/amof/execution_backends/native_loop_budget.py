"""Progress-aware bounded Native agent loop budget (amof.native_loop_budget/v1).

Deterministic absolute ceiling always wins. Extensions require machine-observable
progress evidence — never model self-report.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA = "amof.native_loop_budget/v1"
POLICY_VERSION = "native-loop-budget-v1"

# Boundedness: base 12 preserves historical Native default; absolute 18 =
# base + (extension_increment * max_extension_count). Ceiling stays finite and
# near base so bounded execution remains meaningful.
DEFAULT_BASE_TURN_LIMIT = 12
DEFAULT_EXTENSION_INCREMENT = 3
DEFAULT_MAX_EXTENSION_COUNT = 2
DEFAULT_ABSOLUTE_TURN_LIMIT = 18

ProgressVerdict = Literal[
    "MATERIAL_PROGRESS",
    "PARTIAL_PROGRESS",
    "NO_PROGRESS",
    "UNKNOWN",
]

STOP_BASE_NO_PROGRESS = "amof_native_base_budget_no_progress"
STOP_EXTENSION_NO_PROGRESS = "amof_native_extension_budget_no_progress"
STOP_ABSOLUTE_TURN_LIMIT = "amof_native_absolute_turn_limit"
# Compatibility alias used only when absolute ceiling is hit with no newer reason.
STOP_MAX_TURNS_COMPAT = "amof_native_max_turns"

_PATH_NOISE_RE = re.compile(
    r"(?:ENOENT|no such file|not a file|not a directory|path .* outside|"
    r"package\.json|node_modules)",
    re.IGNORECASE,
)
_EXIT_CODE_RE = re.compile(r"exit_code\s*=\s*(-?\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class LoopBudgetPolicy:
    schema: str = SCHEMA
    policy_version: str = POLICY_VERSION
    base_turn_limit: int = DEFAULT_BASE_TURN_LIMIT
    extension_increment: int = DEFAULT_EXTENSION_INCREMENT
    max_extension_count: int = DEFAULT_MAX_EXTENSION_COUNT
    absolute_turn_limit: int = DEFAULT_ABSOLUTE_TURN_LIMIT

    def validate(self) -> None:
        if self.base_turn_limit < 1:
            raise ValueError("base_turn_limit must be >= 1")
        if self.extension_increment < 1:
            raise ValueError("extension_increment must be >= 1")
        if self.max_extension_count < 0:
            raise ValueError("max_extension_count must be >= 0")
        if self.absolute_turn_limit < self.base_turn_limit:
            raise ValueError("absolute_turn_limit must be >= base_turn_limit")
        max_possible = self.base_turn_limit + (
            self.extension_increment * self.max_extension_count
        )
        if self.absolute_turn_limit > max_possible:
            # Allow absolute equal to computed max; reject looser ceilings that
            # would make extensions meaningless vs absolute.
            pass
        if self.absolute_turn_limit > self.base_turn_limit * 3:
            raise ValueError(
                "absolute_turn_limit too large relative to base (boundedness)"
            )


@dataclass
class ProgressFingerprint:
    """Compact machine-observable progress state (no transcript hashing)."""

    grant_tree_digest: str = ""
    write_digest: str = ""
    shell_exit_fingerprint: str = ""
    failure_signature: str = ""
    tool_outcome_signature: str = ""
    successful_write_count: int = 0
    successful_shell_count: int = 0
    failed_shell_count: int = 0
    path_noise_count: int = 0
    phase: str = "init"

    def digest(self) -> str:
        payload = {
            "grant_tree_digest": self.grant_tree_digest,
            "write_digest": self.write_digest,
            "shell_exit_fingerprint": self.shell_exit_fingerprint,
            "failure_signature": self.failure_signature,
            "tool_outcome_signature": self.tool_outcome_signature,
            "successful_write_count": self.successful_write_count,
            "successful_shell_count": self.successful_shell_count,
            "failed_shell_count": self.failed_shell_count,
            "path_noise_count": self.path_noise_count,
            "phase": self.phase,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class ProgressEvaluation:
    verdict: ProgressVerdict
    evidence: list[str] = field(default_factory=list)
    current_digest: str = ""
    baseline_digest: str = ""


@dataclass
class ExtensionDecision:
    granted: bool
    at_turn: int
    progress_verdict: ProgressVerdict
    evidence: list[str]
    granted_turns: int
    extension_count_after: int
    absolute_limit: int
    reason: str


@dataclass
class LoopBudgetState:
    policy: LoopBudgetPolicy
    turns_used: int = 0
    effective_turn_limit: int = DEFAULT_BASE_TURN_LIMIT
    extension_count: int = 0
    extensions: list[dict[str, Any]] = field(default_factory=list)
    progress_checks: list[dict[str, Any]] = field(default_factory=list)
    denied_extensions: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: ProgressFingerprint = field(default_factory=ProgressFingerprint)
    checkpoint_fingerprint: ProgressFingerprint | None = None
    recent_state_digests: list[str] = field(default_factory=list)
    last_stop_reason: str | None = None

    def __post_init__(self) -> None:
        self.policy.validate()
        self.effective_turn_limit = self.policy.base_turn_limit

    def to_telemetry(self) -> dict[str, Any]:
        return {
            "schema": self.policy.schema,
            "policy_version": self.policy.policy_version,
            "base_turn_limit": self.policy.base_turn_limit,
            "extension_increment": self.policy.extension_increment,
            "max_extension_count": self.policy.max_extension_count,
            "absolute_turn_limit": self.policy.absolute_turn_limit,
            "turns_used": self.turns_used,
            "effective_turn_limit": self.effective_turn_limit,
            "extension_count": self.extension_count,
            "extensions_granted": list(self.extensions),
            "extensions_denied": list(self.denied_extensions),
            "progress_checks": list(self.progress_checks),
            "progress_fingerprint": asdict(self.fingerprint),
            "stop_reason": self.last_stop_reason,
        }


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _normalize_args(arguments: dict[str, Any]) -> str:
    try:
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        return str(arguments)


def _shell_exit_code(output: str) -> int | None:
    match = _EXIT_CODE_RE.search(output or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _classify_tool_failure(name: str, output: str, error: str | None = None) -> str:
    text = f"{error or ''}\n{output or ''}"
    if _PATH_NOISE_RE.search(text):
        return "path_noise"
    if name == "run_shell":
        code = _shell_exit_code(output)
        if code is None:
            return "shell_unknown"
        if code == 0:
            return "shell_ok"
        return f"shell_fail_{code}"
    if error:
        return "tool_error"
    return "ok"


def observe_tool_result(
    fingerprint: ProgressFingerprint,
    *,
    name: str,
    arguments: dict[str, Any],
    output: str,
    error: str | None = None,
    grant_paths_digest: str | None = None,
) -> ProgressFingerprint:
    """Update fingerprint from one tool observation (mutates and returns)."""
    args_key = _normalize_args(arguments if isinstance(arguments, dict) else {})
    failure_class = _classify_tool_failure(name, output, error)
    outcome_bits = f"{name}|{args_key}|{failure_class}|{(output or '')[:240]}"
    prior = fingerprint.tool_outcome_signature
    fingerprint.tool_outcome_signature = _stable_hash(f"{prior}:{outcome_bits}")

    if failure_class == "path_noise":
        fingerprint.path_noise_count += 1
        fingerprint.failure_signature = _stable_hash(
            f"{fingerprint.failure_signature}:path_noise:{name}:{args_key}"
        )
    elif failure_class.startswith("shell_fail"):
        fingerprint.failed_shell_count += 1
        fingerprint.failure_signature = _stable_hash(
            f"{fingerprint.failure_signature}:{failure_class}:{_stable_hash(args_key)}"
        )
        code = _shell_exit_code(output)
        fingerprint.shell_exit_fingerprint = _stable_hash(
            f"{fingerprint.shell_exit_fingerprint}:{code}:{_stable_hash(output[:400])}"
        )
    elif failure_class == "shell_ok":
        fingerprint.successful_shell_count += 1
        fingerprint.shell_exit_fingerprint = _stable_hash(
            f"{fingerprint.shell_exit_fingerprint}:0:{_stable_hash(output[:400])}"
        )
        fingerprint.phase = "validation_ok"
    elif name == "write_file" and not error:
        fingerprint.successful_write_count += 1
        content = str((arguments or {}).get("content") or "")
        path = str((arguments or {}).get("path") or "")
        fingerprint.write_digest = _stable_hash(
            f"{fingerprint.write_digest}:{path}:{_stable_hash(content)}"
        )
        fingerprint.phase = "implementation"
    elif error:
        fingerprint.failure_signature = _stable_hash(
            f"{fingerprint.failure_signature}:error:{name}:{args_key}"
        )

    if grant_paths_digest is not None:
        fingerprint.grant_tree_digest = grant_paths_digest
    return fingerprint


def digest_grant_tree(paths_to_content: dict[str, str]) -> str:
    items = sorted((str(k), _stable_hash(v)) for k, v in paths_to_content.items())
    raw = json.dumps(items, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def evaluate_progress(
    current: ProgressFingerprint,
    baseline: ProgressFingerprint | None,
    *,
    recent_digests: list[str] | None = None,
) -> ProgressEvaluation:
    """Compare current fingerprint to baseline. UNKNOWN never auto-extends."""
    current_digest = current.digest()
    if baseline is None:
        return ProgressEvaluation(
            verdict="UNKNOWN",
            evidence=["no_baseline_fingerprint"],
            current_digest=current_digest,
            baseline_digest="",
        )

    baseline_digest = baseline.digest()
    evidence: list[str] = []

    if recent_digests:
        # Oscillation: current state already seen recently without net advance.
        if current_digest in recent_digests[:-1]:
            return ProgressEvaluation(
                verdict="NO_PROGRESS",
                evidence=["oscillating_state_digest", current_digest],
                current_digest=current_digest,
                baseline_digest=baseline_digest,
            )

    if current_digest == baseline_digest:
        return ProgressEvaluation(
            verdict="NO_PROGRESS",
            evidence=["identical_progress_fingerprint"],
            current_digest=current_digest,
            baseline_digest=baseline_digest,
        )

    write_advanced = current.write_digest != baseline.write_digest
    grant_advanced = current.grant_tree_digest != baseline.grant_tree_digest and bool(
        current.grant_tree_digest
    )
    shell_advanced = current.shell_exit_fingerprint != baseline.shell_exit_fingerprint
    failure_evolved = current.failure_signature != baseline.failure_signature
    writes_increased = current.successful_write_count > baseline.successful_write_count
    shells_ok_increased = current.successful_shell_count > baseline.successful_shell_count
    path_noise_only = (
        current.path_noise_count > baseline.path_noise_count
        and not write_advanced
        and not grant_advanced
        and not shells_ok_increased
        and current.failed_shell_count >= baseline.failed_shell_count
    )

    if path_noise_only:
        return ProgressEvaluation(
            verdict="NO_PROGRESS",
            evidence=["path_noise_without_material_state_change"],
            current_digest=current_digest,
            baseline_digest=baseline_digest,
        )

    # Pure failure churn without writes/validation improvement is not progress.
    if failure_evolved and not write_advanced and not grant_advanced and not shells_ok_increased:
        if current.failed_shell_count > baseline.failed_shell_count or (
            current.path_noise_count > baseline.path_noise_count
        ):
            return ProgressEvaluation(
                verdict="NO_PROGRESS",
                evidence=["failure_churn_without_implementation_or_validation_gain"],
                current_digest=current_digest,
                baseline_digest=baseline_digest,
            )

    material_bits = []
    if (write_advanced or grant_advanced or writes_increased) and (
        shells_ok_increased or shell_advanced or failure_evolved
    ):
        material_bits.append("implementation_plus_validation_movement")
    if shells_ok_increased and (write_advanced or grant_advanced):
        material_bits.append("validation_improved_after_implementation")
    if current.phase == "validation_ok" and baseline.phase != "validation_ok":
        material_bits.append("reached_validation_ok_phase")

    if material_bits:
        evidence.extend(material_bits)
        if write_advanced or grant_advanced:
            evidence.append("relevant_diff_advanced")
        if shells_ok_increased or shell_advanced:
            evidence.append("test_or_shell_state_changed")
        return ProgressEvaluation(
            verdict="MATERIAL_PROGRESS",
            evidence=evidence,
            current_digest=current_digest,
            baseline_digest=baseline_digest,
        )

    if write_advanced or grant_advanced or writes_increased:
        evidence.append("implementation_state_changed")
        return ProgressEvaluation(
            verdict="PARTIAL_PROGRESS",
            evidence=evidence,
            current_digest=current_digest,
            baseline_digest=baseline_digest,
        )

    if shell_advanced and not path_noise_only:
        evidence.append("shell_result_changed")
        return ProgressEvaluation(
            verdict="PARTIAL_PROGRESS",
            evidence=evidence,
            current_digest=current_digest,
            baseline_digest=baseline_digest,
        )

    return ProgressEvaluation(
        verdict="NO_PROGRESS",
        evidence=["no_material_machine_observable_delta"],
        current_digest=current_digest,
        baseline_digest=baseline_digest,
    )


def decide_extension(
    state: LoopBudgetState,
    *,
    at_turn: int,
    require_material: bool = True,
) -> ExtensionDecision:
    """Grant a small extension only on fresh machine-observable progress."""
    baseline = state.checkpoint_fingerprint
    evaluation = evaluate_progress(
        state.fingerprint,
        baseline,
        recent_digests=state.recent_state_digests,
    )
    state.progress_checks.append(
        {
            "at_turn": at_turn,
            "verdict": evaluation.verdict,
            "evidence": list(evaluation.evidence),
            "current_digest": evaluation.current_digest,
            "baseline_digest": evaluation.baseline_digest,
        }
    )

    allowed_verdicts: set[str] = {"MATERIAL_PROGRESS"}
    if not require_material:
        allowed_verdicts.add("PARTIAL_PROGRESS")

    if evaluation.verdict not in allowed_verdicts:
        reason = f"extension_denied:{evaluation.verdict}"
        decision = ExtensionDecision(
            granted=False,
            at_turn=at_turn,
            progress_verdict=evaluation.verdict,
            evidence=list(evaluation.evidence),
            granted_turns=0,
            extension_count_after=state.extension_count,
            absolute_limit=state.policy.absolute_turn_limit,
            reason=reason,
        )
        state.denied_extensions.append(asdict(decision))
        return decision

    if state.extension_count >= state.policy.max_extension_count:
        reason = "extension_denied:max_extension_count"
        decision = ExtensionDecision(
            granted=False,
            at_turn=at_turn,
            progress_verdict=evaluation.verdict,
            evidence=list(evaluation.evidence),
            granted_turns=0,
            extension_count_after=state.extension_count,
            absolute_limit=state.policy.absolute_turn_limit,
            reason=reason,
        )
        state.denied_extensions.append(asdict(decision))
        return decision

    next_limit = min(
        state.effective_turn_limit + state.policy.extension_increment,
        state.policy.absolute_turn_limit,
    )
    granted_turns = next_limit - state.effective_turn_limit
    if granted_turns <= 0:
        reason = "extension_denied:already_at_absolute"
        decision = ExtensionDecision(
            granted=False,
            at_turn=at_turn,
            progress_verdict=evaluation.verdict,
            evidence=list(evaluation.evidence),
            granted_turns=0,
            extension_count_after=state.extension_count,
            absolute_limit=state.policy.absolute_turn_limit,
            reason=reason,
        )
        state.denied_extensions.append(asdict(decision))
        return decision

    state.effective_turn_limit = next_limit
    state.extension_count += 1
    # Fresh progress required for any subsequent extension.
    state.checkpoint_fingerprint = ProgressFingerprint(
        grant_tree_digest=state.fingerprint.grant_tree_digest,
        write_digest=state.fingerprint.write_digest,
        shell_exit_fingerprint=state.fingerprint.shell_exit_fingerprint,
        failure_signature=state.fingerprint.failure_signature,
        tool_outcome_signature=state.fingerprint.tool_outcome_signature,
        successful_write_count=state.fingerprint.successful_write_count,
        successful_shell_count=state.fingerprint.successful_shell_count,
        failed_shell_count=state.fingerprint.failed_shell_count,
        path_noise_count=state.fingerprint.path_noise_count,
        phase=state.fingerprint.phase,
    )
    reason = f"extension_granted:{evaluation.verdict}"
    decision = ExtensionDecision(
        granted=True,
        at_turn=at_turn,
        progress_verdict=evaluation.verdict,
        evidence=list(evaluation.evidence),
        granted_turns=granted_turns,
        extension_count_after=state.extension_count,
        absolute_limit=state.policy.absolute_turn_limit,
        reason=reason,
    )
    state.extensions.append(asdict(decision))
    return decision


def termination_after_denied_extension(state: LoopBudgetState) -> str:
    if state.extension_count == 0:
        return STOP_BASE_NO_PROGRESS
    if state.turns_used >= state.policy.absolute_turn_limit:
        return STOP_ABSOLUTE_TURN_LIMIT
    return STOP_EXTENSION_NO_PROGRESS


def note_turn_complete(state: LoopBudgetState, turn_number: int) -> None:
    state.turns_used = turn_number
    digest = state.fingerprint.digest()
    state.recent_state_digests.append(digest)
    # Keep a small window for oscillation detection.
    if len(state.recent_state_digests) > 6:
        state.recent_state_digests = state.recent_state_digests[-6:]
    # Establish baseline for first extension evaluation near base ceiling.
    if state.checkpoint_fingerprint is None and turn_number >= max(
        1, state.policy.base_turn_limit - 1
    ):
        state.checkpoint_fingerprint = ProgressFingerprint(
            grant_tree_digest=state.fingerprint.grant_tree_digest,
            write_digest=state.fingerprint.write_digest,
            shell_exit_fingerprint=state.fingerprint.shell_exit_fingerprint,
            failure_signature=state.fingerprint.failure_signature,
            tool_outcome_signature=state.fingerprint.tool_outcome_signature,
            successful_write_count=state.fingerprint.successful_write_count,
            successful_shell_count=state.fingerprint.successful_shell_count,
            failed_shell_count=state.fingerprint.failed_shell_count,
            path_noise_count=state.fingerprint.path_noise_count,
            phase=state.fingerprint.phase,
        )


def default_policy() -> LoopBudgetPolicy:
    return LoopBudgetPolicy()
