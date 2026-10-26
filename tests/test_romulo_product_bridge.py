"""Product authority maps to the public single-use exact-operation adapter."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amof.kubernetes_capability import KubernetesCapabilityError
from amof.kubernetes_live import LiveKubernetesExecutor, RecordingTransport, ResolvedClusterTarget
from amof.romulo_product_bridge import execute_authorized_exact_operation
from tests.test_romulo_readiness_pilot import CLUSTER, CONTAINER, DEPLOYMENT, NAMESPACE, _object


class RomuloProductBridgeTest(unittest.TestCase):
    def test_mandate_operation_executes_once_and_stale_review_cannot_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / "registry.json"
            registry.write_text("{}")
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("fixture")
            transport = RecordingTransport({(NAMESPACE, DEPLOYMENT): _object()})
            executor = LiveKubernetesExecutor(transport=transport, registry={
                CLUSTER: ResolvedClusterTarget(CLUSTER, kubeconfig, "synthetic-context"),
            })
            authority = {
                "mission_id": "msn-dogfood-romulo-001", "request_id": "operator-kubernetes-run-001",
                "mandate_id": "11111111-1111-4111-8111-111111111111",
                "attempt_id": "22222222-2222-4222-8222-222222222222",
                "agreement_sha256": "a" * 64, "authorized_by": "operator:dogfood-owner",
                "target_id": "local:hotshot-romulo", "budget_minutes": 5,
                "operation": {
                    "kind": "deployment.readiness_timeout.patch", "cluster": CLUSTER,
                    "context": "synthetic-context", "namespace": NAMESPACE,
                    "deployment": DEPLOYMENT, "container": CONTAINER,
                    "uid": "uid-syn-01", "resource_version": "17",
                    "expected_timeout_seconds": 1, "timeout_seconds": 3,
                },
            }
            with patch.dict(os.environ, {
                "AMOF_HOTSHOT_ISOLATED_DOGFOOD": "1",
                "AMOF_ROMULO_ALLOWED_CLUSTER": CLUSTER,
                "AMOF_ROMULO_ALLOWED_CONTEXT": "synthetic-context",
                "AMOF_ROMULO_ALLOWED_NAMESPACE": NAMESPACE,
            }):
                first = execute_authorized_exact_operation(authority,
                    store_root=root / "store", registry_path=registry, executor=executor)
                self.assertTrue(first["ok"])
                self.assertEqual(first["receipt"]["acceptance_state"], "PASS")
                self.assertEqual(first["before"]["resourceVersion"], "17")
                self.assertEqual(first["after"]["readiness"]["timeoutSeconds"], 3)
                replay = execute_authorized_exact_operation(authority,
                    store_root=root / "store", registry_path=registry, executor=executor)
                self.assertFalse(replay["ok"])
                self.assertEqual([call["op"] for call in transport.calls],
                    ["get", "patch_readiness", "get"])
                changed = dict(authority)
                changed["operation"] = {**authority["operation"], "cluster": "production"}
                with self.assertRaises(KubernetesCapabilityError):
                    execute_authorized_exact_operation(changed,
                        store_root=root / "store", registry_path=registry, executor=executor)


if __name__ == "__main__":
    unittest.main()
