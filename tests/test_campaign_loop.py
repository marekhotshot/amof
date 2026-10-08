"""Continuation requires persisted runtime acceptance plus scope and provenance."""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

from amof.campaign_loop import advance_campaign, create_campaign, create_cloud_hermes_continuation_campaign, create_cloud_native_continuation_campaign, create_hermes_decoupling_campaign, load_campaign, next_hermes_decoupling_slice, run_campaign
from amof.commands import handoff
from amof.write_scope_enforcement import compute_receipt_id
from amof.write_scope_enforcement import apply_enforcement_to_result, load_receipt
from amof.write_scope_proposals import persist_write_scope_proposals_from_result
from amof.write_scope_approvals import approve_proposal, load_approval
from amof.write_scope_bindings import create_binding, load_binding
from test_canonical_acceptance_handoff import SHA, TARGET, definition, observation, backend_result


class CampaignLoopTests(unittest.TestCase):
    def test_literal_next_route_file_can_be_exact_bounded_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = "commerce/app/[locale]/page.tsx"
            target = f"github_app:example/igor:{SHA}"
            path = Path(td) / "route-campaign.json"
            create_campaign(
                path, campaign_id="route-campaign", objective="Bound one route file",
                allowed_backends=["hermes_opensandbox"], allowed_scope_tags=["hero"],
                allowed_capabilities=["read", "bounded_write"],
                allowed_target_id=target, allowed_write_roots=[root],
                slice_plan=[{"slice_id": "route-campaign-slice-1",
                             "parent_campaign_id": "route-campaign", "scope_tag": "hero",
                             "requested_backend": "hermes_opensandbox",
                             "requested_capabilities": ["read", "bounded_write"],
                             "objective": "Edit exact route", "expected_validation": "hero",
                             "write_authority_ref": "wsa-existing",
                             "requested_write_scope": {"target_id": target,
                                                       "base_sha": SHA, "roots": [root]}}],
                max_slices=1,
            )
            self.assertEqual(load_campaign(path)["authority"]["allowed_write_roots"], [root])

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

    def _writable_campaign(self, *, approval="wsa-test", root="docs/campaign-proof.txt"):
        self.path.unlink()
        plan = [{"slice_id": "campaign-write-slice-1", "parent_campaign_id": "campaign-write",
                 "scope_tag": "marker", "requested_backend": "amof_native",
                 "requested_capabilities": ["read", "bounded_write"],
                 "objective": "Write one bounded proof marker", "expected_validation": "repo-head",
                 "mission_acceptance": {"check_id": "repo-head", "target_id": TARGET,
                                        "command": ["git", "rev-parse", "HEAD"],
                                        "expected_stdout": SHA},
                 "requested_write_scope": {"target_id": TARGET, "base_sha": SHA, "roots": [root]},
                 "write_authority_ref": approval},
                {"slice_id": "campaign-write-slice-2", "parent_campaign_id": "campaign-write",
                 "scope_tag": "verify", "requested_backend": "amof_native",
                 "requested_capabilities": ["read"],
                 "objective": "Verify the pinned checkout", "expected_validation": "repo-head",
                 "mission_acceptance": {"check_id": "repo-head", "target_id": TARGET,
                                        "command": ["git", "rev-parse", "HEAD"],
                                        "expected_stdout": SHA}}]
        create_campaign(self.path, campaign_id="campaign-write", objective="Bounded proof",
                        allowed_backends=["amof_native"], allowed_scope_tags=["marker", "verify"],
                        slice_plan=plan, max_slices=2,
                        allowed_capabilities=["read", "bounded_write"],
                        allowed_target_id=TARGET, allowed_write_roots=["docs/campaign-proof.txt"])
        return plan

    def _write_mutation_result(self, *, changed=None, compliance="within_scope",
                               mission_pass=True, receipt=True):
        changed = changed if changed is not None else ["docs/campaign-proof.txt"]
        at = "2026-10-08T00:00:00Z"
        mutation = {"kind": "mutation_receipt", "schema_version": 1,
                    "receipt_id": compute_receipt_id(binding_id="wsb-test", run_id="handoff-one",
                                                     evaluated_at=at, compliance=compliance),
                    "binding_id": "wsb-test", "approval_id": "wsa-test", "run_id": "handoff-one",
                    "target_id": TARGET, "base_sha": SHA, "changed_paths": changed,
                    "in_scope_paths": changed if compliance == "within_scope" else [],
                    "out_of_scope_paths": [] if compliance == "within_scope" else changed,
                    "restored_paths": [], "compliance": compliance,
                    "binding_status": "completed", "approval_status": "consumed",
                    "created_at": at, "evaluated_at": at, "rollback_atomic": False}
        source = self.home / "acceptance-source.json"
        source.write_text(json.dumps(observation(code=0 if mission_pass else 1)))
        body = backend_result(backend="amof_native")
        body.update(changed_paths=changed, write_scope_binding_id="wsb-test",
                    write_scope_approval_id="wsa-test")
        if receipt:
            body["mutation_receipt"] = mutation
        path = handoff._write_execution_result("handoff-one", body,
            sealed_acceptance=definition(), runtime_acceptance_receipt=source)
        self.statuses["handoff-one"] = {"status": "completed", "canonical_result_path": str(path),
                                        "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        return mutation

    def _authority_patches(self, mutation=None, *, active=True, approval_root="docs/campaign-proof.txt"):
        body = {"target_id": TARGET, "base_sha": SHA, "allowed_roots": [approval_root],
                "denied_roots": [], "reason": "bounded test", "expected_checks": [],
                "docs_only": True, "source_mutation": True}
        binding = {"binding_id": "wsb-test", "approval_id": "wsa-test",
                   "run_id": "handoff-one", "target_id": TARGET, "base_sha": SHA,
                   "status": "completed", "workspace_root": str(self.home),
                   "writable_roots": [str((self.home / "docs/campaign-proof.txt").resolve())]}
        stack = ExitStack()
        stack.enter_context(patch("amof.campaign_loop.load_approval", return_value={"body": body}))
        stack.enter_context(patch("amof.campaign_loop.validate_approval_body", return_value=body))
        stack.enter_context(patch("amof.campaign_loop.is_approval_active", return_value=active))
        stack.enter_context(patch("amof.campaign_loop.load_binding", return_value=binding))
        stack.enter_context(patch("amof.campaign_loop.load_receipt", return_value=mutation))
        return stack

    def test_writable_without_authority_blocks_before_dispatch(self):
        self._writable_campaign(approval="")
        dispatched = []
        state = advance_campaign(self.path, propose_next=next_hermes_decoupling_slice,
            dispatch_handoff=lambda item: dispatched.append(item["slice_id"]) or "handoff-one",
            observe_progress=lambda *_: {}, load_status=self.statuses.__getitem__)
        self.assertEqual(state["reason"], "write_authority_missing")
        self.assertEqual(dispatched, [])

    def test_writable_outside_parent_or_approval_blocks(self):
        self._writable_campaign(root="docs/outside.txt")
        self.assertEqual(self._advance()["reason"], "write_scope_outside_campaign")
        self._writable_campaign()
        with self._authority_patches(approval_root="docs/elsewhere"):
            self.assertEqual(self._advance()["reason"], "write_scope_outside_approval")

    def test_revoked_or_expired_approval_blocks_before_dispatch(self):
        self._writable_campaign()
        with self._authority_patches(active=False):
            self.assertEqual(self._advance()["reason"], "write_approval_inactive")

    def test_valid_approval_dispatches_but_missing_receipt_stops(self):
        self._writable_campaign()
        self._write_mutation_result(receipt=False)
        called = []
        with self._authority_patches():
            proof = self.home / "proof.txt"
            proof.write_text("progress")
            state = advance_campaign(self.path, propose_next=next_hermes_decoupling_slice,
                dispatch_handoff=lambda item: called.append(item["slice_id"]) or "handoff-one",
                observe_progress=lambda *_: {"ref": str(proof), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()},
                load_status=self.statuses.__getitem__)
        self.assertEqual(called, ["campaign-write-slice-1"])
        self.assertEqual(state["reason"], "mutation_receipt_missing")

    def test_out_of_scope_changed_path_cannot_continue(self):
        self._writable_campaign()
        mutation = self._write_mutation_result(changed=["src/escape.txt"])
        with self._authority_patches(mutation):
            self.assertEqual(self._advance()["reason"], "mutation_scope_not_verified")

    def test_valid_write_and_both_acceptances_continue_without_new_binding(self):
        self._writable_campaign()
        mutation = self._write_mutation_result()
        with self._authority_patches(mutation):
            state = self._advance()
        self.assertEqual(state["status"], "CONTINUE")
        self.assertEqual(state["completed_slices"][0]["mutation_receipt"]["receipt_id"], mutation["receipt_id"])
        self.assertEqual(state["completed_slices"][0]["mission_acceptance"], "PASS")
        self.assertEqual(state["completed_slices"][0]["execution_acceptance"], "PASS")

    def test_mission_pass_without_write_authority_does_not_dispatch(self):
        self._writable_campaign(approval="")
        self._write_mutation_result()
        self.assertEqual(self._advance()["reason"], "write_authority_missing")

    def test_real_authority_records_and_mutation_receipt_allow_continuation(self):
        repo = self.home / "disposable-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "campaign@example.test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Campaign Test"], cwd=repo, check=True)
        (repo / "docs").mkdir()
        marker = repo / "docs" / "campaign-proof.txt"
        marker.write_text("before\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        target = f"github_app:example/disposable:{sha}"
        body = {"target_id": target, "base_sha": sha, "allowed_roots": ["docs/campaign-proof.txt"],
                "denied_roots": [], "reason": "bounded campaign integration proof",
                "expected_checks": ["git rev-parse HEAD"], "docs_only": True, "source_mutation": False}
        proposal = {"result_kind": "agent_run_result", "contract_version": "agent-run-v1",
                    "schema_version": 1, "status": "completed", "session_id": "proposal-run",
                    "exit_code": 0, "stop_reason": "completed", "final_text": "scope proposed",
                    "plan_path": None, "checkpoint_path": None, "event_log_path": "/tmp/events.jsonl",
                    "journal_path": None, "budget_summary": {"limit": None, "spent": 0, "remaining": None},
                    "write_scope_proposal": body}
        persisted = persist_write_scope_proposals_from_result(proposal)
        approval = approve_proposal(persisted.persisted[0]["proposal_id"], ttl="2h",
                                    approved_by="operator:test")
        approval_id = approval["approval_id"]
        self.path.unlink()
        slice_one = {"slice_id": "campaign-disposable-slice-1", "parent_campaign_id": "campaign-disposable",
                     "scope_tag": "marker", "requested_backend": "amof_native",
                     "requested_capabilities": ["read", "bounded_write"],
                     "objective": "Update the bounded marker", "expected_validation": "repo-head",
                     "mission_acceptance": {"check_id": "repo-head", "target_id": target,
                                            "command": ["git", "rev-parse", "HEAD"], "expected_stdout": sha},
                     "requested_write_scope": {"target_id": target, "base_sha": sha,
                                               "roots": ["docs/campaign-proof.txt"]},
                     "write_authority_ref": approval_id}
        create_campaign(self.path, campaign_id="campaign-disposable", objective="Disposable write proof",
                        allowed_backends=["amof_native"], allowed_scope_tags=["marker"],
                        slice_plan=[slice_one], max_slices=1,
                        allowed_capabilities=["read", "bounded_write"], allowed_target_id=target,
                        allowed_write_roots=["docs/campaign-proof.txt"])
        definition = {"schema": "amof.acceptance_checks/v1", "checks": [{
            "id": "repo-head", "kind": "command", "command": ["git", "rev-parse", "HEAD"],
            "cwd": "repository", "mutability": "read_only", "timeout_seconds": 10,
            "target_id": target, "expected": {"exit_code": 0, "stdout_equals": sha}}]}
        digest = hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        observation = {"schema": "amof.acceptance_observation/v1", "check_id": "repo-head",
                       "definition_sha256": digest, "command": ["git", "rev-parse", "HEAD"],
                       "target_id": target, "expected": {"exit_code": 0, "stdout_equals": sha},
                       "exit_code": 0, "stdout": sha + "\n", "stderr": "", "duration_ms": 2,
                       "status": "PASS", "reason": None}
        source = self.home / "runtime-observation.json"
        source.write_text(json.dumps(observation))
        progress = self.home / "progress.txt"
        progress.write_text("bounded mutation observed")
        def dispatch(item):
            self.assertEqual(item["write_authority_ref"], approval_id)
            gate = create_binding(approval_id, run_id="handoff-disposable", workspace_root=repo,
                                  execution_target_id=target, requested_capabilities=["bounded_write"])
            marker.write_text("after\n")
            result = backend_result(backend="amof_native", session_id="handoff-disposable",
                                    changed_paths=["docs/campaign-proof.txt"])
            enforced = apply_enforcement_to_result(result, binding=gate.binding, workspace_root=repo)
            path = handoff._write_execution_result("handoff-disposable", enforced,
                sealed_acceptance=definition, runtime_acceptance_receipt=source)
            self.statuses["handoff-disposable"] = {"status": "completed", "canonical_result_path": str(path),
                "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            return "handoff-disposable"
        state = run_campaign(self.path, propose_next=next_hermes_decoupling_slice,
            dispatch_handoff=dispatch,
            observe_progress=lambda *_: {"ref": str(progress),
                                         "sha256": hashlib.sha256(progress.read_bytes()).hexdigest()},
            load_status=self.statuses.__getitem__)
        self.assertEqual(state["status"], "DONE", state.get("reason"))
        receipt = state["completed_slices"][0]["mutation_receipt"]
        self.assertEqual(load_receipt(receipt["receipt_id"]), receipt)
        self.assertEqual(load_binding(receipt["binding_id"])["status"], "completed")
        self.assertEqual(load_approval(approval_id)["status"], "consumed")


if __name__ == "__main__":
    unittest.main()
