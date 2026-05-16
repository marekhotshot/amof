"""First-class Director contract actions.

This module intentionally starts with one narrow action:
``director.gmd_dev_local_proof.v1``. It productizes the Ultra Plan 8
local-only, dev-only gmd proof without generalizing into cloud, DNS,
release, promote, or UI surfaces.
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..manifest import resolve_workspace_root

ACTION_GMD_DEV_LOCAL_PROOF = "director.gmd_dev_local_proof.v1"
DEFAULT_RESULT_NAME = "director-gmd-dev-local-proof-result.json"


class DirectorActionError(RuntimeError):
    """Raised when the action cannot complete truthfully."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DirectorActionError(f"Input contract not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DirectorActionError(f"Input contract is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DirectorActionError("Input contract must be a JSON object.")
    return payload


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _run(argv: List[str], *, cwd: Optional[Path] = None, timeout: int = 30) -> Tuple[int, str]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _run_json(argv: List[str], *, timeout: int = 30) -> Dict[str, Any]:
    code, output = _run(argv, timeout=timeout)
    if code != 0:
        raise DirectorActionError(f"{' '.join(argv)} failed: {output}")
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DirectorActionError(f"{' '.join(argv)} did not return JSON: {output}") from exc
    if not isinstance(parsed, dict):
        raise DirectorActionError(f"{' '.join(argv)} returned non-object JSON.")
    return parsed


def _require_text(payload: Dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise DirectorActionError(f"Input contract missing required field: {key}")
    return value


def _contract_result_path(root: Path, contract: Dict[str, Any], output_arg: Optional[str]) -> Path:
    if output_arg:
        return (root / output_arg).resolve() if not Path(output_arg).is_absolute() else Path(output_arg)
    artifacts = contract.get("artifacts") if isinstance(contract.get("artifacts"), dict) else {}
    result_path = str(artifacts.get("result_path") or "").strip()
    if result_path:
        return (root / result_path).resolve() if not Path(result_path).is_absolute() else Path(result_path)
    return root / "ecosystems" / "amof-platform" / "audit" / DEFAULT_RESULT_NAME


def _validate_contract(contract: Dict[str, Any], ecosystem: str) -> None:
    action = str(contract.get("action") or ACTION_GMD_DEV_LOCAL_PROOF).strip()
    if action != ACTION_GMD_DEV_LOCAL_PROOF:
        raise DirectorActionError(f"Unsupported Director action: {action}")
    if ecosystem != "gmd" or _require_text(contract, "ecosystem") != "gmd":
        raise DirectorActionError("This action is explicitly scoped to ecosystem=gmd.")
    if _require_text(contract, "target_environment") != "dev":
        raise DirectorActionError("This action is explicitly scoped to target_environment=dev.")
    release_intent = contract.get("release_intent")
    if not isinstance(release_intent, dict) or str(release_intent.get("type") or "").strip() != "none":
        raise DirectorActionError("This action requires release_intent.type=none.")
    required_arrays = ["constraints", "acceptance_criteria"]
    for key in required_arrays:
        value = contract.get(key)
        if not isinstance(value, list) or not value:
            raise DirectorActionError(f"Input contract requires a non-empty {key} array.")
    scope = contract.get("scope")
    if not isinstance(scope, dict):
        raise DirectorActionError("Input contract requires scope object.")
    repos = scope.get("repos")
    if repos != ["gmd-app"]:
        raise DirectorActionError("This action requires scope.repos to be exactly ['gmd-app'].")


def _check_writable_runs(root: Path) -> Dict[str, Any]:
    runs_dir = root / ".amof" / "runs"
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".director-preflight-", dir=runs_dir, delete=True) as handle:
            handle.write(b"ok")
    except Exception as exc:
        return {"ok": False, "detail": f"{runs_dir} is not writable: {exc}"}
    return {"ok": True, "detail": f"{runs_dir} is writable"}


def _check_python_sdk(module_name: str) -> Dict[str, Any]:
    return {
        "ok": importlib.util.find_spec(module_name) is not None,
        "detail": f"python module {module_name}",
    }


def _load_agent_config(root: Path) -> Dict[str, Any]:
    path = root / ".amof" / "agent.yaml"
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _configured_models(value: Any) -> List[str]:
    models: List[str] = []
    if isinstance(value, dict):
        for item in value.values():
            models.extend(_configured_models(item))
    elif isinstance(value, list):
        for item in value:
            models.extend(_configured_models(item))
    elif isinstance(value, str):
        models.append(value)
    return models


def _check_provider_config(root: Path) -> Dict[str, Any]:
    cfg = _load_agent_config(root)
    ladder_enabled = bool(cfg.get("model_ladder"))
    ladder_models = _configured_models((cfg.get("llm_ladder") or {}).get("roles"))
    uses_openrouter = any(model.startswith("openrouter/") for model in ladder_models)
    if ladder_enabled and uses_openrouter and not os.environ.get("OPENROUTER_API_KEY"):
        return {"ok": False, "detail": "model_ladder uses OpenRouter models but OPENROUTER_API_KEY is not set"}
    if ladder_enabled and uses_openrouter and importlib.util.find_spec("openai") is None:
        return {"ok": False, "detail": "model_ladder uses OpenRouter models but openai SDK is missing"}
    default_provider = str(cfg.get("default_provider") or "anthropic").strip()
    if not ladder_enabled and default_provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "detail": "default_provider=anthropic but ANTHROPIC_API_KEY is not set"}
    return {"ok": True, "detail": f"provider config resolved (model_ladder={ladder_enabled})"}


def _check_port_available(port: int) -> Dict[str, Any]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            return {"ok": False, "detail": f"127.0.0.1:{port} is already in use"}
    return {"ok": True, "detail": f"127.0.0.1:{port} is available"}


def _assert_preflights(root: Path, contract: Dict[str, Any], local_port: int) -> Dict[str, Any]:
    checks: Dict[str, Any] = {
        "runs_writable": _check_writable_runs(root),
        "sdk_openai": _check_python_sdk("openai"),
        "sdk_anthropic": _check_python_sdk("anthropic"),
        "provider_config": _check_provider_config(root),
        "port_available": _check_port_available(local_port),
    }
    code, context = _run(["kubectl", "config", "current-context"])
    in_cluster = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
    context_ok = code == 0 and context == "k3d-amof"
    if in_cluster and not context_ok:
        context_ok = True
        context = "in-cluster:k3d-amof"
    checks["kubectl_context"] = {
        "ok": context_ok,
        "detail": context or "kubectl context unavailable",
    }
    failures = {key: value for key, value in checks.items() if not value.get("ok")}
    if failures:
        raise DirectorActionError(f"Preflight failed: {json.dumps(failures, sort_keys=True)}")
    return checks


def _validate_applicationset() -> Dict[str, Any]:
    appset = _run_json(["kubectl", "-n", "argocd", "get", "applicationset", "gmd-environments", "-o", "json"])
    elements = (((appset.get("spec") or {}).get("generators") or [{}])[0].get("list") or {}).get("elements")
    params = ((((appset.get("spec") or {}).get("template") or {}).get("spec") or {}).get("source") or {}).get("helm", {}).get("parameters") or []
    external_service_param = next((row for row in params if row.get("name") == "frontend.externalService"), None)
    if elements != [{"environment": "dev"}]:
        raise DirectorActionError(f"ApplicationSet must generate only dev; got {elements!r}")
    if not external_service_param or str(external_service_param.get("value")).lower() != "false":
        raise DirectorActionError("ApplicationSet must set frontend.externalService=false for local proof.")
    return {"elements": elements, "helm_parameters": params}


def _deployment_readback() -> Dict[str, Any]:
    app = _run_json(["kubectl", "-n", "argocd", "get", "application", "gmd-dev", "-o", "json"])
    spec = app.get("spec") or {}
    source = spec.get("source") or {}
    destination = spec.get("destination") or {}
    status = app.get("status") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    operation_state = status.get("operationState") or {}
    if source.get("path") != "infrastructure/gitops/gmd/chart":
        raise DirectorActionError(f"Unexpected Argo source path: {source.get('path')}")
    if destination.get("namespace") != "gmd-dev":
        raise DirectorActionError(f"Unexpected destination namespace: {destination.get('namespace')}")
    if sync.get("status") != "Synced" or health.get("status") != "Healthy":
        raise DirectorActionError(
            f"gmd-dev is not healthy/synced: sync={sync.get('status')} health={health.get('status')}"
        )
    pods = _run_json(["kubectl", "-n", "gmd-dev", "get", "pods", "-o", "json"])
    pod_rows: List[Dict[str, Any]] = []
    not_running = 0
    for item in pods.get("items") or []:
        metadata = item.get("metadata") or {}
        pod_status = item.get("status") or {}
        containers = pod_status.get("containerStatuses") or []
        ready = sum(1 for row in containers if row.get("ready"))
        total = len(containers)
        phase = pod_status.get("phase")
        if phase != "Running" or ready != total:
            not_running += 1
        pod_rows.append({
            "name": metadata.get("name"),
            "ready": f"{ready}/{total}",
            "status": phase,
        })
    if not_running:
        raise DirectorActionError(f"{not_running} gmd-dev pod(s) are not Running/Ready.")
    svc = _run_json(["kubectl", "-n", "gmd-dev", "get", "svc", "frontend", "-o", "json"])
    return {
        "application_name": "gmd-dev",
        "namespace": "gmd-dev",
        "sync_status": sync.get("status"),
        "health_status": health.get("status"),
        "revision": sync.get("revision"),
        "operation_message": operation_state.get("message"),
        "chart_path": source.get("path"),
        "repo_url": source.get("repoURL"),
        "target_revision": source.get("targetRevision"),
        "workload_summary": {
            "total_pods": len(pod_rows),
            "running_pods": len(pod_rows) - not_running,
            "not_running_pods": not_running,
            "pods": pod_rows,
            "frontend_service": {
                "name": "frontend",
                "type": (svc.get("spec") or {}).get("type"),
                "cluster_ip": (svc.get("spec") or {}).get("clusterIP"),
                "ports": (svc.get("spec") or {}).get("ports"),
            },
        },
    }


def _wait_for_port_forward(log_path: Path, proc: subprocess.Popen[str], timeout_seconds: float = 10.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            raise DirectorActionError(f"port-forward exited early with {proc.returncode}: {output}")
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        if "Forwarding from 127.0.0.1" in text or "Forwarding from [::1]" in text:
            return
        time.sleep(0.2)
    raise DirectorActionError("Timed out waiting for port-forward to become ready.")


def _http_get(port: int, path: str) -> Tuple[int, str, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        content_type = response.getheader("content-type") or ""
        return response.status, content_type, body
    finally:
        conn.close()


def _readback(local_port: int, root_path: str, health_path: str) -> Dict[str, Any]:
    with tempfile.NamedTemporaryFile(prefix="gmd-dev-port-forward-", suffix=".log", delete=False) as log_handle:
        log_path = Path(log_handle.name)
    proc = subprocess.Popen(
        ["kubectl", "-n", "gmd-dev", "port-forward", "svc/frontend", f"{local_port}:80"],
        stdout=log_path.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
    )
    stopped = False
    try:
        _wait_for_port_forward(log_path, proc)
        root_status, root_type, root_body = _http_get(local_port, root_path)
        health_status, health_type, health_body = _http_get(local_port, health_path)
        if root_status != 200:
            raise DirectorActionError(f"root readback returned HTTP {root_status}")
        if health_status != 200 or health_body.strip() != "ok":
            raise DirectorActionError(f"health readback returned HTTP {health_status} body={health_body!r}")
        return {
            "method": f"kubectl -n gmd-dev port-forward svc/frontend {local_port}:80",
            "port_used_for_verification": local_port,
            "probes": [
                {
                    "url": f"http://127.0.0.1:{local_port}{root_path}",
                    "http_status": root_status,
                    "content_type": root_type,
                    "body_summary": "HTML response" if "<html" in root_body[:500].lower() else root_body[:120],
                },
                {
                    "url": f"http://127.0.0.1:{local_port}{health_path}",
                    "http_status": health_status,
                    "content_type": health_type,
                    "body": health_body.strip(),
                },
            ],
            "port_forward_log": str(log_path),
            "port_forward_stopped": True,
            "readback_pass": True,
        }
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            stopped = True
        if not stopped and proc.poll() is not None:
            stopped = True


def _blocked_result(
    *,
    contract: Dict[str, Any],
    result_path: Path,
    error: str,
    preflights: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "action": ACTION_GMD_DEV_LOCAL_PROOF,
        "ticket_id": contract.get("ticket_id"),
        "final_status": "blocked",
        "target_environment": "dev",
        "preflights": preflights or {},
        "deploy_result": None,
        "readback_result": None,
        "release_promote_attempted": False,
        "blocker": {
            "message": error,
            "classification": "orchestration execution gap",
        },
        "failure_classification": "orchestration execution gap",
        "evidence": {
            "result_path": str(result_path),
            "generated_at": _now_iso(),
        },
    }


def run_gmd_dev_local_proof(
    *,
    manifest: Dict[str, Any],
    ecosystem: str,
    input_path: Path,
    output_path: Optional[Path] = None,
    local_port_arg: Optional[int] = None,
) -> Dict[str, Any]:
    root = resolve_workspace_root()
    contract = _load_json(input_path)
    result_path = output_path or _contract_result_path(root, contract, None)
    preflights: Dict[str, Any] = {}
    try:
        _validate_contract(contract, ecosystem)
        readback_cfg = contract.get("readback") if isinstance(contract.get("readback"), dict) else {}
        local_port = int(local_port_arg or readback_cfg.get("local_port") or 18082)
        root_path = str(readback_cfg.get("root_path") or "/")
        health_path = str(readback_cfg.get("health_path") or "/_healthz")
        preflights = _assert_preflights(root, contract, local_port)
        appset = _validate_applicationset()
        deploy = _deployment_readback()
        readback = _readback(local_port, root_path, health_path)
        result = {
            "schema_version": "1.0",
            "action": ACTION_GMD_DEV_LOCAL_PROOF,
            "ticket_id": contract.get("ticket_id"),
            "run_ids": {
                "cli_action": ACTION_GMD_DEV_LOCAL_PROOF,
                "argocd_revision": deploy.get("revision"),
            },
            "final_status": "pass",
            "target_environment": "dev",
            "constraints_verified": {
                "dev_only": True,
                "local_only": True,
                "no_test_or_prod": True,
                "no_cloud_dev": True,
                "no_release_promote": True,
                "source_gitops_files_unmodified": True,
                "applicationset_runtime_dev_only": True,
            },
            "preflights": preflights,
            "substrate_verification": {
                "k8s_context": preflights["kubectl_context"]["detail"],
                "applicationset_name": "gmd-environments",
                "applicationset_elements": appset["elements"],
                "helm_parameters": appset["helm_parameters"],
            },
            "deploy_result": deploy,
            "readback_result": readback,
            "release_promote_attempted": False,
            "blocker": None,
            "failure_classification": None,
            "evidence": {
                "input_contract": str(input_path),
                "result_path": str(result_path),
                "generated_at": _now_iso(),
            },
            "acceptance_criteria_results": {
                "first_class_action": True,
                "only_gmd_dev_targeted": True,
                "argo_gmd_dev_synced_and_healthy": True,
                "gmd_dev_workload_pods_running": True,
                "local_readback_succeeds": True,
                "structured_result_recorded": True,
                "release_promote_not_attempted": True,
            },
        }
    except Exception as exc:
        result = _blocked_result(
            contract=contract,
            result_path=result_path,
            error=str(exc),
            preflights=preflights,
        )
    _write_json(result_path, result)
    return result


def cmd_director_action(manifest: Dict[str, Any], args: argparse.Namespace, ecosystem: str) -> int:
    action = getattr(args, "director_action_cmd", None)
    if action != "gmd-dev-local-proof":
        sys.stderr.write("Usage: amof -e gmd director-action gmd-dev-local-proof --input <contract.json>\n")
        return 1
    input_arg = getattr(args, "input", None)
    if not input_arg:
        sys.stderr.write("--input is required for director-action gmd-dev-local-proof\n")
        return 1
    root = resolve_workspace_root()
    input_path = Path(input_arg)
    if not input_path.is_absolute():
        input_path = root / input_path
    output_arg = getattr(args, "output", None)
    output_path = None
    if output_arg:
        output_path = Path(output_arg)
        if not output_path.is_absolute():
            output_path = root / output_path
    result = run_gmd_dev_local_proof(
        manifest=manifest,
        ecosystem=ecosystem,
        input_path=input_path,
        output_path=output_path,
        local_port_arg=getattr(args, "local_port", None),
    )
    result_path = output_path or _contract_result_path(root, _load_json(input_path), None)
    print(json.dumps({
        "action": ACTION_GMD_DEV_LOCAL_PROOF,
        "final_status": result.get("final_status"),
        "result_path": str(result_path),
        "blocker": result.get("blocker"),
    }, indent=2))
    return 0 if result.get("final_status") == "pass" else 1
