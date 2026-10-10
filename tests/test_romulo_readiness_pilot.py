"""Offline Romulo case: one exact Deployment readiness timeout, no cluster calls."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from amof.capability import CAPABILITY_KUBERNETES_MUTATE, CapabilityBodyError, normalize_kubernetes_capability_body
from amof.kubernetes_capability import (
    ACCEPTANCE_PASS, ACCEPTANCE_UNVERIFIED, KubernetesCapabilityError,
    KubernetesCapabilityStore, KubernetesOperation, approve_proposal, create_binding,
    execute_kubernetes_capability, load_approval, normalize_patch,
    propose_kubernetes_capability,
)
from amof.kubernetes_live import KubectlTransport, LiveKubernetesExecutor, RecordingTransport, ResolvedClusterTarget


SENTINEL = "SYNTHETIC_SECRET_DO_NOT_EMIT_7863"
CLUSTER = "cluster-syn-01"
NAMESPACE = "apps-syn"
DEPLOYMENT = "invoice-worker"
CONTAINER = "invoice-worker"
PATCH = {"readinessProbe": {
    "container": CONTAINER, "expectedTimeoutSeconds": 1, "timeoutSeconds": 3,
}}


def _object(containers=None):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {
            "name": DEPLOYMENT, "namespace": NAMESPACE, "uid": "uid-syn-01",
            "resourceVersion": "17", "generation": 4,
            "annotations": {"unrelated.example/sentinel": SENTINEL},
        },
        "spec": {"replicas": 1, "template": {"spec": {
            "containers": containers if containers is not None else [{
                "name": CONTAINER,
                "image": "example.invalid/invoice-worker@sha256:" + "a" * 64,
                "env": [{"name": "SYNTHETIC_SECRET", "value": SENTINEL}],
                "readinessProbe": {"timeoutSeconds": 1, "periodSeconds": 10},
            }],
        }}},
        "status": {"readyReplicas": 0},
    }


def _body():
    return {
        "capability": CAPABILITY_KUBERNETES_MUTATE,
        "cluster": CLUSTER, "namespaces": [NAMESPACE],
        "verbs": ["patch"], "resources": ["deployments"],
        "denied_namespaces": ["kube-system"],
        "denied_resources": ["secrets", "clusterroles"],
        "reason": "SYNTHETIC ADO-SYN-1042 readiness timeout 1 to 3",
        "exact_operation": {
            "namespace": NAMESPACE, "verb": "patch", "resource": "deployments",
            "name": DEPLOYMENT, "patch": PATCH,
        },
    }


class RomuloReadinessPilotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="romulo-readiness-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.store = KubernetesCapabilityStore(
            proposals_dir=root / "proposals", approvals_dir=root / "approvals",
            bindings_dir=root / "bindings", receipts_dir=root / "receipts",
            revocations_dir=root / "revocations", events_dir=root / "events",
        )
        kubeconfig = root / "synthetic.kubeconfig"
        kubeconfig.write_text("synthetic path only\n", encoding="utf-8")
        self.transport = RecordingTransport({(NAMESPACE, DEPLOYMENT): _object()})
        self.executor = LiveKubernetesExecutor(
            transport=self.transport,
            registry={CLUSTER: ResolvedClusterTarget(CLUSTER, kubeconfig, "synthetic-context")},
        )

    def _op(self, **changes):
        values = dict(
            cluster=CLUSTER, namespace=NAMESPACE, verb="patch", resource="deployments",
            name=DEPLOYMENT, patch=PATCH, mission_id="ROMULO-SYN-001",
            requested_by="worker:synthetic", run_id="synthetic-run-001",
        )
        values.update(changes)
        return KubernetesOperation(**values)

    def _approval(self, *, now=None, ttl="30m"):
        proposal = propose_kubernetes_capability(
            run_id="synthetic-proposal-001", body=_body(), requested_by="worker:synthetic",
            store=self.store,
        )
        return approve_proposal(
            proposal["proposal_id"], ttl=ttl, approved_by="operator:synthetic-simulated",
            now=now, store=self.store,
        )

    def _execute(self, approval_id=None, **changes):
        return execute_kubernetes_capability(
            operation=self._op(**changes), approval_id=approval_id,
            executor=self.executor, store=self.store,
        )

    def test_matching_simulated_approval_executes_once_with_observed_readback(self):
        approval = self._approval()
        result = self._execute(approval["approval_id"])
        self.assertTrue(result.ok)
        self.assertEqual(result.receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertEqual(result.receipt["operation"]["name"], "readiness_timeout_patch")
        self.assertEqual(result.receipt["verification"]["method"], "live_readback_compare")
        self.assertEqual([call["op"] for call in self.transport.calls], ["get", "patch_readiness", "get"])
        self.assertEqual(
            self.transport.objects[(NAMESPACE, DEPLOYMENT)]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["timeoutSeconds"], 3,
        )
        self.assertEqual(load_approval(approval["approval_id"], store=self.store)["status"], "consumed")
        self.assertNotIn(SENTINEL, json.dumps(result.receipt))

    def test_missing_expired_consumed_and_mismatched_approvals_have_zero_transport_calls(self):
        self.assertEqual(self._execute().code, "no_approval")
        self.assertEqual(self.transport.calls, [])
        expired = self._approval(now=datetime.now(timezone.utc) - timedelta(hours=2), ttl="1h")
        self.assertEqual(self._execute(expired["approval_id"]).code, "expired")
        self.assertEqual(self.transport.calls, [])
        approval = self._approval()
        variants = (
            {"cluster": "other-cluster"}, {"namespace": "kube-system"},
            {"name": "payment-api"}, {"verb": "get", "patch": None},
            {"resource": "secrets"},
            {"patch": {"readinessProbe": {"container": "other-container", "expectedTimeoutSeconds": 1, "timeoutSeconds": 3}}},
        )
        for index, variant in enumerate(variants):
            denied = self._execute(approval["approval_id"], run_id=f"denied-{index}", **variant)
            self.assertFalse(denied.ok)
            self.assertFalse(denied.receipt["executed"])
            self.assertEqual(self.transport.calls, [])
        accepted = self._execute(approval["approval_id"], run_id="accepted-once")
        self.assertTrue(accepted.ok)
        self.transport.calls.clear()
        self.assertEqual(self._execute(approval["approval_id"], run_id="reused").code, "reused_single_use")
        self.assertEqual(self.transport.calls, [])

    def test_expired_or_mismatched_active_binding_has_zero_transport_calls(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=2)
        approval = self._approval(now=old, ttl="1m")
        # Build an active Binding with the original clock, then evaluate at current time.
        binding = create_binding(approval["approval_id"], run_id="bound-run", store=self.store, now=old)
        expired = execute_kubernetes_capability(
            operation=self._op(run_id="bound-run"), binding_id=binding["binding_id"],
            executor=self.executor, store=self.store,
        )
        self.assertEqual(expired.code, "expired")
        self.assertEqual(self.transport.calls, [])
        fresh = self._approval(now=datetime.now(timezone.utc) + timedelta(seconds=2))
        other = create_binding(fresh["approval_id"], run_id="bound-run-2", store=self.store)
        wrong_run = execute_kubernetes_capability(
            operation=self._op(run_id="different-run"), binding_id=other["binding_id"],
            executor=self.executor, store=self.store,
        )
        self.assertEqual(wrong_run.code, "no_binding")
        self.assertEqual(self.transport.calls, [])

    def test_kubectl_json_patch_uses_only_guarded_probe_path_and_stops_on_warning(self):
        captured = []

        def runner(argv, **kwargs):
            captured.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(_object()), "")

        transport = KubectlTransport(kubectl="kubectl", runner=runner)
        transport.patch_deployment_readiness_timeout(
            kubeconfig="/synthetic/path", context="synthetic-context", namespace=NAMESPACE,
            name=DEPLOYMENT, uid="uid-syn-01", resource_version="17",
            container_index=0, container=CONTAINER,
        )
        argv = captured[0]
        self.assertEqual(argv[argv.index("--type") + 1], "json")
        operations = json.loads(argv[argv.index("-p") + 1])
        self.assertEqual([item["op"] for item in operations], ["test"] * 4 + ["replace"])
        self.assertEqual(operations[-1], {
            "op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/timeoutSeconds", "value": 3,
        })
        self.assertEqual(operations[-2]["value"], 1)

        def warned(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, json.dumps(_object()), "synthetic warning")

        with self.assertRaises(KubernetesCapabilityError) as caught:
            KubectlTransport(kubectl="kubectl", runner=warned).get_deployment(
                kubeconfig="/synthetic/path", context="synthetic-context",
                namespace=NAMESPACE, name=DEPLOYMENT,
            )
        self.assertEqual(caught.exception.code, "unverified")

    def test_invalid_patch_shapes_reject_before_transport(self):
        invalid = (
            {"readinessProbe": {"container": CONTAINER, "timeoutSeconds": 3}},
            {"readinessProbe": {"container": CONTAINER, "expectedTimeoutSeconds": 2, "timeoutSeconds": 3}},
            {"readinessProbe": {"container": CONTAINER, "expectedTimeoutSeconds": 1, "timeoutSeconds": 4}},
            {"readinessProbe": {"container": CONTAINER, "expectedTimeoutSeconds": 1, "timeoutSeconds": 3, "extra": True}},
            {"readinessProbe": {"container": CONTAINER, "expectedTimeoutSeconds": 1, "timeoutSeconds": 3}, "replicas": 2},
        )
        for patch in invalid:
            with self.assertRaises(KubernetesCapabilityError):
                self._execute(patch=patch)
        self.assertEqual(self.transport.calls, [])
        with self.assertRaises(CapabilityBodyError):
            normalize_kubernetes_capability_body({**_body(), "exact_operation": {
                **_body()["exact_operation"], "patch": invalid[0],
            }})

    def test_ambiguous_container_and_wrong_preimage_never_patch(self):
        approval = self._approval()
        self.transport.objects[(NAMESPACE, DEPLOYMENT)] = _object(containers=[
            {"name": CONTAINER, "readinessProbe": {"timeoutSeconds": 1}},
            {"name": CONTAINER, "readinessProbe": {"timeoutSeconds": 1}},
        ])
        result = self._execute(approval["approval_id"])
        self.assertFalse(result.ok)
        self.assertNotEqual(result.receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertEqual([call["op"] for call in self.transport.calls], ["get"])

    def test_failed_readback_and_unexpected_change_cannot_pass(self):
        approval = self._approval()
        self.transport.fail_readback = True
        result = self._execute(approval["approval_id"])
        self.assertFalse(result.ok)
        self.assertEqual(result.receipt["acceptance_state"], ACCEPTANCE_UNVERIFIED)
        self.assertEqual(load_approval(approval["approval_id"], store=self.store)["status"], "consumed")
        self.transport.calls.clear()
        self.assertEqual(self._execute(approval["approval_id"]).code, "reused_single_use")
        self.assertEqual(self.transport.calls, [])

        class ChangingTransport(RecordingTransport):
            def patch_deployment_readiness_timeout(self, **kwargs):
                result = super().patch_deployment_readiness_timeout(**kwargs)
                self.objects[(NAMESPACE, DEPLOYMENT)]["spec"]["replicas"] = 5
                return result

        self.transport = ChangingTransport({(NAMESPACE, DEPLOYMENT): _object()})
        self.executor.transport = self.transport
        approval2 = self._approval(now=datetime.now(timezone.utc) + timedelta(seconds=2))
        result2 = self._execute(approval2["approval_id"], run_id="unexpected-change")
        self.assertFalse(result2.ok)
        self.assertEqual(result2.receipt["acceptance_state"], ACCEPTANCE_UNVERIFIED)

    def test_warnings_missing_evidence_and_unsanitized_result_cannot_pass(self):
        class AlteredExecutor:
            def __init__(self, underlying, alteration):
                self.underlying = underlying
                self.alteration = alteration

            def execute(self, operation):
                result = self.underlying.execute(operation)
                self.alteration(result)
                return result

        cases = (
            lambda result: result.update(warnings=["synthetic warning"]),
            lambda result: result.pop("after_digest"),
            lambda result: result.update(secret_value=SENTINEL),
        )
        for index, change in enumerate(cases):
            self.transport.objects[(NAMESPACE, DEPLOYMENT)] = _object()
            self.transport.calls.clear()
            approval = self._approval(now=datetime.now(timezone.utc) + timedelta(seconds=index + 1))
            outcome = execute_kubernetes_capability(
                operation=self._op(run_id=f"altered-{index}"), approval_id=approval["approval_id"],
                executor=AlteredExecutor(self.executor, change), store=self.store,
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.receipt["acceptance_state"], ACCEPTANCE_UNVERIFIED)
            self.assertEqual(load_approval(approval["approval_id"], store=self.store)["status"], "consumed")
            self.assertNotIn(SENTINEL, json.dumps(outcome.receipt))


if __name__ == "__main__":
    unittest.main()
