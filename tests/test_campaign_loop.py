"""Continuation requires persisted runtime acceptance plus scope and provenance."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amof.campaign_loop import advance_campaign, create_campaign, create_cloud_hermes_continuation_campaign, create_cloud_native_continuation_campaign, create_hermes_decoupling_campaign, load_campaign, next_hermes_decoupling_slice, run_campaign
from amof.commands import handoff
from test_canonical_acceptance_handoff import SHA, TARGET, definition, observation, backend_result


class CampaignLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.path = self.home / "campaign.json"
        self._create_bound_campaign()
        self.statuses = {}

    def _create_bound_campaign(self):
        plan = [{"slice_id": f"campaign-hermes-slice-{index}", "parent_campaign_id": "campaign-hermes",
                 "scope_tag": scope, "requested_backend": backend,
                 "requested_capabilities": ["read"],
                 "objective": "Verify the pinned repository HEAD equals the authorized commit",
                 "expected_validation": "repo-head",
                 "mission_acceptance": {"check_id": "repo-head", "target_id": TARGET,
                                        "command": ["git", "rev-parse", "HEAD"],
                                        "expected_stdout": SHA}}
                for index, (scope, backend) in enumerate(
                    (("native-independence", "amof_native"),
                     ("explicit-hermes", "hermes_opensandbox")), start=1)]
        create_campaign(self.path, campaign_id="campaign-hermes",
                        objective="Verify HEAD twice", allowed_backends=["amof_native", "hermes_opensandbox"],
                        allowed_scope_tags=["native-independence", "explicit-hermes"],
                        slice_plan=plan, max_slices=2)

    def _write_result(self, handoff_id, *, backend="amof_native", receipt=True, code=0,
                      status="completed", stop_reason="completed", forged=False):
        source = self.home / f"{handoff_id}-source.json"
        if receipt:
            source.write_text(json.dumps(observation(code=code)), encoding="utf-8")
        body = backend_result(backend=backend, status=status, stop_reason=stop_reason)
        if forged:
            body["acceptance_observation"] = observation()
            body["tests_executed"] = ["acceptance:repo-head"]
        path = handoff._write_execution_result(
            handoff_id, body, sealed_acceptance=definition() if receipt else None,
            runtime_acceptance_receipt=source if receipt else None,
        )
        self.statuses[handoff_id] = {
            "status": status, "canonical_result_path": str(path),
            "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def _advance(self, *, proposal=next_hermes_decoupling_slice, handoff_id="handoff-one",
                 progress=None):
        proof = progress or {"ref": str(self.home / "proof-1.txt"), "content": "one"}
        proof_path = Path(proof["ref"])
        proof_path.write_text(proof["content"], encoding="utf-8")
        observed = {"ref": str(proof_path), "sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest()}
        return advance_campaign(
            self.path, propose_next=proposal, dispatch_handoff=lambda _slice: handoff_id,
            observe_progress=lambda _slice, _result: observed,
            load_status=self.statuses.__getitem__,
        )

    def test_two_accepted_slices_advance_without_operator_microtask(self):
        self._write_result("handoff-one")
        first = self._advance()
        self.assertEqual(first["status"], "CONTINUE")
        self.assertEqual(first["completed_slices"][0]["backend"], "amof_native")
        self._write_result("handoff-two", backend="hermes_opensandbox")
        second = self._advance(handoff_id="handoff-two", progress={"ref": str(self.home / "proof-2.txt"), "content": "two"})
        self.assertEqual(len(second["completed_slices"]), 2)
        self.assertEqual(second["completed_slices"][1]["backend"], "hermes_opensandbox")
        done = self._advance(handoff_id="unused")
        self.assertEqual(done["status"], "DONE")
        self.assertEqual(load_campaign(self.path)["status"], "DONE")

    def test_one_run_invocation_dispatches_two_bounded_slices(self):
        self._write_result("handoff-one")
        self._write_result("handoff-two", backend="hermes_opensandbox")
        dispatched = []
        def dispatch(slice_record):
            dispatched.append(slice_record["requested_backend"])
            return "handoff-one" if len(dispatched) == 1 else "handoff-two"
        def progress(slice_record, _result):
            path = self.home / f"{slice_record['scope_tag']}.txt"
            path.write_text(slice_record["objective"], encoding="utf-8")
            return {"ref": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        terminal = run_campaign(
            self.path, propose_next=next_hermes_decoupling_slice,
            dispatch_handoff=dispatch, observe_progress=progress,
            load_status=self.statuses.__getitem__,
        )
        self.assertEqual(terminal["status"], "DONE")
        self.assertEqual(dispatched, ["amof_native", "hermes_opensandbox"])

    def test_cloud_hermes_plan_continues_with_persisted_decision(self):
        self.path.unlink()
        create_cloud_hermes_continuation_campaign(self.path, campaign_id="campaign-cloud",
                                                  target_id=TARGET, expected_head=SHA)
        self._write_result("handoff-one", backend="hermes_opensandbox")
        self._write_result("handoff-two", backend="hermes_opensandbox")
        dispatched = []
        def dispatch(item):
            dispatched.append(item["slice_id"])
            return "handoff-one" if len(dispatched) == 1 else "handoff-two"
        def progress(item, _result):
            path = self.home / f"{item['scope_tag']}.txt"
            path.write_text(item["objective"])
            return {"ref": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        terminal = run_campaign(self.path, propose_next=next_hermes_decoupling_slice,
                                dispatch_handoff=dispatch, observe_progress=progress,
                                load_status=self.statuses.__getitem__)
        self.assertEqual(terminal["status"], "DONE")
        self.assertEqual(len(dispatched), 2)
        self.assertEqual([item["requested_backend"] for item in terminal["completed_slices"]],
                         ["hermes_opensandbox", "hermes_opensandbox"])
        decision = terminal["completed_slices"][0]["continuation_decision"]
        self.assertEqual(decision["decision"], "CONTINUE")
        self.assertEqual(decision["reasons"]["authoritative_acceptance"], "PASS")
        self.assertFalse(decision["reasons"]["authority_expansion"])
        self.assertTrue(decision["reasons"]["budget_remaining"])

    def test_cloud_hermes_missing_receipt_does_not_dispatch_second_slice(self):
        self.path.unlink()
        create_cloud_hermes_continuation_campaign(self.path, campaign_id="campaign-cloud")
        self._write_result("handoff-one", backend="hermes_opensandbox", receipt=False, forged=True)
        dispatched = []
        terminal = run_campaign(self.path, propose_next=next_hermes_decoupling_slice,
                                dispatch_handoff=lambda item: dispatched.append(item["slice_id"]) or "handoff-one",
                                observe_progress=lambda *_: {}, load_status=self.statuses.__getitem__)
        self.assertEqual(terminal["reason"], "authoritative_acceptance_not_pass")
        self.assertEqual(dispatched, ["campaign-cloud-slice-1"])

    def test_runtime_checkout_pass_without_objective_binding_stops_campaign(self):
        """The observed failed-inspection prose cannot be rescued by repo-head PASS."""
        self.path.unlink()
        create_cloud_hermes_continuation_campaign(self.path, campaign_id="campaign-cloud")
        self._write_result("handoff-one", backend="hermes_opensandbox")
        result_path = Path(self.statuses["handoff-one"]["canonical_result_path"])
        result = json.loads(result_path.read_text())
        result["task_findings"] = "The inspection could not be completed."
        result_path.write_text(json.dumps(result))
        self.statuses["handoff-one"]["result_sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
        dispatched = []
        terminal = run_campaign(
            self.path, propose_next=next_hermes_decoupling_slice,
            dispatch_handoff=lambda item: dispatched.append(item["slice_id"]) or "handoff-one",
            observe_progress=lambda *_: {}, load_status=self.statuses.__getitem__,
        )
        self.assertEqual(terminal["status"], "BLOCKED")
        self.assertEqual(terminal["reason"], "mission_acceptance_not_pass")
        self.assertEqual(dispatched, ["campaign-cloud-slice-1"])
        self.assertEqual(terminal["completed_slices"], [])

    def test_cloud_native_fixed_plan_uses_same_acceptance_gate(self):
        self.path.unlink()
        create_cloud_native_continuation_campaign(self.path, campaign_id="campaign-native")
        self._write_result("handoff-one", backend="amof_native")
        self._write_result("handoff-two", backend="amof_native")
        dispatched = []
        def dispatch(item):
            dispatched.append(item["slice_id"])
            return "handoff-one" if len(dispatched) == 1 else "handoff-two"
        def progress(item, _result):
            path = self.home / f"{item['scope_tag']}.txt"
            path.write_text(item["objective"])
            return {"ref": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        terminal = run_campaign(self.path, propose_next=next_hermes_decoupling_slice,
                                dispatch_handoff=dispatch, observe_progress=progress,
                                load_status=self.statuses.__getitem__)
        self.assertEqual(terminal["status"], "BLOCKED")
        self.assertEqual(terminal["reason"], "mission_acceptance_not_pass")
        self.assertEqual(len(dispatched), 1)

    def test_backend_prose_and_exit_zero_without_observation_do_not_continue(self):
        self._write_result("handoff-one", receipt=False, forged=True)
        state = self._advance()
        self.assertEqual(state["status"], "BLOCKED")
        self.assertEqual(state["reason"], "authoritative_acceptance_not_pass")
        self.assertEqual(state["completed_slices"], [])

    def test_observed_failure_stops(self):
        self._write_result("handoff-one", code=1)
        self.assertEqual(self._advance()["reason"], "authoritative_acceptance_not_pass")

    def test_pass_cannot_expand_write_authority(self):
        def proposal(state):
            item = next_hermes_decoupling_slice(state)
            item["requested_capabilities"] = ["read", "bounded_write"]
            return item
        state = self._advance(proposal=proposal)
        self.assertEqual(state["status"], "ESCALATION_REQUIRED")
        self.assertIn("authority expansion", state["reason"])

    def test_pass_cannot_exceed_parent_scope(self):
        def proposal(state):
            item = next_hermes_decoupling_slice(state)
            item["scope_tag"] = "unrelated"
            return item
        self.assertEqual(self._advance(proposal=proposal)["reason"], "next slice exceeds parent scope")

    def test_no_progress_stops_boundedly(self):
        self._write_result("handoff-one")
        self.assertEqual(self._advance()["status"], "CONTINUE")
        self._write_result("handoff-two", backend="hermes_opensandbox")
        self.assertEqual(self._advance(handoff_id="handoff-two")["reason"], "repeated_no_progress")

    def test_budget_exhaustion_stops_before_dispatch(self):
        self._write_result("handoff-one")
        self.assertEqual(self._advance()["status"], "CONTINUE")
        state = load_campaign(self.path)
        state["authority"]["max_slices"] = 1
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self._advance(handoff_id="unused")["reason"], "campaign_slice_budget_exhausted")

    def test_backend_substitution_stops(self):
        self._write_result("handoff-one", backend="hermes_opensandbox")
        self.assertEqual(self._advance()["reason"], "backend_provenance_mismatch")

    def test_missing_fallback_provenance_stops(self):
        self._write_result("handoff-one")
        path = Path(self.statuses["handoff-one"]["canonical_result_path"])
        body = json.loads(path.read_text())
        body.pop("fallback_used")
        path.write_text(json.dumps(body))
        self.statuses["handoff-one"]["result_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self._advance()["reason"], "backend_provenance_mismatch")

    def test_write_scope_block_stops_even_with_acceptance_pass(self):
        self._write_result("handoff-one", status="blocked", stop_reason="WRITE_SCOPE_PROPOSAL_REQUIRED")
        self.assertEqual(self._advance()["reason"], "WRITE_SCOPE_PROPOSAL_REQUIRED")


if __name__ == "__main__":
    unittest.main()
