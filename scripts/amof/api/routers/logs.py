"""Global orchestrator log stream: SSE of recent run events across all runs."""

import asyncio
import json
from typing import Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from amof.api.dependencies import get_run_manager
from amof.api.run_manager import RunManager

router = APIRouter(prefix="/logs", tags=["logs"])


async def global_log_streamer(run_mgr: RunManager, request: Request):
    """Stream events from all recent runs as a single timeline (high-level orchestrator overview)."""
    last_sent: Dict[str, int] = {}  # run_id -> next event index to send
    while True:
        if await request.is_disconnected():
            break
        runs = run_mgr.list_runs(limit=30)
        for run in runs:
            run_id = run.run_id
            start = last_sent.get(run_id, 0)
            for i in range(start, len(run.events)):
                event = run.events[i]
                data = {
                    "type": event.type,
                    "content": event.message,
                    "level": event.level,
                    "timestamp": event.timestamp,
                    "run_id": run_id,
                    "ecosystem": run.ecosystem,
                }
                if event.payload:
                    data["payload"] = event.payload
                yield f"data: {json.dumps(data)}\n\n"
            last_sent[run_id] = len(run.events)
        await asyncio.sleep(1.0)


@router.get("/stream")
async def stream_global_logs(request: Request, run_mgr: RunManager = Depends(get_run_manager)):
    """SSE endpoint: global feed of log/chat events from all recent runs (orchestrator overview)."""
    return StreamingResponse(
        global_log_streamer(run_mgr, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
