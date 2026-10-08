"""Public handoff must preserve runtime evidence without trusting backend claims."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amof.canonical_acceptance import authoritative_acceptance_state, project_runtime_acceptance
from amof.commands import handoff

SHA = "6576682367ef9ae1fc0960bab62bd91851214464"
TARGET = f"github_app:marekhotshot/simple-ai-shop:{SHA}"


def definition():
    return {"schema": "amof.acceptance_checks/v1", "checks": [{
        "id": "repo-head", "kind": "command", "command": ["git", "rev-parse", "HEAD"],
        "cwd": "repository", "mutability": "read_only", "timeout_seconds": 10,
        "target_id": TARGET, "expected": {"exit_code": 0, "stdout_equals": SHA},
    }]}


def observation(*, code=0, stdout=SHA + "\n"):
    digest = hashlib.sha256(json.dumps(definition(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema": "amof.acceptance_observation/v1", "check_id": "repo-head",
            "definition_sha256": digest, "command": ["git", "rev-parse", "HEAD"],
            "target_id": TARGET, "expected": {"exit_code": 0, "stdout_equals": SHA},
            "exit_code": code, "stdout": stdout, "stderr": "", "duration_ms": 2,
            "status": "PASS" if code == 0 else "FAIL", "reason": None}


def backend_result(**extra):
    return {"result_kind": "agent_run_result", "contract_version": "agent-run-v1",
            "schema_version": 1, "status": "completed", "session_id": "run-1", "exit_code": 0,
            "stop_reason": "completed", "backend": "hermes_opensandbox", "fallback_used": False,
            "final_text": "tests passed", "validation_summary": {"acceptance_state": "PASS"},
            **extra}


class CanonicalAcceptanceHandoffTests(unittest.TestCase):
    def test_forged_backend_pass_without_receipt_is_unverified(self):
        body = project_runtime_acceptance(
            backend_result(acceptance_observation={"status": "PASS"},
                           tests_executed=["acceptance:repo-head"]),
            sealed_definition=definition(), receipt_path=None,
        )
        self.assertEqual(body["validation_summary"]["acceptance_state"], "UNVERIFIED")
        self.assertEqual(body["tests_executed"], [])
        self.assertIsNone(body["acceptance_observation"])

    def test_handoff_writer_downgrades_untrusted_backend_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AMOF_HOME": tmp}):
                handoff._write_execution_result(
                    "handoff-forged", backend_result(
                        acceptance_observation=observation(), tests_executed=["acceptance:repo-head"]),
                )
                loaded = handoff._load_execution_result_payload("handoff-forged")
                self.assertEqual(loaded["validation_summary"]["acceptance_state"], "UNVERIFIED")
                self.assertNotIn("acceptance_observation", loaded)
                self.assertNotIn("tests_executed", loaded)

    def test_runtime_observation_round_trips_through_handoff_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / "runtime-observation.json"
            source.write_text(json.dumps(observation()), encoding="utf-8")
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}):
                path = handoff._write_execution_result(
                    "handoff-acceptance-proof", backend_result(),
                    sealed_acceptance=definition(), runtime_acceptance_receipt=source,
                )
                reloaded = handoff._load_execution_result_payload("handoff-acceptance-proof")
                self.assertIsNotNone(reloaded)
                self.assertEqual(reloaded["tests_executed"], ["acceptance:repo-head"])
                self.assertEqual(reloaded["validation_summary"]["acceptance_state"], "PASS")
                evidence_root = home / "share" / "handoff" / "evidence" / "handoff-acceptance-proof"
                self.assertEqual(authoritative_acceptance_state(reloaded, evidence_root=evidence_root), "PASS")
                self.assertEqual(reloaded["acceptance_observation"]["command"], ["git", "rev-parse", "HEAD"])
                self.assertFalse(reloaded["acceptance_observation"]["shell"])
                source.unlink()  # Job staging may disappear after ingestion.
                self.assertEqual(authoritative_acceptance_state(json.loads(path.read_text()), evidence_root=evidence_root), "PASS")
                payload = handoff._validated_payload_from_text("Read-only campaign check", field_name="test")
                packet = handoff._build_packet(source="test", target="amof-agent", studio_session_id=None,
                                               payload_kind="selected_text", payload=payload)
                # Project the persisted result through the public status consumer.
                with (patch.object(handoff, "_load_execution_result_payload", return_value=reloaded),
                      patch.object(handoff, "_verify_consumed_execution_result"),
                      patch.object(handoff, "_load_run_info", return_value={})):
                    status = handoff._handoff_status_payload("handoff-acceptance-proof", packet=packet)
                self.assertEqual(status["acceptance_state"], "PASS")
                self.assertEqual(status["tests_executed"], ["acceptance:repo-head"])

    def test_observed_failure_persists_and_does_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "observation.json"
            receipt.write_text(json.dumps(observation(code=1, stdout="wrong\n")), encoding="utf-8")
            result = project_runtime_acceptance(backend_result(), sealed_definition=definition(), receipt_path=receipt)
            self.assertEqual(result["validation_summary"]["acceptance_state"], "FAIL")
            self.assertEqual(result["tests_executed"], ["acceptance:repo-head"])

    def test_acceptance_pass_does_not_clear_write_scope_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "observation.json"
            receipt.write_text(json.dumps(observation()), encoding="utf-8")
            result = project_runtime_acceptance(
                backend_result(status="blocked", stop_reason="WRITE_SCOPE_PROPOSAL_REQUIRED"),
                sealed_definition=definition(), receipt_path=receipt,
            )
            self.assertEqual(result["validation_summary"]["acceptance_state"], "PASS")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["stop_reason"], "WRITE_SCOPE_PROPOSAL_REQUIRED")

    def test_receipt_definition_mismatch_and_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / "runtime-observation.json"
            forged = observation()
            forged["definition_sha256"] = "0" * 64
            source.write_text(json.dumps(forged), encoding="utf-8")
            with patch.dict(os.environ, {"AMOF_HOME": str(home)}):
                handoff._write_execution_result("handoff-mismatch", backend_result(),
                                                sealed_acceptance=definition(), runtime_acceptance_receipt=source)
                loaded = handoff._load_execution_result_payload("handoff-mismatch")
                self.assertEqual(loaded["validation_summary"]["acceptance_state"], "UNVERIFIED")
                source.write_text(json.dumps(observation()), encoding="utf-8")
                handoff._write_execution_result("handoff-tamper", backend_result(),
                                                sealed_acceptance=definition(), runtime_acceptance_receipt=source)
                loaded = handoff._load_execution_result_payload("handoff-tamper")
                evidence_root = home / "share" / "handoff" / "evidence" / "handoff-tamper"
                (evidence_root / "acceptance-observation.json").write_text("{}", encoding="utf-8")
                self.assertEqual(authoritative_acceptance_state(loaded, evidence_root=evidence_root), "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
