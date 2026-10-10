"""Durable sequential campaign gate over existing AMOF handoff results.

Slice planning and backend execution stay with the existing Mission/handoff
path. This module owns only bounds, result inspection and continuation truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import fcntl
import re
from pathlib import Path
from typing import Any, Callable

from .canonical_acceptance import authoritative_acceptance_state
from .commands import handoff
from .write_scope_approvals import load_approval, is_approval_active, validate_approval_body
from .write_scope_bindings import load_binding
from .write_scope_enforcement import load_receipt

SCHEMA = "amof.campaign/v1"
TERMINAL = {"DONE", "BLOCKED", "ESCALATION_REQUIRED"}
BOUND_DISPATCH_FLAG = "AMOF_CAMPAIGN_MISSION_RUN_V2"


def next_hermes_decoupling_slice(state: dict[str, Any]) -> dict[str, Any] | None:
    """AMOF-owned bounded verification plan for the first campaign objective."""
    index = len(state["completed_slices"])
    if index >= len(state["slice_plan"]):
        return None
    return dict(state["slice_plan"][index])


def _save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(state, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def create_campaign(
    path: Path, *, campaign_id: str, objective: str, allowed_backends: list[str],
    allowed_scope_tags: list[str], slice_plan: list[dict[str, Any]],
    max_slices: int, max_no_progress: int = 0,
    allowed_capabilities: list[str] | None = None,
    allowed_target_id: str | None = None,
    allowed_write_roots: list[str] | None = None,
    mission_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a bounded campaign envelope; no write authority is minted."""
    if path.exists() or not campaign_id or not objective.strip() or not allowed_backends or not allowed_scope_tags:
        raise ValueError("campaign identity, objective and authority envelope are required")
    if not 1 <= max_slices <= 20 or not 0 <= max_no_progress <= 2 or not 1 <= len(slice_plan) <= max_slices:
        raise ValueError("campaign budget is outside bounded limits")
    capabilities = allowed_capabilities if allowed_capabilities is not None else ["read"]
    if capabilities not in (["read"], ["read", "bounded_write"]):
        raise ValueError("unsupported campaign capabilities")
    roots = list(allowed_write_roots or [])
    if capabilities == ["read", "bounded_write"]:
        if not allowed_target_id or not roots or any(not _scope_path(root) for root in roots):
            raise ValueError("writable campaign requires an exact target and bounded roots")
    elif allowed_target_id or roots:
        raise ValueError("read-only campaign cannot carry write scope")
    state = {
        "schema": SCHEMA, "campaign_id": campaign_id, "objective": objective.strip(),
        "authority": {"allowed_backends": list(dict.fromkeys(allowed_backends)),
                      "allowed_scope_tags": list(dict.fromkeys(allowed_scope_tags)),
                      "allowed_capabilities": capabilities,
                      **({"allowed_target_id": allowed_target_id,
                          "allowed_write_roots": roots} if roots else {}),
                      "max_slices": max_slices,
                      "max_no_progress": max_no_progress},
        "slice_plan": slice_plan,
        "status": "CONTINUE", "reason": "created", "current_slice": None,
        "completed_slices": [], "evidence_refs": [], "progress_fingerprints": [],
        "no_progress_count": 0,
    }
    if mission_binding is not None:
        if os.environ.get(BOUND_DISPATCH_FLAG, "").strip().lower() not in {"1", "true", "yes", "on"}:
            raise ValueError("bound campaign dispatch is disabled")
        base = dict(mission_binding)
        if "campaign_id" in base or "slice_id" in base:
            raise ValueError("campaign binding identity is derived from the campaign plan")
        journal = {}
        for item in slice_plan:
            binding = {**base, "campaign_id": campaign_id, "slice_id": item["slice_id"]}
            handoff_id, key, fingerprint = handoff.campaign_dispatch_identity(binding)
            if roots and binding["target_id"] != allowed_target_id:
                raise ValueError("campaign binding target differs from write authority envelope")
            criterion = item.get("mission_acceptance")
            if isinstance(criterion, dict) and criterion.get("target_id") != binding["target_id"]:
                raise ValueError("campaign binding target differs from slice acceptance target")
            journal[item["slice_id"]] = {
                "handoff_id": handoff_id, "idempotency_key": key,
                "target_fingerprint": fingerprint, "state": "NOT_DISPATCHED",
                "prepared_handoff_ref": None, "accepted_dispatch_ref": None,
                "run_id": None, "terminal_decision": None,
            }
        state["mission_binding"] = base
        state["dispatch_journal"] = journal
    _save(path, state)
    return state


def create_hermes_decoupling_campaign(path: Path, *, campaign_id: str, objective: str) -> dict[str, Any]:
    """Turn one high-level objective into two fixed, read-only proof slices."""
    plan = []
    for index, (scope, goal, backend, validation) in enumerate((
        ("native-independence", "Verify Native executes without importing Hermes implementation", "amof_native", "native-import-proof"),
        ("explicit-hermes", "Verify explicit Hermes dispatch and honest backend attribution", "hermes_opensandbox", "hermes-dispatch-proof"),
    ), start=1):
        plan.append({
            "slice_id": f"{campaign_id}-slice-{index}", "parent_campaign_id": campaign_id,
            "scope_tag": scope, "requested_backend": backend,
            "requested_capabilities": ["read"], "objective": goal,
            "expected_validation": validation,
        })
    return create_campaign(
        path, campaign_id=campaign_id, objective=objective,
        allowed_backends=["amof_native", "hermes_opensandbox"],
        allowed_scope_tags=["native-independence", "explicit-hermes"],
        slice_plan=plan, max_slices=2,
    )


def create_cloud_hermes_continuation_campaign(
    path: Path, *, campaign_id: str, target_id: str | None = None,
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Fixed read-only cloud proof; distinct from Native/Hermes decoupling."""
    objectives = (
        ("pinned-checkout", "Verify that the pinned repository HEAD equals the authorized commit."),
        ("stable-head", "Verify again that the pinned repository HEAD equals the authorized commit."),
    )
    plan = [{
        "slice_id": f"{campaign_id}-slice-{index}",
        "parent_campaign_id": campaign_id,
        "scope_tag": scope,
        "requested_backend": "hermes_opensandbox",
        "requested_capabilities": ["read"],
        "objective": goal,
        "expected_validation": "repo-head",
        **({"mission_acceptance": {
            "check_id": "repo-head", "target_id": target_id,
            "command": ["git", "rev-parse", "HEAD"],
            "expected_stdout": expected_head,
        }} if target_id and expected_head else {}),
    } for index, (scope, goal) in enumerate(objectives, start=1)]
    return create_campaign(
        path, campaign_id=campaign_id,
        objective="Prove two governed read-only cloud slices continue on authoritative runtime acceptance.",
        allowed_backends=["hermes_opensandbox"],
        allowed_scope_tags=[scope for scope, _ in objectives],
        slice_plan=plan, max_slices=2,
    )


def create_cloud_native_continuation_campaign(path: Path, *, campaign_id: str) -> dict[str, Any]:
    """Fixed read-only Native cloud proof; uses the unchanged campaign gate."""
    objectives = (
        ("pinned-checkout", "Verify Native inspects the pinned repository checkout read-only."),
        ("stable-head", "Verify Native observes the same pinned repository HEAD in a second governed slice."),
    )
    plan = [{
        "slice_id": f"{campaign_id}-slice-{index}",
        "parent_campaign_id": campaign_id,
        "scope_tag": scope,
        "requested_backend": "amof_native",
        "requested_capabilities": ["read"],
        "objective": goal,
        "expected_validation": "repo-head",
    } for index, (scope, goal) in enumerate(objectives, start=1)]
    return create_campaign(
        path, campaign_id=campaign_id,
        objective="Prove two governed read-only Native cloud slices continue on authoritative runtime acceptance.",
        allowed_backends=["amof_native"],
        allowed_scope_tags=[scope for scope, _ in objectives],
        slice_plan=plan, max_slices=2,
    )


def load_campaign(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != SCHEMA:
        raise ValueError("unsupported campaign schema")
    return state


def _scope_path(value: Any) -> str | None:
    """Conservative repository-relative path for campaign bounds."""
    if (not isinstance(value, str) or not value or value.startswith("/") or
            "\\" in value or "\x00" in value or any(char in value for char in "*?")):
        return None
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return value


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _write_request(slice_record: dict[str, Any]) -> bool:
    return slice_record.get("requested_capabilities") == ["read", "bounded_write"]


def _check_write_approval(state: dict[str, Any], slice_record: dict[str, Any]) -> str | None:
    """Pre-dispatch read of existing authority; handoff still owns Binding creation."""
    approval_id = slice_record.get("write_authority_ref")
    scope = slice_record.get("requested_write_scope")
    if not isinstance(approval_id, str) or not approval_id.startswith("wsa-") or not isinstance(scope, dict):
        return "write_authority_missing"
    roots = scope.get("roots")
    target = scope.get("target_id")
    base_sha = scope.get("base_sha")
    parent = state["authority"]
    if (not isinstance(roots, list) or not roots or
            any(not _scope_path(root) for root in roots) or
            target != parent.get("allowed_target_id") or
            not isinstance(base_sha, str) or re.fullmatch(r"[0-9a-f]{40}", base_sha) is None or
            any(not any(_within(root, allowed) for allowed in parent["allowed_write_roots"]) for root in roots)):
        return "write_scope_outside_campaign"
    try:
        approval = load_approval(approval_id)
        body = validate_approval_body(approval["body"])
        if not is_approval_active(approval):
            return "write_approval_inactive"
    except (OSError, ValueError):
        return "write_approval_invalid"
    if body["target_id"] != target or body["base_sha"] != base_sha:
        return "write_approval_target_mismatch"
    # Handoff binds the Approval's full roots. Require them to equal the slice
    # request so the worker never receives a broader grant than this plan.
    if (sorted(body["allowed_roots"]) != sorted(roots) or
            any(_within(root, denied) or _within(denied, root)
                for root in roots for denied in body["denied_roots"])):
        return "write_scope_outside_approval"
    return None


def _check_write_result(slice_record: dict[str, Any], result: dict[str, Any], handoff_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """Re-read the runtime-owned Binding and MutationReceipt before continuation."""
    embedded = result.get("mutation_receipt")
    if not isinstance(embedded, dict):
        return "mutation_receipt_missing", None
    try:
        binding = load_binding(str(result.get("write_scope_binding_id") or ""))
        receipt = load_receipt(str(embedded.get("receipt_id") or ""))
    except (OSError, ValueError):
        return "mutation_receipt_invalid", None
    scope = slice_record["requested_write_scope"]
    workspace = Path(str(binding.get("workspace_root") or ""))
    bound_roots = binding.get("writable_roots")
    expected_roots = [str((workspace / root).resolve()) for root in scope["roots"]]
    if (binding.get("approval_id") != slice_record["write_authority_ref"] or
            binding.get("run_id") != handoff_id or
            binding.get("target_id") != scope["target_id"] or
            binding.get("base_sha") != scope["base_sha"] or
            not workspace.is_absolute() or
            not isinstance(bound_roots, list) or
            sorted(bound_roots) != sorted(expected_roots) or
            binding.get("status") != "completed" or
            result.get("write_scope_approval_id") != binding["approval_id"] or
            receipt != embedded or receipt.get("binding_id") != binding["binding_id"] or
            receipt.get("run_id") != handoff_id or
            receipt.get("target_id") != scope["target_id"] or
            receipt.get("compliance") != "within_scope" or
            receipt.get("approval_status") != "consumed" or
            receipt.get("binding_status") != "completed" or
            receipt.get("out_of_scope_paths") or
            not receipt.get("changed_paths") or
            sorted(receipt["changed_paths"]) != sorted(result.get("changed_paths") or []) or
            any(not any(_within(path, root) for root in scope["roots"])
                for path in receipt["changed_paths"])):
        return "mutation_scope_not_verified", None
    return None, receipt


def _candidate(state: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    authority = state["authority"]
    number = len(state["completed_slices"]) + 1
    expected_id = f"{state['campaign_id']}-slice-{number}"
    if proposal.get("slice_id") != expected_id or proposal.get("parent_campaign_id") != state["campaign_id"]:
        raise ValueError("slice identity is outside parent campaign")
    if proposal.get("scope_tag") not in authority["allowed_scope_tags"]:
        raise ValueError("next slice exceeds parent scope")
    if proposal.get("requested_backend") not in authority["allowed_backends"]:
        raise ValueError("next slice requests an unauthorized backend")
    capabilities = proposal.get("requested_capabilities")
    if capabilities not in (["read"], ["read", "bounded_write"]) or (
            _write_request(proposal) and "bounded_write" not in authority["allowed_capabilities"]):
        raise ValueError("next slice requires authority expansion")
    if proposal.get("write_scope_approval") or proposal.get("writable_roots"):
        raise ValueError("next slice cannot inject raw write authority")
    if _write_request(proposal):
        if not authority.get("allowed_write_roots"):
            raise ValueError("next slice requires authority expansion")
    elif proposal.get("write_authority_ref") or proposal.get("requested_write_scope"):
        raise ValueError("read-only slice cannot carry write authority")
    if not str(proposal.get("objective") or "").strip() or not str(proposal.get("expected_validation") or "").strip():
        raise ValueError("next slice lacks objective or expected validation")
    normalized = {key: proposal[key] for key in (
        "slice_id", "parent_campaign_id", "scope_tag", "requested_backend",
        "requested_capabilities", "objective", "expected_validation",
    )}
    if "mission_acceptance" in proposal:
        normalized["mission_acceptance"] = proposal["mission_acceptance"]
    if _write_request(proposal):
        normalized["write_authority_ref"] = proposal.get("write_authority_ref")
        normalized["requested_write_scope"] = proposal.get("requested_write_scope")
    if normalized != state["slice_plan"][number - 1]:
        raise ValueError("next slice differs from AMOF-authorized plan")
    return normalized


def _mission_acceptance_pass(slice_record: dict[str, Any], result: dict[str, Any]) -> bool:
    """Bind the trusted runtime observation to this slice's explicit objective check.

    The runtime receipt establishes execution integrity independently. A slice
    without this separate, pre-authorized objective criterion cannot continue.
    """
    criterion = slice_record.get("mission_acceptance")
    observation = result.get("acceptance_observation")
    if not isinstance(criterion, dict) or not isinstance(observation, dict):
        return False
    expected = criterion.get("expected_stdout")
    target = criterion.get("target_id")
    if (set(criterion) != {"check_id", "target_id", "command", "expected_stdout"}
            or criterion.get("check_id") != slice_record.get("expected_validation")
            or criterion.get("command") != ["git", "rev-parse", "HEAD"]
            or not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{40}", expected) is None
            or not isinstance(target, str) or not target.endswith(":" + expected)):
        return False
    return (observation.get("check_id") == criterion["check_id"]
            and observation.get("command") == criterion["command"]
            and observation.get("target_id") == target
            and observation.get("exit_code") == 0
            and str(observation.get("stdout") or "").strip() == expected
            and observation.get("status") == "PASS")


def _bound_dispatch(
    path: Path, state: dict[str, Any], current: dict[str, Any], *,
    prepare_handoff: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]] | None,
    dispatch_prepared: Callable[[dict[str, Any], str, str], str] | None,
    load_status: Callable[[str], dict[str, Any]],
) -> str:
    """Persist each intent before its side effect and never replay an uncertain one."""
    if prepare_handoff is None or dispatch_prepared is None:
        raise ValueError("bound campaign requires prepare and dispatch callbacks")
    binding = {**state["mission_binding"], "campaign_id": state["campaign_id"],
               "slice_id": current["slice_id"]}
    handoff_id, key, fingerprint = handoff.campaign_dispatch_identity(binding)
    journal = state["dispatch_journal"][current["slice_id"]]
    if (journal["handoff_id"] != handoff_id or journal["idempotency_key"] != key or
            journal["target_fingerprint"] != fingerprint):
        raise ValueError("campaign dispatch journal identity mismatch")
    if journal["state"] in {"NOT_DISPATCHED", "PREPARE_INTENT", "PREPARED"} and _write_request(current):
        write_block = _check_write_approval(state, current)
        if write_block:
            raise ValueError(write_block)
    if journal["state"] == "NOT_DISPATCHED":
        journal["state"] = "PREPARE_INTENT"
        _save(path, state)
    if journal["state"] == "PREPARE_INTENT":
        prepared = prepare_handoff(current, binding)
        if prepared.get("handoff_id") != handoff_id or prepared.get("idempotency_key") != key:
            raise ValueError("prepared handoff identity mismatch")
        _, packet = handoff._load_prepared_packet(handoff_id)
        if packet.campaign_binding != {**binding, "idempotency_key": key,
                                       "target_fingerprint": fingerprint}:
            raise ValueError("prepared handoff binding mismatch")
        journal["prepared_handoff_ref"] = str(prepared.get("packet_path") or "")
        journal["state"] = "PREPARED"
        _save(path, state)
    if journal["state"] in {"PREPARED", "DISPATCH_INTENT", "ACCEPTED"}:
        _, packet = handoff._load_prepared_packet(handoff_id)
        if packet.campaign_binding != {**binding, "idempotency_key": key,
                                       "target_fingerprint": fingerprint}:
            raise ValueError("prepared handoff binding mismatch")
    if journal["state"] == "PREPARED":
        journal["state"] = "DISPATCH_INTENT"
        _save(path, state)
        accepted_ref = dispatch_prepared(current, handoff_id, key)
        if not isinstance(accepted_ref, str) or not accepted_ref.strip():
            raise ValueError("dispatch acceptance reference missing")
        journal["accepted_dispatch_ref"] = accepted_ref
        journal["state"] = "ACCEPTED"
        _save(path, state)
    if journal["state"] == "DISPATCH_INTENT":
        authoritative = load_status(handoff_id)
        if authoritative.get("accepted") is not True:
            raise ValueError("UNCERTAIN: dispatch intent has no authoritative acceptance")
        journal["accepted_dispatch_ref"] = str(authoritative.get("tracking_ref") or handoff_id)
        journal["state"] = "ACCEPTED"
        _save(path, state)
    if journal["state"] != "ACCEPTED":
        raise ValueError("campaign dispatch state cannot be reconciled")
    return handoff_id


def _advance_campaign_unlocked(
    path: Path, *, propose_next: Callable[[dict[str, Any]], dict[str, Any] | None],
    dispatch_handoff: Callable[[dict[str, Any]], str],
    observe_progress: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]],
    load_status: Callable[[str], dict[str, Any]] = handoff._handoff_status_payload,
    prepare_handoff: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]] | None = None,
    dispatch_prepared: Callable[[dict[str, Any], str, str], str] | None = None,
) -> dict[str, Any]:
    """Advance at most one slice, using the existing governed handoff boundary.

    `dispatch_handoff` must create a fresh handoff and return its ID. The status
    loader verifies the existing execution receipt/seal. The canonical result
    and durable acceptance receipt are read again before continuation.
    """
    state = load_campaign(path)
    if state["status"] in TERMINAL:
        return state
    bound = "mission_binding" in state
    if state["current_slice"] is not None and not bound:
        state.update(status="BLOCKED", reason="uncertain_inflight_slice_requires_recovery")
        _save(path, state)
        return state
    if state["current_slice"] is not None:
        current = state["current_slice"]
        if bound and (len(state["completed_slices"]) >= len(state["slice_plan"]) or
                      current != state["slice_plan"][len(state["completed_slices"])]):
            state.update(status="BLOCKED", reason="inflight_slice_differs_from_authorized_plan")
            _save(path, state)
            return state
    else:
        proposal = propose_next(dict(state))
        if proposal is None:
            state.update(status="DONE" if state["completed_slices"] else "BLOCKED",
                         reason="completion_evidence_recorded" if state["completed_slices"] else "completion_without_evidence")
            _save(path, state)
            return state
        if len(state["completed_slices"]) >= state["authority"]["max_slices"]:
            state.update(status="BLOCKED", reason="campaign_slice_budget_exhausted")
            _save(path, state)
            return state
        try:
            current = _candidate(state, proposal)
        except ValueError as exc:
            state.update(status="ESCALATION_REQUIRED", reason=str(exc))
            _save(path, state)
            return state
        if _write_request(current):
            write_block = _check_write_approval(state, current)
            if write_block:
                state.update(status="BLOCKED", reason=write_block)
                _save(path, state)
                return state
        state["current_slice"] = current
        _save(path, state)
    try:
        handoff_id = (_bound_dispatch(
            path, state, current, prepare_handoff=prepare_handoff,
            dispatch_prepared=dispatch_prepared, load_status=load_status,
        ) if bound else dispatch_handoff(current))
        status = load_status(handoff_id)
        result_path = Path(str(status.get("canonical_result_path") or ""))
        result_sha256 = status.get("result_sha256")
        if not result_path.is_file() or not isinstance(result_sha256, str):
            if bound and status.get("accepted") is True:
                state.update(status="WAITING", reason="accepted_dispatch_awaiting_canonical_result")
                _save(path, state)
                return state
            raise ValueError("canonical handoff result is missing")
        raw = result_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != result_sha256:
            raise ValueError("canonical handoff result integrity failed")
        result = json.loads(raw)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        state.update(status="BLOCKED", reason=f"handoff_result_unavailable: {exc}")
        if bound:
            journal = state["dispatch_journal"][current["slice_id"]]
            journal["terminal_decision"] = (
                "BLOCKED_NO_DISPATCH" if journal["state"] in
                {"NOT_DISPATCHED", "PREPARE_INTENT", "PREPARED"} else "BLOCKED_UNCERTAIN"
            )
        _save(path, state)
        return state
    write_receipt = None
    write_block = None
    if _write_request(current):
        write_block, write_receipt = _check_write_result(current, result, handoff_id)
    if result.get("backend") != current["requested_backend"] or result.get("fallback_used") is not False:
        state.update(status="BLOCKED", reason="backend_provenance_mismatch")
    elif status.get("status") not in {"completed", "finalized"} or result.get("status") != "completed" or result.get("stop_reason") != "completed":
        state.update(status="BLOCKED", reason=str(result.get("stop_reason") or status.get("status") or "slice_incomplete"))
    elif authoritative_acceptance_state(
        result, evidence_root=handoff._acceptance_evidence_dir(handoff_id),
    ) != "PASS":
        state.update(status="BLOCKED", reason="authoritative_acceptance_not_pass")
    elif not _mission_acceptance_pass(current, result):
        state.update(status="BLOCKED", reason="mission_acceptance_not_pass")
    elif write_block:
        state.update(status="BLOCKED", reason=write_block)
    else:
        progress = observe_progress(current, result)
        if not isinstance(progress, dict) or not progress.get("ref") or not progress.get("sha256"):
            state.update(status="BLOCKED", reason="machine_progress_evidence_missing")
        else:
            proof_path = Path(progress["ref"])
            if not proof_path.is_file() or hashlib.sha256(proof_path.read_bytes()).hexdigest() != progress["sha256"]:
                state.update(status="BLOCKED", reason="machine_progress_evidence_invalid")
            else:
                fingerprint = f"{progress['ref']}:{progress['sha256']}"
                if fingerprint in state["progress_fingerprints"]:
                    state["no_progress_count"] += 1
                else:
                    state["progress_fingerprints"].append(fingerprint)
                    state["no_progress_count"] = 0
                if state["no_progress_count"] > state["authority"]["max_no_progress"]:
                    state.update(status="BLOCKED", reason="repeated_no_progress")
                else:
                    remaining = len(state["completed_slices"]) + 1 < len(state["slice_plan"])
                    state["completed_slices"].append({
                        **current, "handoff_id": handoff_id, "run_id": result.get("session_id"),
                        "backend": result["backend"], "result_path": str(result_path),
                        "result_sha256": result_sha256, "acceptance_state": "PASS",
                        "execution_acceptance": "PASS", "mission_acceptance": "PASS",
                        "progress_evidence": progress,
                        **({"mutation_receipt": write_receipt} if write_receipt else {}),
                        "continuation_decision": {
                            "decision": "CONTINUE" if remaining else "DONE",
                            "reasons": {
                                "authoritative_acceptance": "PASS",
                                "mission_acceptance": "PASS",
                                "backend_provenance": "valid",
                                "scope": "within_campaign",
                                "authority_expansion": False,
                                "progress": "observed",
                                "budget_remaining": remaining,
                            },
                        },
                    })
                    state["evidence_refs"].extend([str(result_path), progress["ref"]])
                    state.update(status="CONTINUE", reason="accepted_slice_progress", current_slice=None)
    if bound:
        journal = state["dispatch_journal"][current["slice_id"]]
        journal["run_id"] = result.get("session_id")
        journal["terminal_decision"] = (
            state["completed_slices"][-1]["continuation_decision"]["decision"]
            if state["status"] == "CONTINUE" else "BLOCKED"
        )
        journal["state"] = "TERMINAL"
    _save(path, state)
    return state


def advance_campaign(
    path: Path, *, propose_next: Callable[[dict[str, Any]], dict[str, Any] | None],
    dispatch_handoff: Callable[[dict[str, Any]], str],
    observe_progress: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]],
    load_status: Callable[[str], dict[str, Any]] = handoff._handoff_status_payload,
    prepare_handoff: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]] | None = None,
    dispatch_prepared: Callable[[dict[str, Any], str, str], str] | None = None,
) -> dict[str, Any]:
    """Serialize advance calls so one campaign cannot dispatch twice at once."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _advance_campaign_unlocked(
            path, propose_next=propose_next, dispatch_handoff=dispatch_handoff,
            observe_progress=observe_progress, load_status=load_status,
            prepare_handoff=prepare_handoff, dispatch_prepared=dispatch_prepared,
        )


def run_campaign(
    path: Path, *, propose_next: Callable[[dict[str, Any]], dict[str, Any] | None],
    dispatch_handoff: Callable[[dict[str, Any]], str],
    observe_progress: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]],
    load_status: Callable[[str], dict[str, Any]] = handoff._handoff_status_payload,
    prepare_handoff: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]] | None = None,
    dispatch_prepared: Callable[[dict[str, Any], str, str], str] | None = None,
) -> dict[str, Any]:
    """Continue without operator micro-tasks until a bounded terminal decision."""
    while True:
        state = advance_campaign(
            path, propose_next=propose_next, dispatch_handoff=dispatch_handoff,
            observe_progress=observe_progress, load_status=load_status,
            prepare_handoff=prepare_handoff, dispatch_prepared=dispatch_prepared,
        )
        if state["status"] in TERMINAL or state["status"] == "WAITING":
            return state
