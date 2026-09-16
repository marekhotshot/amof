"""Live Kubernetes execution gate — authority before transport, then verify."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from amof.capability import CAPABILITY_KUBERNETES_MUTATE, CAPABILITY_KUBERNETES_READ
from amof.commands.runner import HERMES_DENIED_CAPABILITIES
from amof.execution_backends.hermes_opensandbox import DANGEROUS_CAPABILITIES
from amof.kubernetes_capability import (
    ACCEPTANCE_FAIL,
    ACCEPTANCE_PASS,
    ACCEPTANCE_UNVERIFIED,
    ALLOWED_ANNOTATION_KEY,
    FixtureKubernetesExecutor,
    KubernetesCapabilityError,
    KubernetesCapabilityStore,
    KubernetesOperation,
    approve_proposal,
    execute_kubernetes_capability,
    normalize_patch,
    propose_kubernetes_capability,
    revoke_approval,
)
from amof.kubernetes_live import (
    KubectlTransport,
    LiveKubernetesExecutor,
    RecordingTransport,
    ResolvedClusterTarget,
    save_target_registry,
)


MISSION = "AMOF-K8S-LIVE-EXECUTION-GATE-001"
OPERATOR = "operator:marek"
WORKER = "worker:demo"
CLUSTER = "local-kind"
NAMESPACE = "amof-cap-test"
OTHER_NAMESPACE = "other-ns"
DEPLOYMENT = "demo"
ANNOTATION_VALUE = "amof-live-probe-1"
LIVE_CLUSTER = "amof-cap-test"


def _store(home: str) -> KubernetesCapabilityStore:
    root = Path(home) / "share" / "capabilities" / "kubernetes"
    return KubernetesCapabilityStore(
        proposals_dir=root / "proposals",
        approvals_dir=root / "approvals",
        bindings_dir=root / "bindings",
        receipts_dir=root / "receipts",
        revocations_dir=root / "revocations",
        events_dir=root / "events",
    )


def _deployment_object(
    *,
    namespace: str = NAMESPACE,
    name: str = DEPLOYMENT,
    resource_version: str = "1",
    annotation: str | None = None,
) -> dict:
    annotations = {} if annotation is None else {ALLOWED_ANNOTATION_KEY: annotation}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": "uid-demo",
            "resourceVersion": resource_version,
            "generation": 1,
            "annotations": annotations,
        },
    }


def _write_dummy_kubeconfig(path: Path) -> Path:
    path.write_text("# dummy kubeconfig for path resolution only\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class LiveExecutionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.home = self._tmpdir.name
        self.store = _store(self.home)
        self.kubeconfig = _write_dummy_kubeconfig(Path(self.home) / "local-kind.kubeconfig")
        self.registry = {
            CLUSTER: ResolvedClusterTarget(
                cluster=CLUSTER,
                kubeconfig=self.kubeconfig,
                context="k3d-amof-cap-test",
            )
        }
        self.transport = RecordingTransport(
            objects={(NAMESPACE, DEPLOYMENT): _deployment_object()}
        )
        self.executor = LiveKubernetesExecutor(
            transport=self.transport,
            registry=self.registry,
        )
        self.env = patch.dict(os.environ, {"AMOF_HOME": self.home}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _body(self, **overrides):
        body = {
            "capability": CAPABILITY_KUBERNETES_MUTATE,
            "cluster": CLUSTER,
            "namespace": NAMESPACE,
            "verbs": ["get", "patch"],
            "resources": ["deployments"],
            "reason": "live deployment annotation probe",
        }
        body.update(overrides)
        return body

    def _propose_approve(self, body=None):
        proposal = propose_kubernetes_capability(
            run_id="run-live-001",
            body=body or self._body(),
            requested_by=WORKER,
            store=self.store,
        )
        approval = approve_proposal(
            proposal["proposal_id"],
            ttl="30m",
            approved_by=OPERATOR,
            store=self.store,
        )
        return approval

    def _op(self, **overrides) -> KubernetesOperation:
        values = dict(
            cluster=CLUSTER,
            namespace=NAMESPACE,
            verb="patch",
            resource="deployments",
            name=DEPLOYMENT,
            patch={"annotation": {"key": ALLOWED_ANNOTATION_KEY, "value": ANNOTATION_VALUE}},
            mission_id=MISSION,
            requested_by=WORKER,
            run_id="exec-live-001",
        )
        values.update(overrides)
        return KubernetesOperation(**values)

    def test_runner_and_hermes_still_deny_raw_kubernetes_mutation(self) -> None:
        self.assertIn("kubernetes_mutation", HERMES_DENIED_CAPABILITIES)
        self.assertIn("kubernetes", HERMES_DENIED_CAPABILITIES)
        self.assertIn("kubernetes_mutation", DANGEROUS_CAPABILITIES)

    def test_raw_patch_payload_is_rejected(self) -> None:
        with self.assertRaises(KubernetesCapabilityError) as ctx:
            normalize_patch({"spec": {"replicas": 9}})
        self.assertEqual(ctx.exception.code, "invalid_patch")
        with self.assertRaises(KubernetesCapabilityError):
            normalize_patch(
                {"annotation": {"key": "evil.dev/x", "value": ANNOTATION_VALUE}}
            )

    def test_approved_get_and_annotation_patch_pass(self) -> None:
        approval = self._propose_approve()
        got = execute_kubernetes_capability(
            operation=self._op(verb="get", patch=None, run_id="exec-get"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertTrue(got.ok)
        self.assertEqual(got.receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertEqual(got.receipt["operation"]["verb"], "get")
        self.assertEqual(got.receipt["verification"]["method"], "live_readback_compare")
        patched = execute_kubernetes_capability(
            operation=self._op(run_id="exec-patch"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertTrue(patched.ok)
        receipt = patched.receipt
        self.assertEqual(receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertEqual(receipt["mission_id"], MISSION)
        self.assertEqual(receipt["requested_by"], WORKER)
        self.assertEqual(receipt["target"]["cluster"], CLUSTER)
        self.assertEqual(receipt["target"]["namespace"], NAMESPACE)
        self.assertEqual(receipt["target"]["resource"], "deployments")
        self.assertEqual(receipt["target"]["name"], DEPLOYMENT)
        self.assertEqual(receipt["operation"]["verb"], "patch")
        self.assertEqual(receipt["operation"]["name"], "annotation_patch")
        self.assertTrue(receipt["change"]["changed"])
        self.assertTrue(str(receipt["change"]["before_digest"]).startswith("sha256:"))
        self.assertTrue(str(receipt["change"]["after_digest"]).startswith("sha256:"))
        self.assertNotEqual(receipt["change"]["before_digest"], receipt["change"]["after_digest"])
        self.assertNotEqual(
            receipt["change"]["before_resource_version"],
            receipt["change"]["after_resource_version"],
        )
        self.assertTrue(receipt["secrets_omitted"])
        dumped = json.dumps(receipt)
        self.assertNotIn("dummy kubeconfig", dumped)
        self.assertNotIn(str(self.kubeconfig), dumped)
        self.assertNotIn("bearer", dumped.lower())
        self.assertIn("patch", [call["op"] for call in self.transport.calls])

    def test_unverified_when_readback_cannot_prove_postcondition(self) -> None:
        approval = self._propose_approve()
        self.transport.fail_readback = True
        outcome = execute_kubernetes_capability(
            operation=self._op(run_id="exec-unverified"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "unverified")
        self.assertEqual(outcome.receipt["acceptance_state"], ACCEPTANCE_UNVERIFIED)
        self.assertNotEqual(outcome.receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertTrue(any(call["op"] == "patch" for call in self.transport.calls))

    def _assert_rejected_before_transport(self, outcome, code: str) -> None:
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, code)
        self.assertEqual(self.transport.calls, [])
        if outcome.receipt is not None:
            self.assertEqual(outcome.receipt["acceptance_state"], ACCEPTANCE_FAIL)
            self.assertFalse(outcome.receipt["executed"])

    def test_no_approval_does_not_invoke_transport(self) -> None:
        outcome = execute_kubernetes_capability(
            operation=self._op(),
            executor=self.executor,
            store=self.store,
        )
        self._assert_rejected_before_transport(outcome, "no_approval")

    def test_no_binding_does_not_invoke_transport(self) -> None:
        outcome = execute_kubernetes_capability(
            operation=self._op(),
            binding_id="kcb-" + ("0" * 24),
            executor=self.executor,
            store=self.store,
        )
        self._assert_rejected_before_transport(outcome, "no_binding")

    def test_expired_does_not_invoke_transport(self) -> None:
        proposal = propose_kubernetes_capability(
            run_id="run-expired",
            body=self._body(),
            requested_by=WORKER,
            store=self.store,
        )
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        approval = approve_proposal(
            proposal["proposal_id"],
            ttl="1h",
            approved_by=OPERATOR,
            store=self.store,
            now=past,
        )
        outcome = execute_kubernetes_capability(
            operation=self._op(),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
            now=datetime.now(timezone.utc),
        )
        self._assert_rejected_before_transport(outcome, "expired")

    def test_revoked_does_not_invoke_transport(self) -> None:
        approval = self._propose_approve()
        revoke_approval(
            approval["approval_id"],
            reason="operator abort",
            revoked_by=OPERATOR,
            store=self.store,
        )
        outcome = execute_kubernetes_capability(
            operation=self._op(),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self._assert_rejected_before_transport(outcome, "revoked")

    def test_wrong_cluster_does_not_invoke_transport(self) -> None:
        approval = self._propose_approve()
        outcome = execute_kubernetes_capability(
            operation=self._op(cluster="other-cluster", run_id="exec-wrong-cluster"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self._assert_rejected_before_transport(outcome, "wrong_cluster")

    def test_wrong_namespace_does_not_invoke_transport(self) -> None:
        approval = self._propose_approve()
        outcome = execute_kubernetes_capability(
            operation=self._op(namespace=OTHER_NAMESPACE, run_id="exec-wrong-ns"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self._assert_rejected_before_transport(outcome, "wrong_namespace")

    def test_wrong_resource_does_not_invoke_transport(self) -> None:
        approval = self._propose_approve()
        outcome = execute_kubernetes_capability(
            operation=self._op(resource="pods", run_id="exec-wrong-res"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self._assert_rejected_before_transport(outcome, "wrong_resource")

    def test_wrong_verb_does_not_invoke_transport(self) -> None:
        approval = self._propose_approve(self._body(verbs=["get"]))
        outcome = execute_kubernetes_capability(
            operation=self._op(run_id="exec-wrong-verb"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self._assert_rejected_before_transport(outcome, "wrong_verb")

    def test_read_grant_cannot_patch_and_does_not_invoke_transport(self) -> None:
        approval = self._propose_approve(
            self._body(capability=CAPABILITY_KUBERNETES_READ, verbs=["get"])
        )
        outcome = execute_kubernetes_capability(
            operation=self._op(run_id="exec-read-escalate"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self._assert_rejected_before_transport(outcome, "scope_escalation")

    def test_denied_target_does_not_invoke_transport(self) -> None:
        approval = self._propose_approve(self._body(denied_namespaces=[NAMESPACE]))
        outcome = execute_kubernetes_capability(
            operation=self._op(run_id="exec-denied"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self._assert_rejected_before_transport(outcome, "wrong_namespace")

    def test_consumed_grant_reuse_does_not_invoke_transport(self) -> None:
        approval = self._propose_approve()
        first = execute_kubernetes_capability(
            operation=self._op(run_id="exec-1"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertTrue(first.ok)
        calls_after_first = list(self.transport.calls)
        self.assertTrue(calls_after_first)
        second = execute_kubernetes_capability(
            operation=self._op(
                patch={"annotation": {"key": ALLOWED_ANNOTATION_KEY, "value": "amof-live-probe-2"}},
                run_id="exec-2",
            ),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(second.ok)
        self.assertEqual(second.code, "reused_single_use")
        self.assertEqual(self.transport.calls, calls_after_first)

    def test_kubectl_transport_builds_argv_without_shell(self) -> None:
        recorded: list[dict] = []

        def runner(argv, **kwargs):
            recorded.append({"argv": list(argv), "kwargs": kwargs})

            class Result:
                returncode = 0
                stdout = json.dumps(_deployment_object())
                stderr = ""

            return Result()

        transport = KubectlTransport(kubectl="/usr/bin/kubectl", runner=runner)
        transport.patch_deployment_annotation(
            kubeconfig=str(self.kubeconfig),
            context="k3d-amof-cap-test",
            namespace=NAMESPACE,
            name=DEPLOYMENT,
            key=ALLOWED_ANNOTATION_KEY,
            value=ANNOTATION_VALUE,
        )
        self.assertEqual(len(recorded), 1)
        self.assertFalse(recorded[0]["kwargs"].get("shell"))
        argv = recorded[0]["argv"]
        self.assertEqual(argv[0], "/usr/bin/kubectl")
        self.assertEqual(argv[1:5], ["--kubeconfig", str(self.kubeconfig), "--context", "k3d-amof-cap-test"])
        self.assertIn("patch", argv)
        self.assertIn("deployment", argv)
        self.assertNotIn("delete", argv)
        self.assertNotIn("apply", argv)
        self.assertNotIn("create", argv)

    def test_fixture_annotation_patch_still_works(self) -> None:
        approval = self._propose_approve(self._body(cluster="local-fixture", namespace="demo"))
        fixture = FixtureKubernetesExecutor()
        outcome = execute_kubernetes_capability(
            operation=self._op(cluster="local-fixture", namespace="demo", name="web", run_id="exec-fix"),
            approval_id=approval["approval_id"],
            executor=fixture,
            store=self.store,
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(
            fixture.state["namespaces"]["demo"]["deployments"]["web"]["annotations"][ALLOWED_ANNOTATION_KEY],
            ANNOTATION_VALUE,
        )


def _live_flag_enabled() -> bool:
    return os.environ.get("AMOF_K8S_LIVE_TEST", "").strip().lower() in {"1", "true", "yes"}


def _have_k3d() -> bool:
    return shutil.which("k3d") is not None and shutil.which("kubectl") is not None


@unittest.skipUnless(_live_flag_enabled() and _have_k3d(), "set AMOF_K8S_LIVE_TEST=1 with k3d+kubectl")
class LiveKubernetesIntegrationTests(unittest.TestCase):
    cluster_name = LIVE_CLUSTER
    kubeconfig_path: Path
    _tmpdir: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.kubeconfig_path = Path(cls._tmpdir.name) / "amof-cap-test.kubeconfig"
        subprocess.run(
            ["k3d", "cluster", "delete", cls.cluster_name],
            check=False,
            capture_output=True,
            text=True,
        )
        created = subprocess.run(
            ["k3d", "cluster", "create", cls.cluster_name, "--wait", "--timeout", "180s"],
            check=False,
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            cls._tmpdir.cleanup()
            raise unittest.SkipTest(f"k3d cluster create failed: {created.stderr}")
        written = subprocess.run(
            ["k3d", "kubeconfig", "write", cls.cluster_name, "--output", str(cls.kubeconfig_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if written.returncode != 0 or not cls.kubeconfig_path.is_file():
            subprocess.run(["k3d", "cluster", "delete", cls.cluster_name], check=False)
            cls._tmpdir.cleanup()
            raise unittest.SkipTest(f"k3d kubeconfig write failed: {written.stderr}")
        env = os.environ.copy()
        env["KUBECONFIG"] = str(cls.kubeconfig_path)
        context = f"k3d-{cls.cluster_name}"
        setup = [
            ["kubectl", "--context", context, "create", "namespace", NAMESPACE],
            [
                "kubectl",
                "--context",
                context,
                "-n",
                NAMESPACE,
                "create",
                "deployment",
                DEPLOYMENT,
                "--image=nginx:1.27-alpine",
            ],
        ]
        for argv in setup:
            completed = subprocess.run(argv, check=False, capture_output=True, text=True, env=env)
            if completed.returncode != 0:
                subprocess.run(["k3d", "cluster", "delete", cls.cluster_name], check=False)
                cls._tmpdir.cleanup()
                raise unittest.SkipTest(f"fixture setup failed: {completed.stderr}")
        cls.context = context

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(["k3d", "cluster", "delete", cls.cluster_name], check=False, capture_output=True)
        cls._tmpdir.cleanup()

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.home = self._home.name
        self.store = _store(self.home)
        save_target_registry(
            {
                CLUSTER: {
                    "kubeconfig": str(self.kubeconfig_path),
                    "context": self.context,
                }
            },
            path=Path(self.home) / "targets.json",
        )
        self.env = patch.dict(
            os.environ,
            {
                "AMOF_HOME": self.home,
                "AMOF_K8S_TARGETS_FILE": str(Path(self.home) / "targets.json"),
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.transport = KubectlTransport()
        self.executor = LiveKubernetesExecutor(transport=self.transport)

    def _grant(self):
        proposal = propose_kubernetes_capability(
            run_id="run-live-int",
            body={
                "capability": CAPABILITY_KUBERNETES_MUTATE,
                "cluster": CLUSTER,
                "namespace": NAMESPACE,
                "verbs": ["get", "patch"],
                "resources": ["deployments"],
                "reason": "live k3d annotation probe",
            },
            requested_by=WORKER,
            store=self.store,
        )
        return approve_proposal(
            proposal["proposal_id"],
            ttl="30m",
            approved_by=OPERATOR,
            store=self.store,
        )

    def _op(self, **overrides) -> KubernetesOperation:
        values = dict(
            cluster=CLUSTER,
            namespace=NAMESPACE,
            verb="patch",
            resource="deployments",
            name=DEPLOYMENT,
            patch={"annotation": {"key": ALLOWED_ANNOTATION_KEY, "value": ANNOTATION_VALUE}},
            mission_id=MISSION,
            requested_by=WORKER,
            run_id="exec-live-int",
        )
        values.update(overrides)
        return KubernetesOperation(**values)

    def test_wrong_namespace_then_approved_patch_then_reuse(self) -> None:
        approval = self._grant()
        denied = execute_kubernetes_capability(
            operation=self._op(namespace=OTHER_NAMESPACE, run_id="exec-oos"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(denied.ok)
        self.assertEqual(denied.code, "wrong_namespace")
        self.assertEqual(self.transport.calls, [])
        allowed = execute_kubernetes_capability(
            operation=self._op(run_id="exec-ok"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertTrue(any(argv[0].endswith("kubectl") or argv[0] == "kubectl" for argv in self.transport.calls))
        self.assertGreaterEqual(len(self.transport.calls), 3)
        after = allowed.receipt["change"]
        self.assertTrue(after["changed"])
        self.assertNotEqual(after["before_resource_version"], after["after_resource_version"])
        env = os.environ.copy()
        env["KUBECONFIG"] = str(self.kubeconfig_path)
        observed = subprocess.run(
            [
                "kubectl",
                "--context",
                self.context,
                "-n",
                NAMESPACE,
                "get",
                "deployment",
                DEPLOYMENT,
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        live_obj = json.loads(observed.stdout)
        self.assertEqual(
            (live_obj.get("metadata") or {}).get("annotations", {}).get(ALLOWED_ANNOTATION_KEY),
            ANNOTATION_VALUE,
        )
        reuse = execute_kubernetes_capability(
            operation=self._op(
                patch={"annotation": {"key": ALLOWED_ANNOTATION_KEY, "value": "amof-live-probe-2"}},
                run_id="exec-reuse",
            ),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(reuse.ok)
        self.assertEqual(reuse.code, "reused_single_use")


if __name__ == "__main__":
    unittest.main()
