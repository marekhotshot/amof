"""amof.validation_closure/v1 — separate execution terminal from validation/acceptance.

Execution status (completed|failed|blocked) is not Mission acceptance.
Required packet gates without executable evidence remain NOT_RUN; completed +
not_run must yield acceptance UNVERIFIED, never PASS.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

SCHEMA_ID = "amof.validation_closure/v1"

_GATE_PASSED = "PASSED"
_GATE_FAILED = "FAILED"
_GATE_NOT_RUN = "NOT_RUN"
_GATE_BLOCKED = "BLOCKED"
_GATE_PENDING = "PENDING"
_GATE_UNKNOWN = "UNKNOWN"

_OPEN_STATES = {_GATE_NOT_RUN, _GATE_PENDING, _GATE_UNKNOWN}


def map_acceptance_to_legacy_status(acceptance_state: str) -> str:
    """Legacy validation_summary.status: failed|passed|not_run (compat only)."""
    if acceptance_state == "FAIL":
        return "failed"
    if acceptance_state == "PASS":
        return "passed"
    return "not_run"


def derive_validation_closure(
    *,
    execution_status: str,  # completed|failed|blocked
    validation_gates: list[str] | None,
    heuristic_status: str,  # passed|failed|not_run from _infer_validation_status
    structured_results: list[dict] | None = None,  # optional gate rows
    tests_executed: list[str] | None = None,
) -> dict[str, Any]:
    """Derive typed validation closure + acceptance from execution + gates.

    Packet validation_gates are required. Prose/heuristic "passed" without
    per-gate executable evidence does not elevate required gates to PASSED.
    """
    exec_status = str(execution_status or "").strip().lower() or "completed"
    heuristic = str(heuristic_status or "not_run").strip().lower()
    if heuristic not in {"passed", "failed", "not_run"}:
        heuristic = "not_run"

    gates = [str(g).strip() for g in (validation_gates or []) if str(g).strip()]
    tests = [str(t).strip() for t in (tests_executed or []) if str(t).strip()]
    notes: list[str] = []

    requirements = _build_requirements(
        gates=gates,
        heuristic=heuristic,
        structured_results=structured_results,
        tests_executed=tests,
        notes=notes,
    )

    passed_count = sum(1 for r in requirements if r["state"] == _GATE_PASSED)
    failed_count = sum(1 for r in requirements if r["state"] == _GATE_FAILED)
    blocked_count = sum(1 for r in requirements if r["state"] == _GATE_BLOCKED)
    not_run_count = sum(1 for r in requirements if r["state"] in _OPEN_STATES)
    required_count = len(requirements)

    validation_status = _aggregate_validation_status(
        required_count=required_count,
        passed_count=passed_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
        not_run_count=not_run_count,
        heuristic=heuristic,
    )
    acceptance_state = _derive_acceptance_state(
        execution_status=exec_status,
        heuristic=heuristic,
        required_count=required_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
        not_run_count=not_run_count,
        passed_count=passed_count,
    )

    if required_count > 0 and heuristic == "passed" and not_run_count > 0:
        notes.append(
            "heuristic passed without per-gate executable evidence; "
            "required gates remain NOT_RUN (prose is not acceptance)."
        )
    if required_count == 0 and heuristic == "not_run":
        notes.append(
            "no required validation gates; heuristic not_run → acceptance UNVERIFIED."
        )
    if exec_status in {"failed", "blocked"} and acceptance_state == "UNVERIFIED":
        notes.append(
            f"execution_status={exec_status}; validation not proven → UNVERIFIED."
        )

    return {
        "schema": SCHEMA_ID,
        "execution_status": exec_status,
        "validation_status": validation_status,
        "acceptance_state": acceptance_state,
        "required_count": required_count,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "not_run_count": not_run_count,
        "blocked_count": blocked_count,
        "requirements": requirements,
        "heuristic_status": heuristic,
        "acceptance_evidence_refs": [],
        "notes": " ".join(notes).strip(),
    }


def attach_post_verify(
    closure: dict[str, Any],
    *,
    state: str,
    evidence_refs: list[str] | None = None,
    verified_by: str | None = None,
    verified_at: str | None = None,
    verification_mode: str | None = None,
) -> dict[str, Any]:
    """Return a new closure with post-verify provenance; never mutates inputs.

    Historical execution fields remain caller-owned. Elevates open required
    gates to ``state`` when that state is terminal (PASSED/FAILED/BLOCKED).
    """
    out = deepcopy(closure if isinstance(closure, dict) else {})
    target = str(state or "").strip().upper() or _GATE_NOT_RUN
    refs = [str(r).strip() for r in (evidence_refs or []) if str(r).strip()]

    requirements = list(out.get("requirements") or [])
    updated: list[dict[str, Any]] = []
    for req in requirements:
        row = dict(req) if isinstance(req, dict) else {}
        prior = str(row.get("state") or _GATE_UNKNOWN).upper()
        if prior in _OPEN_STATES and target in {
            _GATE_PASSED,
            _GATE_FAILED,
            _GATE_BLOCKED,
        }:
            row["state"] = target
            prior_refs = [
                str(r).strip()
                for r in (row.get("evidence_refs") or [])
                if str(r).strip()
            ]
            row["evidence_refs"] = list(dict.fromkeys([*prior_refs, *refs]))
        updated.append(row)

    out["requirements"] = updated
    passed_count = sum(1 for r in updated if r.get("state") == _GATE_PASSED)
    failed_count = sum(1 for r in updated if r.get("state") == _GATE_FAILED)
    blocked_count = sum(1 for r in updated if r.get("state") == _GATE_BLOCKED)
    not_run_count = sum(
        1 for r in updated if str(r.get("state") or "").upper() in _OPEN_STATES
    )
    required_count = len(updated)
    heuristic = str(out.get("heuristic_status") or "not_run").lower()
    exec_status = str(out.get("execution_status") or "completed").lower()

    out["passed_count"] = passed_count
    out["failed_count"] = failed_count
    out["blocked_count"] = blocked_count
    out["not_run_count"] = not_run_count
    out["required_count"] = required_count
    out["validation_status"] = _aggregate_validation_status(
        required_count=required_count,
        passed_count=passed_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
        not_run_count=not_run_count,
        heuristic=heuristic,
    )
    out["acceptance_state"] = _derive_acceptance_state(
        execution_status=exec_status,
        heuristic=heuristic,
        required_count=required_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
        not_run_count=not_run_count,
        passed_count=passed_count,
    )
    prior_refs = [
        str(r).strip()
        for r in (out.get("acceptance_evidence_refs") or [])
        if str(r).strip()
    ]
    out["acceptance_evidence_refs"] = list(dict.fromkeys([*prior_refs, *refs]))
    provenance = {
        "verified_by": verified_by,
        "verified_at": verified_at,
        "verification_mode": verification_mode,
        "post_verify_state": target,
        "evidence_refs": refs,
    }
    out["post_verify"] = provenance
    note = str(out.get("notes") or "").strip()
    suffix = (
        f"post_verify applied state={target} "
        f"by={verified_by or 'unspecified'} mode={verification_mode or 'unspecified'}."
    )
    out["notes"] = f"{note} {suffix}".strip()
    out["schema"] = SCHEMA_ID
    return out


def build_validation_summary(
    closure: dict[str, Any],
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Envelope fragment for agent-run-result.validation_summary."""
    acceptance = str(closure.get("acceptance_state") or "UNVERIFIED")
    legacy = map_acceptance_to_legacy_status(acceptance)
    default_reason = str(closure.get("notes") or "").strip() or (
        f"acceptance_state={acceptance}; validation_status="
        f"{closure.get('validation_status')}"
    )
    return {
        "status": legacy,
        "reason": reason or default_reason,
        "acceptance_state": acceptance,
        "closure": closure,
    }


def _normalize_gate_state(raw: Any) -> str:
    text = str(raw or "").strip().upper().replace("-", "_")
    aliases = {
        "PASS": _GATE_PASSED,
        "PASSED": _GATE_PASSED,
        "OK": _GATE_PASSED,
        "FAIL": _GATE_FAILED,
        "FAILED": _GATE_FAILED,
        "NOT_RUN": _GATE_NOT_RUN,
        "NOTRUN": _GATE_NOT_RUN,
        "BLOCKED": _GATE_BLOCKED,
        "PENDING": _GATE_PENDING,
        "UNKNOWN": _GATE_UNKNOWN,
    }
    return aliases.get(text, _GATE_UNKNOWN)


def _build_requirements(
    *,
    gates: list[str],
    heuristic: str,
    structured_results: list[dict] | None,
    tests_executed: list[str],
    notes: list[str],
) -> list[dict[str, Any]]:
    structured_by_id: dict[str, dict[str, Any]] = {}
    if structured_results:
        for item in structured_results:
            if not isinstance(item, dict):
                continue
            vid = str(
                item.get("validation_id") or item.get("gate") or item.get("id") or ""
            ).strip()
            if not vid:
                continue
            structured_by_id[vid] = item

    if not gates:
        return []

    # Structured rows are authoritative when present for a gate id.
    if structured_by_id:
        requirements: list[dict[str, Any]] = []
        for gate in gates:
            row = structured_by_id.get(gate)
            if row is None:
                requirements.append(
                    {
                        "validation_id": gate,
                        "required": True,
                        "state": _GATE_NOT_RUN,
                        "evidence_refs": [],
                    }
                )
                continue
            state = _normalize_gate_state(row.get("state"))
            refs = [
                str(r).strip()
                for r in (row.get("evidence_refs") or [])
                if str(r).strip()
            ]
            required = row.get("required")
            requirements.append(
                {
                    "validation_id": gate,
                    "required": True if required is None else bool(required),
                    "state": state,
                    "evidence_refs": refs,
                }
            )
        return requirements

    # No structured evidence: heuristic failure can mark required FAILED
    # (cannot attribute per-gate → all required FAILED). Heuristic "passed"
    # or tests_executed alone never elevates named gates to PASSED.
    if heuristic == "failed":
        notes.append(
            "heuristic failed without structured per-gate results; "
            "marking all required gates FAILED."
        )
        return [
            {
                "validation_id": gate,
                "required": True,
                "state": _GATE_FAILED,
                "evidence_refs": [],
            }
            for gate in gates
        ]

    if tests_executed:
        notes.append(
            "tests_executed present but not attributable to named gates without "
            "structured_results; required gates remain NOT_RUN."
        )
    return [
        {
            "validation_id": gate,
            "required": True,
            "state": _GATE_NOT_RUN,
            "evidence_refs": [],
        }
        for gate in gates
    ]


def _aggregate_validation_status(
    *,
    required_count: int,
    passed_count: int,
    failed_count: int,
    blocked_count: int,
    not_run_count: int,
    heuristic: str,
) -> str:
    if required_count == 0:
        if heuristic == "failed":
            return "FAILED"
        if heuristic == "passed":
            return "PASSED"
        return "NOT_REQUIRED"
    if failed_count > 0:
        return "FAILED"
    if blocked_count > 0:
        return "BLOCKED"
    if passed_count == required_count:
        return "PASSED"
    if passed_count > 0 and not_run_count > 0:
        return "PARTIAL"
    return "NOT_RUN"


def _derive_acceptance_state(
    *,
    execution_status: str,
    heuristic: str,
    required_count: int,
    failed_count: int,
    blocked_count: int,
    not_run_count: int,
    passed_count: int,
) -> str:
    if required_count == 0:
        if heuristic == "failed":
            return "FAIL"
        if heuristic == "passed":
            return "PASS"
        # CRITICAL: completed + not_run with no gates → UNVERIFIED, never PASS
        return "UNVERIFIED"

    if failed_count > 0:
        return "FAIL"
    if blocked_count > 0:
        return "BLOCKED"
    if not_run_count > 0:
        # Execution failed/blocked with validation not run stays UNVERIFIED
        # unless heuristic already failed (handled above via failed_count).
        return "UNVERIFIED"
    if passed_count == required_count:
        return "PASS"
    return "UNVERIFIED"
