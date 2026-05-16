"""Background dispatcher for durable serial queue execution."""

from __future__ import annotations

import json
import logging
import os
import re
import select
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


def _subprocess_deadline_seconds(action: str) -> int:
    """Wallclock deadline for a single dispatcher subprocess.

    The QueueDispatcher worker thread is single-threaded: while one subprocess
    is running, no other queued task can be claimed. A subprocess that hangs
    silently (e.g. a cluster build wrapper waiting on a missing builder Job
    or a wedged ``kubectl logs -f`` pipe) freezes every subsequent action
    (``ticket_switch``, follow-up build, etc.) in ``queued`` forever.

    The deadline gives every subprocess a truthful upper bound: if it runs
    longer, the dispatcher SIGTERMs (then SIGKILLs) it and marks the run
    failed with exit_code 124, instead of head-of-line blocking the queue.
    Defaults are tuned to comfortably accommodate a real cluster build while
    bounding worst-case dispatcher freeze.
    """

    action_text = str(action or "").strip()
    env_override = os.environ.get(
        "AMOF_SUBPROCESS_HARD_DEADLINE_SECONDS"
    ) or os.environ.get("AMOF_DISPATCHER_SUBPROCESS_DEADLINE_SECONDS")
    if env_override:
        try:
            value = int(env_override)
            if value > 0:
                return value
        except ValueError:
            logger.warning(
                "Invalid AMOF_SUBPROCESS_HARD_DEADLINE_SECONDS=%r; falling back to default",
                env_override,
            )
    if action_text.startswith("release/lifecycle/build"):
        return 1800
    if action_text.startswith("release/lifecycle/"):
        return 1200
    return 600


SUBPROCESS_TIMEOUT_EXIT_CODE = 124

from amof.api.command_builder import get_workspace_root
from amof.api.run_manager import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PAUSED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCESS,
    RunManager,
    summarize_loop_state,
)
from amof.api.services.agent_runner import run_agent_for_ui
from amof.orchestrator.batch_contract import (
    BatchManifestContract,
    FORCED_CHOICE_OPTIONS,
    evaluate_batch_item,
    scope_gate_item,
)

from .models import (
    QUEUE_STATUS_CANCELLED,
    QUEUE_STATUS_PAUSED,
    QUEUE_STATUS_RUNNING,
    QueueTaskRecord,
)
from .store import QueueStore, QueueTransitionError

logger = logging.getLogger(__name__)


def _normalized_control_metadata(control_metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(control_metadata, dict):
        return {}
    normalized: Dict[str, Any] = {}
    for key, value in control_metadata.items():
        if value is None:
            continue
        if isinstance(value, dict):
            nested = {nested_key: nested_value for nested_key, nested_value in value.items() if nested_value is not None}
            if nested:
                normalized[key] = nested
            continue
        normalized[key] = value
    return normalized


def _director_result_artifact(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    result_path = str(payload.get("result_path") or "").strip()
    if not result_path:
        return None
    path = Path(result_path)
    if not path.is_absolute():
        path = get_workspace_root() / path
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    deploy_result = result.get("deploy_result") if isinstance(result.get("deploy_result"), dict) else {}
    workload = (
        deploy_result.get("workload_summary")
        if isinstance(deploy_result.get("workload_summary"), dict)
        else {}
    )
    readback = result.get("readback_result") if isinstance(result.get("readback_result"), dict) else {}
    probes = readback.get("probes") if isinstance(readback.get("probes"), list) else []
    compact_probes = []
    for probe in probes:
        if not isinstance(probe, dict):
            continue
        compact_probes.append({
            "url": probe.get("url"),
            "http_status": probe.get("http_status"),
            "body": probe.get("body"),
            "body_summary": probe.get("body_summary"),
        })
    return {
        "final_status": result.get("final_status"),
        "result_path": str(path),
        "deploy_result": {
            "sync_status": deploy_result.get("sync_status"),
            "health_status": deploy_result.get("health_status"),
            "revision": deploy_result.get("revision"),
            "total_pods": workload.get("total_pods"),
            "running_pods": workload.get("running_pods"),
            "not_running_pods": workload.get("not_running_pods"),
        },
        "readback_result": {
            "readback_pass": readback.get("readback_pass"),
            "probes": compact_probes,
        },
        "release_promote_attempted": result.get("release_promote_attempted"),
        "blocker": result.get("blocker"),
        "failure_classification": result.get("failure_classification"),
    }


def _lifecycle_run_evidence(
    action: str,
    event_payload: Optional[Dict[str, Any]],
    *,
    status: str,
    exit_code: Optional[int] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> Dict[str, Any]:
    action_text = str(action or "").strip()
    if action_text.startswith("director-action/"):
        payload = dict(event_payload or {})
        director_action = str(payload.get("director_action") or action_text).strip()
        target = str(
            payload.get("target_environment")
            or payload.get("environment_id")
            or payload.get("stage_id")
            or "dev"
        ).strip()
        evidence: Dict[str, Any] = {
            "director_action": director_action,
            "director_action_status": status,
            "environment_id": target,
            "target_environment": target,
            "summary": f"Director action {director_action} {status} for {target}",
            "result": (
                "success"
                if status == "completed"
                else ("failed" if status == "failed" else ("cancelled" if status == "cancelled" else status))
            ),
        }
        for key in (
            "ticket_id",
            "input_path",
            "result_path",
            "local_only",
            "cloud_dev",
            "release_promote",
            "request_id",
        ):
            if key in payload and payload.get(key) not in (None, ""):
                evidence[key] = payload.get(key)
        if started_at:
            evidence["started_at"] = started_at
        if finished_at:
            evidence["finished_at"] = finished_at
        if exit_code is not None:
            evidence["exit_code"] = exit_code
        if status in {"completed", "failed"}:
            result_artifact = _director_result_artifact(payload)
            if result_artifact is not None:
                evidence["director_result"] = result_artifact
                final_status = str(result_artifact.get("final_status") or "").strip()
                if final_status:
                    evidence["director_final_status"] = final_status
                if result_artifact.get("blocker"):
                    evidence["blocker"] = "director_action_result_blocked"
                    evidence["blocker_summary"] = str(result_artifact.get("blocker"))
        if status == "failed":
            evidence["blocker"] = "director_action_failed"
            evidence["blocker_summary"] = evidence["summary"]
        return evidence
    if not action_text.startswith("release/lifecycle/"):
        return {}
    payload = dict(event_payload or {})
    lifecycle_action = str(payload.get("lifecycle_action") or payload.get("action") or action_text.rsplit("/", 1)[-1]).strip()
    evidence: Dict[str, Any] = {
        "lifecycle_action": lifecycle_action,
        "lifecycle_status": status,
    }
    for key in (
        "environment_id",
        "stage_id",
        "image_tag",
        "release_id",
        "release_name",
        "namespace",
        "deploy_profile",
        "public_host",
        "public_url",
        "build_backend",
        "source_mode",
        "source_branch",
        "source_commit",
        "amof_ref",
        "amof_commit",
        "amof_ui_ref",
        "amof_ui_commit",
        "assistant_ref",
        "assistant_commit",
        "builder",
        "builder_job_name",
        "builder_namespace",
        "builder_pod_name",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            evidence[key] = value
    for key in ("sources", "images"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            evidence[key] = list(value)
    if "no_push" in payload:
        evidence["no_push"] = bool(payload.get("no_push"))
    if started_at:
        evidence["started_at"] = started_at
    if finished_at:
        evidence["finished_at"] = finished_at
    target = str(evidence.get("environment_id") or evidence.get("stage_id") or lifecycle_action).strip()
    summary = f"Lifecycle {lifecycle_action} {status} for {target}"
    image_tag = str(evidence.get("image_tag") or "").strip()
    if image_tag:
        summary += f" ({image_tag})"
    evidence["summary"] = summary
    evidence["result"] = (
        "success"
        if status == "completed"
        else ("failed" if status == "failed" else ("cancelled" if status == "cancelled" else status))
    )
    if exit_code is not None:
        evidence["exit_code"] = exit_code
    if status == "failed":
        evidence["blocker"] = f"lifecycle_{lifecycle_action}_failed"
        evidence["blocker_summary"] = summary
    return evidence


_CLUSTER_BUILDER_JOB_RE = re.compile(
    r"Launching cluster builder job '([^']+)' in namespace '([^']+)'"
)
_CLUSTER_BUILDER_POD_RE = re.compile(r"Builder pod: ([^\s]+)")


def _cluster_builder_identity_from_log(line: str) -> Dict[str, str]:
    text = str(line or "").strip()
    if not text:
        return {}
    job_match = _CLUSTER_BUILDER_JOB_RE.search(text)
    if job_match:
        return {
            "builder_job_name": job_match.group(1).strip(),
            "builder_namespace": job_match.group(2).strip(),
        }
    pod_match = _CLUSTER_BUILDER_POD_RE.search(text)
    if pod_match:
        return {"builder_pod_name": pod_match.group(1).strip()}
    return {}


class QueueDispatcher:
    """Single-process dispatcher that replays persisted queue tasks."""

    def __init__(self, run_manager: RunManager, queue_store: QueueStore):
        self.run_manager = run_manager
        self.queue_store = queue_store
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self._active_run_id: Optional[str] = None
        self._active_payload: Optional[Dict[str, Any]] = None
        self._active_started_at: Optional[float] = None
        self._active_process: Optional[subprocess.Popen[str]] = None

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._reconcile_orphaned_running_tasks()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="amof-queue-dispatcher",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake_event.set()

    def _reconcile_orphaned_running_tasks(self) -> None:
        terminal_statuses = {
            RUN_STATUS_SUCCESS,
            RUN_STATUS_FAILED,
            RUN_STATUS_CANCELLED,
            "stopped",
            "error",
        }
        for item in self.queue_store.list():
            if item.status != QUEUE_STATUS_RUNNING:
                continue
            run = self.run_manager.get_run(item.run_id)
            if run is not None and str(run.status or "").strip().lower() in terminal_statuses:
                try:
                    self.queue_store.transition(
                        item.run_id,
                        self.run_manager._queue_status_for_run(run.status),
                        control_updates={
                            "reconciled_at": item.updated_at,
                            "reconciled_reason": "terminal_run_outpaced_queue",
                        },
                    )
                except Exception:
                    logger.warning(
                        "Failed to reconcile terminal queue task %s during dispatcher start",
                        item.run_id,
                        exc_info=True,
                    )
                continue

            self.run_manager.append_log(
                item.run_id,
                "Recovered orphaned running task during dispatcher start; "
                "no live worker owns this run anymore.",
            )
            self.run_manager.update_status(item.run_id, RUN_STATUS_FAILED, exit_code=1)

    def enqueue_subprocess(
        self,
        ecosystem: str,
        action: str,
        command: List[str],
        *,
        cwd: str,
        session_id: Optional[str] = None,
        event_payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        self.start()
        queue_payload = {
            "kind": "subprocess",
            "cmd": list(command),
            "cwd": cwd,
        }
        if isinstance(event_payload, dict):
            queue_payload["event_payload"] = dict(event_payload)
        run_id = self.run_manager.create_run(
            ecosystem,
            action,
            command,
            session_id=session_id,
            queue_payload=queue_payload,
        )
        if isinstance(event_payload, dict):
            lifecycle_action = str(event_payload.get("action") or event_payload.get("lifecycle_action") or "").strip()
            if lifecycle_action:
                self.run_manager.append_event(
                    run_id,
                    level="info",
                    type="lifecycle_action_queued",
                    message=f"Lifecycle action queued: {lifecycle_action}",
                    payload={
                        **event_payload,
                        "action": lifecycle_action,
                        "lifecycle_action": lifecycle_action,
                        "status": "queued",
                    },
                )
        queued_evidence = _lifecycle_run_evidence(action, event_payload, status="queued")
        if queued_evidence:
            self.run_manager.update_loop_state(
                run_id,
                {
                    "run_id": run_id,
                    "task_id": run_id,
                    "queue_state": "queued",
                    "loop_state": "queued",
                    "loop_step": 0,
                    "current_goal": str(command),
                    "decision": "CONTINUE",
                    "stop_reason": None,
                    "cancel_requested": False,
                    "steps": [],
                    "latest_evidence": queued_evidence,
                    "last_result_summary": queued_evidence.get("summary"),
                },
            )
        self.wake()
        return run_id

    def enqueue_agent(
        self,
        ecosystem: str,
        *,
        prompt: str,
        mode: str = "plan-execute",
        runtime_profile: Optional[str] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        arena_mode: Optional[str] = None,
        team_id: Optional[str] = None,
        delegation_id: Optional[str] = None,
        backlog_item_id: Optional[str] = None,
        trigger_kind: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        control_metadata: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, str]:
        self.start()
        resolved_session_id = session_id or str(uuid.uuid4())
        run_id = self.run_manager.create_run(
            ecosystem,
            "agent",
            ["amof", "agent", "..."],
            session_id=resolved_session_id,
            queue_payload={
                "kind": "agent",
                "ecosystem": ecosystem,
                "prompt": prompt,
                "mode": mode,
                "runtime_profile": runtime_profile,
                "session_id": resolved_session_id,
                "agent_id": agent_id,
                "thread_id": thread_id,
                "arena_mode": arena_mode,
                "team_id": team_id,
                "delegation_id": delegation_id,
                "backlog_item_id": backlog_item_id,
                "trigger_kind": trigger_kind,
                "parent_run_id": parent_run_id,
            },
        )
        normalized_control = _normalized_control_metadata(control_metadata)
        if normalized_control:
            self.queue_store.transition(
                run_id,
                self.queue_store.load(run_id).status,
                control_updates=normalized_control,
            )
        self.wake()
        return run_id, resolved_session_id

    def enqueue_batch(
        self,
        ecosystem: str,
        *,
        batch_manifest: Dict[str, Any],
        runtime_profile: Optional[str] = None,
        session_id: Optional[str] = None,
        control_metadata: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, str]:
        self.start()
        resolved_session_id = session_id or str(uuid.uuid4())
        run_id = self.run_manager.create_run(
            ecosystem,
            "batch",
            ["amof", "batch", "..."],
            session_id=resolved_session_id,
            queue_payload={
                "kind": "batch",
                "ecosystem": ecosystem,
                "batch_manifest": dict(batch_manifest or {}),
                "runtime_profile": runtime_profile,
                "session_id": resolved_session_id,
            },
        )
        normalized_control = _normalized_control_metadata(control_metadata)
        if normalized_control:
            self.queue_store.transition(
                run_id,
                self.queue_store.load(run_id).status,
                control_updates=normalized_control,
            )
        self.wake()
        return run_id, resolved_session_id

    def pause_task(self, run_id: str, control_metadata: Optional[Dict[str, Any]] = None) -> QueueTaskRecord:
        item = self.queue_store.request_pause(run_id)
        normalized_control = _normalized_control_metadata(control_metadata)
        if normalized_control:
            item = self.queue_store.transition(run_id, item.status, control_updates=normalized_control)
        if item.status != QUEUE_STATUS_RUNNING:
            self.run_manager.update_status(run_id, RUN_STATUS_PAUSED)
        self.wake()
        return item

    def resume_task(
        self,
        run_id: str,
        choice: Optional[str] = None,
        control_metadata: Optional[Dict[str, Any]] = None,
    ) -> QueueTaskRecord:
        run = self.run_manager.get_run(run_id)
        loop_state = dict(run.loop_state or {}) if run is not None and isinstance(run.loop_state, dict) else {}
        batch_state = str(loop_state.get("batch_state") or "").strip()
        forced_choices = list(loop_state.get("forced_choices") or [])
        normalized_choice = str(choice or "").strip()
        if batch_state == "paused_for_choice":
            if not normalized_choice:
                raise QueueTransitionError("Paused batch task requires an explicit resume choice")
            if forced_choices and normalized_choice not in forced_choices:
                raise QueueTransitionError(
                    f"Resume choice '{normalized_choice}' is not allowed for batch task '{run_id}'"
                )
            if normalized_choice == "stop":
                return self.stop_task(run_id, control_metadata=control_metadata)
            if normalized_choice == "handoff":
                item = self.queue_store.transition(
                    run_id,
                    QUEUE_STATUS_PAUSED,
                    control_updates={
                        "selected_choice": normalized_choice,
                        "resume_choice": None,
                        "resume_requested_at": None,
                    },
                )
                self.run_manager.update_loop_state(
                    run_id,
                    {
                        **loop_state,
                        "batch_state": "handoff_requested",
                        "pause_mode": None,
                        "required_choice": None,
                        "forced_choices": [],
                        "selected_choice": normalized_choice,
                        "decision": "STOP",
                        "stop_reason": "handoff_requested",
                        "latest_evidence": {
                            "summary": "Batch handed off after forced-choice pause.",
                            "decision": "STOP",
                            "selected_choice": normalized_choice,
                            "batch_id": loop_state.get("batch_id"),
                            "item_id": loop_state.get("current_item_id"),
                            "handoff": dict(loop_state.get("handoff") or {}),
                        },
                        "last_result_summary": "Batch handed off after forced-choice pause.",
                    },
                )
                self.run_manager.update_status(run_id, RUN_STATUS_PAUSED)
                normalized_control = _normalized_control_metadata(control_metadata)
                if normalized_control:
                    item = self.queue_store.transition(run_id, item.status, control_updates=normalized_control)
                self.wake()
                return item
            item = self.queue_store.request_resume(run_id, resume_choice=normalized_choice)
            self.run_manager.update_loop_state(
                run_id,
                {
                    **loop_state,
                    "batch_state": "resume_requested",
                    "pause_mode": None,
                    "required_choice": None,
                    "forced_choices": [],
                    "selected_choice": normalized_choice,
                    "decision": "CONTINUE",
                    "stop_reason": None,
                    "latest_evidence": {
                        "summary": f"Batch resume requested with choice {normalized_choice}.",
                        "decision": "CONTINUE",
                        "selected_choice": normalized_choice,
                        "batch_id": loop_state.get("batch_id"),
                        "item_id": loop_state.get("current_item_id"),
                    },
                    "last_result_summary": f"Batch resume requested with choice {normalized_choice}.",
                },
            )
            self.run_manager.update_status(run_id, RUN_STATUS_QUEUED)
            normalized_control = _normalized_control_metadata(control_metadata)
            if normalized_control:
                item = self.queue_store.transition(run_id, item.status, control_updates=normalized_control)
            self.wake()
            return item
        item = self.queue_store.request_resume(run_id)
        self.run_manager.update_status(run_id, RUN_STATUS_QUEUED)
        normalized_control = _normalized_control_metadata(control_metadata)
        if normalized_control:
            item = self.queue_store.transition(run_id, item.status, control_updates=normalized_control)
        self.wake()
        return item

    def stop_task(self, run_id: str, control_metadata: Optional[Dict[str, Any]] = None) -> QueueTaskRecord:
        item = self.queue_store.request_stop(run_id)
        normalized_control = _normalized_control_metadata(control_metadata)
        if normalized_control:
            item = self.queue_store.transition(run_id, item.status, control_updates=normalized_control)
        run = self.run_manager.get_run(run_id)
        if run is not None:
            current_loop_state = dict(run.loop_state or {})
            current_loop_state["cancel_requested"] = True
            current_loop_state.setdefault("stop_reason", "cancelled")
            self.run_manager.update_loop_state(run_id, current_loop_state)
        if item.status == QUEUE_STATUS_CANCELLED:
            self.run_manager.append_log(run_id, "Task cancelled before execution")
            self.run_manager.update_status(run_id, RUN_STATUS_CANCELLED, exit_code=130)
        else:
            self.run_manager.append_log(
                run_id,
                "Stop requested for active task; runtime will honor it at the next supported interruption point.",
            )
            self._stop_active_cluster_builder(run_id)
            process = self._active_process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    logger.debug("Failed to terminate active process for run %s", run_id, exc_info=True)
        self.wake()
        return item

    def _record_cluster_builder_identity(self, run_id: str, identity: Dict[str, str]) -> None:
        if not identity:
            return
        run = self.run_manager.get_run(run_id)
        if run is None:
            return
        current_loop_state = dict(run.loop_state or {})
        latest_evidence = dict(current_loop_state.get("latest_evidence") or {})
        changed = False
        for key, value in identity.items():
            if value and latest_evidence.get(key) != value:
                latest_evidence[key] = value
                changed = True
        if not changed:
            return
        current_loop_state["latest_evidence"] = latest_evidence
        self.run_manager.update_loop_state(run_id, current_loop_state)

    def _stop_active_cluster_builder(self, run_id: str) -> None:
        run = self.run_manager.get_run(run_id)
        if run is None or not str(run.action or "").startswith("release/lifecycle/build"):
            return
        latest_evidence = dict((run.loop_state or {}).get("latest_evidence") or {})
        job_name = str(latest_evidence.get("builder_job_name") or "").strip()
        namespace = str(latest_evidence.get("builder_namespace") or "").strip()
        pod_name = str(latest_evidence.get("builder_pod_name") or "").strip()
        if not job_name or not namespace:
            return
        if pod_name:
            self.run_manager.append_log(
                run_id,
                f"Deleting cluster builder pod '{pod_name}' in namespace '{namespace}' due to stop request",
            )
            try:
                subprocess.run(
                    [
                        "kubectl",
                        "-n",
                        namespace,
                        "delete",
                        "pod",
                        pod_name,
                        "--ignore-not-found=true",
                        "--wait=false",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except Exception:
                logger.debug("Failed to delete builder pod for run %s", run_id, exc_info=True)
        self.run_manager.append_log(
            run_id,
            f"Deleting cluster builder job '{job_name}' in namespace '{namespace}' due to stop request",
        )
        try:
            subprocess.run(
                [
                    "kubectl",
                    "-n",
                    namespace,
                    "delete",
                    "job",
                    job_name,
                    "--ignore-not-found=true",
                    "--wait=false",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception:
            logger.debug("Failed to delete builder job for run %s", run_id, exc_info=True)

    def runtime_state(self) -> Dict[str, Any]:
        state = self.queue_store.runtime_state()
        active_loop_state: Dict[str, Any] = {}
        run = None
        active_run_id = state.get("active_run_id")
        if isinstance(active_run_id, str):
            run = self.run_manager.get_run(active_run_id)
            if run and isinstance(run.loop_state, dict):
                active_loop_state = dict(run.loop_state)
        if not active_loop_state:
            active_loop_state = {
                "loop_state": "idle",
                "queue_state": "idle" if int(state.get("queue_depth") or 0) == 0 else "queued",
                "queue_depth": int(state.get("queue_depth") or 0),
                "run_counts": dict(state.get("counts") or {}),
            }
        active_summary = summarize_loop_state(active_loop_state, run.events if run else None, run)
        with self._state_lock:
            state.update(
                {
                    "dispatcher_alive": bool(self._thread and self._thread.is_alive()),
                    "active_run_id": self._active_run_id or state.get("active_run_id"),
                    "active_payload": dict(self._active_payload or {}),
                    "active_started_at": self._active_started_at,
                    "active_loop_state": active_loop_state,
                    "active_latest_evidence": dict(active_summary.get("latest_evidence") or {}),
                    "active_latest_decision": active_summary.get("latest_decision"),
                    "active_latest_summary": active_summary.get("latest_summary"),
                    "active_terminal_condition": active_summary.get("terminal_condition"),
                    "active_blocker": active_summary.get("blocker"),
                    "active_blocker_summary": active_summary.get("blocker_summary"),
                    "active_materialization_mode": active_summary.get("materialization_mode"),
                    "active_promotion_applied": active_summary.get("promotion_applied"),
                    "active_scratch_artifact_paths": list(active_summary.get("scratch_artifact_paths") or []),
                    "active_canonical_write_targets": list(active_summary.get("canonical_write_targets") or []),
                    "active_promotion_evidence": list(active_summary.get("promotion_evidence") or []),
                    "active_pause_mode": active_summary.get("pause_mode"),
                    "active_required_choice": active_summary.get("required_choice"),
                    "active_forced_choices": list(active_summary.get("forced_choices") or []),
                    "active_handoff": dict(active_summary.get("handoff") or {}),
                    "active_runtime_mode": active_summary.get("runtime_mode"),
                    "active_batch_id": active_summary.get("batch_id"),
                    "active_batch_state": active_summary.get("batch_state"),
                    "active_batch_cursor": active_summary.get("batch_cursor"),
                    "active_current_item_id": active_summary.get("current_item_id"),
                    "active_current_child_run_id": active_summary.get("current_child_run_id"),
                    "active_child_run_ids": list(active_summary.get("child_run_ids") or []),
                    "active_completed_items": list(active_summary.get("completed_items") or []),
                    "active_persistence_store_status": active_summary.get("persistence_store_status"),
                    "active_persistence_warning_active": active_summary.get("persistence_warning_active"),
                    "active_persistence_warning": active_summary.get("persistence_warning"),
                    "active_last_persistence_error": active_summary.get("last_persistence_error"),
                    "active_last_persistence_error_at": active_summary.get("last_persistence_error_at"),
                    "active_last_persisted_at": active_summary.get("last_persisted_at"),
                    "active_persistence_error_count": active_summary.get("persistence_error_count"),
                    "active_runtime_merkle_root": dict(active_summary.get("runtime_merkle_root") or {}),
                    "active_artifact_merkle_root": dict(active_summary.get("artifact_merkle_root") or {}),
                    "active_agent_merkle_root": dict(active_summary.get("agent_merkle_root") or {}),
                    "active_pipeline_merkle_root": dict(active_summary.get("pipeline_merkle_root") or {}),
                    "active_stop_boundary_requested": active_summary.get("stop_boundary_requested"),
                    "active_stop_boundary_phase": active_summary.get("stop_boundary_phase"),
                    "active_stop_boundary_step": active_summary.get("stop_boundary_step"),
                    "active_stop_boundary_honored": active_summary.get("stop_boundary_honored"),
                    "active_stop_boundary_decision": active_summary.get("stop_boundary_decision"),
                    "active_stop_boundary_summary": active_summary.get("stop_boundary_summary"),
                    "active_stop_boundary_stop_reason": active_summary.get("stop_boundary_stop_reason"),
                }
            )
        return state

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            task = self.queue_store.claim_next()
            if task is None:
                self._wake_event.wait(timeout=0.5)
                self._wake_event.clear()
                continue
            try:
                self._execute_task(task)
            except Exception as exc:
                logger.exception("Queue task %s failed", task.run_id)
                self.run_manager.append_log(task.run_id, f"Queue dispatcher error: {exc}")
                self.run_manager.update_status(task.run_id, RUN_STATUS_FAILED)
            finally:
                with self._state_lock:
                    self._active_run_id = None
                    self._active_payload = None
                    self._active_started_at = None
                    self._active_process = None

    def _execute_task(self, task: QueueTaskRecord) -> None:
        with self._state_lock:
            self._active_run_id = task.run_id
            self._active_payload = dict(task.payload or {})
            self._active_started_at = time.time()

        payload = dict(task.payload or {})
        if task.control.get("stop_requested"):
            self.run_manager.append_log(task.run_id, "Task cancelled before dispatch")
            self.run_manager.update_loop_state(
                task.run_id,
                {
                    "run_id": task.run_id,
                    "task_id": task.run_id,
                    "queue_state": "cancelled",
                    "loop_state": "cancelled",
                    "loop_step": 0,
                    "current_goal": str(payload.get("prompt") or task.action),
                    "decision": "STOP",
                    "stop_reason": "cancelled",
                    "cancel_requested": True,
                    "steps": [],
                },
            )
            self.run_manager.update_status(task.run_id, RUN_STATUS_CANCELLED, exit_code=130)
            return

        kind = str(payload.get("kind") or "").strip().lower()
        if kind == "subprocess":
            self._run_subprocess_task(task, payload)
            return
        if kind == "agent":
            self._run_agent_task(task, payload)
            return
        if kind == "batch":
            self._run_batch_task(task, payload)
            return

        self.run_manager.append_log(task.run_id, f"Unknown queue task kind: {kind or 'missing'}")
        self.run_manager.update_status(task.run_id, RUN_STATUS_FAILED)

    def _run_subprocess_task(self, task: QueueTaskRecord, payload: Dict[str, Any]) -> None:
        cmd = payload.get("cmd")
        cwd = payload.get("cwd")
        event_payload = payload.get("event_payload") if isinstance(payload.get("event_payload"), dict) else None
        captured_output: List[str] = []
        if not isinstance(cmd, list) or not cmd:
            self.run_manager.append_log(task.run_id, "Queue task is missing subprocess command payload")
            self.run_manager.update_status(task.run_id, RUN_STATUS_FAILED)
            return

        def maybe_attach_release_validation_evidence(return_code: int) -> None:
            if str(task.action or "").strip() != "release/validate":
                return
            from amof.api.routers.release import _persist_validation_result_evidence

            _persist_validation_result_evidence(
                self.run_manager,
                task.run_id,
                return_code,
                "\n".join(captured_output),
            )

        def emit_lifecycle_event(event_type: str, status: str, message: str) -> None:
            if not isinstance(event_payload, dict):
                return
            lifecycle_action = str(event_payload.get("action") or event_payload.get("lifecycle_action") or "").strip()
            if not lifecycle_action:
                return
            self.run_manager.append_event(
                task.run_id,
                level="info" if status not in {"failed", "error"} else "error",
                type=event_type,
                message=message,
                payload={
                    **event_payload,
                    "action": lifecycle_action,
                    "lifecycle_action": lifecycle_action,
                    "status": status,
                },
            )

        self.run_manager.update_status(task.run_id, RUN_STATUS_RUNNING)
        emit_lifecycle_event("lifecycle_action_started", "started", "Lifecycle action started")
        run = self.run_manager.get_run(task.run_id)
        running_evidence = _lifecycle_run_evidence(
            task.action,
            event_payload,
            status="running",
            started_at=getattr(run, "started_at", None),
        )
        self.run_manager.update_loop_state(
            task.run_id,
            {
                "run_id": task.run_id,
                "task_id": task.run_id,
                "queue_state": "running",
                "loop_state": "running",
                "loop_step": 0,
                "current_goal": str(payload.get("cmd") or task.action),
                "decision": "CONTINUE",
                "stop_reason": None,
                "cancel_requested": False,
                "steps": [],
                "latest_evidence": running_evidence,
                "last_result_summary": running_evidence.get("summary") if running_evidence else None,
            },
        )
        try:
            process = subprocess.Popen(
                [str(part) for part in cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(cwd) if cwd else None,
            )
            with self._state_lock:
                self._active_process = process

            assert process.stdout is not None
            # Non-blocking + select-driven so the loop can observe stop_requested
            # and the wallclock deadline even when the subprocess is silent.
            os.set_blocking(process.stdout.fileno(), False)
            deadline_seconds = _subprocess_deadline_seconds(task.action)
            deadline_at = time.monotonic() + deadline_seconds
            stdout_buffer = ""
            timed_out = False
            stop_terminate_requested_at: Optional[float] = None
            stop_kill_requested = False

            def _drain_pending() -> None:
                nonlocal stdout_buffer
                try:
                    chunk = process.stdout.read()
                except (BlockingIOError, ValueError):
                    chunk = None
                if not chunk:
                    return
                stdout_buffer += chunk
                while "\n" in stdout_buffer:
                    line, stdout_buffer = stdout_buffer.split("\n", 1)
                    if not line:
                        continue
                    captured_output.append(line)
                    self.run_manager.append_log(task.run_id, line)
                    self._record_cluster_builder_identity(
                        task.run_id,
                        _cluster_builder_identity_from_log(line),
                    )

            while True:
                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if ready:
                    _drain_pending()
                if self._stop_requested(task.run_id) and process.poll() is None:
                    if stop_terminate_requested_at is None:
                        self.run_manager.append_log(task.run_id, "Stop requested; terminating subprocess task")
                        captured_output.append("Stop requested; terminating subprocess task")
                        process.terminate()
                        stop_terminate_requested_at = time.monotonic()
                    elif (
                        not stop_kill_requested
                        and time.monotonic() - stop_terminate_requested_at >= 5.0
                    ):
                        self.run_manager.append_log(
                            task.run_id,
                            "Subprocess did not exit after stop request; force killing to finalize cancellation",
                        )
                        captured_output.append(
                            "Subprocess did not exit after stop request; force killing to finalize cancellation",
                        )
                        process.kill()
                        stop_kill_requested = True
                if process.poll() is None and time.monotonic() >= deadline_at:
                    timed_out = True
                    timeout_msg = (
                        f"Subprocess wallclock deadline ({deadline_seconds}s) exceeded; "
                        "terminating to unblock queue dispatcher (exit_code=124)"
                    )
                    self.run_manager.append_log(task.run_id, timeout_msg)
                    captured_output.append(timeout_msg)
                    emit_lifecycle_event(
                        "lifecycle_action_failed",
                        "failed",
                        f"Lifecycle action timed out after {deadline_seconds}s",
                    )
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                if process.poll() is not None:
                    _drain_pending()
                    if stdout_buffer:
                        for residual in stdout_buffer.split("\n"):
                            if residual:
                                captured_output.append(residual)
                                self.run_manager.append_log(task.run_id, residual)
                                self._record_cluster_builder_identity(
                                    task.run_id,
                                    _cluster_builder_identity_from_log(residual),
                                )
                        stdout_buffer = ""
                    break

            return_code = process.wait()
            if timed_out and return_code == 0:
                return_code = SUBPROCESS_TIMEOUT_EXIT_CODE
            if self._stop_requested(task.run_id):
                self.run_manager.update_status(task.run_id, RUN_STATUS_CANCELLED, exit_code=130)
                run = self.run_manager.get_run(task.run_id)
                cancelled_evidence = _lifecycle_run_evidence(
                    task.action,
                    event_payload,
                    status="cancelled",
                    exit_code=130,
                    started_at=getattr(run, "started_at", None),
                    finished_at=getattr(run, "finished_at", None),
                )
                self.run_manager.update_loop_state(
                    task.run_id,
                    {
                        "run_id": task.run_id,
                        "task_id": task.run_id,
                        "queue_state": "cancelled",
                        "loop_state": "cancelled",
                        "loop_step": 1,
                        "current_goal": str(payload.get("cmd") or task.action),
                        "decision": "STOP",
                        "stop_reason": "cancelled",
                        "cancel_requested": True,
                        "steps": [],
                        "latest_evidence": cancelled_evidence,
                        "last_result_summary": cancelled_evidence.get("summary") if cancelled_evidence else None,
                    },
                )
                maybe_attach_release_validation_evidence(130)
            elif return_code == 0:
                emit_lifecycle_event("lifecycle_action_completed", "completed", "Lifecycle action completed")
                self.run_manager.update_status(task.run_id, RUN_STATUS_SUCCESS, exit_code=0)
                run = self.run_manager.get_run(task.run_id)
                completed_evidence = _lifecycle_run_evidence(
                    task.action,
                    event_payload,
                    status="completed",
                    exit_code=0,
                    started_at=getattr(run, "started_at", None),
                    finished_at=getattr(run, "finished_at", None),
                )
                self.run_manager.update_loop_state(
                    task.run_id,
                    {
                        "run_id": task.run_id,
                        "task_id": task.run_id,
                        "queue_state": "done",
                        "loop_state": "completed",
                        "loop_step": 1,
                        "current_goal": str(payload.get("cmd") or task.action),
                        "decision": "DONE",
                        "stop_reason": "done",
                        "cancel_requested": False,
                        "steps": [],
                        "latest_evidence": completed_evidence,
                        "last_result_summary": completed_evidence.get("summary") if completed_evidence else None,
                    },
                )
                maybe_attach_release_validation_evidence(0)
            else:
                self.run_manager.append_log(task.run_id, f"Process exited with code {return_code}")
                captured_output.append(f"Process exited with code {return_code}")
                emit_lifecycle_event("lifecycle_action_failed", "failed", f"Lifecycle action failed with code {return_code}")
                self.run_manager.update_status(task.run_id, RUN_STATUS_FAILED, exit_code=return_code)
                run = self.run_manager.get_run(task.run_id)
                failed_evidence = _lifecycle_run_evidence(
                    task.action,
                    event_payload,
                    status="failed",
                    exit_code=return_code,
                    started_at=getattr(run, "started_at", None),
                    finished_at=getattr(run, "finished_at", None),
                )
                self.run_manager.update_loop_state(
                    task.run_id,
                    {
                        "run_id": task.run_id,
                        "task_id": task.run_id,
                        "queue_state": "error",
                        "loop_state": "failed",
                        "loop_step": 1,
                        "current_goal": str(payload.get("cmd") or task.action),
                        "decision": "STOP",
                        "stop_reason": "manual_stop" if return_code == 130 else "verifier_rejected_terminal",
                        "last_error": f"Process exited with code {return_code}",
                        "cancel_requested": False,
                        "steps": [],
                        "latest_evidence": failed_evidence,
                        "last_result_summary": failed_evidence.get("summary") if failed_evidence else None,
                    },
                )
                maybe_attach_release_validation_evidence(return_code)
        except Exception as exc:
            self.run_manager.append_log(task.run_id, f"Error: {exc}")
            captured_output.append(f"Error: {exc}")
            emit_lifecycle_event("lifecycle_action_failed", "failed", f"Lifecycle action failed: {exc}")
            self.run_manager.update_status(task.run_id, RUN_STATUS_FAILED)
            run = self.run_manager.get_run(task.run_id)
            failed_evidence = _lifecycle_run_evidence(
                task.action,
                event_payload,
                status="failed",
                exit_code=1,
                started_at=getattr(run, "started_at", None),
                finished_at=getattr(run, "finished_at", None),
            )
            self.run_manager.update_loop_state(
                task.run_id,
                {
                    "run_id": task.run_id,
                    "task_id": task.run_id,
                    "queue_state": "error",
                    "loop_state": "failed",
                    "loop_step": 1,
                    "current_goal": str(payload.get("cmd") or task.action),
                    "decision": "STOP",
                    "stop_reason": "verifier_rejected_terminal",
                    "last_error": str(exc),
                    "cancel_requested": False,
                    "steps": [],
                    "latest_evidence": failed_evidence,
                    "last_result_summary": failed_evidence.get("summary") if failed_evidence else None,
                },
            )
            maybe_attach_release_validation_evidence(1)

    def _run_agent_task(self, task: QueueTaskRecord, payload: Dict[str, Any]) -> None:
        run_agent_for_ui(
            self.run_manager,
            task.run_id,
            task.ecosystem,
            str(payload.get("prompt") or ""),
            str(payload.get("mode") or "execute"),
            payload.get("runtime_profile"),
            payload.get("session_id"),
            payload.get("agent_id"),
            payload.get("thread_id"),
            payload.get("arena_mode"),
            payload.get("team_id"),
            payload.get("delegation_id"),
            payload.get("backlog_item_id"),
            payload.get("trigger_kind"),
            payload.get("parent_run_id"),
        )
        if self._stop_requested(task.run_id):
            run = self.run_manager.get_run(task.run_id)
            if run is None or run.status != RUN_STATUS_CANCELLED:
                self.run_manager.append_log(
                    task.run_id,
                    "Stop was requested during agent execution and was honored only after the active step finished.",
                )

    def _run_batch_task(self, task: QueueTaskRecord, payload: Dict[str, Any]) -> None:
        workspace_root = get_workspace_root()
        runtime_profile = payload.get("runtime_profile")
        batch_data = payload.get("batch_manifest") or {}
        session_id = payload.get("session_id")

        try:
            manifest = BatchManifestContract.from_dict(batch_data)
        except Exception as exc:
            self.run_manager.append_log(task.run_id, f"Invalid batch manifest: {exc}")
            self.run_manager.update_loop_state(
                task.run_id,
                {
                    "run_id": task.run_id,
                    "task_id": task.run_id,
                    "queue_state": "error",
                    "loop_state": "failed",
                    "loop_step": 0,
                    "current_goal": "bounded_batch",
                    "decision": "STOP",
                    "stop_reason": "invalid_batch_manifest",
                    "cancel_requested": False,
                    "steps": [],
                    "runtime_mode": "headless_batch",
                    "batch_state": "invalid_manifest",
                    "latest_evidence": {
                        "summary": f"Invalid batch manifest: {exc}",
                        "decision": "STOP",
                        "terminal_condition": "invalid_batch_manifest",
                        "blocker": "invalid_batch_manifest",
                        "blocker_summary": str(exc),
                    },
                    "last_result_summary": f"Invalid batch manifest: {exc}",
                },
            )
            self.run_manager.update_status(task.run_id, RUN_STATUS_FAILED, exit_code=1)
            return

        self.run_manager.update_status(task.run_id, RUN_STATUS_RUNNING)
        previous_run = self.run_manager.get_run(task.run_id)
        previous_state = (
            dict(previous_run.loop_state or {})
            if previous_run is not None and isinstance(previous_run.loop_state, dict)
            else {}
        )
        selected_choice = str(task.control.get("resume_choice") or "").strip()
        child_run_ids: List[str] = list(previous_state.get("child_run_ids") or [])
        completed_items: List[str] = list(previous_state.get("completed_items") or [])
        items = manifest.items[: manifest.max_items]
        start_index = 0
        if previous_state.get("batch_state") == "resume_requested" and selected_choice in {"retry_current", "resume_next"}:
            current_item_id = str(previous_state.get("current_item_id") or "").strip()
            paused_index = next((idx for idx, candidate in enumerate(items) if candidate.id == current_item_id), len(completed_items))
            start_index = paused_index if selected_choice == "retry_current" else min(paused_index + 1, len(items))
            self.queue_store.transition(
                task.run_id,
                QUEUE_STATUS_RUNNING,
                control_updates={
                    "resume_choice": None,
                    "resume_requested_at": None,
                    "last_resume_choice": selected_choice,
                },
            )
        base_state = {
            "run_id": task.run_id,
            "task_id": task.run_id,
            "queue_state": "running",
            "loop_state": "running",
            "loop_step": 0,
            "current_goal": manifest.batch_id,
            "decision": "CONTINUE",
            "stop_reason": None,
            "cancel_requested": False,
            "steps": [],
            "runtime_mode": manifest.runtime_mode,
            "batch_id": manifest.batch_id,
            "batch_state": "running_batch",
            "batch_cursor": start_index,
            "current_item_id": None,
            "current_child_run_id": None,
            "child_run_ids": list(child_run_ids),
            "completed_items": list(completed_items),
        }
        self.run_manager.update_loop_state(task.run_id, base_state)

        for index, item in enumerate(items[start_index:], start=start_index + 1):
            if self._stop_requested(task.run_id):
                self.run_manager.append_log(task.run_id, "Batch stop requested before next item.")
                self.run_manager.update_status(task.run_id, RUN_STATUS_CANCELLED, exit_code=130)
                return

            try:
                scope_result = scope_gate_item(item.scope, workspace_root=workspace_root)
            except Exception as exc:
                scope_payload = {
                    "allowed": False,
                    "reason": "scope_gate_invalid",
                    "normalized_scope": {},
                    "blocked_paths": [{"path": "", "reason": str(exc)}],
                }
                self.run_manager.update_loop_state(
                    task.run_id,
                    {
                        **base_state,
                        "batch_cursor": index - 1,
                        "current_item_id": item.id,
                        "batch_state": "paused_for_choice",
                        "pause_mode": "forced_choice",
                        "required_choice": "handoff",
                        "forced_choices": list(FORCED_CHOICE_OPTIONS),
                        "handoff": {
                            "item_id": item.id,
                            "attempts_used": 0,
                            "last_child_run_id": None,
                            "last_stop_reason": "scope_gate_invalid",
                            "last_blocker": "scope_gate_invalid",
                            "last_summary": str(exc),
                            "recommended_next_choice": "handoff",
                        },
                        "decision": "STOP",
                        "stop_reason": "scope_gate_invalid",
                        "child_run_ids": list(child_run_ids),
                        "completed_items": list(completed_items),
                        "latest_evidence": {
                            "summary": f"Scope gate rejected batch item {item.id}.",
                            "decision": "STOP",
                            "terminal_condition": "scope_gate_invalid",
                            "blocker": "scope_gate_invalid",
                            "blocker_summary": str(exc),
                            "pause_mode": "forced_choice",
                            "required_choice": "handoff",
                            "forced_choices": list(FORCED_CHOICE_OPTIONS),
                            "handoff": {
                                "item_id": item.id,
                                "attempts_used": 0,
                                "last_child_run_id": None,
                                "last_stop_reason": "scope_gate_invalid",
                                "last_blocker": "scope_gate_invalid",
                                "last_summary": str(exc),
                                "recommended_next_choice": "handoff",
                            },
                            "scope_gate": scope_payload,
                            "item_id": item.id,
                            "batch_id": manifest.batch_id,
                        },
                        "last_result_summary": f"Scope gate rejected batch item {item.id}.",
                    },
                )
                self.run_manager.update_status(task.run_id, RUN_STATUS_PAUSED)
                return

            if not scope_result.allowed:
                self.run_manager.update_loop_state(
                    task.run_id,
                    {
                        **base_state,
                        "batch_cursor": index - 1,
                        "current_item_id": item.id,
                        "batch_state": "paused_for_choice",
                        "pause_mode": "forced_choice",
                        "required_choice": "handoff",
                        "forced_choices": list(FORCED_CHOICE_OPTIONS),
                        "handoff": {
                            "item_id": item.id,
                            "attempts_used": 0,
                            "last_child_run_id": None,
                            "last_stop_reason": "scope_gate_rejected",
                            "last_blocker": "scope_gate_rejected",
                            "last_summary": scope_result.reason,
                            "recommended_next_choice": "handoff",
                        },
                        "decision": "STOP",
                        "stop_reason": "scope_gate_rejected",
                        "child_run_ids": list(child_run_ids),
                        "completed_items": list(completed_items),
                        "latest_evidence": {
                            "summary": f"Scope gate rejected batch item {item.id}.",
                            "decision": "STOP",
                            "terminal_condition": "scope_gate_rejected",
                            "blocker": "scope_gate_rejected",
                            "blocker_summary": scope_result.reason,
                            "pause_mode": "forced_choice",
                            "required_choice": "handoff",
                            "forced_choices": list(FORCED_CHOICE_OPTIONS),
                            "handoff": {
                                "item_id": item.id,
                                "attempts_used": 0,
                                "last_child_run_id": None,
                                "last_stop_reason": "scope_gate_rejected",
                                "last_blocker": "scope_gate_rejected",
                                "last_summary": scope_result.reason,
                                "recommended_next_choice": "handoff",
                            },
                            "scope_gate": scope_result.to_dict(),
                            "item_id": item.id,
                            "batch_id": manifest.batch_id,
                        },
                        "last_result_summary": f"Scope gate rejected batch item {item.id}.",
                    },
                )
                self.run_manager.update_status(task.run_id, RUN_STATUS_PAUSED)
                return

            child_session_id = str(uuid.uuid4())
            child_run_id = self.run_manager.create_run(
                task.ecosystem,
                "agent",
                ["amof", "agent", "..."],
                session_id=child_session_id,
                queue_payload={
                    "kind": "agent",
                    "ecosystem": task.ecosystem,
                    "prompt": item.prompt,
                    "mode": "plan-execute",
                    "runtime_profile": runtime_profile,
                    "session_id": child_session_id,
                    "parent_run_id": task.run_id,
                    "batch_id": manifest.batch_id,
                    "batch_item_id": item.id,
                    "scope": scope_result.normalized_scope,
                },
            )
            child_run_ids.append(child_run_id)
            self.run_manager.update_loop_state(
                task.run_id,
                {
                    **base_state,
                    "loop_step": index,
                    "batch_cursor": index - 1,
                    "current_item_id": item.id,
                    "current_child_run_id": child_run_id,
                    "batch_state": "child_run_running",
                    "child_run_ids": list(child_run_ids),
                    "completed_items": list(completed_items),
                    "latest_evidence": {
                        "summary": f"Running batch item {item.id}.",
                        "decision": "CONTINUE",
                        "item_id": item.id,
                        "child_run_id": child_run_id,
                        "scope_gate": scope_result.to_dict(),
                        "batch_id": manifest.batch_id,
                    },
                    "last_result_summary": f"Running batch item {item.id}.",
                },
            )
            run_agent_for_ui(
                self.run_manager,
                child_run_id,
                task.ecosystem,
                item.prompt,
                "plan-execute",
                runtime_profile,
                child_session_id,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "batch",
                task.run_id,
            )
            child_run = self.run_manager.get_run(child_run_id)
            if child_run is None:
                self.run_manager.update_loop_state(
                    task.run_id,
                    {
                        **base_state,
                        "loop_step": index,
                        "batch_cursor": index - 1,
                        "current_item_id": item.id,
                        "current_child_run_id": child_run_id,
                        "batch_state": "child_run_missing",
                        "decision": "STOP",
                        "stop_reason": "child_run_missing",
                        "child_run_ids": list(child_run_ids),
                        "completed_items": list(completed_items),
                        "latest_evidence": {
                            "summary": f"Child run {child_run_id} was not persisted.",
                            "decision": "STOP",
                            "terminal_condition": "child_run_missing",
                            "blocker": "child_run_missing",
                            "blocker_summary": f"Child run {child_run_id} was not persisted.",
                            "item_id": item.id,
                            "child_run_id": child_run_id,
                            "batch_id": manifest.batch_id,
                        },
                        "last_result_summary": f"Child run {child_run_id} was not persisted.",
                    },
                )
                self.run_manager.update_status(task.run_id, RUN_STATUS_FAILED, exit_code=1)
                return

            evaluation = evaluate_batch_item(
                item,
                attempts_used=1,
                last_child_run_id=child_run_id,
                run_payload=child_run.to_dict(),
            )
            if evaluation.decision == "continue":
                completed_items.append(item.id)
                self.run_manager.update_loop_state(
                    task.run_id,
                    {
                        **base_state,
                        "loop_step": index,
                        "batch_cursor": index,
                        "current_item_id": item.id,
                        "current_child_run_id": child_run_id,
                        "batch_state": "running_batch",
                        "child_run_ids": list(child_run_ids),
                        "completed_items": list(completed_items),
                        "latest_evidence": {
                            "summary": f"Completed batch item {item.id}.",
                            "decision": "CONTINUE",
                            "item_id": item.id,
                            "child_run_id": child_run_id,
                            "meaningful_delta": evaluation.meaningful_delta.to_dict(),
                            "batch_id": manifest.batch_id,
                        },
                        "last_result_summary": f"Completed batch item {item.id}.",
                    },
                )
                continue

            self.run_manager.update_loop_state(
                task.run_id,
                {
                    **base_state,
                    "loop_step": index,
                    "batch_cursor": index - 1,
                    "current_item_id": item.id,
                    "current_child_run_id": child_run_id,
                    "batch_state": "paused_for_choice",
                    "pause_mode": evaluation.pause_mode,
                    "required_choice": evaluation.required_choice,
                    "forced_choices": list(evaluation.forced_choices),
                    "handoff": evaluation.handoff,
                    "decision": "STOP",
                    "stop_reason": "batch_item_not_meaningful",
                    "child_run_ids": list(child_run_ids),
                    "completed_items": list(completed_items),
                    "latest_evidence": {
                        "summary": f"Batch item {item.id} stopped the bounded batch.",
                        "decision": "STOP",
                        "terminal_condition": "batch_item_not_meaningful",
                        "blocker": "batch_item_not_meaningful",
                        "blocker_summary": evaluation.meaningful_delta.reason,
                        "item_id": item.id,
                        "child_run_id": child_run_id,
                        "pause_mode": evaluation.pause_mode,
                        "meaningful_delta": evaluation.meaningful_delta.to_dict(),
                        "required_choice": evaluation.required_choice,
                        "forced_choices": list(evaluation.forced_choices),
                        "handoff": evaluation.handoff,
                        "batch_id": manifest.batch_id,
                    },
                    "last_result_summary": f"Batch item {item.id} stopped the bounded batch.",
                },
            )
            self.run_manager.update_status(task.run_id, RUN_STATUS_PAUSED)
            return

        self.run_manager.update_loop_state(
            task.run_id,
            {
                **base_state,
                "loop_step": len(completed_items),
                "batch_cursor": len(completed_items),
                "current_item_id": None,
                "current_child_run_id": child_run_ids[-1] if child_run_ids else None,
                "batch_state": "completed",
                "decision": "DONE",
                "stop_reason": "done",
                "child_run_ids": list(child_run_ids),
                "completed_items": list(completed_items),
                "latest_evidence": {
                    "summary": f"Completed {len(completed_items)}/{len(items)} batch items.",
                    "decision": "DONE",
                    "terminal_condition": "batch_completed",
                    "batch_id": manifest.batch_id,
                    "completed_items": list(completed_items),
                    "child_run_ids": list(child_run_ids),
                },
                "last_result_summary": f"Completed {len(completed_items)}/{len(items)} batch items.",
            },
        )
        self.run_manager.update_status(task.run_id, RUN_STATUS_SUCCESS, exit_code=0)

    def _stop_requested(self, run_id: str) -> bool:
        item = self.queue_store.load(run_id)
        return bool(item and item.control.get("stop_requested"))
