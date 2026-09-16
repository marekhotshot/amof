"""Kubernetes capability authority v0 — contract, negative, and demo tests."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from amof.capability import (
    CAPABILITY_KUBERNETES_MUTATE,
    CAPABILITY_KUBERNETES_READ,
    CapabilityBodyError,
    normalize_kubernetes_capability_body,
)
from amof.commands import scope as scope_cmd
from amof.kubernetes_capability import (
    ACCEPTANCE_FAIL,
    ACCEPTANCE_PASS,
    ACCEPTANCE_UNVERIFIED,
    COMPLIANCE_UNVERIFIED,
    COMPLIANCE_WITHIN_SCOPE,
    DEFAULT_FIXTURE_CLUSTER,
    FixtureKubernetesExecutor,
    KubernetesCapabilityError,
    KubernetesCapabilityInterrupted,
    KubernetesCapabilityStore,
    KubernetesOperation,
    approve_proposal,
    create_binding,
    execute_kubernetes_capability,
    load_approval,
    propose_kubernetes_capability,
    revoke_approval,
)


OPERATOR = "operator:marek"
WORKER = "worker:demo"
MISSION = "AMOF-CAPABILITY-AUTHORITY-V0-K8S-001"


def _body(**overrides):
    body = {
        "capability": CAPABILITY_KUBERNETES_MUTATE,
        "cluster": DEFAULT_FIXTURE_CLUSTER,
        "namespace": "demo",
        "verbs": ["get", "list", "patch"],
        "resources": ["deployments"],
        "reason": "patch demo/web within fixture cluster",
    }
    body.update(overrides)
    return body


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


class KubernetesCapabilityV0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.home = self._tmpdir.name
        self.store = _store(self.home)
        self.env = patch.dict(os.environ, {"AMOF_HOME": self.home}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.executor = FixtureKubernetesExecutor()

    def _propose(self, body=None, *, run_id: str = "run-k8s-001"):
        return propose_kubernetes_capability(
            run_id=run_id,
            body=body or _body(),
            requested_by=WORKER,
            store=self.store,
        )

    def _approve(self, proposal_id: str, *, ttl: str = "30m", **kwargs):
        return approve_proposal(
            proposal_id,
            ttl=ttl,
            approved_by=kwargs.pop("approved_by", OPERATOR),
            store=self.store,
            **kwargs,
        )

    def _op(self, **overrides) -> KubernetesOperation:
        values = dict(
            cluster=DEFAULT_FIXTURE_CLUSTER,
            namespace="demo",
            verb="patch",
            resource="deployments",
            name="web",
            patch={"replicas": 3},
            mission_id=MISSION,
            requested_by=WORKER,
            run_id="exec-k8s-001",
        )
        values.update(overrides)
        return KubernetesOperation(**values)

    def test_read_body_rejects_mutate_verb(self) -> None:
        with self.assertRaises(CapabilityBodyError):
            normalize_kubernetes_capability_body(
                _body(capability=CAPABILITY_KUBERNETES_READ, verbs=["patch"])
            )

    def test_worker_cannot_approve(self) -> None:
        proposal = self._propose()
        with self.assertRaises(KubernetesCapabilityError):
            self._approve(proposal["proposal_id"], approved_by="worker:evil")

    def test_approved_get_and_list(self) -> None:
        proposal = self._propose(_body(capability=CAPABILITY_KUBERNETES_READ, verbs=["get", "list"]))
        approval = self._approve(proposal["proposal_id"])
        listed = execute_kubernetes_capability(
            operation=self._op(verb="list", name=None, patch=None, run_id="exec-list"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertTrue(listed.ok)
        self.assertEqual(listed.receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertEqual(listed.receipt["compliance"], COMPLIANCE_WITHIN_SCOPE)
        got = execute_kubernetes_capability(
            operation=self._op(verb="get", patch=None, run_id="exec-get"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertTrue(got.ok)
        self.assertEqual(got.receipt["verification"]["verified"], True)
        self.assertIsNone(got.receipt.get("change") or None)

    def test_approved_mutation_and_receipt(self) -> None:
        proposal = self._propose()
        approval = self._approve(proposal["proposal_id"])
        outcome = execute_kubernetes_capability(
            operation=self._op(),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertTrue(outcome.ok)
        receipt = outcome.receipt
        assert receipt is not None
        self.assertEqual(receipt["kind"], "kubernetes_capability_receipt")
        self.assertEqual(receipt["mission_id"], MISSION)
        self.assertEqual(receipt["requested_by"], WORKER)
        self.assertEqual(receipt["capability"], CAPABILITY_KUBERNETES_MUTATE)
        self.assertEqual(receipt["target"]["namespace"], "demo")
        self.assertEqual(receipt["operation"]["verb"], "patch")
        self.assertTrue(receipt["within_scope"])
        self.assertEqual(receipt["verification"]["method"], "fixture_state_compare")
        self.assertEqual(receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertTrue(receipt["secrets_omitted"])
        self.assertEqual(receipt["change"]["after_generation"], 2)
        self.assertEqual(outcome.approval["status"], "consumed")
        self.assertNotIn("spec", json.dumps(receipt))

    def test_no_approval_fails_closed(self) -> None:
        outcome = execute_kubernetes_capability(
            operation=self._op(),
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "no_approval")
        self.assertIsNone(outcome.receipt)
        self.assertEqual(outcome.denial["acceptance_state"], ACCEPTANCE_FAIL)

    def test_no_binding_fails_closed(self) -> None:
        outcome = execute_kubernetes_capability(
            operation=self._op(),
            binding_id="kcb-" + ("0" * 24),
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "no_binding")

    def test_expired_approval_rejected(self) -> None:
        proposal = self._propose()
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        approval = self._approve(proposal["proposal_id"], ttl="1h", now=past)
        outcome = execute_kubernetes_capability(
            operation=self._op(),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
            now=datetime.now(timezone.utc),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "expired")
        self.assertIsNone(outcome.receipt)
        self.assertEqual(load_approval(approval["approval_id"], store=self.store)["status"], "expired")

    def test_revoked_approval_rejected(self) -> None:
        proposal = self._propose()
        approval = self._approve(proposal["proposal_id"])
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
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "revoked")

    def test_wrong_namespace_rejected(self) -> None:
        proposal = self._propose()
        approval = self._approve(proposal["proposal_id"])
        outcome = execute_kubernetes_capability(
            operation=self._op(namespace="other", run_id="exec-wrong-ns"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "wrong_namespace")
        self.assertEqual(outcome.receipt["acceptance_state"], ACCEPTANCE_FAIL)
        self.assertFalse(outcome.receipt["executed"])
        self.assertEqual(self.executor.state["namespaces"]["demo"]["deployments"]["web"]["replicas"], 1)

    def test_wrong_resource_rejected(self) -> None:
        proposal = self._propose()
        approval = self._approve(proposal["proposal_id"])
        outcome = execute_kubernetes_capability(
            operation=self._op(resource="pods", run_id="exec-wrong-res"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "wrong_resource")

    def test_wrong_verb_rejected(self) -> None:
        proposal = self._propose(_body(verbs=["get", "list"]))
        approval = self._approve(proposal["proposal_id"])
        outcome = execute_kubernetes_capability(
            operation=self._op(run_id="exec-wrong-verb"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "wrong_verb")

    def test_deny_wins_on_overlapping_namespace(self) -> None:
        proposal = self._propose(_body(denied_namespaces=["demo"]))
        approval = self._approve(proposal["proposal_id"])
        outcome = execute_kubernetes_capability(
            operation=self._op(run_id="exec-deny"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "wrong_namespace")

    def test_scope_escalation_read_grant_cannot_patch(self) -> None:
        proposal = self._propose(
            _body(capability=CAPABILITY_KUBERNETES_READ, verbs=["get", "list"])
        )
        approval = self._approve(proposal["proposal_id"])
        outcome = execute_kubernetes_capability(
            operation=self._op(run_id="exec-escalate"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "scope_escalation")
        self.assertFalse(outcome.receipt["executed"])

    def test_single_use_mutate_cannot_be_reused(self) -> None:
        proposal = self._propose()
        approval = self._approve(proposal["proposal_id"])
        first = execute_kubernetes_capability(
            operation=self._op(run_id="exec-1"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertTrue(first.ok)
        second = execute_kubernetes_capability(
            operation=self._op(patch={"replicas": 5}, run_id="exec-2"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(second.ok)
        self.assertEqual(second.code, "reused_single_use")

    def test_unverified_never_becomes_pass(self) -> None:
        proposal = self._propose()
        approval = self._approve(proposal["proposal_id"])

        class Boom:
            def execute(self, operation):
                raise KubernetesCapabilityInterrupted("killed mid-flight")

        outcome = execute_kubernetes_capability(
            operation=self._op(run_id="exec-unverified"),
            approval_id=approval["approval_id"],
            executor=Boom(),
            store=self.store,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "unverified")
        self.assertEqual(outcome.receipt["acceptance_state"], ACCEPTANCE_UNVERIFIED)
        self.assertEqual(outcome.receipt["compliance"], COMPLIANCE_UNVERIFIED)
        self.assertNotEqual(outcome.receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertEqual(self.executor.state["namespaces"]["demo"]["deployments"]["web"]["replicas"], 1)

    def test_binding_required_before_executor(self) -> None:
        called = {"n": 0}

        class Spy(FixtureKubernetesExecutor):
            def execute(self, operation):
                called["n"] += 1
                return super().execute(operation)

        spy = Spy()
        execute_kubernetes_capability(operation=self._op(), executor=spy, store=self.store)
        self.assertEqual(called["n"], 0)

    def test_demo_lifecycle_fixture(self) -> None:
        proposal = self._propose()
        self.assertEqual(proposal["status"], "proposed")
        approval = self._approve(proposal["proposal_id"])
        self.assertEqual(approval["status"], "approved")
        denied = execute_kubernetes_capability(
            operation=self._op(namespace="kube-system", run_id="exec-oos"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertFalse(denied.ok)
        self.assertEqual(denied.code, "wrong_namespace")
        self.assertEqual(denied.receipt["acceptance_state"], ACCEPTANCE_FAIL)
        allowed = execute_kubernetes_capability(
            operation=self._op(run_id="exec-ok"),
            approval_id=approval["approval_id"],
            executor=self.executor,
            store=self.store,
        )
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.receipt["acceptance_state"], ACCEPTANCE_PASS)
        self.assertTrue(allowed.receipt["within_scope"])
        self.assertEqual(allowed.receipt["change"]["after_generation"], 2)
        self.assertEqual(self.executor.state["namespaces"]["demo"]["deployments"]["web"]["replicas"], 3)

    def test_cli_propose_approve_execute(self) -> None:
        propose_args = argparse.Namespace(
            scope_cmd="propose",
            capability=CAPABILITY_KUBERNETES_MUTATE,
            cluster=DEFAULT_FIXTURE_CLUSTER,
            namespaces=["demo"],
            verbs=["get", "list", "patch"],
            resources=["deployments"],
            denied_namespaces=[],
            denied_verbs=[],
            denied_resources=[],
            from_run="run-cli-1",
            requested_by=WORKER,
            reason="cli demo",
            json=True,
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scope_cmd.cmd_scope(propose_args), 0)
        proposals = list((Path(self.home) / "share" / "capabilities" / "kubernetes" / "proposals").glob("kcp-*.json"))
        self.assertEqual(len(proposals), 1)
        proposal_id = json.loads(proposals[0].read_text())["proposal_id"]
        approve_args = argparse.Namespace(
            scope_cmd="approve",
            proposal_id=proposal_id,
            ttl="15m",
            approved_by=OPERATOR,
            json=True,
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scope_cmd.cmd_scope(approve_args), 0)
        approvals = list((Path(self.home) / "share" / "capabilities" / "kubernetes" / "approvals").glob("kca-*.json"))
        approval_id = json.loads(approvals[0].read_text())["approval_id"]
        execute_args = argparse.Namespace(
            scope_cmd="execute",
            approval_id=approval_id,
            binding_id=None,
            cluster=DEFAULT_FIXTURE_CLUSTER,
            namespace="demo",
            verb="patch",
            resource="deployments",
            name="web",
            patch_replicas=4,
            mission_id=MISSION,
            run_id="exec-cli-1",
            requested_by=WORKER,
            json=True,
        )
        captured = io.StringIO()
        with redirect_stdout(captured):
            self.assertEqual(scope_cmd.cmd_scope(execute_args), 0)
        payload = json.loads(captured.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["receipt"]["acceptance_state"], ACCEPTANCE_PASS)

    def test_create_binding_without_execute_then_missing_active_reuse(self) -> None:
        proposal = self._propose()
        approval = self._approve(proposal["proposal_id"])
        binding = create_binding(approval["approval_id"], run_id="bound-only", store=self.store)
        self.assertEqual(binding["status"], "active")
        with self.assertRaises(KubernetesCapabilityError) as ctx:
            create_binding(approval["approval_id"], run_id="bound-again", store=self.store)
        self.assertEqual(ctx.exception.code, "reused_single_use")


if __name__ == "__main__":
    unittest.main()
