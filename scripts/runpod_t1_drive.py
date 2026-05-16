"""T1 operator driver - create one AMOF-owned RunPod pod, poll /models.

This is an operator-run script, not an agent script. It creates exactly
one pod from the gpt-oss-120b profile, resolves the pod's proxy URL,
polls /models until the vLLM server responds, and emits one audit
record. If anything goes wrong, the operator stops and deletes the pod
manually via ``amof release``-style commands or ``DELETE /api/v1/runpod/pods/{id}``.

Usage (from repos/amof):

    # 1. copy the example profile once
    mkdir -p ../../.amof/runpod-profiles
    cp docs/contracts/examples/runpod-profiles/gpt-oss-120b-vllm-single-h100.yaml \\
       ../../.amof/runpod-profiles/gpt-oss-120b.yaml

    # 2. run the driver
    python3 scripts/runpod_t1_drive.py --profile gpt-oss-120b

    # 3. when done, stop and delete the pod via curl or UI

Flags:
    --profile NAME           Profile stem under .amof/runpod-profiles/
    --max-wait-minutes N     Stop polling after N minutes (default 45)
    --poll-interval-seconds N  (default 20)
    --stop-on-success        Call stop_pod after one successful /models
    --delete-on-success      Call delete_pod after successful /models
                             (only if --stop-on-success is also set)
    --dry-run                Print plan, do not create a pod
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

_repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo_root / "scripts"))

from amof.api.services.runpod import (  # noqa: E402
    RunpodClient,
    RunpodClientError,
    RunpodHttpError,
    RunpodNotConfigured,
    load_profile,
    load_runpod_settings,
    project_pod_status,
)


_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env_refs(
    profile_env: Dict[str, str], host_env: Dict[str, str]
) -> Dict[str, str]:
    """Substitute ``${VAR}`` references in profile env values from host env.

    Keeps real secrets out of the profile YAML: the YAML writes
    ``HF_TOKEN: "${HF_TOKEN}"`` and the driver resolves it from the
    operator's shell/.env-loaded process env at pod creation time. An
    unknown ``${VAR}`` becomes empty string (so a missing HF_TOKEN leaves
    the pod running in unauthenticated mode, preserving prior behaviour).
    """

    resolved: Dict[str, str] = {}
    for key, value in profile_env.items():
        text = str(value or "")

        def _repl(match: "re.Match[str]") -> str:
            return host_env.get(match.group(1), "")

        resolved[str(key)] = _ENV_REF_RE.sub(_repl, text)
    return resolved


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _resolve_openai_base_url(
    pod_status: Dict[str, Any], profile_ports: Optional[list] = None
) -> Optional[str]:
    """Pick an http port and build the RunPod proxy URL.

    Observed (UP11-3, UP11-4): the RunPod REST ``portMappings`` field stays
    ``null`` even when the pod's ``/v1/models`` is serving 200 through the
    proxy. Falling back to the profile-declared http port makes the driver
    actually progress instead of waiting forever.
    """

    pod_id = pod_status.get("pod_id")
    if not pod_id:
        return None

    mappings = pod_status.get("port_mappings") or []
    for row in mappings:
        protocol = str(row.get("protocol") or "").lower()
        private_port = row.get("private")
        if protocol == "http" and isinstance(private_port, int):
            return f"https://{pod_id}-{private_port}.proxy.runpod.net/v1"

    if profile_ports:
        for port_spec in profile_ports:
            text = str(port_spec or "").strip()
            if "/" not in text:
                continue
            port_str, proto = text.split("/", 1)
            if proto.strip().lower() == "http":
                try:
                    return f"https://{pod_id}-{int(port_str)}.proxy.runpod.net/v1"
                except ValueError:
                    continue
    return None


def _probe_models(base_url: str, api_key: str, timeout: int = 15) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        payload_excerpt: Optional[Dict[str, Any]] = None
        model_ids: list[str] = []
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, list):
                    model_ids = [str(row.get("id") or "") for row in data if isinstance(row, dict)]
                payload_excerpt = {"keys": list(payload.keys())[:6]}
        except ValueError:
            payload_excerpt = {"raw_excerpt": (resp.text or "")[:200]}
        return {
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "model_ids": model_ids,
            "payload_excerpt": payload_excerpt,
        }
    except requests.Timeout:
        return {"status_code": None, "error": "timeout"}
    except requests.RequestException as exc:
        return {"status_code": None, "error": repr(exc)[:300]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="Profile name (stem under .amof/runpod-profiles/)")
    parser.add_argument("--max-wait-minutes", type=int, default=45)
    parser.add_argument("--poll-interval-seconds", type=int, default=20)
    parser.add_argument("--stop-on-success", action="store_true")
    parser.add_argument("--delete-on-success", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--audit-path", type=str, default=None)
    args = parser.parse_args()

    settings = load_runpod_settings()
    if settings is None:
        print("RUNPOD_API_KEY is not set; refusing to proceed.", file=sys.stderr)
        return 2

    profile = load_profile(args.profile, settings)
    if profile.env:
        resolved_env = _interpolate_env_refs(dict(profile.env), dict(os.environ))
        if resolved_env != dict(profile.env):
            profile = dataclass_replace(profile, env=resolved_env)
    plan = {
        "profile": args.profile,
        "image_name": profile.image_name,
        "template_id": profile.template_id,
        "gpu_count": profile.gpu_count,
        "gpu_type_ids": list(profile.gpu_type_ids),
        "ports": list(profile.ports),
        "cloud_type": profile.cloud_type,
        "ttl_minutes": profile.ttl_minutes,
        "docker_args": profile.docker_args,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan}, indent=2))
        return 0

    client = RunpodClient(settings=settings)
    record: Dict[str, Any] = {
        "audit_id": "runpod-t1-pod-endpoint-proof",
        "ts_iso_start": _now_iso(),
        "profile": args.profile,
        "plan": plan,
        "create": None,
        "polls": [],
        "resolved_base_url": None,
        "models_probe": None,
        "stop": None,
        "delete": None,
        "verdict": None,
        "errors": [],
    }

    try:
        pod = client.create_pod(profile)
        status = project_pod_status(pod)
        record["create"] = {"pod_id": status.get("pod_id"), "status": status}
    except RunpodHttpError as exc:
        record["errors"].append({"stage": "create", "status_code": exc.status_code, "body": exc.body[:400]})
        record["verdict"] = "fail_create"
        print(json.dumps(record, indent=2))
        return 1
    except RunpodClientError as exc:
        record["errors"].append({"stage": "create", "error": str(exc)[:400]})
        record["verdict"] = "fail_create"
        print(json.dumps(record, indent=2))
        return 1

    pod_id = record["create"]["pod_id"]
    deadline_at = time.monotonic() + args.max_wait_minutes * 60
    success = False
    while time.monotonic() < deadline_at:
        try:
            current = client.get_pod(pod_id)
        except RunpodClientError as exc:
            record["polls"].append({"ts_iso": _now_iso(), "error": str(exc)[:300]})
            time.sleep(args.poll_interval_seconds)
            continue
        status = project_pod_status(current)
        base_url = _resolve_openai_base_url(status, profile_ports=list(profile.ports))
        record["resolved_base_url"] = base_url
        if not base_url:
            record["polls"].append({
                "ts_iso": _now_iso(),
                "desired_status": status.get("desired_status"),
                "port_mappings": status.get("port_mappings"),
                "note": "no http port mapping yet",
            })
            time.sleep(args.poll_interval_seconds)
            continue
        probe = _probe_models(base_url, settings.api_key)
        record["polls"].append({
            "ts_iso": _now_iso(),
            "desired_status": status.get("desired_status"),
            "probe": probe,
            "hourly_cost_usd": status.get("hourly_cost_usd"),
            "ttl_remaining_seconds": status.get("ttl_remaining_seconds"),
        })
        if probe.get("status_code") == 200 and probe.get("model_ids"):
            record["models_probe"] = probe
            success = True
            break
        time.sleep(args.poll_interval_seconds)

    record["verdict"] = "usable" if success else "timeout_or_unhealthy"

    if success and args.stop_on_success:
        try:
            stopped = client.stop_pod(pod_id)
            record["stop"] = project_pod_status(stopped)
        except RunpodClientError as exc:
            record["errors"].append({"stage": "stop", "error": str(exc)[:400]})
        if args.delete_on_success:
            try:
                client.delete_pod(pod_id)
                record["delete"] = {"pod_id": pod_id, "deleted": True}
            except RunpodClientError as exc:
                record["errors"].append({"stage": "delete", "error": str(exc)[:400]})

    record["ts_iso_end"] = _now_iso()
    if args.audit_path:
        audit_dir = Path(args.audit_path).parent
        audit_dir.mkdir(parents=True, exist_ok=True)
        Path(args.audit_path).write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")

    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
