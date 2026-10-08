"""Public carrying contract for a runtime-owned executable acceptance receipt.

The trusted handoff parent supplies the sealed definition and durable runtime
receipt separately from the backend result. No command is executed here.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .execution_backends.validation_closure import build_validation_summary, derive_validation_closure

DEFINITION_SCHEMA = "amof.acceptance_checks/v1"
OBSERVATION_SCHEMA = "amof.acceptance_observation/v1"
COMMAND = ["git", "rev-parse", "HEAD"]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sealed_check(definition: Any) -> dict[str, Any] | None:
    if not isinstance(definition, dict) or set(definition) != {"schema", "checks"}:
        return None
    if definition["schema"] != DEFINITION_SCHEMA or not isinstance(definition["checks"], list) or len(definition["checks"]) != 1:
        return None
    check = definition["checks"][0]
    if not isinstance(check, dict) or set(check) != {"id", "kind", "command", "cwd", "mutability", "timeout_seconds", "expected", "target_id"}:
        return None
    expected = check["expected"]
    if (not isinstance(check["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", check["id"])
            or check["kind"] != "command" or check["command"] != COMMAND
            or check["cwd"] != "repository" or check["mutability"] != "read_only"
            or type(check["timeout_seconds"]) is not int or not 1 <= check["timeout_seconds"] <= 20
            or not isinstance(check["target_id"], str) or not check["target_id"]
            or not isinstance(expected, dict) or set(expected) != {"exit_code", "stdout_equals"}
            or expected["exit_code"] != 0 or not isinstance(expected["stdout_equals"], str)
            or len(expected["stdout_equals"]) != 40
            or any(c not in "0123456789abcdef" for c in expected["stdout_equals"])
            or not check["target_id"].endswith(":" + expected["stdout_equals"])):
        return None
    return check


def project_runtime_acceptance(
    backend_result: dict[str, Any], *, sealed_definition: dict[str, Any],
    receipt_path: Path | None,
) -> dict[str, Any]:
    """Derive canonical acceptance from a trusted-parent receipt, never backend fields."""
    result = dict(backend_result)
    for field in ("acceptance_observation", "tests_executed", "structured_results", "validation", "delivery_eligible"):
        result.pop(field, None)
    check = _sealed_check(sealed_definition)
    observation: dict[str, Any] | None = None
    receipt_sha256: str | None = None
    reason = "runtime acceptance observation missing"
    if check is not None and receipt_path is not None and receipt_path.is_file():
        raw = receipt_path.read_bytes()
        if len(raw) <= 16384:
            try:
                candidate = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                candidate = None
            if (isinstance(candidate, dict) and candidate.get("schema") == OBSERVATION_SCHEMA
                    and candidate.get("check_id") == check["id"]
                    and candidate.get("definition_sha256") == _digest(sealed_definition)
                    and candidate.get("command") == COMMAND
                    and candidate.get("target_id") == check["target_id"]
                    and candidate.get("expected") == check["expected"]
                    and type(candidate.get("duration_ms")) is int
                    and isinstance(candidate.get("stdout"), str)
                    and isinstance(candidate.get("stderr"), str)):
                observation = candidate
                receipt_sha256 = hashlib.sha256(raw).hexdigest()
    state = "UNVERIFIED"
    executed = False
    if observation is not None:
        code = observation.get("exit_code")
        executed = type(code) is int or (code is None and observation.get("reason") == "timeout")
        if executed:
            state = "PASS" if code == 0 and observation["stdout"].strip() == check["expected"]["stdout_equals"] else "FAIL"
            reason = "runtime command observed" if state == "PASS" else "observed command failed or output mismatched"
    test = f"acceptance:{check['id']}" if check and executed else None
    evidence_ref = str(receipt_path) if observation is not None and receipt_path is not None else None
    closure = derive_validation_closure(
        execution_status=str(result.get("status") or "blocked"),
        validation_gates=[check["id"] if check else "acceptance_check"],
        heuristic_status="not_run",
        structured_results=[{
            "validation_id": check["id"] if check else "acceptance_check",
            "required": True,
            "state": "PASSED" if state == "PASS" else "FAILED" if state == "FAIL" else "NOT_RUN",
            "evidence_refs": [evidence_ref] if evidence_ref else [],
        }],
        tests_executed=[test] if test else [],
    )
    summary = build_validation_summary(closure, reason=reason)
    result["validation_summary"] = summary
    result["tests_executed"] = [test] if test else []
    result["acceptance_observation"] = ({
        "schema": OBSERVATION_SCHEMA, "check_id": check["id"],
        "definition_sha256": _digest(sealed_definition), "command": COMMAND,
        "shell": False, "target_id": check["target_id"],
        "exit_code": observation["exit_code"],
        "stdout": observation["stdout"][:4096], "stderr": observation["stderr"][:4096],
        "duration_ms": observation["duration_ms"], "status": state,
        "evidence_ref": evidence_ref, "evidence_sha256": receipt_sha256,
    } if observation is not None else None)
    return result


def authoritative_acceptance_state(result: dict[str, Any], *, evidence_root: Path) -> str:
    """Recheck persisted evidence before a public consumer uses PASS."""
    observation = result.get("acceptance_observation")
    if not isinstance(observation, dict) or observation.get("schema") != OBSERVATION_SCHEMA:
        return "UNVERIFIED"
    path = Path(str(observation.get("evidence_ref") or ""))
    expected_hash = observation.get("evidence_sha256")
    if path.resolve(strict=False).parent != evidence_root.resolve(strict=False):
        return "UNVERIFIED"
    if not path.is_file() or not isinstance(expected_hash, str):
        return "UNVERIFIED"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        return "UNVERIFIED"
    try:
        receipt = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "UNVERIFIED"
    if (receipt.get("schema") != OBSERVATION_SCHEMA
            or receipt.get("check_id") != observation.get("check_id")
            or receipt.get("definition_sha256") != observation.get("definition_sha256")
            or receipt.get("command") != COMMAND
            or receipt.get("target_id") != observation.get("target_id")
            or receipt.get("exit_code") != observation.get("exit_code")
            or receipt.get("stdout") != observation.get("stdout")
            or observation.get("shell") is not False):
        return "UNVERIFIED"
    state = "PASS" if receipt.get("exit_code") == 0 and str(receipt.get("stdout") or "").strip() == str(receipt.get("expected", {}).get("stdout_equals") or "") else "FAIL"
    if observation.get("status") != state:
        return "UNVERIFIED"
    if result.get("tests_executed") != [f"acceptance:{observation['check_id']}"]:
        return "UNVERIFIED"
    return state if result.get("validation_summary", {}).get("acceptance_state") == state else "UNVERIFIED"
