"""Agent entrypoint for one Agreement-bound Romulo Deployment operation.

The authenticated console supplies the claimed mandate authority. The bridge
does not accept a free-form operation or an operator identity from a browser.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .kubernetes_capability import (
    KubernetesCapabilityError, KubernetesCapabilityStore, KubernetesOperation,
    approve_proposal, execute_kubernetes_capability, propose_kubernetes_capability,
)
from .kubernetes_live import LiveKubernetesExecutor


def _required(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KubernetesCapabilityError(f"missing {key}", code="invalid_request")
    return value.strip()


def _store(root: Path) -> KubernetesCapabilityStore:
    return KubernetesCapabilityStore(
        proposals_dir=root / "proposals", approvals_dir=root / "approvals",
        bindings_dir=root / "bindings", receipts_dir=root / "receipts",
        revocations_dir=root / "revocations", events_dir=root / "events",
    )


def execute_authorized_exact_operation(
    authority: dict[str, Any], *, store_root: Path, registry_path: Path,
    executor: Any | None = None,
) -> dict[str, Any]:
    """Produce one single-use public capability receipt for an AMOF Run."""
    if os.environ.get("AMOF_HOTSHOT_ISOLATED_DOGFOOD") != "1":
        raise KubernetesCapabilityError("isolated product bridge is disabled", code="bridge_disabled")
    mission_id = _required(authority, "mission_id")
    run_id = _required(authority, "request_id")
    approved_by = _required(authority, "authorized_by")
    operation = authority.get("operation")
    if not isinstance(operation, dict) or set(operation) != {
        "kind", "cluster", "context", "namespace", "deployment", "container", "uid",
        "resource_version", "expected_timeout_seconds", "timeout_seconds",
    } or operation["kind"] != "deployment.readiness_timeout.patch" or (
        operation["expected_timeout_seconds"], operation["timeout_seconds"]
    ) != (1, 3):
        raise KubernetesCapabilityError("reviewed operation shape changed", code="scope_escalation")
    for field, env in (("cluster", "AMOF_ROMULO_ALLOWED_CLUSTER"),
                       ("context", "AMOF_ROMULO_ALLOWED_CONTEXT"),
                       ("namespace", "AMOF_ROMULO_ALLOWED_NAMESPACE")):
        if not os.environ.get(env) or operation[field] != os.environ[env]:
            raise KubernetesCapabilityError(f"{field} outside isolated target", code="wrong_cluster")
    if not registry_path.is_file():
        raise KubernetesCapabilityError("target registry unavailable", code="wrong_cluster")
    expected = {
        "namespace": operation["namespace"], "verb": "patch", "resource": "deployments",
        "name": operation["deployment"], "patch": {"readinessProbe": {
            "container": operation["container"], "expectedTimeoutSeconds": 1,
            "timeoutSeconds": 3,
        }},
        "expected_context": operation["context"], "expected_uid": operation["uid"],
        "expected_resource_version": operation["resource_version"],
    }
    body = {
        "capability": "kubernetes.mutate", "cluster": operation["cluster"],
        "namespaces": [operation["namespace"]], "verbs": ["patch"],
        "resources": ["deployments"], "denied_namespaces": ["kube-system"],
        "denied_resources": ["secrets", "clusterroles"],
        "reason": f"AMOF Agreement {authority['agreement_sha256']} / mandate {authority['mandate_id']}",
        "exact_operation": expected,
    }
    requested_by = f"agent:{_required(authority, 'target_id')}"
    store = _store(store_root)
    proposal = propose_kubernetes_capability(
        run_id=run_id, body=body, requested_by=requested_by, store=store,
    )
    minutes = authority.get("budget_minutes")
    if type(minutes) is not int or not 1 <= minutes <= 120:
        raise KubernetesCapabilityError("invalid mandate budget", code="invalid_approval")
    approval = approve_proposal(
        proposal["proposal_id"], ttl=f"{minutes}m", approved_by=approved_by,
        approval_source="api", store=store,
    )
    exact = KubernetesOperation(
        cluster=operation["cluster"], namespace=operation["namespace"],
        verb="patch", resource="deployments", name=operation["deployment"],
        patch=expected["patch"], mission_id=mission_id, run_id=run_id,
        requested_by=requested_by, expected_context=operation["context"],
        expected_uid=operation["uid"], expected_resource_version=operation["resource_version"],
    )
    live = executor or LiveKubernetesExecutor(registry_path=registry_path)
    observed: dict[str, Any] = {}

    class Capture:
        def execute(self, requested: KubernetesOperation) -> dict[str, Any]:
            result = live.execute(requested)
            observed.update(result)
            return result

    outcome = execute_kubernetes_capability(
        operation=exact, approval_id=approval["approval_id"], executor=Capture(), store=store,
    )
    return {
        "run_id": run_id, "mission_id": mission_id,
        "mandate_id": authority["mandate_id"], "attempt_id": authority["attempt_id"],
        "approval_id": approval["approval_id"], "ok": outcome.ok, "code": outcome.code,
        "receipt": outcome.receipt, "before": observed.get("before"), "after": observed.get("after"),
    }


def main() -> int:
    try:
        authority = json.load(sys.stdin)
        if not isinstance(authority, dict):
            raise KubernetesCapabilityError("authority must be an object", code="invalid_request")
        result = execute_authorized_exact_operation(
            authority,
            store_root=Path(os.environ["AMOF_ROMULO_PRODUCT_STORE_DIR"]),
            registry_path=Path(os.environ["AMOF_ROMULO_TARGETS_REGISTRY"]),
        )
        json.dump(result, sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except (KubernetesCapabilityError, KeyError, ValueError) as exc:
        json.dump({"ok": False, "code": getattr(exc, "code", "invalid_request")}, sys.stdout)
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
