"""Cross-process proof for the durable handoff execution claim."""

import multiprocessing
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from amof.commands import handoff


def _contender(home, handoff_id, start, answers):
    os.environ["AMOF_HOME"] = home
    start.wait()
    try:
        handoff._claim_execution_once(handoff_id)
    except ValueError:
        answers.put("rejected")
    else:
        answers.put("claimed")


def _crash_after_claim(home, handoff_id):
    os.environ["AMOF_HOME"] = home
    handoff._claim_execution_once(handoff_id)
    os._exit(17)


def _execute_contender(home, handoff_id, barrier, marker, answers):
    os.environ["AMOF_HOME"] = home

    def manifest(_args):
        barrier.wait(timeout=10)
        return {}

    def external(*_args, **_kwargs):
        fd = os.open(marker, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("entered\n")
        return SimpleNamespace(result={"result_kind": "agent_run_result", "contract_version": "agent-run-v1",
                                       "schema_version": 1, "status": "completed", "session_id": "run-once",
                                       "exit_code": 0, "stop_reason": "completed", "backend": "amof_builtin_code",
                                       "fallback_used": False, "final_text": "done"})

    with (patch.object(handoff, "_load_execution_manifest", side_effect=manifest),
          patch.object(handoff, "_stderr"),
          patch.object(handoff, "_apply_write_scope_bind_gate", return_value=None),
          patch.object(handoff, "_selected_runner_id", return_value=None),
          patch.object(handoff, "_builtin_read_only_structured_proposal_discovery", return_value=False),
          patch.object(handoff.agent_cmd, "run_external_agent_plan_execute_envelope", side_effect=external)):
        try:
            handoff._execute_agent_from_handoff(SimpleNamespace(handoff_id=handoff_id, confirm=True))
        except Exception as exc:
            answers.put(type(exc).__name__)
        else:
            answers.put("completed")


class HandoffAtomicClaimTests(unittest.TestCase):
    def test_cli_prepare_accepts_exact_campaign_identity_and_replay(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AMOF_HOME": home}):
            binding = {
                "version": 2, "campaign_id": "campaign-cli", "slice_id": "slice-one",
                "project_id": "project-one", "canonical_mission_id": "mission-one",
                "target_id": "github_app:example/repo:" + "a" * 40,
                "expected_source_sha": "a" * 40, "workspace_id": str(Path(home) / "workspace"),
                "repo_name": "repo", "repo_owner": "example", "branch_ref": "main",
            }
            canonical = handoff.CanonicalMissionPacket(
                schema_version=1, contract_version="canonical-mission-packet-v1",
                mission_id="mission-one", ticket_id="ticket-one", task_class="validation",
                classification="internal", goal="Verify", objective="Verify one slice",
                repo_name="repo", repo_owner="example", branch_ref="main",
                execution_allowed=True, requested_mode="read_only",
                allowed_mutations=("read_only",), forbidden_mutations=("runtime_mutation",),
                validation_gates=("repo-head",),
            )
            args = SimpleNamespace(source="campaign", target="amof-agent", studio_session=None,
                                   payload_kind="canonical-mission-packet", confirm=True,
                                   campaign_binding_json=json.dumps(binding))
            with patch.object(handoff.sys, "stdin") as stdin, patch.object(handoff, "_emit_json_stdout") as emit:
                stdin.buffer.read.return_value = handoff._canonical_json(canonical.to_payload()).encode()
                self.assertEqual(handoff.cmd_handoff_prepare(args), 0)
                self.assertEqual(handoff.cmd_handoff_prepare(args), 0)
            self.assertEqual(emit.call_count, 2)
            self.assertEqual(emit.call_args.args[0]["handoff_id"],
                             handoff.campaign_dispatch_identity(binding)[0])

    def test_legacy_queued_without_claim_is_not_reexecuted(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AMOF_HOME": home}):
            payload = handoff._validated_payload_from_text("Inspect only", field_name="test")
            packet = handoff._build_packet(source="test", target="amof-agent", studio_session_id=None,
                                           payload_kind="selected_text", payload=payload)
            handoff._write_packet(packet)
            handoff._write_execution_state(handoff.HandoffExecutionState(
                schema_version=1, handoff_id=packet.handoff_id, status="queued",
                request_id=packet.handoff_id, updated_at="2026-10-10T00:00:00Z",
            ))
            with self.assertRaisesRegex(ValueError, "not eligible for re-execution"):
                handoff._execute_agent_from_handoff(SimpleNamespace(handoff_id=packet.handoff_id, confirm=True))
            self.assertFalse(handoff._execution_claim_path(packet.handoff_id).exists())

    def test_two_processes_only_one_enters_external_runner(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AMOF_HOME": home}):
            payload = handoff._validated_payload_from_text("Inspect only", field_name="test")
            packet = handoff._build_packet(source="test", target="amof-agent", studio_session_id=None,
                                           payload_kind="selected_text", payload=payload)
            handoff._write_packet(packet)
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            answers = context.Queue()
            marker = str(Path(home) / "external-calls.txt")
            workers = [context.Process(target=_execute_contender,
                                       args=(home, packet.handoff_id, barrier, marker, answers)) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(15)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(Path(marker).read_text().splitlines(), ["entered"])
            self.assertEqual(sorted(answers.get(timeout=2) for _ in workers).count("ValueError"), 1)

    def test_bound_prepare_replays_same_packet_and_rejects_changed_target(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"AMOF_HOME": home}):
            binding = {
                "version": 2, "campaign_id": "campaign-one", "slice_id": "campaign-one-slice-1",
                "project_id": "project-one", "canonical_mission_id": "mission-one",
                "target_id": "github_app:example/repo:" + "a" * 40,
                "expected_source_sha": "a" * 40, "workspace_id": str(Path(home) / "workspace"),
                "repo_name": "repo", "repo_owner": "example", "branch_ref": "main",
            }
            canonical = handoff.CanonicalMissionPacket(
                schema_version=1, contract_version="canonical-mission-packet-v1",
                mission_id="mission-one", ticket_id="ticket-one", task_class="validation",
                classification="internal", goal="Verify", objective="Verify one slice",
                repo_name="repo", repo_owner="example", branch_ref="main",
                execution_allowed=True, requested_mode="read_only",
                allowed_mutations=("read_only",), forbidden_mutations=("runtime_mutation",),
                validation_gates=("repo-head",),
            )
            text = handoff._canonical_json(canonical.to_payload())
            first = handoff.prepare_campaign_handoff(binding=binding, canonical_packet_text=text)
            self.assertEqual(first, handoff.prepare_campaign_handoff(binding=binding, canonical_packet_text=text))
            path = Path(first["packet_path"])
            self.assertTrue(path.is_file())
            self.assertEqual(handoff._load_prepared_packet(first["handoff_id"])[1].campaign_binding["project_id"], "project-one")
            changed = {**binding, "workspace_id": str(Path(home) / "other-workspace")}
            with self.assertRaisesRegex(ValueError, "identity conflict"):
                handoff.prepare_campaign_handoff(binding=changed, canonical_packet_text=text)
            with self.assertRaisesRegex(ValueError, "target and source identity mismatch"):
                handoff.campaign_dispatch_identity({**binding, "target_id": "github_app:example/other:" + "a" * 40})
            wrong_manifest = {"repos": [{"name": "repo", "url": "https://github.com/example/repo.git",
                                          "path": str(Path(home) / "wrong-workspace")}]}
            with (patch.object(handoff, "_load_execution_manifest", return_value=wrong_manifest),
                  patch.object(handoff, "_stderr"),
                  patch.object(handoff.agent_cmd, "run_external_agent_plan_execute_envelope") as external):
                with self.assertRaisesRegex(ValueError, "workspace or repository mismatch"):
                    handoff._execute_agent_from_handoff(
                        SimpleNamespace(handoff_id=first["handoff_id"], confirm=True))
                external.assert_not_called()
            self.assertFalse(handoff._execution_claim_path(first["handoff_id"]).exists())
            job_manifest = {"repos": [{"name": "repo", "url": "https://github.com/example/repo.git",
                                       "path": "/run-work/files"}]}
            with (patch.object(handoff.subprocess, "run", return_value=SimpleNamespace(stdout="a" * 40)) as git):
                handoff._verify_campaign_execution_target(handoff._load_prepared_packet(first["handoff_id"])[1], job_manifest)
                self.assertEqual(git.call_args.args[0][2], "/run-work/files")
            right_manifest = {"repos": [{"name": "repo", "url": "https://github.com/example/repo.git",
                                          "path": binding["workspace_id"]}]}
            with (patch.object(handoff, "_load_execution_manifest", return_value=right_manifest),
                  patch.object(handoff.subprocess, "run", return_value=SimpleNamespace(stdout="b" * 40)),
                  patch.object(handoff, "_stderr"),
                  patch.object(handoff.agent_cmd, "run_external_agent_plan_execute_envelope") as external):
                with self.assertRaisesRegex(ValueError, "source SHA mismatch"):
                    handoff._execute_agent_from_handoff(
                        SimpleNamespace(handoff_id=first["handoff_id"], confirm=True))
                external.assert_not_called()

    def test_two_processes_only_one_claims_execution(self):
        with tempfile.TemporaryDirectory() as home:
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            answers = context.Queue()
            workers = [context.Process(target=_contender, args=(home, "handoff-race", start, answers))
                       for _ in range(2)]
            for worker in workers:
                worker.start()
            start.set()
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(sorted(answers.get(timeout=2) for _ in workers), ["claimed", "rejected"])
            with patch.dict(os.environ, {"AMOF_HOME": home}):
                self.assertTrue(handoff._execution_claim_path("handoff-race").is_file())

    def test_crash_after_claim_never_reclaims_unknown_attempt(self):
        with tempfile.TemporaryDirectory() as home:
            context = multiprocessing.get_context("spawn")
            worker = context.Process(target=_crash_after_claim, args=(home, "handoff-crash"))
            worker.start()
            worker.join(10)
            self.assertEqual(worker.exitcode, 17)
            with patch.dict(os.environ, {"AMOF_HOME": home}):
                with self.assertRaisesRegex(ValueError, "already claimed"):
                    handoff._claim_execution_once("handoff-crash")


if __name__ == "__main__":
    unittest.main()
