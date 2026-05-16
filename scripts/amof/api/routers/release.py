"""Release management API: status, bump, promote, log, validate."""

import hashlib
import ipaddress
import json
import os
import sys
import subprocess
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import re
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends

from amof.cli import get_available_ecosystems
from amof.manifest import load_manifest, simple_parse_yaml
from amof.api.command_builder import get_workspace_root, get_code_root
from amof.api.dependencies import get_queue_dispatcher, get_run_manager, require_step_up_user
from amof.api.services.argocd import (
    ArgoCdClient,
    ArgoCdClientError,
    demo_microsaas_app_name,
    extract_demo_microsaas_application_status,
    load_argocd_settings,
)
from amof.api.services.cloudflare_dns import (
    CloudflareDnsError,
    delete_first_level_amof_dns,
    extract_managed_amof_hosts,
    upsert_first_level_amof_dns,
)
from amof.api.services.runner import run_subprocess_task
from amof.api.run_manager import RUN_STATUS_FAILED, RUN_STATUS_QUEUED, RUN_STATUS_RUNNING, RUN_STATUS_SUCCESS
from amof.queue import QueueDispatcher
from amof.commands.promote_main import _fetch_origin_main, _find_existing_promotion

router = APIRouter(prefix="/ecosystems", tags=["release"])

# Demo-microsaas constants below are RETIRED from current truth by
# clean-start slice 7 (see contracts/clean-start-target-topology.md).
# They are preserved byte-stable as a legacy-recovery surface for
# existing demo-microsaas operators. The canonical example
# application ecosystem is now ``gmd`` (clean-start slice 6, see
# ecosystems/gmd/README.md and infrastructure/gitops/gmd/). New
# release-flow code paths must NOT depend on these constants.
DEMO_MICROSAAS_ECOSYSTEM = "demo-microsaas"
DEMO_MICROSAAS_DEFAULT_IMAGE_TAG = "dev-20260312035031"
DEMO_MICROSAAS_IMAGE_REPOSITORY = "ghcr.io/marekhotshot/microsaas-backend"
DEMO_MICROSAAS_DEPLOYMENT_NAME = "microsaas-backend"
DEMO_MICROSAAS_DEFAULT_PUBLIC_BASE_DOMAIN = "amof.dev"
AMOF_ENVIRONMENT_DEFAULT_PUBLIC_BASE_DOMAIN = "amof.dev"
RELEASE_VALIDATE_ACTION = "release/validate"
RELEASE_VALIDATE_SUMMARY_ACTION = "release/validate/read"
RELEASE_VALIDATE_SUMMARY_REUSE_WINDOW_SECONDS = 10
RELEASE_VALIDATE_SUMMARY_INFLIGHT_WAIT_SECONDS = 2.0
PROMOTION_ENV_PATH_PREFIX = "envs/tickets/"
_PUBLIC_RELEASE_PROBE_REMOVED_NOTE = (
    "Registered target only. Public AMOF canonical main does not perform live "
    "release probes or kubeconfig-backed cluster inspection."
)

def _release_command_argv(ecosystem: str, args: List[str], cwd: Path) -> List[str]:
    code_root = get_code_root(cwd)
    candidates = [
        code_root / "scripts" / "amof.py",
        cwd / "scripts" / "amof.py",
        code_root / "scripts" / "amof" / "__main__.py",
        cwd / "scripts" / "amof" / "__main__.py",
    ]
    script = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    return [sys.executable, str(script), "-e", ecosystem, "release", *args]


def _run_release_capture(ecosystem: str, args: List[str], cwd: Path) -> Tuple[int, str]:
    """Run amof release subcommand and return (return_code, combined stdout+stderr)."""
    proc = subprocess.run(
        _release_command_argv(ecosystem, args, cwd),
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _ensure_release_validate_passes(ecosystem: str, cwd: Path) -> None:
    code, output = _run_release_capture(ecosystem, ["validate"], cwd)
    if code == 0:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "message": "Release validation failed. Fix blockers before deploy.",
            "validation_output": output,
        },
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_versions(text: str) -> List[str]:
    seen: List[str] = []
    for match in re.findall(r"\b\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?\b", text or ""):
        if match not in seen:
            seen.append(match)
    return seen


def _empty_summary(latest_candidate_release: Optional[str] = None) -> Dict[str, Any]:
    return {
        "latest_candidate_release": latest_candidate_release,
        "environments_behind_latest": 0,
        "failed_deploys": 0,
        "promotion_queue": 0,
        "validation_blockers": 0,
        "validation_warnings": 0,
        "validation_status": "unknown",
        "validation_notes": [],
        "validation_result_fingerprint": None,
        "validation_checked_at": None,
        "latest_validation_run": None,
        "latest_explicit_validation_run": None,
        "validation_read_mode": "unknown",
        "validation_read_note": None,
        "validation_evidence_status": "missing",
        "validation_evidence_age_seconds": None,
        "validation_evidence_note": None,
    }


def _release_payload_from_tag(
    tag: str, release_id: Optional[str] = None, created_at: Optional[str] = None
) -> Dict[str, Any]:
    version = str(tag).strip()
    return {
        "release_id": release_id or version,
        "tag": version,
        "version": version,
        "channel": ("stable" if "-" not in version else version.split("-", 1)[1].split(".", 1)[0]),
        "image_tag": version,
        "build_id": None,
        "created_at": _release_created_at(version, created_at),
        "deployments": [],
        "active_environments": [],
    }


def _release_created_at(tag: str, observed_at: Optional[str] = None) -> Optional[str]:
    observed = str(observed_at or "").strip()
    if observed:
        return observed
    match = re.search(r"(\d{14}|\d{12})", str(tag).strip())
    if not match:
        return None
    raw = match.group(1)
    try:
        fmt = "%Y%m%d%H%M%S" if len(raw) == 14 else "%Y%m%d%H%M"
        return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _latest_run_summary(name: str, run_mgr, actions: List[str]) -> Optional[Dict[str, Any]]:
    if run_mgr is None:
        return None
    runs = []
    for action in actions:
        runs.extend(run_mgr.list_runs_summary(ecosystem=name, action=action, limit=1))
    if not runs:
        return None
    runs.sort(key=lambda run: getattr(run, "created_at", "") or "", reverse=True)
    return _run_summary_payload(runs[0])


def _run_summary_payload(run: Any) -> Dict[str, Any]:
    return {
        "run_id": getattr(run, "run_id", None),
        "action": getattr(run, "action", None),
        "status": getattr(run, "status", None),
        "created_at": getattr(run, "created_at", None),
        "started_at": getattr(run, "started_at", None),
        "finished_at": getattr(run, "finished_at", None),
        "exit_code": getattr(run, "exit_code", None),
    }


def _latest_validation_run_summary(name: str, run_mgr) -> Optional[Dict[str, Any]]:
    return _latest_run_summary(name, run_mgr, [RELEASE_VALIDATE_SUMMARY_ACTION, RELEASE_VALIDATE_ACTION])


def _latest_explicit_validation_run_summary(name: str, run_mgr) -> Optional[Dict[str, Any]]:
    return _latest_run_summary(name, run_mgr, [RELEASE_VALIDATE_ACTION])


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _validation_evidence_summary(checked_at: str, validation_run: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not validation_run:
        return {
            "validation_evidence_status": "missing",
            "validation_evidence_age_seconds": None,
            "validation_evidence_note": "No recorded release/validate run available.",
        }

    checked_dt = _parse_iso_timestamp(checked_at)
    run_dt = (
        _parse_iso_timestamp(validation_run.get("finished_at"))
        or _parse_iso_timestamp(validation_run.get("started_at"))
        or _parse_iso_timestamp(validation_run.get("created_at"))
    )
    if checked_dt is None or run_dt is None:
        return {
            "validation_evidence_status": "missing",
            "validation_evidence_age_seconds": None,
            "validation_evidence_note": "Latest release/validate run is missing usable timestamps.",
        }

    age_seconds = max(0, int((checked_dt - run_dt).total_seconds()))
    if run_dt < checked_dt:
        return {
            "validation_evidence_status": "stale",
            "validation_evidence_age_seconds": age_seconds,
            "validation_evidence_note": "Latest recorded validation run predates the current summary check.",
        }
    return {
        "validation_evidence_status": "fresh",
        "validation_evidence_age_seconds": age_seconds,
        "validation_evidence_note": None,
    }


def _run_effective_timestamp(run: Any) -> Optional[datetime]:
    return (
        _parse_iso_timestamp(getattr(run, "finished_at", None))
        or _parse_iso_timestamp(getattr(run, "started_at", None))
        or _parse_iso_timestamp(getattr(run, "created_at", None))
    )


def _normalize_validation_output(output: str) -> str:
    normalized_lines: List[str] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "Merkle root present (" in line:
            line = re.sub(r"\([0-9a-fA-F]{8,}\)", "(hash)", line)
        normalized_lines.append(line)
    return "\n".join(normalized_lines)


def _validation_output_fingerprint(output: str) -> str:
    normalized = _normalize_validation_output(output)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validation_result_from_output(code: int, output: str) -> Dict[str, Any]:
    notes: List[str] = []
    warnings = 0
    blockers = 0

    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("⚠"):
            warnings += 1
            notes.append(line[1:].strip())
        elif line.startswith("✗"):
            blockers += 1
            notes.append(line[1:].strip())

    if blockers > 0 or code != 0:
        status = "failed"
    elif warnings > 0:
        status = "warning"
    else:
        status = "ok"

    evidence: Dict[str, Any] = {
        "summary": f"Release validation {status} ({blockers} blockers, {warnings} warnings)",
        "validation_status": status,
        "validation_blockers": blockers,
        "validation_warnings": warnings,
        "validation_notes": notes[:10],
        "validation_result_fingerprint": _validation_output_fingerprint(output),
    }
    normalized_output = _normalize_validation_output(output)
    if normalized_output:
        evidence["validation_output_normalized"] = normalized_output
    if status == "failed":
        evidence["blocker"] = "release_validation_failed"
        evidence["blocker_summary"] = evidence["summary"]
    return evidence


def _persist_validation_result_evidence(run_mgr, run_id: str, code: int, output: str) -> None:
    if run_mgr is None:
        return
    run = run_mgr.get_run(run_id)
    if run is None:
        return
    loop_state = dict(getattr(run, "loop_state", {}) or {})
    latest_evidence = dict(loop_state.get("latest_evidence") or {})
    latest_evidence.update(_validation_result_from_output(code, output))
    loop_state["latest_evidence"] = latest_evidence
    loop_state["last_result_summary"] = latest_evidence.get("summary")
    run_mgr.update_loop_state(run_id, loop_state)


def _newer_release_activity_requires_new_proof(name: str, run_mgr, baseline_run: Any) -> bool:
    if run_mgr is None or baseline_run is None:
        return False
    baseline_ts = _run_effective_timestamp(baseline_run)
    if baseline_ts is None:
        return True
    baseline_output = _validation_output_from_run(run_mgr, getattr(baseline_run, "run_id", ""))
    baseline_fingerprint = _validation_output_fingerprint(baseline_output)
    recent_runs = run_mgr.list_runs_summary(ecosystem=name, limit=25)
    for run in recent_runs:
        action = str(getattr(run, "action", "") or "").strip()
        if not action.startswith("release/") or action == RELEASE_VALIDATE_SUMMARY_ACTION:
            continue
        run_ts = _run_effective_timestamp(run)
        if run_ts is None or run_ts <= baseline_ts:
            continue
        if action == RELEASE_VALIDATE_ACTION:
            newer_output = _validation_output_from_run(run_mgr, getattr(run, "run_id", ""))
            if _validation_output_fingerprint(newer_output) == baseline_fingerprint:
                continue
            return True
    return False


def _recent_summary_validation_run(name: str, run_mgr) -> Optional[Any]:
    if run_mgr is None:
        return None
    runs = run_mgr.list_runs_summary(ecosystem=name, action=RELEASE_VALIDATE_SUMMARY_ACTION, limit=1)
    if not runs:
        return None
    run = runs[0]
    if getattr(run, "status", None) not in {RUN_STATUS_SUCCESS, RUN_STATUS_FAILED}:
        return None
    run_dt = (
        _parse_iso_timestamp(getattr(run, "finished_at", None))
        or _parse_iso_timestamp(getattr(run, "started_at", None))
        or _parse_iso_timestamp(getattr(run, "created_at", None))
    )
    if run_dt is None:
        return None
    age_seconds = (datetime.now(timezone.utc) - run_dt).total_seconds()
    if age_seconds > RELEASE_VALIDATE_SUMMARY_REUSE_WINDOW_SECONDS:
        return None
    if _newer_release_activity_requires_new_proof(name, run_mgr, run):
        return None
    return run


def _await_inflight_summary_validation_run(name: str, run_mgr) -> Optional[Any]:
    if run_mgr is None:
        return None
    runs = run_mgr.list_runs_summary(ecosystem=name, action=RELEASE_VALIDATE_SUMMARY_ACTION, limit=1)
    if not runs:
        return None
    baseline = runs[0]
    if getattr(baseline, "status", None) not in {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}:
        return None
    started = time.monotonic()
    run_id = str(getattr(baseline, "run_id", "") or "")
    while time.monotonic() - started <= RELEASE_VALIDATE_SUMMARY_INFLIGHT_WAIT_SECONDS:
        current = run_mgr.get_run(run_id)
        if current is None:
            return None
        status = getattr(current, "status", None)
        if status in {RUN_STATUS_SUCCESS, RUN_STATUS_FAILED}:
            if _newer_release_activity_requires_new_proof(name, run_mgr, current):
                return None
            return current
        if status not in {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}:
            return None
        time.sleep(0.05)
    return None


def _validation_output_from_run(run_mgr, run_id: str) -> str:
    if run_mgr is None:
        return ""
    run = run_mgr.get_run(run_id)
    if run is None:
        return ""
    lines = [str(event.message) for event in getattr(run, "events", []) if getattr(event, "type", None) == "log" and str(getattr(event, "message", "")).strip()]
    return "\n".join(lines)


def _finalize_validation_summary_run(run_mgr, run_id: str, code: int, output: str) -> Optional[Dict[str, Any]]:
    if run_mgr is None:
        return None
    for line in (output or "").splitlines():
        if line.strip():
            run_mgr.append_log(run_id, line)
    run_mgr.update_status(run_id, RUN_STATUS_SUCCESS if code == 0 else RUN_STATUS_FAILED, exit_code=code)
    _persist_validation_result_evidence(run_mgr, run_id, code, output)
    run = run_mgr.get_run(run_id)
    if run is None:
        return None
    return _run_summary_payload(run)


def _materialize_validation_summary_run(name: str, cwd: Path, run_mgr) -> Tuple[int, str, Optional[Dict[str, Any]]]:
    if run_mgr is None:
        code, output = _run_release_capture(name, ["validate"], cwd)
        return code, output, None
    run_id = run_mgr.create_run(
        name,
        RELEASE_VALIDATE_SUMMARY_ACTION,
        _release_command_argv(name, ["validate"], cwd),
    )
    run_mgr.update_status(run_id, RUN_STATUS_RUNNING)
    code, output = _run_release_capture(name, ["validate"], cwd)
    return code, output, _finalize_validation_summary_run(run_mgr, run_id, code, output)


def _release_validation_summary(
    name: str,
    cwd: Path,
    run_mgr,
    latest_candidate_release: Optional[str] = None,
    validation_run: Optional[Dict[str, Any]] = None,
    explicit_validation_run: Optional[Dict[str, Any]] = None,
    allow_materialize: bool = True,
) -> Dict[str, Any]:
    summary = _empty_summary(latest_candidate_release)
    recent_run = _recent_summary_validation_run(name, run_mgr)
    if recent_run is not None:
        summary["validation_read_mode"] = "reused_recent_proof"
        summary["validation_read_note"] = "Summary reused a recent release/validate/read proof run."
        code = int(getattr(recent_run, "exit_code", 0) or 0)
        output = _validation_output_from_run(run_mgr, getattr(recent_run, "run_id", ""))
        validation_run = _run_summary_payload(recent_run)
    else:
        inflight_run = _await_inflight_summary_validation_run(name, run_mgr)
        if inflight_run is not None:
            summary["validation_read_mode"] = "reused_inflight_proof"
            summary["validation_read_note"] = "Summary reused an in-flight release/validate/read proof run."
            code = int(getattr(inflight_run, "exit_code", 0) or 0)
            output = _validation_output_from_run(run_mgr, getattr(inflight_run, "run_id", ""))
            validation_run = _run_summary_payload(inflight_run)
        elif allow_materialize:
            summary["validation_read_mode"] = "fresh_check"
            summary["validation_read_note"] = "Summary executed a fresh validation check for this response."
            code, output, materialized_run = _materialize_validation_summary_run(name, cwd, run_mgr)
            validation_run = materialized_run or validation_run
        else:
            summary["validation_read_mode"] = "cached_only"
            summary["validation_read_note"] = "Summary used cached validation evidence only; no fresh validation run was created."
            output = _validation_output_from_run(run_mgr, str((validation_run or {}).get("run_id") or ""))
            if validation_run is not None:
                code = int((validation_run or {}).get("exit_code") or 0)
            else:
                code = 0
                output = ""
    if output:
        validation_result = _validation_result_from_output(code, output)
        summary["validation_status"] = validation_result["validation_status"]
        summary["validation_blockers"] = validation_result["validation_blockers"]
        summary["validation_warnings"] = validation_result["validation_warnings"]
        summary["validation_notes"] = list(validation_result["validation_notes"])
        summary["validation_result_fingerprint"] = validation_result["validation_result_fingerprint"]
    summary["latest_validation_run"] = validation_run
    summary["latest_explicit_validation_run"] = explicit_validation_run
    summary["validation_checked_at"] = str(
        (validation_run or {}).get("finished_at")
        or (validation_run or {}).get("started_at")
        or _now_iso()
    )
    summary.update(_validation_evidence_summary(summary["validation_checked_at"], validation_run))
    return summary


def _run_kubectl_capture(args: List[str], kubeconfig_path: Optional[str] = None) -> Tuple[int, str]:
    command = ["kubectl"]
    resolved_kubeconfig = str(kubeconfig_path or "").strip()
    if resolved_kubeconfig:
        command.extend(["--kubeconfig", resolved_kubeconfig])
    proc = subprocess.run(
        [*command, *args],
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _demo_microsaas_default_image_tag() -> str:
    value = (
        os.environ.get("MICROSAAS_IMAGE_TAG")
        or os.environ.get("AMOF_MICROSAAS_IMAGE_TAG")
        or DEMO_MICROSAAS_DEFAULT_IMAGE_TAG
    )
    return str(value).strip() or DEMO_MICROSAAS_DEFAULT_IMAGE_TAG


def _normalize_public_base_url(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text if "://" in text else f"http://{text}"
    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


def _public_host_from_base_url(value: Any) -> Optional[str]:
    normalized = _normalize_public_base_url(value)
    if not normalized:
        return None
    return urlparse(normalized).netloc or None


def _default_environment_public_host(
    *,
    name: str,
    stage_id: Optional[str] = None,
    environment_id: Optional[str] = None,
) -> Optional[str]:
    if name == DEMO_MICROSAAS_ECOSYSTEM:
        return _public_host_from_base_url(
            _demo_microsaas_default_public_base_url(stage_id=stage_id or environment_id or "dev")
        )
    slug = _slug(environment_id or stage_id or "dev")
    if name == "amof-platform":
        return f"{slug}.{AMOF_ENVIRONMENT_DEFAULT_PUBLIC_BASE_DOMAIN}"
    return f"{slug}-{_slug(name)}.{AMOF_ENVIRONMENT_DEFAULT_PUBLIC_BASE_DOMAIN}"


def _default_environment_public_base_url(
    *,
    name: str,
    stage_id: Optional[str] = None,
    environment_id: Optional[str] = None,
) -> Optional[str]:
    host = _default_environment_public_host(name=name, stage_id=stage_id, environment_id=environment_id)
    return f"https://{host}/" if host else None


def _default_environment_endpoints(
    *,
    name: str,
    stage_id: Optional[str] = None,
    environment_id: Optional[str] = None,
    public_base_url: Optional[str] = None,
) -> List[Dict[str, str]]:
    url = _normalize_public_base_url(public_base_url) or _default_environment_public_base_url(
        name=name,
        stage_id=stage_id,
        environment_id=environment_id,
    )
    if not url:
        return []
    label = "Microsaas Demo" if name == DEMO_MICROSAAS_ECOSYSTEM else ("Platform" if name == "amof-platform" else "Public")
    return [{"label": label, "url": url}]


def _managed_environment_dns_hosts(profile: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(profile, dict):
        return []
    return extract_managed_amof_hosts(
        hostname=profile.get("hostname"),
        public_base_url=profile.get("public_base_url"),
        endpoints=profile.get("endpoints"),
    )


def _reconcile_environment_dns(previous_profile: Optional[Dict[str, Any]], next_profile: Optional[Dict[str, Any]]) -> List[str]:
    previous_hosts = _managed_environment_dns_hosts(previous_profile)
    next_hosts = _managed_environment_dns_hosts(next_profile)
    applied: List[str] = []
    if next_hosts:
        applied.extend(upsert_first_level_amof_dns(next_hosts))
    removed_hosts = [host for host in previous_hosts if host not in set(next_hosts)]
    if removed_hosts:
        applied.extend(delete_first_level_amof_dns(removed_hosts))
    return applied


def _reconcile_environment_dns_or_raise(
    previous_profile: Optional[Dict[str, Any]],
    next_profile: Optional[Dict[str, Any]],
) -> List[str]:
    try:
        return _reconcile_environment_dns(previous_profile, next_profile)
    except CloudflareDnsError as exc:
        raise HTTPException(status_code=502, detail=f"Environment DNS reconciliation failed: {exc}") from exc


def _demo_microsaas_stage_slug(profile: Optional[Dict[str, Any]] = None, stage_id: Optional[str] = None) -> str:
    value = stage_id or (profile or {}).get("stage_id") or (profile or {}).get("id") or "dev"
    return _slug(str(value or "dev")) or "dev"


def _demo_microsaas_default_public_base_url(
    profile: Optional[Dict[str, Any]] = None,
    stage_id: Optional[str] = None,
) -> str:
    stage_slug = _demo_microsaas_stage_slug(profile=profile, stage_id=stage_id)
    return f"https://{stage_slug}-demo-microsaas.{DEMO_MICROSAAS_DEFAULT_PUBLIC_BASE_DOMAIN}/"


def _demo_microsaas_configured_public_base_url(
    profile: Optional[Dict[str, Any]] = None,
    stage_id: Optional[str] = None,
) -> Optional[str]:
    return (
        _normalize_public_base_url(os.environ.get("MICROSAAS_PUBLIC_BASE_URL"))
        or _normalize_public_base_url(os.environ.get("AMOF_MICROSAAS_PUBLIC_BASE_URL"))
        or _normalize_public_base_url((profile or {}).get("public_base_url"))
        or _demo_microsaas_default_public_base_url(profile=profile, stage_id=stage_id)
    )


def _demo_microsaas_public_base_url(
    profile: Optional[Dict[str, Any]] = None,
    probe_host: Optional[str] = None,
    server_ip: Optional[str] = None,
    stage_id: Optional[str] = None,
) -> Optional[str]:
    configured = _demo_microsaas_configured_public_base_url(profile, stage_id=stage_id)
    if configured:
        return configured
    host = str(probe_host or "").strip()
    if not host and server_ip:
        host = f"microsaas.{server_ip}.sslip.io"
    return f"http://{host}/" if host else None


def _demo_microsaas_lifecycle_run_payload(
    action: str,
    profile: Optional[Dict[str, Any]] = None,
    stage_id: Optional[str] = None,
    environment_id: Optional[str] = None,
    public_base_url: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_stage_id = str(stage_id or (profile or {}).get("stage_id") or (profile or {}).get("id") or "dev").strip() or "dev"
    resolved_environment_id = str(environment_id or (profile or {}).get("id") or resolved_stage_id).strip() or resolved_stage_id
    resolved_public_base_url = (
        _normalize_public_base_url(public_base_url)
        or _demo_microsaas_configured_public_base_url(profile, stage_id=resolved_stage_id)
        or _demo_microsaas_default_public_base_url(profile=profile, stage_id=resolved_stage_id)
    )
    payload: Dict[str, Any] = {
        "action": str(action or "").strip().lower(),
        "lifecycle_action": str(action or "").strip().lower(),
        "environment_id": resolved_environment_id,
        "stage_id": resolved_stage_id,
    }
    if resolved_public_base_url:
        payload["public_url"] = resolved_public_base_url
        payload["public_host"] = _public_host_from_base_url(resolved_public_base_url)
    return payload


def _first_ipv4_address(raw: str) -> Optional[str]:
    for token in str(raw or "").split():
        try:
            parsed = ipaddress.ip_address(token.strip())
        except ValueError:
            continue
        if parsed.version == 4:
            return token.strip()
    return None


def _demo_microsaas_server_ip() -> Optional[str]:
    code, output = _run_kubectl_capture(
        ["get", "nodes", "-o", "jsonpath={.items[0].status.addresses[?(@.type=='ExternalIP')].address}"]
    )
    value = _first_ipv4_address(output)
    if code == 0 and value:
        return value
    code, output = _run_kubectl_capture(
        ["get", "nodes", "-o", "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}"]
    )
    return _first_ipv4_address(output)


def _demo_microsaas_profile_from_target(target: Any) -> Dict[str, Any]:
    if isinstance(target, dict):
        return dict(target)
    wanted = str(target or "").strip()
    profiles = _read_environment_profiles(DEMO_MICROSAAS_ECOSYSTEM)
    for profile in profiles:
        candidates = {
            str(profile.get("id") or "").strip(),
            str(profile.get("stage_id") or "").strip(),
            str(profile.get("namespace") or "").strip(),
        }
        if wanted in candidates:
            return dict(profile)
    return {"id": wanted or "dev", "stage_id": wanted or "dev", "namespace": wanted or "demo-microsaas"}


def _demo_microsaas_argocd_probe(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    environment_id = str(profile.get("id") or profile.get("stage_id") or "dev").strip() or "dev"
    settings = load_argocd_settings()
    app_name = str(profile.get("argocd_app_name") or demo_microsaas_app_name(environment_id, settings)).strip()
    host = _public_host_from_base_url(
        _demo_microsaas_configured_public_base_url(profile, stage_id=environment_id)
    )
    if settings is None:
        return {
            "exists": False,
            "app_name": app_name,
            "image": None,
            "image_tag": None,
            "ready": False,
            "finished_at": None,
            "host": host,
            "checked_at": _now_iso(),
            "state": "registered_only",
            "detail": f"Argo CD is not configured for app {app_name}.",
        }
    try:
        app = ArgoCdClient(settings).get_application(app_name, refresh="normal")
    except ArgoCdClientError as exc:
        return {
            "exists": False,
            "app_name": app_name,
            "image": None,
            "image_tag": None,
            "ready": False,
            "finished_at": None,
            "host": host,
            "checked_at": _now_iso(),
            "state": "registered_only",
            "detail": f"Argo CD app {app_name} unavailable: {exc}",
        }
    status = extract_demo_microsaas_application_status(app)
    if not status.get("host"):
        status["host"] = _public_host_from_base_url(
            _demo_microsaas_configured_public_base_url(profile, stage_id=environment_id)
        )
    return status


def _demo_microsaas_probe(target: Any) -> Dict[str, Any]:
    profile = _demo_microsaas_profile_from_target(target)
    return _demo_microsaas_argocd_probe(profile)


def _default_target_host(profile: Dict[str, Any]) -> Optional[str]:
    endpoints = profile.get("endpoints") if isinstance(profile.get("endpoints"), list) else []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        host = _public_host_from_base_url(endpoint.get("url"))
        if host:
            return host
    return None


def _amof_platform_probe(profile: Dict[str, Any]) -> Dict[str, Any]:
    target_host = _default_target_host(profile)
    return {
        "state": "registered_only",
        "detail": _PUBLIC_RELEASE_PROBE_REMOVED_NOTE,
        "checked_at": _now_iso(),
        "image": None,
        "image_tag": None,
        "ready": False,
        "host": target_host,
    }


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "env"


def _environment_store_path(name: str) -> Path:
    root = get_workspace_root()
    store_dir = root / ".amof" / "release-environments"
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir / f"{name}.json"


# Canonical ordered stages when no persisted profiles exist.
AUTHORITATIVE_STAGE_IDS = ["dev", "test", "prod"]

LEGACY_STAGE_ID_ALIASES = {
    "dev": "dev",
    "stage": "test",
    "tooling": "test",
    "test": "test",
    "main": "prod",
    "temp": "prod",
    "prod": "prod",
}


def _canonical_stage_id(value: Any) -> str:
    normalized = _slug(value or "dev")
    return LEGACY_STAGE_ID_ALIASES.get(normalized, normalized)


def _looks_like_base_environment(profile_id: str, stage_id: str) -> bool:
    if profile_id == stage_id:
        return True
    return _canonical_stage_id(profile_id) == _canonical_stage_id(stage_id)


def _promotion_target_for_stage(stage_id: Optional[str]) -> Optional[str]:
    normalized = _canonical_stage_id(stage_id)
    if normalized not in AUTHORITATIVE_STAGE_IDS:
        return None
    index = AUTHORITATIVE_STAGE_IDS.index(normalized)
    return AUTHORITATIVE_STAGE_IDS[index + 1] if index + 1 < len(AUTHORITATIVE_STAGE_IDS) else None


# Stale ai_log_chunks strings persisted by older code versions that should
# be scrubbed from amof-platform base environments at projection time. These
# are exact-match strings rather than substrings to avoid clobbering legit
# operator-authored notes that happen to share words.
_STALE_AI_LOG_CHUNKS = frozenset(
    {
        # Original demo placeholder copy from the pre-cloud-dev shared demo
        # stage. The lane is no longer a demo lane and the copy misled
        # operators about what the dev environment actually validates.
        "Shared demo mode is active. Use this environment to validate login, "
        "Arena flows, and release visibility.",
    }
)


def _canonical_default_profile(name: str, stage_id: str) -> Optional[Dict[str, Any]]:
    """Return the canonical default profile for a known authoritative base
    stage (e.g. ``amof-platform``/``dev``).

    Used both for first-time bootstrap (via ``_default_environment_profiles``)
    and for in-place backfill of fields that older persisted profiles never
    populated, e.g. ``deploy_profile`` defaulting to ``null`` for legacy
    amof-platform/dev rows. Returns ``None`` for unknown ecosystems so the
    caller can leave persisted profiles untouched.
    """
    canonical_stage = _canonical_stage_id(stage_id)
    if canonical_stage not in AUTHORITATIVE_STAGE_IDS:
        return None
    for profile in _default_environment_profiles(name):
        if str(profile.get("stage_id")) == canonical_stage:
            return profile
    return None


def _default_environment_profiles(name: str) -> List[Dict[str, Any]]:
    """Return authoritative dev/test/prod targets. Used when no .amof/release-environments/<name>.json exists."""
    default_ns = name
    default_release = name
    profiles: List[Dict[str, Any]] = []
    for i, stage_id in enumerate(AUTHORITATIVE_STAGE_IDS):
        promotion_target = AUTHORITATIVE_STAGE_IDS[i + 1] if i + 1 < len(AUTHORITATIVE_STAGE_IDS) else None
        title = stage_id.capitalize()
        if name == "amof-platform" and stage_id == "dev":
            summary = "Development target managed from the prod-dev control base."
            ns = f"{default_ns}-dev"
            helm = f"{default_release}-dev"
            endpoints = [{"label": "Platform", "url": "https://dev-platform.amof.dev"}]
            deploy_profile = "dev"
            repo_summary = "Managed downstream development target"
            ai_log_chunks = [
                "Managed downstream development target registration for the AMOF master control plane.",
            ]
        elif name == "amof-platform" and stage_id == "test":
            summary = "Shared test target managed from the prod-dev control base."
            ns = "amof-system"
            helm = "amof"
            endpoints = [
                {"label": "Platform", "url": "https://platform.amof.dev"},
                {"label": "Public", "url": "https://amof.dev"},
            ]
            deploy_profile = "cloud-dev"
            repo_summary = "Managed downstream shared test target"
            ai_log_chunks = [
                "Current shared test deployment lane managed through platform.amof.dev.",
            ]
        elif name == "amof-platform" and stage_id == "prod":
            summary = "Production target managed from the prod-dev control base."
            ns = "amof-system"
            helm = "amof"
            endpoints = []
            deploy_profile = "prod-dev"
            repo_summary = "Managed downstream production target"
            ai_log_chunks = [
                "Production target registration for the AMOF control plane.",
            ]
        elif stage_id == "dev":
            summary = "Development target managed from the prod-dev control base."
            ns = f"{default_ns}-dev"
            helm = f"{default_release}-dev"
            endpoints = []
            deploy_profile = "dev"
            repo_summary = "Managed downstream development target"
            ai_log_chunks = [
                "Managed downstream development target registration.",
            ]
        elif stage_id == "test":
            summary = "Shared test target managed from the prod-dev control base."
            ns = f"{default_ns}-test"
            helm = f"{default_release}-test"
            endpoints = []
            deploy_profile = "stage"
            repo_summary = "Managed downstream shared test target"
            ai_log_chunks = [
                "Managed downstream shared test target registration.",
            ]
        else:
            summary = "Production target managed from the prod-dev control base."
            ns = f"{default_ns}-prod"
            helm = f"{default_release}-prod"
            endpoints = []
            deploy_profile = "prod-dev"
            repo_summary = "Managed downstream production target"
            ai_log_chunks = [
                "Managed downstream production target registration.",
            ]
        profiles.append({
            "id": stage_id,
            "stage_id": stage_id,
            "title": title,
            "summary": summary,
            "label": stage_id,
            "ticket_label": "main",
            "workspace": f"{name} / main",
            "repo_summary": repo_summary,
            "status": "healthy",
            "namespace": ns,
            "helm_release": helm,
            "public_base_url": (
                _demo_microsaas_default_public_base_url(stage_id=stage_id)
                if name == DEMO_MICROSAAS_ECOSYSTEM
                else None
            ),
            "endpoints": (
                [{"label": "Microsaas Demo", "url": _demo_microsaas_default_public_base_url(stage_id=stage_id)}]
                if name == DEMO_MICROSAAS_ECOSYSTEM
                else endpoints
            ),
            "promotion_target": promotion_target,
            "deploy_profile": deploy_profile,
            "ai_log_chunks": ai_log_chunks,
        })
    return profiles


def _normalize_environment_profiles(name: str, profiles: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    normalized_profiles: List[Dict[str, Any]] = []
    changed = False
    for profile in profiles:
        updated = dict(profile)
        raw_stage_id = _slug(profile.get("stage_id") or profile.get("id") or "dev")
        raw_profile_id = _slug(profile.get("id") or raw_stage_id)
        canonical_stage_id = _canonical_stage_id(raw_stage_id)
        is_base_environment = _looks_like_base_environment(raw_profile_id, raw_stage_id)

        if canonical_stage_id != raw_stage_id:
            changed = True
        updated["stage_id"] = canonical_stage_id

        canonical_profile_id = canonical_stage_id if is_base_environment else raw_profile_id
        if canonical_profile_id != raw_profile_id:
            changed = True
        updated["id"] = canonical_profile_id

        if is_base_environment:
            if str(profile.get("label") or "").strip().lower() in {"", raw_profile_id, raw_stage_id}:
                if profile.get("label") != canonical_stage_id:
                    changed = True
                updated["label"] = canonical_stage_id
            if str(profile.get("title") or "").strip().lower() in {"", raw_profile_id, raw_stage_id, raw_profile_id.capitalize().lower(), raw_stage_id.capitalize().lower()}:
                canonical_title = canonical_stage_id.capitalize()
                if profile.get("title") != canonical_title:
                    changed = True
                updated["title"] = canonical_title

        current_promotion_target = profile.get("promotion_target")
        normalized_promotion_target = (
            _canonical_stage_id(current_promotion_target)
            if current_promotion_target not in {None, ""}
            else _promotion_target_for_stage(canonical_stage_id)
        )
        if updated.get("promotion_target") != normalized_promotion_target:
            updated["promotion_target"] = normalized_promotion_target
            changed = True

        if name == DEMO_MICROSAAS_ECOSYSTEM and is_base_environment:
            canonical_public_base_url = _demo_microsaas_default_public_base_url(stage_id=canonical_stage_id)
            if updated.get("public_base_url") != canonical_public_base_url:
                updated["public_base_url"] = canonical_public_base_url
                changed = True
            canonical_endpoint = [{"label": "Microsaas Demo", "url": canonical_public_base_url}]
            if updated.get("endpoints") != canonical_endpoint:
                updated["endpoints"] = canonical_endpoint
                changed = True

        # Backfill canonical defaults for known authoritative base stages.
        # Older persisted profiles (created before the canonical defaults
        # existed) leave deploy_profile as null and carry stale demo
        # ai_log_chunks copy. We only touch fields the caller would otherwise
        # see as ``n/a`` / placeholder; operator-customized fields (label,
        # endpoints, status, ticket_label) are preserved as-is.
        if is_base_environment:
            canonical = _canonical_default_profile(name, canonical_stage_id)
            if canonical is not None:
                if not str(updated.get("deploy_profile") or "").strip():
                    updated["deploy_profile"] = canonical.get("deploy_profile")
                    changed = True
                if not str(updated.get("namespace") or "").strip():
                    updated["namespace"] = canonical.get("namespace")
                    changed = True
                if not str(updated.get("helm_release") or "").strip():
                    updated["helm_release"] = canonical.get("helm_release")
                    changed = True
                if not str(updated.get("repo_summary") or "").strip():
                    updated["repo_summary"] = canonical.get("repo_summary")
                    changed = True
                if not str(updated.get("summary") or "").strip():
                    updated["summary"] = canonical.get("summary")
                    changed = True
                persisted_chunks = updated.get("ai_log_chunks")
                if isinstance(persisted_chunks, list):
                    scrubbed = [
                        entry
                        for entry in persisted_chunks
                        if not (isinstance(entry, str) and entry.strip() in _STALE_AI_LOG_CHUNKS)
                    ]
                    if scrubbed != persisted_chunks:
                        updated["ai_log_chunks"] = scrubbed
                        changed = True
                    if not scrubbed:
                        canonical_chunks = canonical.get("ai_log_chunks")
                        if isinstance(canonical_chunks, list) and canonical_chunks:
                            updated["ai_log_chunks"] = list(canonical_chunks)
                            changed = True

        normalized_profiles.append(updated)
    return normalized_profiles, changed


def _read_environment_profiles(name: str) -> List[Dict[str, Any]]:
    path = _environment_store_path(name)
    if not path.exists():
        profiles = _default_environment_profiles(name)
        path.write_text(json.dumps(profiles, indent=2) + "\n", encoding="utf-8")
        return profiles
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = _default_environment_profiles(name)
    if not isinstance(payload, list) or not payload:
        payload = _default_environment_profiles(name)
    normalized_payload, changed = _normalize_environment_profiles(
        name,
        [row for row in payload if isinstance(row, dict)],
    )
    if changed:
        _write_environment_profiles(name, normalized_payload)
    return normalized_payload


def _write_environment_profiles(name: str, profiles: List[Dict[str, Any]]) -> None:
    _environment_store_path(name).write_text(json.dumps(profiles, indent=2) + "\n", encoding="utf-8")


def _normalize_environment_endpoints(value: Any) -> Optional[List[Dict[str, str]]]:
    if value is None:
        return None
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="Environment endpoints must be a list")
    endpoints: List[Dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        label = str(entry.get("label") or "Endpoint").strip() or "Endpoint"
        endpoints.append({"label": label, "url": url})
    return endpoints


def _apply_environment_profile_updates(
    *,
    name: str,
    profile: Dict[str, Any],
    body: Dict[str, Any],
) -> Dict[str, Any]:
    updated = dict(profile)
    for key in (
        "title",
        "summary",
        "label",
        "ticket_label",
        "workspace",
        "repo_summary",
        "status",
        "namespace",
        "helm_release",
        "deploy_profile",
        "promotion_target",
    ):
        if key in body:
            updated[key] = body.get(key)
    if "public_base_url" in body:
        updated["public_base_url"] = (
            _normalize_public_base_url(body.get("public_base_url"))
            or (
                _demo_microsaas_default_public_base_url(profile=updated)
                if name == DEMO_MICROSAAS_ECOSYSTEM
                else None
            )
        )
    if "endpoints" in body:
        updated["endpoints"] = (
            _normalize_environment_endpoints(body.get("endpoints"))
            or (
                [{"label": "Microsaas Demo", "url": _demo_microsaas_default_public_base_url(profile=updated)}]
                if name == DEMO_MICROSAAS_ECOSYSTEM
                else []
            )
        )
    if "ai_log_chunks" in body:
        chunks = body.get("ai_log_chunks")
        if chunks is None:
            updated["ai_log_chunks"] = []
        elif not isinstance(chunks, list):
            raise HTTPException(status_code=400, detail="Environment ai_log_chunks must be a list")
        else:
            updated["ai_log_chunks"] = [str(entry) for entry in chunks if str(entry).strip()]
    return updated


def _build_release_builds(registry: Dict[str, Any]) -> List[Dict[str, Any]]:
    builds: List[Dict[str, Any]] = []
    for index, release in enumerate(registry.get("releases", []), start=1):
        builds.append(
            {
                "build_id": f"compat-build-{index}",
                "image_tag": release.get("image_tag") or release.get("version") or "n/a",
                "status": "success",
                "run_id": None,
                "source_branch": None,
                "source_commit": None,
                "created_at": release.get("created_at"),
                "finished_at": release.get("created_at"),
            }
        )
    return builds


def _release_build_image_tag(command: Any) -> Optional[str]:
    argv = list(command or [])
    if not argv:
        return None
    script_index = next(
        (
            index
            for index, token in enumerate(argv)
            if str(token or "").endswith(".sh") or str(token or "").endswith(".py")
        ),
        None,
    )
    if script_index is None:
        return None
    for token in argv[script_index + 1 :]:
        value = str(token or "").strip()
        if value and not value.startswith("-"):
            return value
    return None


def _registry_base_from_env() -> Optional[str]:
    registry = str(os.environ.get("GHCR_REGISTRY") or "").strip()
    if registry:
        return registry
    owner = str(os.environ.get("GITHUB_OWNER") or "").strip()
    if owner:
        return f"ghcr.io/{owner}"
    return None


_ASSISTANT_IMAGE_NAME = "amof-assistant"
_ASSISTANT_SKIP_REASON = (
    "Assistant build is opt-in (set AMOF_REQUIRE_ASSISTANT=1 to enable). "
    "Not part of the cloud-dev happy path."
)


def _assistant_required() -> bool:
    raw = str(os.environ.get("AMOF_REQUIRE_ASSISTANT") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _build_image_targets(image_tag: Optional[str]) -> List[Dict[str, Optional[Any]]]:
    tag = str(image_tag or "").strip() or None
    registry_base = _registry_base_from_env()
    assistant_required = _assistant_required()
    targets: List[Dict[str, Optional[Any]]] = []
    for name in ("amof-controlplane", "amof-agent", "amof-dashboard", _ASSISTANT_IMAGE_NAME):
        repository = f"{registry_base}/{name}" if registry_base else None
        skipped = name == _ASSISTANT_IMAGE_NAME and not assistant_required
        target: Dict[str, Optional[Any]] = {
            "name": name,
            "repository": repository,
            "tag": tag,
            "image": f"{repository}:{tag}" if repository and tag else None,
            "built": not skipped,
            "skipped": skipped,
        }
        if skipped:
            target["skip_reason"] = _ASSISTANT_SKIP_REASON
        targets.append(target)
    return targets


def _split_built_and_skipped_targets(
    targets: Optional[List[Dict[str, Any]]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Partition image targets into actually-built vs intentionally skipped.

    Older lifecycle runs persisted images without ``built``/``skipped`` flags.
    Treat the absence of an explicit ``skipped=True`` as ``built`` so legacy
    evidence keeps rendering, while the assistant target is recognised as
    skipped if either flag is present.
    """
    built: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for entry in list(targets or []):
        if not isinstance(entry, dict):
            continue
        if bool(entry.get("skipped")):
            skipped.append(entry)
        else:
            built.append(entry)
    return built, skipped


def _build_source_repos(sources: Dict[str, Dict[str, Optional[str]]]) -> List[Dict[str, Optional[str]]]:
    source_rows: List[Dict[str, Optional[str]]] = []
    for repo_key, source in (
        ("amof", sources.get("amof", {})),
        ("amof-ui", sources.get("amof_ui", {})),
        ("amof-assistant", sources.get("assistant", {})),
    ):
        source_rows.append(
            {
                "repo": repo_key,
                "branch": str(source.get("branch") or "").strip() or None,
                "commit": str(source.get("commit") or "").strip() or None,
            }
        )
    return source_rows


def _run_latest_evidence(run: Any) -> Dict[str, Any]:
    loop_state = dict(getattr(run, "loop_state", {}) or {})
    return dict(loop_state.get("latest_evidence") or {})


def _build_release_builds_from_runs(name: str, run_mgr) -> List[Dict[str, Any]]:
    builds: List[Dict[str, Any]] = []
    for run in run_mgr.list_runs_summary(ecosystem=name, action="release/lifecycle/build", limit=200):
        latest_evidence = _run_latest_evidence(run)
        image_tag = str(latest_evidence.get("image_tag") or "").strip() or _release_build_image_tag(getattr(run, "command", []))
        build_id = image_tag or str(getattr(run, "run_id", "") or "").strip() or f"build-{len(builds) + 1}"
        all_images = list(latest_evidence.get("images") or [])
        built_images, skipped_images = _split_built_and_skipped_targets(all_images)
        builds.append(
            {
                "build_id": build_id,
                "image_tag": image_tag or "n/a",
                "status": str(getattr(run, "status", "") or "queued"),
                "run_id": getattr(run, "run_id", None),
                "source_branch": str(latest_evidence.get("source_branch") or "").strip() or None,
                "source_commit": str(latest_evidence.get("source_commit") or "").strip() or None,
                "sources": list(latest_evidence.get("sources") or []),
                "images": all_images,
                "built_images": built_images,
                "skipped_images": skipped_images,
                "builder": str(latest_evidence.get("builder") or "").strip() or None,
                "result": str(latest_evidence.get("result") or "").strip() or None,
                "created_at": getattr(run, "created_at", None),
                "started_at": getattr(run, "started_at", None),
                "finished_at": getattr(run, "finished_at", None),
            }
        )
    return builds


def _build_action_enabled() -> bool:
    build_backend = _get_build_backend()
    if build_backend != "cluster":
        return True
    return bool(str(os.environ.get("AMOF_CLUSTER_BUILDER_IMAGE") or "").strip())


def _release_panel_cards(build_enabled: bool) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = [
        {
            "id": "build_backend",
            "title": "Build backend",
            "subtitle": "Current backend {buildBackend}",
        },
        {
            "id": "build_source_commit",
            "title": "Pinned source commit",
            "subtitle": "Latest branch {sourceBranch}",
        },
    ]
    if build_enabled:
        cards.insert(
            1,
            {
                "id": "latest_successful_build",
                "title": "Latest successful build",
                "subtitle": "{latestBuildStatus}",
            }
        )
    cards.append(
        {
            "id": "registry_head",
            "title": "Registry head",
            "subtitle": "Releases reuse the latest saved ticket version unless you override it.",
        }
    )
    return cards


def _overview_panel_cards() -> List[Dict[str, Any]]:
    return [
        {
            "id": "current_release",
            "title": "Current release",
            "subtitle": "Last rollout {lastRollout}",
        },
        {
            "id": "candidate_release",
            "title": "Candidate release",
            "subtitle": "Behind latest: {behindLatest}",
        },
        {
            "id": "target_host",
            "title": "Primary target host",
            "subtitle": "{targetUrl}",
        },
        {
            "id": "deploy_profile",
            "title": "Deploy profile",
            "subtitle": "Profile {deployProfile}",
        },
        {
            "id": "observed_state",
            "title": "Observed state",
            "subtitle": "{observedDetail}",
        },
    ]


def _build_evidence_summary(build_enabled: bool, build_backend: str) -> Dict[str, Any]:
    present = [
        "Pinned source repos, branches, and commits",
        "Image target set",
        "Builder identity",
        "Result and timing fields for recorded lifecycle builds",
    ]
    missing: List[str] = []
    note = "Recorded lifecycle builds already persist reproducible git refs and evidence fields."
    if not build_enabled:
        missing = [
            "Dedicated cluster builder runtime with Docker and workspace access",
            "Cluster build-run evidence emitted by that dedicated runtime",
        ]
        note = (
            f"Build actions stay hidden because build.backend={build_backend} is selected but the cluster builder "
            "runtime is not configured. Set AMOF_CLUSTER_BUILDER_IMAGE and the related workspace/docker socket "
            "mount values to enable the dedicated build job path."
        )
    return {
        "present": present,
        "missing": missing,
        "note": note,
    }


def _append_supported_actions(existing: Optional[List[str]], *extra: Optional[str]) -> List[str]:
    supported: List[str] = []
    for action in list(existing or []) + [value for value in extra if value]:
        action_id = str(action or "").strip()
        if action_id and action_id not in supported:
            supported.append(action_id)
    return supported


def _primary_environment_hostname(endpoints: Any, observed_host: Optional[str] = None) -> Optional[str]:
    if observed_host:
        return str(observed_host).strip() or None
    if isinstance(endpoints, list):
        for entry in endpoints:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            if not url:
                continue
            parsed = urlparse(url)
            if parsed.hostname:
                return parsed.hostname
    return None


def _git_repo(
    repo_path: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo_path.resolve()}", *args],
        cwd=str(repo_path),
        text=True,
        capture_output=True,
    )


def _git_repo_ok(repo_path: Path, *args: str) -> str:
    completed = _git_repo(repo_path, *args)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "").strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _manifest_repo_path(name: str, repo_name: str, workspace_root: Path) -> Path:
    manifest = load_manifest(name)
    for repo in manifest.get("repos", []):
        if repo.get("name") == repo_name:
            return (workspace_root / str(repo.get("path") or f"repos/{repo_name}")).resolve()
    raise HTTPException(status_code=404, detail=f"Repo '{repo_name}' is not configured for ecosystem {name}.")


def _ticket_env_relpath(ticket_id: str) -> str:
    return f"{PROMOTION_ENV_PATH_PREFIX}{_slug(ticket_id)}.yaml"


def _ticket_env_context(repo_path: Path, ticket_id: str) -> Dict[str, str]:
    env_relpath = _ticket_env_relpath(ticket_id)
    try:
        payload = simple_parse_yaml(_git_repo_ok(repo_path, "show", f"HEAD:{env_relpath}"))
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Ticket env file for {ticket_id} was not found at {env_relpath}: {exc}")
    ticket = payload.get("ticket") if isinstance(payload, dict) else {}
    ticket = ticket if isinstance(ticket, dict) else {}
    candidate_branch = str(ticket.get("branch") or "").strip()
    source_sha = str(ticket.get("commitSha") or "").strip()
    if not candidate_branch or not source_sha:
        raise HTTPException(
            status_code=409,
            detail=f"Ticket env file {env_relpath} is missing ticket.branch or ticket.commitSha.",
        )
    gitops_commit_sha = _git_repo_ok(repo_path, "log", "-n", "1", "--format=%H", "--", env_relpath)
    return {
        "env_path": env_relpath,
        "candidate_branch": candidate_branch,
        "source_sha": source_sha,
        "gitops_commit_sha": gitops_commit_sha,
    }


def _promotion_action_context(name: str, body: Dict[str, Any]) -> Dict[str, str]:
    workspace_root = get_workspace_root()
    repo_name = str(body.get("repo") or "amof").strip() or "amof"
    ticket_id = str(body.get("ticket_id") or "").strip().upper()
    if not ticket_id or ticket_id == "MAIN":
        raise HTTPException(status_code=409, detail="Promotion actions require a non-main ticket context.")
    repo_path = _manifest_repo_path(name, repo_name, workspace_root)
    if not repo_path.exists():
        raise HTTPException(status_code=404, detail=f"Repo path does not exist: {repo_path}")
    env_context = _ticket_env_context(repo_path, ticket_id)
    fetch_ok, fetch_output = _fetch_origin_main(repo_path, workspace_root)
    if not fetch_ok:
        raise HTTPException(status_code=409, detail=f"Failed to refresh origin/main: {fetch_output or 'git fetch failed'}")
    expected_main_sha = _git_repo_ok(repo_path, "rev-parse", "origin/main^{commit}")
    return {
        "repo": repo_name,
        "ticket_id": ticket_id,
        "candidate_branch": str(body.get("candidate_branch") or env_context["candidate_branch"]).strip(),
        "source_sha": str(body.get("source_sha") or env_context["source_sha"]).strip(),
        "gitops_commit_sha": str(body.get("gitops_commit_sha") or env_context["gitops_commit_sha"]).strip(),
        "expected_main_sha": str(body.get("expected_main_sha") or expected_main_sha).strip(),
        "promotion_reason": (
            str(body.get("promotion_reason") or f"Lifecycle operation from environments UI for {ticket_id}").strip()
            or f"Lifecycle operation from environments UI for {ticket_id}"
        ),
    }


def _revert_action_context(name: str, body: Dict[str, Any]) -> Dict[str, str]:
    workspace_root = get_workspace_root()
    synthetic_commit_sha = str(body.get("synthetic_commit_sha") or "").strip()
    repo_name = str(body.get("repo") or "amof").strip() or "amof"
    if synthetic_commit_sha:
        return {"repo": repo_name, "synthetic_commit_sha": synthetic_commit_sha}
    promotion_context = _promotion_action_context(name, body)
    repo_path = _manifest_repo_path(name, promotion_context["repo"], workspace_root)
    promoted_commit_sha = _find_existing_promotion(
        repo_path,
        ref="origin/main",
        bundle_id="",
        source_sha=promotion_context["source_sha"],
        gitops_commit_sha=promotion_context["gitops_commit_sha"],
    )
    if not promoted_commit_sha:
        raise HTTPException(
            status_code=404,
            detail=(
                "No synthetic promotion commit for the current ticket bundle was found on origin/main. "
                "Provide synthetic_commit_sha explicitly if you want to revert a different promotion."
            ),
        )
    return {"repo": repo_name, "synthetic_commit_sha": promoted_commit_sha}


_BUILD_CONTRACT_KIND_UNSUPPORTED = "unsupported"
_BUILD_CONTRACT_KIND_MIRROR_UPSTREAM = "mirror_upstream"
_BUILD_CONTRACT_KIND_SOURCE_BUILD = "source_build"
_BUILD_CONTRACT_KINDS = frozenset(
    (
        _BUILD_CONTRACT_KIND_UNSUPPORTED,
        _BUILD_CONTRACT_KIND_MIRROR_UPSTREAM,
        _BUILD_CONTRACT_KIND_SOURCE_BUILD,
    )
)


_PUBLIC_RUNTIME_SURFACE_NOTE = (
    "Public AMOF canonical main intentionally excludes runtime build/deploy "
    "lanes. Those operator surfaces moved out of the public repo."
)


def _public_deploy_surface_enabled(name: str) -> bool:
    return False


def _ecosystem_build_contract(name: str) -> Dict[str, Any]:
    """Minimal per-ecosystem build contract.

    Each ecosystem declares one of three execution-oriented kinds:

    - ``source_build``  — AMOF runs a real first-party build for this
      ecosystem's images. The lifecycle ``build`` action is supported.
    - ``mirror_upstream`` — the ecosystem deploys upstream-provided images
      (e.g. via Argo CD pointed at a public registry). There is no AMOF-
      driven build, so the lifecycle ``build`` action is hidden.
    - ``unsupported`` — no build path of any kind is wired today; the
      lifecycle ``build`` action is hidden.

    This stays intentionally minimal: kind + produces/upstream metadata +
    a human-readable note. It is the contract the UI uses to decide
    whether to surface Build, and the contract the API uses to decide
    whether to admit a ``release/lifecycle/build`` request.
    """
    if name == "amof-platform":
        return {
            "kind": _BUILD_CONTRACT_KIND_UNSUPPORTED,
            "produces": [],
            "note": _PUBLIC_RUNTIME_SURFACE_NOTE,
        }
    if name == DEMO_MICROSAAS_ECOSYSTEM:
        return {
            "kind": _BUILD_CONTRACT_KIND_UNSUPPORTED,
            "produces": [],
            "note": _PUBLIC_RUNTIME_SURFACE_NOTE,
        }
    if name == "gmd":
        return {
            "kind": _BUILD_CONTRACT_KIND_MIRROR_UPSTREAM,
            "produces": [],
            "upstream": {
                "project": "googlecloudplatform/microservices-demo",
                "delivered_via": "Argo CD ApplicationSet at infrastructure/gitops/gmd/applicationset.yaml",
                "chart": "infrastructure/gitops/gmd/chart/",
            },
            "note": (
                "gmd deploys upstream googlecloudplatform/microservices-demo "
                "images via Argo CD. There is no first-party gmd-app build "
                "path in AMOF today, so Build is hidden."
            ),
        }
    return {
        "kind": _BUILD_CONTRACT_KIND_UNSUPPORTED,
        "produces": [],
        "note": (
            f"No build contract is wired for ecosystem '{name}'. Add an "
            "entry to _ecosystem_build_contract to expose Build for this "
            "ecosystem."
        ),
    }


def _build_target_truth(name: str) -> Dict[str, Any]:
    """Backwards-compatible projection of the build contract.

    Older clients read ``build_target_truth`` directly. New clients
    should read ``ecosystem_build_contract`` instead. Both are surfaced
    on lifecycle capabilities and stay consistent with each other.
    """
    contract = _ecosystem_build_contract(name)
    kind = contract.get("kind")
    builds_platform = kind == _BUILD_CONTRACT_KIND_SOURCE_BUILD
    builds_app = name == "amof-platform"
    if kind == _BUILD_CONTRACT_KIND_SOURCE_BUILD:
        if name == "amof-platform":
            note = (
                "This action builds the AMOF platform images "
                "(controlplane, agent, dashboard, optional assistant). For "
                "amof-platform this IS the app."
            )
        else:
            note = contract.get("note") or (
                "This action builds the AMOF platform images. It is the "
                "runtime substrate, not the ecosystem app."
            )
    elif kind == _BUILD_CONTRACT_KIND_MIRROR_UPSTREAM:
        note = contract.get("note") or (
            "This ecosystem mirrors upstream images; no first-party build "
            "is wired."
        )
    else:
        note = contract.get("note") or (
            f"No build contract is wired for ecosystem '{name}'."
        )
    return {
        "produces": list(contract.get("produces") or []),
        "builds_platform_substrate": builds_platform,
        "builds_ecosystem_app": builds_app,
        "ecosystem_app_build_contract": (
            "wired"
            if name == "amof-platform"
            else (
                "mirror_upstream"
                if kind == _BUILD_CONTRACT_KIND_MIRROR_UPSTREAM
                else "not_wired"
            )
        ),
        "note": note,
    }


def _lifecycle_capabilities(name: str) -> Dict[str, Any]:
    build_enabled = _build_action_enabled()
    build_backend = _get_build_backend()
    ecosystem_build_contract = _ecosystem_build_contract(name)
    contract_kind = str(ecosystem_build_contract.get("kind") or _BUILD_CONTRACT_KIND_UNSUPPORTED)
    build_visible_by_contract = contract_kind == _BUILD_CONTRACT_KIND_SOURCE_BUILD
    deploy_visible = _public_deploy_surface_enabled(name)
    # Build is only surfaced when (a) the build backend can run a build at
    # all and (b) the ecosystem's build contract actually declares a
    # source_build. mirror_upstream and unsupported ecosystems hide Build
    # so the operator never sees an action that would not produce that
    # ecosystem's own images.
    build_visible = build_enabled and build_visible_by_contract
    if not build_enabled:
        build_backend_note = (
            "Build is hidden because the cluster backend still needs a dedicated build pod plus explicit builder image/runtime configuration."
        )
    elif not build_visible_by_contract:
        build_backend_note = str(ecosystem_build_contract.get("note") or "").strip() or (
            f"Build is hidden because ecosystem '{name}' has build contract '{contract_kind}'. "
            "Build is only surfaced for ecosystems with contract kind 'source_build'."
        )
    else:
        build_backend_note = (
            "Builds resolve explicit git refs and run from detached worktrees before deploy or promote."
        )
    build_evidence_summary = _build_evidence_summary(build_visible, build_backend)
    build_target_truth = _build_target_truth(name)
    is_platform_ecosystem = name == "amof-platform"
    build_label = (
        "Build AMOF platform images" if not is_platform_ecosystem else "Build images"
    )
    build_activity_summary_suffix = (
        " (AMOF platform substrate)" if not is_platform_ecosystem else ""
    )
    if name == DEMO_MICROSAAS_ECOSYSTEM:
        capabilities = {
            "supported_actions": (
                ["build", "deploy"]
                if build_visible and deploy_visible
                else ["deploy"] if deploy_visible else []
            ),
            "activity_actions": (
                ["build", "deploy"]
                if build_visible and deploy_visible
                else ["deploy"] if deploy_visible else []
            ),
            "approval_actions": ["deploy"] if deploy_visible else [],
            "action_layout": (
                [{"id": "build", "icon": "hammer"}, {"id": "deploy", "icon": "arrow-right"}]
                if build_visible and deploy_visible
                else [{"id": "deploy", "icon": "arrow-right"}] if deploy_visible else []
            ),
            "release_panel": {"cards": _release_panel_cards(build_visible)},
            "overview_panel": {"cards": _overview_panel_cards()},
            "endpoints_panel": {
                "empty_state": "No endpoints published yet.",
                "label_fallback": "Endpoint",
            },
            "tab_layout": [
                {"id": "overview", "label": "Overview"},
                {"id": "endpoints", "label": "Endpoints"},
                {"id": "release", "label": "Release"},
                {"id": "logs", "label": "Logs"},
            ],
            "header_copy": {
                "release_fallback": "No deployed release",
                "namespace_fallback": "no namespace",
                "helm_release_fallback": "no helm release",
            },
            "stage_shell": {
                "environment_count": "{count} env",
                "metric_labels": {
                    "healthy": "Healthy",
                    "deploying": "Deploying",
                    "drift": "Drift",
                },
            },
            "insights_panel": {
                "promotion_model_title": "Promotion model",
                "promotion_model_body": (
                    "Releases move forward by ID. DEV validates a candidate release, and STAGE consumes "
                    "the exact same release object instead of rebuilding or forking state."
                ),
                "cross_stage_notes_title": "Cross-environment notes",
                "cross_stage_notes_empty": "No aggregated rollout notes yet.",
            },
            "logs_panel": {
                "no_live_run": "No live run selected",
                "terminal_title": "Lifecycle run log",
                "terminal_empty": (
                    "No lifecycle log yet. Build or deploy from the Release tab to stream logs here."
                    if build_visible and deploy_visible
                    else "No public lifecycle deploy/build log is available on canonical main."
                ),
                "analyzed_notes_empty": "No analyzed rollout notes yet.",
            },
            "action_copy": (
                {
                    "build": {
                        "label": build_label,
                        "pending_label": "Building...",
                        "activity_summary": "Build requested for {environmentId} ({publicHost})"
                        + build_activity_summary_suffix,
                    },
                    "deploy": {
                        "label": "Deploy candidate",
                        "pending_label": "Deploying...",
                        "activity_summary": "Deploy requested for {environmentId} ({publicHost})",
                        "approval_title": "Review deploy to {environmentId} ({publicHost})",
                        "approval_summary": "A second operator can confirm the deploy request for {environmentId} on {publicUrl}.",
                    },
                }
                if build_visible and deploy_visible
                else {
                    "deploy": {
                        "label": "Deploy candidate",
                        "pending_label": "Deploying...",
                        "activity_summary": "Deploy requested for {environmentId} ({publicHost})",
                        "approval_title": "Review deploy to {environmentId} ({publicHost})",
                        "approval_summary": "A second operator can confirm the deploy request for {environmentId} on {publicUrl}.",
                    },
                }
                if deploy_visible
                else {}
            ),
            "surface_description": (
                "Compact demo lifecycle surface with truthful build and deploy lanes plus live rollout logs."
                if build_visible and deploy_visible
                else "Compact demo lifecycle surface is read-only on public canonical main."
            ),
            "build_backend": build_backend,
            "build_backend_note": build_backend_note,
            "build_evidence_summary": build_evidence_summary,
            "build_target_truth": build_target_truth,
            "ecosystem_build_contract": ecosystem_build_contract,
            "release_note": (
                "This demo lane currently supports build and deploy only. "
                "Promote, rollback, discard, and manual release-tag steps stay hidden until the backend contract exists."
                if build_visible and deploy_visible
                else _PUBLIC_RUNTIME_SURFACE_NOTE
            ),
        }
        return capabilities
    supported_actions = ["promote"]
    activity_actions = ["promote"]
    if deploy_visible:
        supported_actions = ["deploy", *supported_actions]
        activity_actions = ["deploy", *activity_actions]
    if build_visible and deploy_visible:
        supported_actions = ["build", *supported_actions]
        activity_actions = ["build", *activity_actions]
    capabilities = {
        "supported_actions": supported_actions,
        "activity_actions": activity_actions,
        "approval_actions": ["promote"] if not deploy_visible else ["deploy", "promote"],
        "action_layout": (
            [
                {"id": "build", "icon": "hammer"},
                {"id": "deploy", "icon": "arrow-right"},
                {"id": "promote", "icon": "arrow-right"},
            ]
            if build_visible and deploy_visible
            else (
                [{"id": "deploy", "icon": "arrow-right"}, {"id": "promote", "icon": "arrow-right"}]
                if deploy_visible
                else [{"id": "promote", "icon": "arrow-right"}]
            )
        ),
        "release_panel": {"cards": _release_panel_cards(build_visible)},
        "overview_panel": {"cards": _overview_panel_cards()},
        "endpoints_panel": {
            "empty_state": "No endpoints published yet.",
            "label_fallback": "Endpoint",
        },
        "tab_layout": [
            {"id": "overview", "label": "Overview"},
            {"id": "endpoints", "label": "Endpoints"},
            {"id": "release", "label": "Release"},
            {"id": "logs", "label": "Logs"},
        ],
        "header_copy": {
            "release_fallback": "No deployed release",
            "namespace_fallback": "no namespace",
            "helm_release_fallback": "no helm release",
        },
        "stage_shell": {
            "environment_count": "{count} env",
            "metric_labels": {
                "healthy": "Healthy",
                "deploying": "Deploying",
                "drift": "Drift",
            },
        },
        "insights_panel": {
            "promotion_model_title": "Promotion model",
            "promotion_model_body": (
                "Releases move forward by ID. DEV validates a candidate release, and STAGE consumes "
                "the exact same release object instead of rebuilding or forking state."
            ),
            "cross_stage_notes_title": "Cross-environment notes",
            "cross_stage_notes_empty": "No aggregated rollout notes yet.",
        },
        "logs_panel": {
            "no_live_run": "No live run selected",
            "terminal_title": "Lifecycle run log",
            "terminal_empty": (
                "No lifecycle log yet. Build, deploy, or promote from the Release tab to stream logs here."
                if build_visible and deploy_visible
                else (
                    "No lifecycle log yet. Deploy or promote from the Release tab to stream logs here."
                    if deploy_visible
                    else "No public lifecycle deploy/build log is available on canonical main."
                )
            ),
            "analyzed_notes_empty": "No analyzed rollout notes yet.",
        },
        "action_copy": (
            {
                "build": {
                    "label": build_label,
                    "pending_label": "Building...",
                    "activity_summary": "Build requested for {environmentId}"
                    + build_activity_summary_suffix,
                },
                "deploy": {
                    "label": "Deploy candidate",
                    "pending_label": "Deploying...",
                    "activity_summary": "Deploy requested for {environmentId}",
                    "approval_title": "Review deploy to {environmentId}",
                    "approval_summary": "A second operator can confirm the deploy request for {environmentId}.",
                },
                "promote": {
                    "label": "Promote to {target}",
                    "pending_label": "Promoting...",
                    "activity_summary": "Promote requested from {environmentId} to {target}",
                    "approval_title": "Review promote to {target}",
                    "approval_summary": "A second operator can confirm the promote request from {environmentId} to {target}.",
                },
            }
            if build_visible and deploy_visible
            else ({
                "deploy": {
                    "label": "Deploy candidate",
                    "pending_label": "Deploying...",
                    "activity_summary": "Deploy requested for {environmentId}",
                    "approval_title": "Review deploy to {environmentId}",
                    "approval_summary": "A second operator can confirm the deploy request for {environmentId}.",
                },
                "promote": {
                    "label": "Promote to {target}",
                    "pending_label": "Promoting...",
                    "activity_summary": "Promote requested from {environmentId} to {target}",
                    "approval_title": "Review promote to {target}",
                    "approval_summary": "A second operator can confirm the promote request from {environmentId} to {target}.",
                },
            } if deploy_visible else {
                "promote": {
                    "label": "Promote to {target}",
                    "pending_label": "Promoting...",
                    "activity_summary": "Promote requested from {environmentId} to {target}",
                    "approval_title": "Review promote to {target}",
                    "approval_summary": "A second operator can confirm the promote request from {environmentId} to {target}.",
                },
            })
        ),
        "surface_description": (
            "DB-backed lifecycle surface for local build, deploy, and promote with live updates."
            if build_visible and deploy_visible
            else (
                "DB-backed lifecycle surface for deploy and promote with live updates."
                if deploy_visible
                else "DB-backed lifecycle surface for promote-only governance actions on public canonical main."
            )
        ),
        "build_backend": build_backend,
        "build_backend_note": build_backend_note,
        "build_evidence_summary": build_evidence_summary,
        "build_target_truth": build_target_truth,
        "ecosystem_build_contract": ecosystem_build_contract,
        "release_note": (
            "This lane currently supports build, deploy, and promote only. Tag, rollback, and discard stay hidden until the backend lifecycle contract exists."
            if build_visible and deploy_visible
            else (
                "This lane currently supports promote only. Public canonical main does not expose deploy/build runtime lanes."
                if not deploy_visible
                else (
                f"This lane currently supports deploy and promote only. Build is hidden because ecosystem '{name}' has build contract '{contract_kind}' (only 'source_build' surfaces Build). Tag, rollback, and discard stay hidden until the backend lifecycle contract exists."
                if not build_visible_by_contract
                else "This lane currently supports deploy and promote only. Build is hidden because build.backend=cluster requires a dedicated build pod, and tag, rollback, and discard stay hidden until the backend lifecycle contract exists."
                )
            )
        ),
    }
    return capabilities


def _ensure_lifecycle_action_supported(name: str, action: str) -> None:
    action_lower = str(action or "").strip().lower()
    supported_actions = _lifecycle_capabilities(name).get("supported_actions") or []
    if action_lower in supported_actions:
        return
    unavailable_note = None
    if action_lower == "build":
        contract_kind = str(_ecosystem_build_contract(name).get("kind") or _BUILD_CONTRACT_KIND_UNSUPPORTED)
        if not _build_action_enabled():
            unavailable_note = (
                "Build is hidden because build.backend=cluster requires a dedicated build pod with Docker and the workspace."
            )
        elif contract_kind != _BUILD_CONTRACT_KIND_SOURCE_BUILD:
            unavailable_note = (
                f"Build is hidden because ecosystem '{name}' has build contract '{contract_kind}'. "
                "Build is only surfaced for ecosystems whose contract kind is 'source_build'."
            )
    elif action_lower == "deploy":
        unavailable_note = _PUBLIC_RUNTIME_SURFACE_NOTE
    elif action_lower in {"tag", "promote", "rollback", "discard"}:
        unavailable_note = (
            "Tag, promote, rollback, and discard stay hidden until the backend lifecycle contract exists."
        )
    raise HTTPException(
        status_code=409,
        detail=(
            f"Lifecycle action '{action_lower}' is not supported for {name}. "
            f"Supported actions: {', '.join(supported_actions) or 'none'}."
            + (f" {unavailable_note}" if unavailable_note else "")
        ),
    )


def _build_lifecycle_stages(name: str, registry: Dict[str, Any]) -> List[Dict[str, Any]]:
    latest_release = registry["releases"][0] if registry.get("releases") else None
    profiles = _read_environment_profiles(name)
    base_lifecycle_capabilities = deepcopy(_lifecycle_capabilities(name))
    demo_argocd_settings = load_argocd_settings() if name == DEMO_MICROSAAS_ECOSYSTEM else None
    if not registry.get("releases"):
        release_panel = dict(base_lifecycle_capabilities.get("release_panel") or {})
        release_panel["cards"] = [
            card
            for card in list(release_panel.get("cards") or [])
            if str(card.get("id") or "") != "registry_head"
        ]
        base_lifecycle_capabilities["release_panel"] = release_panel
    environment_rows: List[Dict[str, Any]] = []
    for profile in profiles:
        status = str(profile.get("status") or "healthy")
        environment_id = str(profile.get("id") or profile.get("stage_id") or "dev")
        stage_id = str(profile.get("stage_id") or environment_id)
        current_release = latest_release if latest_release else None
        candidate_release = latest_release if latest_release else None
        live_probe = _amof_platform_probe(profile) if name == "amof-platform" else None
        ai_log_chunks = profile.get("ai_log_chunks") if isinstance(profile.get("ai_log_chunks"), list) else []
        endpoints = profile.get("endpoints") if isinstance(profile.get("endpoints"), list) else []
        ticket_id = str(profile.get("ticket_id") or profile.get("ticket_label") or "main").strip() or "main"
        environment_mode = (
            str(profile.get("environment_mode") or "").strip()
            or ("shared_namespace" if ticket_id != "main" else "shared_stage")
        )
        environment_target = str(profile.get("environment_target") or environment_id).strip() or environment_id
        cluster_name = str(profile.get("cluster_name") or profile.get("deploy_profile") or "").strip() or None
        source_sha = str(profile.get("source_sha") or "").strip() or None
        argo_app = str(profile.get("argo_app") or "").strip() or None
        observed_host = str((live_probe or {}).get("host") or "").strip() or None
        hostname = _primary_environment_hostname(endpoints, observed_host)
        lifecycle_capabilities = deepcopy(base_lifecycle_capabilities)
        if isinstance(live_probe, dict):
            probe_detail = str(live_probe.get("detail") or "").strip()
            if probe_detail and probe_detail not in ai_log_chunks:
                ai_log_chunks = [*ai_log_chunks, probe_detail]
        environment_rows.append(
            {
                "profile": profile,
                "environment": {
                    "id": environment_id,
                    "stage_id": stage_id,
                    "ticket_id": ticket_id,
                    "label": str(profile.get("label") or environment_id),
                    "promotion_target": profile.get("promotion_target"),
                    "ticket_label": str(profile.get("ticket_label") or "main"),
                    "environment_mode": environment_mode,
                    "environment_target": environment_target,
                    "release": latest_release.get("version") if latest_release else None,
                    "workspace": str(profile.get("workspace") or f"{name} / main"),
                    "repo_summary": str(profile.get("repo_summary") or "Shared workspace environment"),
                    "status": status,
                    "cluster_name": cluster_name,
                    "deploy_profile": profile.get("deploy_profile"),
                    "namespace": profile.get("namespace"),
                    "hostname": hostname,
                    "source_sha": source_sha,
                    "argo_app": argo_app,
                    "helm_release": profile.get("helm_release"),
                    "endpoints": endpoints,
                    "lifecycle_capabilities": lifecycle_capabilities,
                    "ai_log_chunks": ai_log_chunks,
                    "observed_state": (live_probe or {}).get("state") if isinstance(live_probe, dict) else None,
                    "observed_detail": (live_probe or {}).get("detail") if isinstance(live_probe, dict) else None,
                    "observed_checked_at": (live_probe or {}).get("checked_at") if isinstance(live_probe, dict) else None,
                    "observed_image_tag": (live_probe or {}).get("image_tag") if isinstance(live_probe, dict) else None,
                    "current_deployment": None,
                    "current_release": current_release,
                    "candidate_release": candidate_release,
                    "last_successful_deployment": None,
                },
            }
        )
    if name == DEMO_MICROSAAS_ECOSYSTEM:
        for row in environment_rows:
            profile = row["profile"]
            env = row["environment"]
            probe = _demo_microsaas_probe(profile)
            latest_release = registry["releases"][0] if registry.get("releases") else None
            env["candidate_release"] = latest_release
            env["observed_state"] = probe.get("state")
            env["observed_detail"] = probe.get("detail")
            env["observed_checked_at"] = probe.get("checked_at")
            env["observed_image_tag"] = probe.get("image_tag")
            public_base_url = _demo_microsaas_public_base_url(
                profile=profile,
                probe_host=probe.get("host"),
                stage_id=str(env.get("stage_id") or env.get("id") or "dev"),
            )
            if public_base_url:
                env["endpoints"] = [{"label": "Microsaas Demo", "url": public_base_url}]
                env["hostname"] = _primary_environment_hostname(env.get("endpoints"), probe.get("host"))
                ai_log_chunks = env["ai_log_chunks"] if isinstance(env.get("ai_log_chunks"), list) else []
                public_lane_note = f"Public lane: {public_base_url}"
                if public_lane_note not in ai_log_chunks:
                    env["ai_log_chunks"] = [*ai_log_chunks, public_lane_note]
            env["argo_app"] = demo_microsaas_app_name(
                str(env.get("id") or env.get("stage_id") or "dev"),
                demo_argocd_settings,
            )
            if probe.get("exists") and probe.get("image_tag"):
                current_release = _release_payload_from_tag(
                    str(probe["image_tag"]), created_at=str(probe.get("finished_at") or "").strip() or None
                )
                rollout_notes = [f"Live cluster image: {probe['image']}"]
                if public_base_url:
                    rollout_notes.append(f"Public lane: {public_base_url}")
                current_deployment = {
                    "deployment_id": f"{env['id']}-{probe['image_tag']}",
                    "environment_id": env["id"],
                    "release_id": current_release["release_id"],
                    "action": "deploy",
                    "status": "success" if probe.get("ready") else "running",
                    "run_id": None,
                    "finished_at": probe.get("finished_at"),
                    "rollout_notes_json": rollout_notes,
                }
                env["status"] = "healthy" if probe.get("ready") else "deploying"
                env["current_release"] = current_release
                env["current_deployment"] = current_deployment
                env["last_successful_deployment"] = current_deployment if probe.get("ready") else None
    stage_groups: Dict[str, List[Dict[str, Any]]] = {}
    stage_first_index: Dict[str, int] = {}
    for index, row in enumerate(environment_rows):
        stage_id = str(row["environment"].get("stage_id") or row["environment"].get("id") or "dev")
        if stage_id not in stage_groups:
            stage_groups[stage_id] = []
            stage_first_index[stage_id] = index
        stage_groups[stage_id].append(row)
    ordered_stage_ids = sorted(
        stage_groups.keys(),
        key=lambda stage_id: (
            AUTHORITATIVE_STAGE_IDS.index(stage_id) if stage_id in AUTHORITATIVE_STAGE_IDS else len(AUTHORITATIVE_STAGE_IDS),
            stage_first_index.get(stage_id, 0),
        ),
    )
    stages: List[Dict[str, Any]] = []
    for stage_id in ordered_stage_ids:
        rows = stage_groups[stage_id]
        first_profile = rows[0]["profile"]
        environments = [row["environment"] for row in rows]
        metrics = {
            "healthy": sum(1 for env in environments if str(env.get("status") or "") == "healthy"),
            "deploying": sum(1 for env in environments if str(env.get("status") or "") in {"deploying", "running"}),
            "drift": sum(1 for env in environments if str(env.get("status") or "") in {"drift", "failed"}),
        }
        stages.append(
            {
                "id": stage_id,
                "title": str(first_profile.get("title") or stage_id.upper()),
                "summary": str(first_profile.get("summary") or "Environment lane for shared validation and promotion."),
                "promotion_target": first_profile.get("promotion_target") or _promotion_target_for_stage(stage_id),
                "metrics": metrics,
                "environment": environments[0],
                "environments": environments,
            }
        )
    return stages


def _compat_registry_payload(name: str, run_mgr=None) -> Dict[str, Any]:
    latest_validation_run = _latest_validation_run_summary(name, run_mgr)
    latest_explicit_validation_run = _latest_explicit_validation_run_summary(name, run_mgr)
    if name == DEMO_MICROSAAS_ECOSYSTEM:
        root = get_workspace_root()
        profiles = _read_environment_profiles(name)
        default_profile = next(
            (
                profile
                for profile in profiles
                if str(profile.get("id") or profile.get("stage_id") or "").strip() == "dev"
            ),
            profiles[0] if profiles else {"id": "dev", "stage_id": "dev", "namespace": "demo-microsaas"},
        )
        tags: List[str] = []
        probe = _demo_microsaas_probe(default_profile)
        current_tag = str(probe.get("image_tag") or "").strip()
        default_tag = _demo_microsaas_default_image_tag()
        for tag in [current_tag, default_tag]:
            if tag and tag not in tags:
                tags.append(tag)
        releases = []
        for tag in tags:
            created_at = None
            if tag == current_tag:
                created_at = str(probe.get("finished_at") or "").strip() or None
            releases.append(_release_payload_from_tag(tag, created_at=created_at))
        latest = releases[0]["version"] if releases else None
        return {
            "ecosystem": name,
            "generated_at": _now_iso(),
            "releases": releases,
            "promotion_history": [],
            "rollback_targets": [],
            "summary": _release_validation_summary(
                name,
                root,
                run_mgr,
                latest,
                latest_validation_run,
                latest_explicit_validation_run,
                allow_materialize=True,
            ),
        }
    root = get_workspace_root()
    _, status_output = _run_release_capture(name, ["status"], root)
    _, log_output = _run_release_capture(name, ["log"], root)
    versions = _extract_versions("\n".join([status_output, log_output]))
    releases = [_release_payload_from_tag(version) for version in versions]
    latest = releases[0]["version"] if releases else None
    return {
        "ecosystem": name,
        "generated_at": _now_iso(),
        "releases": releases,
        "promotion_history": [],
        "rollback_targets": [],
        "summary": _release_validation_summary(
            name,
            root,
            run_mgr,
            latest,
            latest_validation_run,
            latest_explicit_validation_run,
            allow_materialize=True,
        ),
    }


@router.get("/{name}/release/status")
def release_status(name: str) -> Dict[str, Any]:
    """Current version info and next version options."""
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    root = get_workspace_root()
    code, output = _run_release_capture(name, ["status"], root)
    return {"ok": code == 0, "output": output, "exit_code": code}


@router.get("/{name}/release/log")
def release_log(name: str) -> Dict[str, Any]:
    """Release history (releases/*.json or git tags)."""
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    root = get_workspace_root()
    code, output = _run_release_capture(name, ["log"], root)
    return {"ok": code == 0, "output": output, "exit_code": code}


@router.get("/{name}/release/overview")
def release_overview(name: str, run_mgr=Depends(get_run_manager)) -> Dict[str, Any]:
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    return build_lifecycle_overview_payload(name, run_mgr)


def build_lifecycle_overview_payload(name: str, run_mgr=None) -> Dict[str, Any]:
    registry = _compat_registry_payload(name, run_mgr)
    latest_release = registry["releases"][0] if registry["releases"] else None
    return {
        "ecosystem": name,
        "generated_at": registry["generated_at"],
        "latest_release": latest_release,
        "releases": registry["releases"],
        "stages": _build_lifecycle_stages(name, registry),
        "summary": registry["summary"],
    }


def build_lifecycle_environment_index(name: str, run_mgr=None) -> Dict[str, Dict[str, Any]]:
    overview = build_lifecycle_overview_payload(name, run_mgr)
    environment_index: Dict[str, Dict[str, Any]] = {}
    for stage in overview.get("stages") or []:
        environments = stage.get("environments") or []
        for environment in environments:
            if not isinstance(environment, dict):
                continue
            environment_id = str(environment.get("id") or "").strip()
            stage_id = str(environment.get("stage_id") or "").strip()
            if environment_id and environment_id not in environment_index:
                environment_index[environment_id] = environment
            if stage_id and stage_id not in environment_index:
                environment_index[stage_id] = environment
    return environment_index


def _resolve_lifecycle_environment(name: str, body: Dict[str, Any], run_mgr=None) -> Dict[str, Any]:
    environment_id = str(body.get("environment_id") or body.get("to_environment_id") or "").strip()
    stage_id = str(body.get("stage_id") or "").strip()
    environment_index = build_lifecycle_environment_index(name, run_mgr)
    environment = None
    if environment_id:
        environment = environment_index.get(environment_id)
    if environment is None and stage_id:
        environment = environment_index.get(stage_id)
    if not isinstance(environment, dict):
        lookup = environment_id or stage_id or "<missing>"
        raise HTTPException(status_code=404, detail=f"Lifecycle environment '{lookup}' was not found.")
    return environment


def _resolve_lifecycle_environment_lookup(
    name: str,
    *,
    lookup: Optional[str],
    run_mgr=None,
) -> Optional[Dict[str, Any]]:
    normalized_lookup = str(lookup or "").strip() or None
    if not normalized_lookup:
        return None
    environment = build_lifecycle_environment_index(name, run_mgr).get(normalized_lookup)
    if not isinstance(environment, dict):
        raise HTTPException(status_code=404, detail=f"Lifecycle environment '{normalized_lookup}' was not found.")
    return environment


def _target_field(environment: Dict[str, Any], key: str) -> Optional[str]:
    value = str(environment.get(key) or "").strip()
    return value or None


def _assert_target_profile_matches_request(
    *,
    action: str,
    explicit_profile: Optional[str],
    target_profile: Optional[str],
    lookup: str,
) -> None:
    if explicit_profile and target_profile and explicit_profile != target_profile:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Lifecycle action '{action}' requested profile '{explicit_profile}', "
                f"but target '{lookup}' resolves to deploy_profile '{target_profile}'."
            ),
        )


def _require_target_contract(
    *,
    action: str,
    ecosystem: str,
    lookup: str,
    environment: Dict[str, Any],
    fields: List[str],
) -> None:
    missing = [field for field in fields if not _target_field(environment, field)]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Lifecycle action '{action}' for ecosystem '{ecosystem}' cannot use target '{lookup}' "
                f"because it is missing required fields: {', '.join(missing)}."
            ),
        )


@router.get("/{name}/release/registry")
def release_registry(name: str, run_mgr=Depends(get_run_manager)) -> Dict[str, Any]:
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    return _compat_registry_payload(name, run_mgr)


@router.get("/{name}/release/builds")
def release_builds(name: str, run_mgr=Depends(get_run_manager)) -> Dict[str, Any]:
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    if "build" not in (_lifecycle_capabilities(name).get("supported_actions") or []):
        return {"builds": []}
    builds = _build_release_builds_from_runs(name, run_mgr)
    if builds:
        return {"builds": builds}
    return {"builds": _build_release_builds(_compat_registry_payload(name, run_mgr))}


def _get_build_backend() -> str:
    return (os.environ.get("AMOF_BUILD_BACKEND") or "local").strip().lower()


def _truthy_request_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _active_lifecycle_build_for_lane(
    run_mgr: Any, name: str, environment_id: Optional[str], stage_id: Optional[str]
) -> Optional[Any]:
    """Find an in-flight ``release/lifecycle/build`` run for the same lane.

    The QueueDispatcher worker is single-threaded and the cluster builder runs
    one builder pod per build, so two concurrent build requests for the same
    environment cannot both be honestly "running": one of them is at most
    queued behind the other. Surface the existing run instead of pretending
    that two independent builds are in flight.
    """
    if run_mgr is None:
        return None
    try:
        candidates = run_mgr.list_runs_summary(
            ecosystem=name,
            action="release/lifecycle/build",
            limit=20,
        )
    except Exception:
        return None
    target_env = (environment_id or "").strip() or None
    target_stage = (stage_id or "").strip() or None
    for run in candidates or []:
        status = str(getattr(run, "status", "") or "").strip().lower()
        if status not in {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}:
            continue
        if not (target_env or target_stage):
            return run
        evidence = _run_latest_evidence(run)
        run_env = str(evidence.get("environment_id") or "").strip() or None
        run_stage = str(evidence.get("stage_id") or "").strip() or None
        if target_env and run_env and target_env == run_env:
            return run
        if target_stage and run_stage and target_stage == run_stage:
            return run
        # Older builds did not record a lane on the evidence payload. Treat
        # any active build as colliding with a build that also did not pin a
        # lane — there is still only one builder pod available globally.
        if not run_env and not run_stage and not target_env and not target_stage:
            return run
    return None


@router.post("/{name}/release/lifecycle/{action}", dependencies=[Depends(require_step_up_user)])
def release_lifecycle_action(
    name: str,
    action: str,
    body: Dict[str, Any],
    dispatcher: QueueDispatcher = Depends(get_queue_dispatcher),
) -> Dict[str, Any]:
    """Run build or deploy script in the background; stream output via GET /runs/{run_id} and /runs/{run_id}/events or /stream."""
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    root = get_workspace_root()
    action_lower = (action or "").strip().lower()
    _ensure_lifecycle_action_supported(name, action_lower)
    stage_id = (body.get("stage_id") or "").strip() or None
    matched_profile: Optional[Dict[str, Any]] = None
    environment_id = str(body.get("environment_id") or body.get("to_environment_id") or stage_id or "").strip() or None
    run_mgr = getattr(dispatcher, "run_manager", None)
    demo_public_base_url: Optional[str] = None
    demo_public_host: Optional[str] = None
    demo_argocd_settings = load_argocd_settings() if name == DEMO_MICROSAAS_ECOSYSTEM else None
    if name == DEMO_MICROSAAS_ECOSYSTEM:
        demo_profiles = _read_environment_profiles(name)
        profile_stage_id = stage_id or environment_id or "dev"
        for profile in demo_profiles:
            if str(profile.get("stage_id") or profile.get("id") or "") == profile_stage_id:
                matched_profile = profile
                break
        environment_id = str(environment_id or (matched_profile or {}).get("id") or profile_stage_id).strip() or profile_stage_id
        demo_public_base_url = (
            _normalize_public_base_url(body.get("public_base_url"))
            or _demo_microsaas_configured_public_base_url(matched_profile, stage_id=profile_stage_id)
            or _demo_microsaas_default_public_base_url(profile=matched_profile, stage_id=profile_stage_id)
        )
        demo_public_host = _public_host_from_base_url(demo_public_base_url)
    if action_lower == "build":
        build_backend = _get_build_backend()
        requested_profile = str(body.get("profile") or "").strip() or None
        build_target_lookup = environment_id or stage_id
        if name != DEMO_MICROSAAS_ECOSYSTEM and build_target_lookup:
            build_target = _resolve_lifecycle_environment(name, body, run_mgr)
            stage_id = _target_field(build_target, "stage_id") or stage_id
            environment_id = _target_field(build_target, "id") or environment_id
            _require_target_contract(
                action=action_lower,
                ecosystem=name,
                lookup=build_target_lookup,
                environment=build_target,
                fields=["deploy_profile"],
            )
            build_profile = _target_field(build_target, "deploy_profile")
            _assert_target_profile_matches_request(
                action=action_lower,
                explicit_profile=requested_profile,
                target_profile=build_profile,
                lookup=build_target_lookup,
            )
        else:
            build_profile = (
                requested_profile
                or str(os.environ.get("AMOF_RELEASE_PROFILE") or "cloud-dev").strip()
                or "cloud-dev"
            )
        force_build = _truthy_request_flag(body.get("force"))
        admission_run_mgr = run_mgr
        if not force_build:
            existing_active_build = _active_lifecycle_build_for_lane(
                admission_run_mgr, name, environment_id, stage_id
            )
            if existing_active_build is not None:
                existing_run_id = getattr(existing_active_build, "run_id", None)
                existing_status = str(getattr(existing_active_build, "status", "") or RUN_STATUS_QUEUED)
                lane_label = (environment_id or stage_id or "this environment").strip() or "this environment"
                return {
                    "run_id": existing_run_id,
                    "status": existing_status,
                    "deduped": True,
                    "reason": "active_build_for_lane",
                    "message": (
                        f"A {existing_status} build is already in flight for "
                        f"{lane_label} (run_id={existing_run_id}). Pass force=true to start a parallel build."
                    ),
                    "active_run_id": existing_run_id,
                }
        from amof.api.command_builder import (
            build_cluster_lifecycle_build_command,
            build_lifecycle_build_command,
            resolve_lifecycle_build_sources,
        )
        image_tag = body.get("image_tag") or body.get("build_id")
        build_sources = resolve_lifecycle_build_sources(root)
        build_image_tag = str(image_tag).strip() if image_tag else None
        build_source_rows = _build_source_repos(build_sources)
        amof_source = build_sources.get("amof", {})
        amof_ui_source = build_sources.get("amof_ui", {})
        assistant_source = build_sources.get("assistant", {})
        amof_branch = str(amof_source.get("branch") or "").strip() or None
        amof_commit = str(amof_source.get("commit") or "").strip() or None
        amof_ui_branch = str(amof_ui_source.get("branch") or "").strip() or None
        amof_ui_commit = str(amof_ui_source.get("commit") or "").strip() or None
        assistant_branch = str(assistant_source.get("branch") or "").strip() or None
        assistant_commit = str(assistant_source.get("commit") or "").strip() or None
        source_branch = str(body.get("source_branch") or amof_branch or "").strip() or None
        # Default lifecycle builds to approved canonical branch refs. The
        # provenance gate must stay strict; using pinned HEAD SHAs here makes a
        # canonical local main checkout inadmissible because explicit-ref mode
        # only approves main/release/tag refs, not raw commits.
        amof_ref = str(body.get("amof_ref") or amof_branch or "").strip() or None
        amof_ui_ref = str(body.get("amof_ui_ref") or amof_ui_branch or "").strip() or None
        assistant_ref = str(body.get("assistant_ref") or assistant_branch or "").strip() or None
        try:
            if build_backend == "cluster":
                cmd, cwd = build_cluster_lifecycle_build_command(
                    root,
                    image_tag=str(image_tag) if image_tag else None,
                    no_push=bool(body.get("no_push")),
                    profile=build_profile,
                    source_branch=source_branch,
                    amof_ref=amof_ref,
                    amof_ui_ref=amof_ui_ref,
                    assistant_ref=assistant_ref,
                )
            else:
                cmd, cwd = build_lifecycle_build_command(
                    root,
                    image_tag=str(image_tag) if image_tag else None,
                    no_push=bool(body.get("no_push")),
                    profile=build_profile,
                    source_mode="canonical",
                    source_branch=source_branch,
                    amof_ref=amof_ref,
                    amof_ui_ref=amof_ui_ref,
                    assistant_ref=assistant_ref,
                )
        except (FileNotFoundError, RuntimeError) as e:
            raise HTTPException(status_code=501, detail=str(e))
    elif action_lower in {"deploy", "promote"}:
        _ensure_release_validate_passes(name, root)
        if name == DEMO_MICROSAAS_ECOSYSTEM:
            from amof.api.command_builder import build_demo_microsaas_deploy_command
        else:
            from amof.api.command_builder import build_lifecycle_deploy_command
        registry = _compat_registry_payload(name)
        # Resolve image_tag: prefer explicit image_tag; else treat release_id as the stable tag.
        # Legacy compat-* aliases still map by registry order for older callers.
        raw_tag = body.get("image_tag") or body.get("release_id")
        if raw_tag and str(raw_tag).startswith("compat-"):
            try:
                idx = int(str(raw_tag).replace("compat-", "").strip())
                releases = registry.get("releases") or []
                if 1 <= idx <= len(releases):
                    raw_tag = releases[idx - 1].get("image_tag") or releases[idx - 1].get("version")
            except (ValueError, TypeError):
                pass
        image_tag = str(raw_tag).strip() if raw_tag else None
        raw_digest = body.get("image_digest") or body.get("digest")
        image_digest = str(raw_digest).strip() if raw_digest else None
        deploy_profile = "cloud-dev"
        release_name = "amof"
        namespace = "amof-system"
        host = None
        from_environment_id = str(body.get("from_environment_id") or "").strip() or None
        target_environment_id = str(body.get("to_environment_id") or body.get("environment_id") or "").strip() or None
        target_profile_key = target_environment_id or stage_id
        matched_target_profile: Optional[Dict[str, Any]] = None
        if target_profile_key:
            for p in demo_profiles if name == DEMO_MICROSAAS_ECOSYSTEM else _read_environment_profiles(name):
                profile_stage_id = str(p.get("stage_id") or "").strip()
                profile_id = str(p.get("id") or "").strip()
                if target_profile_key in {profile_stage_id, profile_id}:
                    matched_target_profile = p
                    matched_profile = p if name == DEMO_MICROSAAS_ECOSYSTEM else matched_profile
                    deploy_profile = str(p.get("deploy_profile") or body.get("profile") or "cloud-dev").strip() or "cloud-dev"
                    release_name = str(p.get("helm_release") or release_name)
                    namespace = str(p.get("namespace") or namespace)
                    stage_id = profile_stage_id or stage_id
                    environment_id = profile_id or environment_id or target_profile_key
                    break
        elif name == DEMO_MICROSAAS_ECOSYSTEM:
            deploy_profile = str(body.get("profile") or "cloud-dev").strip() or "cloud-dev"
        elif action_lower == "deploy":
            raise HTTPException(status_code=400, detail="Deploy requires environment_id or stage_id.")
        if action_lower == "promote" and not target_profile_key:
            raise HTTPException(status_code=400, detail="Promote requires to_environment_id or stage_id.")
        if action_lower == "promote" and target_profile_key and matched_target_profile is None:
            raise HTTPException(status_code=404, detail=f"Promote target '{target_profile_key}' was not found.")
        if name != DEMO_MICROSAAS_ECOSYSTEM:
            resolved_target = _resolve_lifecycle_environment(
                name,
                {"environment_id": target_environment_id, "stage_id": stage_id},
                run_mgr,
            )
            resolved_target_lookup = target_profile_key or _target_field(resolved_target, "id") or _target_field(resolved_target, "stage_id") or "<missing>"
            _require_target_contract(
                action=action_lower,
                ecosystem=name,
                lookup=resolved_target_lookup,
                environment=resolved_target,
                fields=["deploy_profile", "namespace", "helm_release"],
            )
            target_profile = _target_field(resolved_target, "deploy_profile")
            _assert_target_profile_matches_request(
                action=action_lower,
                explicit_profile=str(body.get("profile") or "").strip() or None,
                target_profile=target_profile,
                lookup=resolved_target_lookup,
            )
            stage_id = _target_field(resolved_target, "stage_id") or stage_id
            environment_id = _target_field(resolved_target, "id") or environment_id
            deploy_profile = target_profile or deploy_profile
            release_name = _target_field(resolved_target, "helm_release") or release_name
            namespace = _target_field(resolved_target, "namespace") or namespace
            target_environment_id = environment_id or target_environment_id
            matched_target_profile = resolved_target
            if action_lower == "promote" and from_environment_id:
                _resolve_lifecycle_environment_lookup(name, lookup=from_environment_id, run_mgr=run_mgr)
        try:
            if name == DEMO_MICROSAAS_ECOSYSTEM:
                cmd, cwd = build_demo_microsaas_deploy_command(
                    root,
                    environment_id=environment_id or stage_id or "dev",
                    namespace=namespace,
                    release_name=release_name,
                    image_tag=image_tag or _demo_microsaas_default_image_tag(),
                    image_digest=image_digest,
                    image_repository=DEMO_MICROSAAS_IMAGE_REPOSITORY,
                    public_base_url=demo_public_base_url,
                    host=demo_public_host,
                    skip_build=True,
                )
            else:
                cmd, cwd = build_lifecycle_deploy_command(
                    root,
                    profile=deploy_profile,
                    image_tag=image_tag,
                    release_name=release_name,
                    namespace=namespace,
                )
        except (FileNotFoundError, RuntimeError) as e:
            raise HTTPException(status_code=501, detail=str(e))
    elif action_lower in {"promote_dry_run", "promote_main"}:
        from amof.api.command_builder import build_promote_main_command

        promotion_context = _promotion_action_context(name, body)
        cmd, cwd = build_promote_main_command(
            root,
            name,
            repo=promotion_context["repo"],
            ticket_id=promotion_context["ticket_id"],
            candidate_branch=promotion_context["candidate_branch"],
            source_sha=promotion_context["source_sha"],
            gitops_commit_sha=promotion_context["gitops_commit_sha"],
            expected_main_sha=promotion_context["expected_main_sha"],
            promotion_reason=promotion_context["promotion_reason"],
            push=action_lower == "promote_main",
        )
        stage_id = stage_id or str(body.get("stage_id") or "").strip() or None
        environment_id = environment_id or str(body.get("environment_id") or "").strip() or None
        lifecycle_event_payload = {
            "action": action_lower,
            "lifecycle_action": action_lower,
            "environment_id": environment_id or stage_id or promotion_context["ticket_id"],
            "stage_id": stage_id,
            "ticket_id": promotion_context["ticket_id"],
            "repo": promotion_context["repo"],
            "candidate_branch": promotion_context["candidate_branch"],
            "source_sha": promotion_context["source_sha"],
            "gitops_commit_sha": promotion_context["gitops_commit_sha"],
            "expected_main_sha": promotion_context["expected_main_sha"],
            "promotion_reason": promotion_context["promotion_reason"],
        }
        run_id = dispatcher.enqueue_subprocess(
            name,
            f"release/lifecycle/{action_lower}",
            cmd,
            cwd=str(cwd),
            event_payload=lifecycle_event_payload,
        )
        return {"run_id": run_id, "status": RUN_STATUS_QUEUED}
    elif action_lower == "revert_promotion":
        from amof.api.command_builder import build_promote_main_revert_command

        revert_context = _revert_action_context(name, body)
        cmd, cwd = build_promote_main_revert_command(
            root,
            name,
            repo=revert_context["repo"],
            synthetic_commit_sha=revert_context["synthetic_commit_sha"],
        )
        lifecycle_event_payload = {
            "action": action_lower,
            "lifecycle_action": action_lower,
            "environment_id": environment_id or stage_id or str(body.get("ticket_id") or "main"),
            "stage_id": stage_id,
            "ticket_id": str(body.get("ticket_id") or "").strip().upper() or None,
            "repo": revert_context["repo"],
            "synthetic_commit_sha": revert_context["synthetic_commit_sha"],
        }
        run_id = dispatcher.enqueue_subprocess(
            name,
            f"release/lifecycle/{action_lower}",
            cmd,
            cwd=str(cwd),
            event_payload=lifecycle_event_payload,
        )
        return {"run_id": run_id, "status": RUN_STATUS_QUEUED}
    elif action_lower == "refresh_status":
        environment = _resolve_lifecycle_environment(name, body)
        return {
            "status": "ok",
            "refreshed_at": _now_iso(),
            "environment_id": environment.get("id"),
            "stage_id": environment.get("stage_id"),
        }
    elif action_lower == "open_app":
        environment = _resolve_lifecycle_environment(name, body)
        endpoints = environment.get("endpoints") if isinstance(environment.get("endpoints"), list) else []
        url = None
        if endpoints:
            url = str((endpoints[0] or {}).get("url") or "").strip() or None
        if not url:
            hostname = str(environment.get("hostname") or "").strip()
            url = f"https://{hostname}" if hostname else None
        if not url:
            raise HTTPException(status_code=404, detail="No application URL is published for this environment.")
        return {
            "status": "ok",
            "environment_id": environment.get("id"),
            "stage_id": environment.get("stage_id"),
            "url": url,
        }
    elif action_lower == "open_logs":
        environment = _resolve_lifecycle_environment(name, body)
        return {
            "status": "ok",
            "environment_id": environment.get("id"),
            "stage_id": environment.get("stage_id"),
            "tab": "debug",
        }
    elif action_lower == "destroy_environment":
        environment_id = str(body.get("environment_id") or "").strip()
        if not environment_id:
            raise HTTPException(status_code=400, detail="destroy_environment requires environment_id.")
        return delete_release_environment(name, environment_id)
    else:
        raise HTTPException(
            status_code=501,
            detail=f"Lifecycle action '{action}' not implemented in this slice.",
        )
    lifecycle_event_payload = (
        _demo_microsaas_lifecycle_run_payload(
            action_lower,
            profile=matched_profile,
            stage_id=stage_id,
            environment_id=environment_id,
            public_base_url=demo_public_base_url,
        )
        if name == DEMO_MICROSAAS_ECOSYSTEM
        else {
            "ecosystem": name,
            "action": action_lower,
            "lifecycle_action": action_lower,
            "environment_id": environment_id or stage_id or action_lower,
            "stage_id": stage_id,
        }
    )
    lifecycle_event_payload.setdefault("ecosystem", name)
    if action_lower == "build":
        lifecycle_event_payload.update(
            {
                "image_tag": _release_build_image_tag(cmd),
                "no_push": bool(body.get("no_push")),
                "build_backend": build_backend,
                "source_mode": "canonical",
                "source_branch": source_branch,
                "source_commit": amof_commit,
                "sources": build_source_rows,
                "images": _build_image_targets(build_image_tag or _release_build_image_tag(cmd)),
                "builder": build_backend,
                "build_profile": build_profile,
                "amof_ref": amof_ref,
                "amof_commit": amof_commit,
                "amof_ui_ref": amof_ui_ref,
                "amof_ui_commit": amof_ui_commit,
                "assistant_ref": assistant_ref,
                "assistant_commit": assistant_commit,
            }
        )
    elif action_lower in {"deploy", "promote"}:
        lifecycle_event_payload.update(
            {
                "image_tag": image_tag or (cmd[cmd.index("--image-tag") + 1] if "--image-tag" in cmd else None),
                "image_digest": image_digest,
                "release_id": image_tag or body.get("release_id"),
                "deploy_profile": deploy_profile,
                "namespace": namespace,
                "release_name": release_name,
                "from_environment_id": from_environment_id,
                "to_environment_id": target_environment_id or environment_id,
            }
        )
        if name == DEMO_MICROSAAS_ECOSYSTEM:
            lifecycle_event_payload.update(
                {
                    "deploy_backend": "argocd",
                    "image_repository": DEMO_MICROSAAS_IMAGE_REPOSITORY,
                    "argocd_app_name": demo_microsaas_app_name(environment_id or stage_id or "dev", demo_argocd_settings),
                    "argocd_namespace": (demo_argocd_settings.namespace if demo_argocd_settings else None),
                    "argocd_project": (demo_argocd_settings.project if demo_argocd_settings else None),
                    "argocd_repo_url": (demo_argocd_settings.repo_url if demo_argocd_settings else None),
                    "argocd_target_revision": (
                        demo_argocd_settings.target_revision if demo_argocd_settings else None
                    ),
                    "argocd_chart_path": (
                        demo_argocd_settings.demo_microsaas_chart_path if demo_argocd_settings else None
                    ),
                }
            )
    run_id = dispatcher.enqueue_subprocess(
        name,
        f"release/lifecycle/{action_lower}",
        cmd,
        cwd=str(cwd),
        event_payload=lifecycle_event_payload,
    )
    return {"run_id": run_id, "status": RUN_STATUS_QUEUED}


@router.post("/{name}/release/environments", dependencies=[Depends(require_step_up_user)])
def create_release_environment(name: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    label = str(body.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="Environment label is required")
    stage_id = _slug(body.get("stage_id") or label)
    profiles = _read_environment_profiles(name)
    environment_id = _slug(body.get("environment_id") or label or stage_id)
    if any(str(row.get("id") or "") == environment_id for row in profiles):
        raise HTTPException(status_code=409, detail=f"Environment id '{environment_id}' already exists")
    resolved_public_base_url = (
        _normalize_public_base_url(body.get("public_base_url"))
        or (
            _demo_microsaas_default_public_base_url(stage_id=stage_id)
            if name == DEMO_MICROSAAS_ECOSYSTEM
            else _default_environment_public_base_url(
                name=name,
                stage_id=stage_id,
                environment_id=environment_id,
            )
        )
    )
    resolved_endpoints = (
        _normalize_environment_endpoints(body.get("endpoints"))
        if "endpoints" in body
        else _default_environment_endpoints(
            name=name,
            stage_id=stage_id,
            environment_id=environment_id,
            public_base_url=resolved_public_base_url,
        )
    )
    profile = {
        "id": environment_id,
        "stage_id": stage_id,
        "title": str(body.get("title") or label.title()),
        "summary": str(body.get("summary") or f"Manually created environment lane within {stage_id}."),
        "label": label,
        "ticket_label": str(body.get("ticket_label") or "main"),
        "workspace": str(body.get("workspace") or f"{name} / main"),
        "repo_summary": str(body.get("repo_summary") or "Shared workspace environment"),
        "status": str(body.get("status") or "healthy"),
        "namespace": body.get("namespace") or f"{name}-{environment_id}",
        "helm_release": body.get("helm_release") or f"{name}-{environment_id}",
        "public_base_url": resolved_public_base_url,
        "endpoints": resolved_endpoints,
        "promotion_target": body.get("promotion_target") or _promotion_target_for_stage(stage_id),
        "ai_log_chunks": [
            "Environment created from the compact lifecycle flow.",
        ],
    }
    dns_actions = _reconcile_environment_dns_or_raise(None, profile)
    if dns_actions:
        profile["ai_log_chunks"].append("Environment DNS reconciled automatically.")
    profiles.append(profile)
    _write_environment_profiles(name, profiles)
    registry = _compat_registry_payload(name)
    stage = next(
        (row for row in _build_lifecycle_stages(name, registry) if str(row.get("id") or "") == stage_id),
        None,
    )
    return {"status": "ok", "stage": stage, "environment_id": environment_id}


@router.delete("/{name}/release/environments/{environment_id}", dependencies=[Depends(require_step_up_user)])
def delete_release_environment(name: str, environment_id: str) -> Dict[str, Any]:
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    normalized_environment_id = _slug(environment_id)
    profiles = _read_environment_profiles(name)
    matched_profile = next(
        (row for row in profiles if str(row.get("id") or "") == normalized_environment_id),
        None,
    )
    if not matched_profile:
        raise HTTPException(status_code=404, detail=f"Environment id '{normalized_environment_id}' not found")
    matched_stage_id = str(matched_profile.get("stage_id") or normalized_environment_id)
    if normalized_environment_id == matched_stage_id:
        raise HTTPException(
            status_code=400,
            detail=f"Environment id '{normalized_environment_id}' is the protected base environment for stage '{matched_stage_id}'",
        )

    remaining_profiles = [
        row for row in profiles if str(row.get("id") or "") != normalized_environment_id
    ]
    _reconcile_environment_dns_or_raise(matched_profile, None)
    _write_environment_profiles(name, remaining_profiles)
    registry = _compat_registry_payload(name)
    stage = next(
        (row for row in _build_lifecycle_stages(name, registry) if str(row.get("id") or "") == matched_stage_id),
        None,
    )
    return {
        "status": "ok",
        "removed_environment_id": normalized_environment_id,
        "stage_id": matched_stage_id,
        "stage": stage,
    }


@router.patch("/{name}/release/environments/{environment_id}", dependencies=[Depends(require_step_up_user)])
@router.put("/{name}/release/environments/{environment_id}", dependencies=[Depends(require_step_up_user)])
def update_release_environment(name: str, environment_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    normalized_environment_id = _slug(environment_id)
    profiles = _read_environment_profiles(name)
    matched_index = next(
        (index for index, row in enumerate(profiles) if str(row.get("id") or "") == normalized_environment_id),
        None,
    )
    if matched_index is None:
        raise HTTPException(status_code=404, detail=f"Environment id '{normalized_environment_id}' not found")
    matched_profile = profiles[matched_index]
    if "environment_id" in body and _slug(body.get("environment_id")) != normalized_environment_id:
        raise HTTPException(status_code=400, detail="Environment id cannot be changed")
    if "stage_id" in body and _slug(body.get("stage_id")) != str(matched_profile.get("stage_id") or normalized_environment_id):
        raise HTTPException(status_code=400, detail="Environment stage_id cannot be changed")

    updated_profile = _apply_environment_profile_updates(name=name, profile=matched_profile, body=body)
    _reconcile_environment_dns_or_raise(matched_profile, updated_profile)
    profiles[matched_index] = updated_profile
    _write_environment_profiles(name, profiles)
    matched_stage_id = str(matched_profile.get("stage_id") or normalized_environment_id)
    registry = _compat_registry_payload(name)
    stage = next(
        (row for row in _build_lifecycle_stages(name, registry) if str(row.get("id") or "") == matched_stage_id),
        None,
    )
    return {
        "status": "ok",
        "environment_id": normalized_environment_id,
        "stage_id": matched_stage_id,
        "stage": stage,
    }


@router.post("/{name}/release/validate", dependencies=[Depends(require_step_up_user)])
def release_validate(name: str, background_tasks: BackgroundTasks, run_mgr=Depends(get_run_manager)):
    """Run bounded release-readiness validation."""
    from amof.api.command_builder import build_release_validate_command
    from amof.api.services.runner import execute_action
    return execute_action(run_mgr, name, "release/validate", build_release_validate_command, background_tasks)


@router.post("/{name}/release/bump", dependencies=[Depends(require_step_up_user)])
def release_bump(
    name: str,
    body: Dict[str, Any],
    background_tasks: BackgroundTasks,
    run_mgr=Depends(get_run_manager),
) -> Dict[str, Any]:
    """Bump version: body { type: 'patch'|'minor'|'major', prerelease?: 'alpha'|'beta'|'rc', dry_run?: bool }."""
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    bump_type = body.get("type", "patch")
    if bump_type not in ("major", "minor", "patch"):
        raise HTTPException(status_code=400, detail="type must be major, minor, or patch")
    prerelease = body.get("prerelease")
    dry_run = body.get("dry_run", False)
    root = get_workspace_root()
    script = root / "scripts" / "amof.py"
    cmd = [sys.executable, str(script), "release", bump_type, "-y"]
    if prerelease:
        cmd.append(f"--{prerelease}")
    if dry_run:
        cmd.append("--dry-run")
    run_id = run_mgr.create_run(name, "release/bump", cmd)
    background_tasks.add_task(run_subprocess_task, run_mgr, run_id, cmd, str(root))
    return {"run_id": run_id, "status": RUN_STATUS_QUEUED}


@router.post("/{name}/release/promote", dependencies=[Depends(require_step_up_user)])
def release_promote(
    name: str,
    body: Dict[str, Any],
    background_tasks: BackgroundTasks,
    run_mgr=Depends(get_run_manager),
) -> Dict[str, Any]:
    """Promote pre-release: body { target?: 'beta'|'rc', dry_run?: bool }. Omit target for stable."""
    if name not in get_available_ecosystems():
        raise HTTPException(status_code=404, detail=f"Ecosystem {name} not found")
    target: Optional[str] = body.get("target")
    dry_run = body.get("dry_run", False)
    root = get_workspace_root()
    script = root / "scripts" / "amof.py"
    cmd = [sys.executable, str(script), "release", "promote", "-y"]
    if target:
        cmd.append(f"--{target}")
    if dry_run:
        cmd.append("--dry-run")
    run_id = run_mgr.create_run(name, "release/promote", cmd)
    background_tasks.add_task(run_subprocess_task, run_mgr, run_id, cmd, str(root))
    return {"run_id": run_id, "status": RUN_STATUS_QUEUED}
