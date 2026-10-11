"""Opt-in campaign journal and replay boundaries using disposable handoffs."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amof import campaign_loop
from amof.commands import handoff
from test_canonical_acceptance_handoff import SHA, TARGET, backend_result, definition, observation


class BoundCampaignTests(unittest.TestCase):
    def test_opt_in_switch_blocks_new_bound_campaigns(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AMOF_CAMPAIGN_MISSION_RUN_V2": "0"}):
            with self.assertRaisesRegex(ValueError, "disabled"):
                campaign_loop.create_campaign(
                    Path(home) / "disabled.json", campaign_id="disabled", objective="Read",
                    allowed_backends=["hermes_opensandbox"], allowed_scope_tags=["step"],
                    slice_plan=[{"slice_id": "disabled-slice-1", "parent_campaign_id": "disabled"}],
                    max_slices=1, mission_binding={"version": 2},
                )

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = patch.dict(os.environ, {"AMOF_HOME": str(self.home),
                                      "AMOF_CAMPAIGN_MISSION_RUN_V2": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.path = self.home / "campaign.json"
        plan = [{"slice_id": f"campaign-bound-slice-{index}", "parent_campaign_id": "campaign-bound",
                 "scope_tag": f"step-{index}", "requested_backend": "hermes_opensandbox",
                 "requested_capabilities": ["read"], "objective": f"Verify step {index}",
                 "expected_validation": "repo-head",
                 "mission_acceptance": {"check_id": "repo-head", "target_id": TARGET,
                                        "command": ["git", "rev-parse", "HEAD"], "expected_stdout": SHA}}
                for index in (1, 2)]
        campaign_loop.create_campaign(
            self.path, campaign_id="campaign-bound", objective="Verify both steps",
            allowed_backends=["hermes_opensandbox"], allowed_scope_tags=["step-1", "step-2"],
            slice_plan=plan, max_slices=2,
            mission_binding={"version": 2, "project_id": "project-one",
                             "canonical_mission_id": "mission-one", "target_id": TARGET,
                             "expected_source_sha": SHA, "workspace_id": str(self.home / "workspace"),
                             "repo_name": "simple-ai-shop", "repo_owner": "marekhotshot",
                             "branch_ref": "main"},
        )
        self.dispatches = []

    def prepare(self, item, binding):
        canonical = handoff.CanonicalMissionPacket(
            schema_version=1, contract_version="canonical-mission-packet-v1",
            mission_id="mission-one", ticket_id="ticket-one", task_class="validation",
            classification="internal", goal=item["objective"], objective="Verify both steps",
            repo_name="simple-ai-shop", repo_owner="marekhotshot", branch_ref="main",
            execution_allowed=True, requested_mode="read_only",
            allowed_mutations=("read_only",), forbidden_mutations=("runtime_mutation",),
            validation_gates=("repo-head",),
        )
        return handoff.prepare_campaign_handoff(
            binding=binding, canonical_packet_text=handoff._canonical_json(canonical.to_payload()))

    def dispatch(self, item, handoff_id, key):
        self.dispatches.append((item["slice_id"], handoff_id, key))
        source = self.home / f"{handoff_id}-observation.json"
        source.write_text(json.dumps(observation()), encoding="utf-8")
        handoff._write_execution_result(
            handoff_id, backend_result(session_id=f"run-{item['slice_id']}"),
            sealed_acceptance=definition(), runtime_acceptance_receipt=source,
        )
        return handoff_id

    def status(self, handoff_id):
        path = handoff._handoff_results_dir() / f"{handoff_id}.json"
        if not path.exists():
            return {"status": "prepared", "accepted": False}
        return {"status": "completed", "accepted": True, "tracking_ref": handoff_id,
                "canonical_result_path": str(path),
                "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def progress(self, item, _result):
        path = self.home / f"{item['slice_id']}.proof"
        path.write_text(item["objective"], encoding="utf-8")
        return {"ref": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def advance(self):
        return campaign_loop.advance_campaign(
            self.path, propose_next=campaign_loop.next_hermes_decoupling_slice,
            dispatch_handoff=lambda _: self.fail("legacy dispatch invoked"),
            prepare_handoff=self.prepare, dispatch_prepared=self.dispatch,
            observe_progress=self.progress, load_status=self.status,
        )

    def test_one_mission_two_distinct_runs_and_stable_prepared_ids(self):
        first = self.advance()
        self.assertEqual(first["status"], "CONTINUE")
        second = self.advance()
        self.assertEqual(second["status"], "CONTINUE")
        done = self.advance()
        self.assertEqual(done["status"], "DONE")
        self.assertEqual(len(self.dispatches), 2)
        self.assertEqual(len({entry[1] for entry in self.dispatches}), 2)
        self.assertEqual({item["campaign_binding"]["canonical_mission_id"]
                          for item in [handoff._load_packet_payload(entry[1])[1]
                                       for entry in self.dispatches]}, {"mission-one"})
        self.assertEqual(len({item["run_id"] for item in done["completed_slices"]}), 2)
        self.assertTrue(all(entry["terminal_decision"] in {"CONTINUE", "DONE"}
                            for entry in done["dispatch_journal"].values()))

    def test_crash_after_prepare_resumes_same_identity(self):
        real_save = campaign_loop._save
        crashed = False

        def crash_after_prepared(path, state):
            nonlocal crashed
            real_save(path, state)
            if not crashed and state["dispatch_journal"]["campaign-bound-slice-1"]["state"] == "PREPARED":
                crashed = True
                raise RuntimeError("injected crash")

        with patch.object(campaign_loop, "_save", side_effect=crash_after_prepared):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.advance()
        saved = campaign_loop.load_campaign(self.path)
        handoff_id = saved["dispatch_journal"]["campaign-bound-slice-1"]["handoff_id"]
        self.assertEqual(saved["dispatch_journal"]["campaign-bound-slice-1"]["state"], "PREPARED")
        self.assertEqual(self.dispatches, [])
        with patch.dict(os.environ, {"AMOF_CAMPAIGN_MISSION_RUN_V2": "0"}):
            resumed = self.advance()
        self.assertEqual(resumed["status"], "CONTINUE")
        self.assertEqual(self.dispatches[0][1], handoff_id)

    def test_crash_before_durable_prepare_has_no_execution(self):
        real_save = campaign_loop._save
        crashed = False

        def crash_before_prepare(path, state):
            nonlocal crashed
            real_save(path, state)
            if not crashed and state["dispatch_journal"]["campaign-bound-slice-1"]["state"] == "NOT_DISPATCHED" \
                    and state["current_slice"] is not None:
                crashed = True
                raise RuntimeError("injected crash")

        with patch.object(campaign_loop, "_save", side_effect=crash_before_prepare):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.advance()
        self.assertEqual(self.dispatches, [])
        self.assertFalse(handoff._handoff_outbox_dir().exists())
        resumed = self.advance()
        self.assertEqual(resumed["status"], "CONTINUE")
        self.assertEqual(len(self.dispatches), 1)

    def test_crash_after_prepare_intent_replays_only_local_prepare(self):
        real_save = campaign_loop._save
        crashed = False

        def crash_at_prepare_intent(path, state):
            nonlocal crashed
            real_save(path, state)
            if not crashed and state["dispatch_journal"]["campaign-bound-slice-1"]["state"] == "PREPARE_INTENT":
                crashed = True
                raise RuntimeError("injected crash")

        with patch.object(campaign_loop, "_save", side_effect=crash_at_prepare_intent):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.advance()
        self.assertEqual(self.dispatches, [])
        self.assertEqual(self.advance()["status"], "CONTINUE")
        self.assertEqual(len(self.dispatches), 1)

    def test_crash_at_dispatch_intent_blocks_unknown_outcome(self):
        real_save = campaign_loop._save
        crashed = False

        def crash_before_dispatch(path, state):
            nonlocal crashed
            real_save(path, state)
            if not crashed and state["dispatch_journal"]["campaign-bound-slice-1"]["state"] == "DISPATCH_INTENT":
                crashed = True
                raise RuntimeError("injected crash")

        with patch.object(campaign_loop, "_save", side_effect=crash_before_dispatch):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.advance()
        self.assertEqual(self.dispatches, [])
        resumed = self.advance()
        self.assertEqual(resumed["status"], "BLOCKED")
        self.assertIn("UNCERTAIN", resumed["reason"])
        self.assertEqual(self.dispatches, [])

    def test_crash_after_external_result_before_acceptance_journal_reconciles(self):
        real_save = campaign_loop._save
        crashed = False

        def crash_before_accepted_save(path, state):
            nonlocal crashed
            if not crashed and state["dispatch_journal"]["campaign-bound-slice-1"]["state"] == "ACCEPTED":
                crashed = True
                raise RuntimeError("injected crash")
            real_save(path, state)

        with patch.object(campaign_loop, "_save", side_effect=crash_before_accepted_save):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.advance()
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(campaign_loop.load_campaign(self.path)["dispatch_journal"]["campaign-bound-slice-1"]["state"],
                         "DISPATCH_INTENT")
        resumed = self.advance()
        self.assertEqual(resumed["status"], "CONTINUE")
        self.assertEqual(len(self.dispatches), 1)

    def test_crash_after_accepted_journal_reconciles_without_callback(self):
        real_save = campaign_loop._save
        crashed = False

        def crash_after_accepted_save(path, state):
            nonlocal crashed
            real_save(path, state)
            if not crashed and state["dispatch_journal"]["campaign-bound-slice-1"]["state"] == "ACCEPTED":
                crashed = True
                raise RuntimeError("injected crash")

        with patch.object(campaign_loop, "_save", side_effect=crash_after_accepted_save):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.advance()
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.advance()["status"], "CONTINUE")
        self.assertEqual(len(self.dispatches), 1)

    def test_crash_after_terminal_journal_never_replays_first_slice(self):
        real_save = campaign_loop._save
        crashed = False

        def crash_after_terminal_save(path, state):
            nonlocal crashed
            real_save(path, state)
            if not crashed and state["dispatch_journal"]["campaign-bound-slice-1"]["state"] == "TERMINAL":
                crashed = True
                raise RuntimeError("injected crash")

        with patch.object(campaign_loop, "_save", side_effect=crash_after_terminal_save):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.advance()
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.advance()["status"], "CONTINUE")
        self.assertEqual([entry[0] for entry in self.dispatches],
                         ["campaign-bound-slice-1", "campaign-bound-slice-2"])

    def test_lost_result_after_external_side_effect_is_uncertain(self):
        effects = []

        def side_effect_without_result(_item, handoff_id, _key):
            effects.append(handoff_id)
            return handoff_id

        with patch.object(self, "dispatch", side_effect=side_effect_without_result):
            blocked = self.advance()
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["dispatch_journal"]["campaign-bound-slice-1"]["terminal_decision"],
                         "BLOCKED_UNCERTAIN")
        self.assertEqual(len(effects), 1)
        self.assertEqual(self.advance()["status"], "BLOCKED")
        self.assertEqual(len(effects), 1)

    def test_accepted_dispatch_waits_and_reconciles_late_result(self):
        accepted = []

        def enqueue_only(_item, handoff_id, _key):
            accepted.append(handoff_id)
            return handoff_id

        original_status = self.status

        def accepted_status(handoff_id):
            status = original_status(handoff_id)
            if status["status"] == "prepared":
                status.update(status="accepted", accepted=True, tracking_ref=handoff_id)
            return status

        with (patch.object(self, "dispatch", side_effect=enqueue_only),
              patch.object(self, "status", side_effect=accepted_status)):
            waiting = self.advance()
            replayed = self.advance()
        self.assertEqual(waiting["status"], "WAITING")
        self.assertEqual(replayed["status"], "WAITING")
        self.assertEqual(len(accepted), 1)
        source = self.home / "late-observation.json"
        source.write_text(json.dumps(observation()), encoding="utf-8")
        handoff._write_execution_result(
            accepted[0], backend_result(session_id="run-late"),
            sealed_acceptance=definition(), runtime_acceptance_receipt=source,
        )
        resumed = self.advance()
        self.assertEqual(resumed["status"], "CONTINUE")
        self.assertEqual(resumed["completed_slices"][0]["run_id"], "run-late")
        self.assertEqual(len(accepted), 1)

    def test_changed_mission_binding_blocks_before_prepare_or_dispatch(self):
        state = campaign_loop.load_campaign(self.path)
        state["mission_binding"]["canonical_mission_id"] = "mission-other"
        campaign_loop._save(self.path, state)
        blocked = self.advance()
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn("identity mismatch", blocked["reason"])
        self.assertEqual(self.dispatches, [])

    def test_consumed_or_mismatched_approval_never_prepares_or_dispatches(self):
        scope = {"target_id": TARGET, "base_sha": SHA, "roots": ["commerce/app/page.tsx"]}
        writable = {"slice_id": "campaign-write-slice-1", "parent_campaign_id": "campaign-write",
                    "scope_tag": "write", "requested_backend": "hermes_opensandbox",
                    "requested_capabilities": ["read", "bounded_write"],
                    "objective": "Edit one file", "expected_validation": "repo-head",
                    "write_authority_ref": "wsa-existing", "requested_write_scope": scope}
        for active, approval_target in ((False, TARGET), (True, "wrong-target")):
            path = self.home / f"writable-{active}.json"
            campaign_loop.create_campaign(
                path, campaign_id="campaign-write", objective="Edit one file",
                allowed_backends=["hermes_opensandbox"], allowed_scope_tags=["write"],
                slice_plan=[writable], max_slices=1,
                allowed_capabilities=["read", "bounded_write"],
                allowed_target_id=TARGET, allowed_write_roots=["commerce/app/page.tsx"],
                mission_binding={"version": 2, "project_id": "project-one",
                                 "canonical_mission_id": "mission-one", "target_id": TARGET,
                                 "expected_source_sha": SHA, "workspace_id": str(self.home / "workspace"),
                                 "repo_name": "simple-ai-shop", "repo_owner": "marekhotshot",
                                 "branch_ref": "main"},
            )
            body = {"target_id": approval_target, "base_sha": SHA,
                    "allowed_roots": scope["roots"], "denied_roots": []}
            with (patch.object(campaign_loop, "load_approval", return_value={"body": body}),
                  patch.object(campaign_loop, "validate_approval_body", return_value=body),
                  patch.object(campaign_loop, "is_approval_active", return_value=active)):
                result = campaign_loop.advance_campaign(
                    path, propose_next=campaign_loop.next_hermes_decoupling_slice,
                    dispatch_handoff=lambda _: self.fail("legacy dispatch invoked"),
                    prepare_handoff=lambda *_: self.fail("prepared with invalid Approval"),
                    dispatch_prepared=lambda *_: self.fail("dispatched with invalid Approval"),
                    observe_progress=self.progress, load_status=self.status,
                )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["dispatch_journal"]["campaign-write-slice-1"]["state"],
                             "NOT_DISPATCHED")


if __name__ == "__main__":
    unittest.main()
