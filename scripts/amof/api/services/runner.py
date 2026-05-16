import os
import subprocess
import asyncio
from typing import Any, Dict, List, Optional
from pathlib import Path
from fastapi import BackgroundTasks, HTTPException
from amof.cli import get_available_ecosystems
from amof.api.command_builder import get_workspace_root
from amof.queue.store import serial_queue_slot
from amof.api.run_manager import (
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCESS,
    RUN_STATUS_FAILED,
)

def run_subprocess_task(
    run_manager,
    run_id: str,
    cmd: List[str],
    cwd: Optional[str] = None,
    event_payload: Optional[Dict[str, Any]] = None,
) -> None:
    captured_output: List[str] = []

    def maybe_attach_release_validation_evidence(return_code: int) -> None:
        run = run_manager.get_run(run_id)
        if run is None or str(getattr(run, "action", "") or "").strip() != "release/validate":
            return
        from amof.api.routers.release import _persist_validation_result_evidence

        _persist_validation_result_evidence(run_manager, run_id, return_code, "\n".join(captured_output))

    def emit_lifecycle_event(event_type: str, status: str, message: str) -> None:
        if not isinstance(event_payload, dict):
            return
        action = str(event_payload.get("action") or event_payload.get("lifecycle_action") or "").strip()
        if not action:
            return
        run_manager.append_event(
            run_id,
            level="info" if status not in {"failed", "error"} else "error",
            type=event_type,
            message=message,
            payload={**event_payload, "status": status, "action": action, "lifecycle_action": action},
        )

    with serial_queue_slot():
        run_manager.update_status(run_id, RUN_STATUS_RUNNING)
        emit_lifecycle_event("lifecycle_action_started", "started", "Lifecycle action started")
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cwd or os.getcwd(),
            )
            for line in iter(process.stdout.readline, ""):
                if line:
                    normalized_line = line.rstrip("\n")
                    captured_output.append(normalized_line)
                    run_manager.append_log(run_id, normalized_line)
            process.stdout.close()
            return_code = process.wait()
            if return_code == 0:
                emit_lifecycle_event("lifecycle_action_completed", "completed", "Lifecycle action completed")
                run_manager.update_status(run_id, RUN_STATUS_SUCCESS, exit_code=0)
                maybe_attach_release_validation_evidence(0)
            else:
                run_manager.append_log(run_id, f"Process exited with code {return_code}")
                captured_output.append(f"Process exited with code {return_code}")
                emit_lifecycle_event("lifecycle_action_failed", "failed", f"Lifecycle action failed with code {return_code}")
                run_manager.update_status(run_id, RUN_STATUS_FAILED, exit_code=return_code)
                maybe_attach_release_validation_evidence(return_code)
        except Exception as e:
            run_manager.append_log(run_id, f"Error: {str(e)}")
            captured_output.append(f"Error: {str(e)}")
            emit_lifecycle_event("lifecycle_action_failed", "failed", f"Lifecycle action failed: {str(e)}")
            run_manager.update_status(run_id, RUN_STATUS_FAILED)
            maybe_attach_release_validation_evidence(1)

def execute_action(
    run_manager,
    ecosystem: str,
    action: str,
    build_fn,
    background_tasks: BackgroundTasks,
    *build_args,
    **build_kwargs,
):
    ecosystems = get_available_ecosystems()
    if ecosystem not in ecosystems:
        raise HTTPException(status_code=404, detail=f"Ecosystem {ecosystem} not found")
    root = get_workspace_root()
    request_id = build_kwargs.pop("request_id", None)
    cmd, cwd = build_fn(root, ecosystem, *build_args, **build_kwargs)
    run_id = run_manager.create_run(
        ecosystem,
        action,
        cmd,
        queue_payload={
            "kind": "subprocess",
            "cmd": [str(part) for part in cmd],
            "cwd": str(cwd),
        },
    )
    if request_id:
        run_manager.append_event(
            run_id,
            level="info",
            type="action_request",
            message=f"Accepted {action} request {request_id}",
            payload={"request_id": request_id, "action": action, "ecosystem": ecosystem},
        )
    return {"task_id": run_id, "run_id": run_id, "status": RUN_STATUS_QUEUED, "request_id": request_id}
