import asyncio
import json as _json
from pathlib import Path
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse

from amof.api.dependencies import get_queue_dispatcher, get_run_manager, require_step_up_user
from amof.api.command_builder import get_workspace_root
from amof.api.models.action import RunStatusResponse
from amof.api.run_manager import (
    RunManager,
    RunRecord,
    RUN_STATUS_SUCCESS,
    RUN_STATUS_FAILED,
)
from amof.queue import QueueDispatcher, QueueTransitionError

router = APIRouter(prefix="/runs", tags=["runs"])


def _run_status_payload(run: RunRecord) -> Dict[str, Any]:
    last_event = run.events[-1] if run.events else None
    return RunStatusResponse(
        run_id=run.run_id,
        ecosystem=run.ecosystem,
        action=run.action,
        status=run.status,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        exit_code=run.exit_code,
        terminal=run.status in (RUN_STATUS_SUCCESS, RUN_STATUS_FAILED, "cancelled", "stopped", "error"),
        last_event_at=last_event.timestamp if last_event else None,
        last_event_type=last_event.type if last_event else None,
        last_message=last_event.message if last_event else None,
    ).model_dump()


def _ensure_stoppable_lifecycle_run(run: RunRecord) -> None:
    action = str(run.action or "").strip()
    if not action.startswith("release/lifecycle/"):
        raise HTTPException(status_code=409, detail="Stop is only supported for release lifecycle runs")
    if run.status in (RUN_STATUS_SUCCESS, RUN_STATUS_FAILED, "cancelled", "stopped", "error"):
        raise HTTPException(status_code=409, detail=f"Run is already terminal ({run.status})")

@router.get("")
def list_runs(
    ecosystem: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    run_mgr: RunManager = Depends(get_run_manager),
):
    runs = run_mgr.list_runs(ecosystem=ecosystem, status=status, limit=limit)
    return {"runs": [r.to_dict() for r in runs]}

@router.get("/{run_id}")
def get_run_details(run_id: str, run_mgr: RunManager = Depends(get_run_manager)):
    run = run_mgr.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run.to_dict()


@router.get("/{run_id}/status", response_model=RunStatusResponse)
def get_run_status(run_id: str, run_mgr: RunManager = Depends(get_run_manager)):
    run = run_mgr.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_status_payload(run)


@router.post("/{run_id}/control", dependencies=[Depends(require_step_up_user)])
def control_run(
    run_id: str,
    body: Dict[str, Any],
    run_mgr: RunManager = Depends(get_run_manager),
    dispatcher: QueueDispatcher = Depends(get_queue_dispatcher),
):
    run = run_mgr.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    action = str(body.get("action") or "").strip().lower()
    if action != "stop":
        raise HTTPException(status_code=400, detail="Unsupported control action")
    _ensure_stoppable_lifecycle_run(run)
    try:
        queue_item = dispatcher.stop_task(
            run_id,
            control_metadata={
                "requested_via": "api",
                "control_action": "stop",
            },
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QueueTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    refreshed = run_mgr.get_run(run_id)
    return {
        "run_id": run_id,
        "action": "stop",
        "status": refreshed.status if refreshed else run.status,
        "queue_status": queue_item.status,
        "stop_requested": bool(queue_item.control.get("stop_requested")),
    }

async def log_streamer(run_id: str, run_mgr: RunManager, request: Request):
    idx = 0
    while True:
        if await request.is_disconnected():
            break
            
        run = run_mgr.get_run(run_id)
        if not run:
            yield "data: Run not found\n\n"
            break
        # Process new events
        while idx < len(run.events):
            event = run.events[idx]
            
            payload = {
                "type": event.type,
                "content": event.message,
                "level": event.level,
                "timestamp": event.timestamp,
            }
            if event.payload:
                payload["payload"] = event.payload
            yield f"data: {_json.dumps(payload)}\n\n"
                    
            idx += 1
            
        if run.status in (RUN_STATUS_SUCCESS, RUN_STATUS_FAILED, "cancelled", "stopped", "error"):
            while idx < len(run.events):
                event = run.events[idx]
                payload = {
                    "type": event.type,
                    "content": event.message,
                    "level": event.level,
                    "timestamp": event.timestamp,
                }
                if event.payload:
                    payload["payload"] = event.payload
                yield f"data: {_json.dumps(payload)}\n\n"
                idx += 1
                
            yield "data: [DONE]\n\n"
            break
        await asyncio.sleep(0.5)

@router.get("/{run_id}/stream")
async def stream_run_logs(run_id: str, request: Request, run_mgr: RunManager = Depends(get_run_manager)):
    if not run_mgr.get_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return StreamingResponse(log_streamer(run_id, run_mgr, request), media_type="text/event-stream")

@router.get("/{run_id}/session")
def get_run_session(run_id: str, run_mgr: RunManager = Depends(get_run_manager)):
    """Return session telemetry + messages for a completed run."""
    run = run_mgr.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    sid = run.session_id
    if not sid:
        raise HTTPException(status_code=404, detail="No session linked to this run")

    root = get_workspace_root()
    session_dir = root / ".amof" / "sessions" / sid
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail=f"Session directory not found: {sid}")

    telemetry: Dict[str, Any] = {}
    telemetry_path = session_dir / "telemetry.json"
    if telemetry_path.exists():
        telemetry = _json.loads(telemetry_path.read_text(encoding="utf-8"))

    messages = []
    messages_path = session_dir / "messages.jsonl"
    if messages_path.exists():
        for line in messages_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    messages.append(_json.loads(line))
                except _json.JSONDecodeError:
                    pass

    # Surface the run's session_snapshot fields so existing UI reads of
    # `session.session_state` / `session.stop_reason` / `session.iterations_used`
    # stop returning null for kind=agent runs. The snapshot is already written
    # by agent_runner after agent.run completes (see C4); without this the UI
    # falls back to "n/a" even though backend truth exists.
    snapshot = run.session_snapshot if isinstance(run.session_snapshot, dict) else {}
    return {
        "session_id": sid,
        "session_state": snapshot.get("session_state"),
        "session_message": snapshot.get("message"),
        "stop_reason": snapshot.get("stop_reason"),
        "iterations_used": snapshot.get("iterations_used"),
        "max_iterations": snapshot.get("max_iterations"),
        "telemetry": telemetry,
        "messages": messages,
    }
