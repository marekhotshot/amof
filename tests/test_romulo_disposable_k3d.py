"""Opt-in integration against the specifically named disposable Romulo k3d cluster."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from amof.kubernetes_capability import (
    ACCEPTANCE_PASS, KubernetesCapabilityStore, KubernetesOperation,
    approve_proposal, execute_kubernetes_capability, propose_kubernetes_capability,
)
from amof.kubernetes_live import KubectlTransport, LiveKubernetesExecutor, ResolvedClusterTarget
from tests.test_romulo_readiness_pilot import CLUSTER, CONTAINER, DEPLOYMENT, NAMESPACE, PATCH, SENTINEL, _body


CONTEXT = "k3d-romulo-syn-20261010"


@unittest.skipUnless(os.environ.get("AMOF_ROMULO_DISPOSABLE_K3D") == "1", "opt-in disposable k3d only")
class RomuloDisposableK3dTest(unittest.TestCase):
    def test_exact_patch_and_independent_readback(self):
        kubeconfig = Path(os.environ["AMOF_ROMULO_DISPOSABLE_KUBECONFIG"])
        if kubeconfig.name != "kubeconfig-romulo-syn-20261010.yaml" or not kubeconfig.is_file():
            self.fail("refusing to use a different kubeconfig")
        base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", CONTEXT]

        def kubectl(*args, payload=None):
            result = subprocess.run(
                [*base, *args], input=None if payload is None else json.dumps(payload),
                text=True, capture_output=True, check=False, timeout=30,
            )
            if result.returncode != 0 or result.stderr.strip():
                self.fail("disposable kubectl command failed or warned")
            return json.loads(result.stdout) if result.stdout.strip().startswith("{") else result.stdout

        self.assertEqual(kubectl("config", "current-context").strip(), CONTEXT)
        namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}
        kubectl("apply", "-f", "-", payload=namespace)
        deployment = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": DEPLOYMENT, "namespace": NAMESPACE},
            "spec": {
                "replicas": 1, "selector": {"matchLabels": {"app": DEPLOYMENT}},
                "template": {
                    "metadata": {"labels": {"app": DEPLOYMENT}},
                    "spec": {"containers": [{
                        "name": CONTAINER, "image": "registry.k8s.io/pause:3.10",
                        "env": [{"name": "SYNTHETIC_SENTINEL", "value": SENTINEL}],
                        "readinessProbe": {"exec": {"command": ["/pause"]}, "timeoutSeconds": 1},
                    }]},
                },
            },
        }
        try:
            kubectl("-n", NAMESPACE, "apply", "-f", "-", payload=deployment)
            with tempfile.TemporaryDirectory(prefix="romulo-k3d-authority-") as home:
                root = Path(home)
                store = KubernetesCapabilityStore(
                    proposals_dir=root / "proposals", approvals_dir=root / "approvals",
                    bindings_dir=root / "bindings", receipts_dir=root / "receipts",
                    revocations_dir=root / "revocations", events_dir=root / "events",
                )
                proposal = propose_kubernetes_capability(
                    run_id="romulo-disposable-k3d", body=_body(),
                    requested_by="worker:synthetic", store=store,
                )
                approval = approve_proposal(
                    proposal["proposal_id"], ttl="5m",
                    approved_by="operator:synthetic-simulated", store=store,
                )
                operation = KubernetesOperation(
                    cluster=CLUSTER, namespace=NAMESPACE, resource="deployments",
                    name=DEPLOYMENT, verb="patch", patch=PATCH,
                    run_id="romulo-disposable-k3d-exec", mission_id="ROMULO-SYN-001",
                    requested_by="worker:synthetic",
                )
                class CaptureTransport(KubectlTransport):
                    def __init__(self):
                        super().__init__()
                        self.reads = []

                    def get_deployment(self, **kwargs):
                        obj = super().get_deployment(**kwargs)
                        self.reads.append(obj)
                        return obj

                capture_transport = CaptureTransport()
                executor = LiveKubernetesExecutor(transport=capture_transport, registry={
                    CLUSTER: ResolvedClusterTarget(CLUSTER, kubeconfig, CONTEXT),
                })
                captured = {}

                class Capture:
                    def execute(self, requested):
                        captured.update(executor.execute(requested))
                        return captured

                outcome = execute_kubernetes_capability(
                    operation=operation, approval_id=approval["approval_id"],
                    executor=Capture(), store=store,
                )
                changed_paths = []
                if len(capture_transport.reads) == 2:
                    def paths(left, right, prefix=""):
                        if isinstance(left, dict) and isinstance(right, dict):
                            for key in sorted(set(left) | set(right)):
                                paths(left.get(key), right.get(key), prefix + "/" + key)
                        elif isinstance(left, list) and isinstance(right, list):
                            for index in range(max(len(left), len(right))):
                                paths(
                                    left[index] if index < len(left) else None,
                                    right[index] if index < len(right) else None,
                                    prefix + "/" + str(index),
                                )
                        elif left != right:
                            changed_paths.append(prefix)
                    paths(*capture_transport.reads)
                self.assertTrue(outcome.ok, {
                    "code": outcome.code,
                    "stop_reason": outcome.receipt.get("stop_reason") if outcome.receipt else None,
                    "verification": outcome.receipt.get("verification") if outcome.receipt else None,
                    "adapter_reason": captured.get("verification_reason"),
                    "safe_before": captured.get("before"), "safe_after": captured.get("after"),
                    "changed_paths_only": changed_paths,
                })
                self.assertEqual(outcome.receipt["acceptance_state"], ACCEPTANCE_PASS)
                self.assertNotIn(SENTINEL, json.dumps(outcome.receipt))
                observed = kubectl("-n", NAMESPACE, "get", "deployment", DEPLOYMENT, "-o", "json")
                self.assertEqual(
                    observed["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["timeoutSeconds"], 3,
                )
        finally:
            kubectl("delete", "namespace", NAMESPACE, "--wait=true", "--timeout=60s")


if __name__ == "__main__":
    unittest.main()
