"""Authority Gateway: OpenAI-shaped demo ingress backed by AMOF runs."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from amof.api.dependencies import get_queue_dispatcher, get_run_manager
from amof.api.run_manager import RUN_STATUS_CANCELLED, RUN_STATUS_FAILED, RUN_STATUS_SUCCESS, RunManager
from amof.cli import get_available_ecosystems
from amof.orchestrator.llm.profile_catalog import get_profile_catalog, get_profile_selection
from amof.api.services.settings_service import get_agent_config
from amof.queue import QueueDispatcher


router = APIRouter(prefix="/gateway", tags=["gateway"])

GATEWAY_ECOSYSTEM = "gmd"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_DEDUP_WINDOW_SECONDS = 30
TERMINAL_STATUSES = {RUN_STATUS_SUCCESS, RUN_STATUS_FAILED, RUN_STATUS_CANCELLED, "stopped", "error"}
_DEDUP_LOCK = asyncio.Lock()
_DEDUP_CACHE: Dict[str, Dict[str, Any]] = {}


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    runtime_profile: Optional[str] = None
    conversation_id: Optional[str] = None
    thread_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    user: Optional[str] = None


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _configured_key() -> tuple[str, str]:
    key = str(os.environ.get("AMOF_GATEWAY_DEMO_API_KEY") or os.environ.get("AMOF_AUTHORITY_GATEWAY_API_KEY") or "").strip()
    name = str(os.environ.get("AMOF_GATEWAY_DEMO_KEY_NAME") or "demo-key").strip() or "demo-key"
    return key, name


def _require_gateway_key(authorization: Optional[str]) -> str:
    configured, key_name = _configured_key()
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="Authority Gateway is not configured: set AMOF_GATEWAY_DEMO_API_KEY.",
        )
    scheme, _, value = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or value.strip() != configured:
        raise HTTPException(status_code=401, detail="Invalid Authority Gateway API key")
    return key_name


def _model_entries() -> List[Dict[str, Any]]:
    cfg = get_agent_config()
    selection = get_profile_selection(cfg)
    catalog = get_profile_catalog()
    entries: List[Dict[str, Any]] = []
    seen: set[str] = set()
    standard_profile_id = selection.get("standard")
    standard_profile = catalog.get(standard_profile_id or "")
    if standard_profile is not None:
        entries.append(
            {
                "id": "amof",
                "object": "model",
                "owned_by": standard_profile.provider,
                "profile": standard_profile.id,
                "provider": standard_profile.provider,
                "model": standard_profile.model_id,
                "routing_reason": "stable_gateway_model:amof->standard_profile",
            }
        )
        seen.add("amof")

    for slot in ("fast", "standard", "strong"):
        profile_id = selection.get(slot)
        profile = catalog.get(profile_id or "")
        if profile is None:
            continue
        entries.append(
            {
                "id": slot,
                "object": "model",
                "owned_by": profile.provider,
                "profile": profile.id,
                "provider": profile.provider,
                "model": profile.model_id,
            }
        )
        seen.add(slot)
        seen.add(profile.id)

    for profile in catalog.values():
        if profile.id in seen:
            continue
        entries.append(
            {
                "id": profile.id,
                "object": "model",
                "owned_by": profile.provider,
                "profile": profile.id,
                "provider": profile.provider,
                "model": profile.model_id,
            }
        )
        seen.add(profile.id)
    return entries


def _status_payload() -> Dict[str, Any]:
    configured, key_name = _configured_key()
    return {
        "status": "ok",
        "client_target": "zed",
        "ecosystem": GATEWAY_ECOSYSTEM,
        "api_key_configured": bool(configured),
        "api_key_name": key_name if configured else None,
        "zed": {
            "api_url_path": "/api/v1/gateway/v1",
            "chat_completions_path": "/api/v1/gateway/v1/chat/completions",
            "models_path": "/api/v1/gateway/v1/models",
            "recommended_model": "amof",
        },
        "models": _model_entries(),
    }


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type"):
                    parts.append(f"[{item.get('type')}]")
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part).strip()
    return str(content or "").strip()


def _prompt_from_messages(messages: List[ChatMessage]) -> str:
    lines: List[str] = []
    for message in messages:
        role = str(message.role or "user").strip() or "user"
        text = _message_text(message.content)
        if text:
            lines.append(f"{role}: {text}")
    prompt = "\n\n".join(lines).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="messages must include at least one non-empty content value")
    return prompt


def _first_user_text(messages: List[ChatMessage]) -> str:
    fallback = ""
    for message in messages:
        text = _message_text(message.content)
        if text and not fallback:
            fallback = text
        role = str(message.role or "").strip().lower()
        if role == "user" and text:
            return text
    return fallback


def _stable_thread_id(prefix: str, payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{prefix}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def _external_thread_id(*, key_name: str, profile: Dict[str, str], messages: List[ChatMessage]) -> str:
    root_text = _first_user_text(messages)
    payload = {
        "client": "zed",
        "key_name": key_name,
        "ecosystem": GATEWAY_ECOSYSTEM,
        "requested_model": profile.get("requested_model"),
        "profile": profile.get("profile_id"),
        "root_user_message": root_text,
    }
    return _stable_thread_id("zed", payload)


def _first_non_empty(*candidates: tuple[str, Optional[str]]) -> tuple[Optional[str], Optional[str]]:
    for source, value in candidates:
        normalized = str(value or "").strip()
        if normalized:
            return normalized, source
    return None, None


def _metadata_thread_id(metadata: Optional[Dict[str, Any]]) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(metadata, dict):
        return None, None
    for key in ("amof_thread_id", "thread_id", "conversation_id", "zed_thread_id"):
        value = metadata.get(key)
        if isinstance(value, (str, int, float)):
            normalized = str(value).strip()
            if normalized:
                return normalized, f"body.metadata.{key}"
    return None, None


def _resolve_external_thread(
    *,
    key_name: str,
    profile: Dict[str, str],
    body: ChatCompletionRequest,
    x_amof_thread_id: Optional[str],
    x_zed_thread_id: Optional[str],
    x_openai_conversation_id: Optional[str],
) -> tuple[str, str, Optional[str]]:
    metadata_value, metadata_source = _metadata_thread_id(body.metadata)
    explicit_value, explicit_source = _first_non_empty(
        ("header.x-amof-thread-id", x_amof_thread_id),
        ("header.x-zed-thread-id", x_zed_thread_id),
        ("header.x-openai-conversation-id", x_openai_conversation_id),
        ("body.thread_id", body.thread_id),
        ("body.conversation_id", body.conversation_id),
        (metadata_source or "body.metadata", metadata_value),
    )
    if explicit_value:
        thread_id = _stable_thread_id(
            "zed-explicit",
            {
                "client": "zed",
                "key_name": key_name,
                "ecosystem": GATEWAY_ECOSYSTEM,
                "requested_model": profile.get("requested_model"),
                "profile": profile.get("profile_id"),
                "source": explicit_source,
                "external_thread_value": explicit_value,
            },
        )
        return thread_id, f"explicit:{explicit_source}", explicit_value

    return _external_thread_id(key_name=key_name, profile=profile, messages=body.messages), "fallback:first_user_message", None


def _dedup_window_seconds() -> int:
    raw = os.environ.get("AMOF_GATEWAY_DEDUP_WINDOW_SECONDS")
    try:
        return max(1, int(raw or DEFAULT_DEDUP_WINDOW_SECONDS))
    except ValueError:
        return DEFAULT_DEDUP_WINDOW_SECONDS


def _dedup_key(
    *,
    key_name: str,
    profile: Dict[str, str],
    prompt: str,
    runtime_profile: Optional[str],
    external_thread_id: str,
) -> str:
    payload = {
        "key_name": key_name,
        "ecosystem": GATEWAY_ECOSYSTEM,
        "external_thread_id": external_thread_id,
        "requested_model": profile.get("requested_model"),
        "profile": profile.get("profile_id"),
        "resolved_profile": profile.get("resolved_profile") or profile.get("profile_id"),
        "provider": profile.get("provider"),
        "model": profile.get("model"),
        "resolved_model": profile.get("resolved_model") or profile.get("model"),
        "routing_reason": profile.get("routing_reason"),
        "runtime_profile": runtime_profile or "",
        "prompt": prompt,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _dedup_get(key: str) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    window = _dedup_window_seconds()
    async with _DEDUP_LOCK:
        expired = [cache_key for cache_key, row in _DEDUP_CACHE.items() if now - float(row.get("created_at") or 0) > window]
        for cache_key in expired:
            _DEDUP_CACHE.pop(cache_key, None)
        row = _DEDUP_CACHE.get(key)
        return copy.deepcopy(row) if row is not None else None


async def _dedup_put(key: str, **values: Any) -> None:
    async with _DEDUP_LOCK:
        row = dict(_DEDUP_CACHE.get(key) or {})
        row.setdefault("created_at", time.monotonic())
        row.update(values)
        _DEDUP_CACHE[key] = row


def _resolve_profile(model: Optional[str]) -> Dict[str, str]:
    cfg = get_agent_config()
    selection = get_profile_selection(cfg)
    requested = str(model or "amof").strip() or "amof"
    routing_reason = "direct_profile"
    if requested == "amof":
        profile_id = selection.get("standard") or requested
        routing_reason = "stable_gateway_model:amof->standard_profile"
    elif requested in selection:
        profile_id = selection[requested]
        routing_reason = f"legacy_slot:{requested}->{profile_id}"
    else:
        profile_id = requested
    catalog = get_profile_catalog()
    if profile_id not in catalog:
        allowed = sorted(set(catalog) | set(selection) | {"amof"})
        raise HTTPException(
            status_code=400,
            detail=f"Unknown gateway model/profile '{requested}'. Use one of: {', '.join(allowed)}",
        )
    profile = catalog[profile_id]
    return {
        "requested_model": requested,
        "profile_id": profile.id,
        "resolved_profile": profile.id,
        "provider": profile.provider,
        "model": profile.model_id,
        "resolved_model": profile.model_id,
        "routing_reason": routing_reason,
    }


def _latest_llm_call(run: Any) -> Dict[str, Any]:
    for event in reversed(run.events or []):
        if event.type == "llm_call" and isinstance(event.payload, dict):
            return dict(event.payload)
    return {}


def _assistant_reply(run: Any) -> str:
    chunks = [event.message for event in (run.events or []) if event.type == "chat" and event.message]
    if chunks:
        return "\n".join(chunks).strip()
    snapshot = run.session_snapshot if isinstance(run.session_snapshot, dict) else {}
    message = snapshot.get("message")
    return str(message or "").strip()


async def _wait_for_run(run_id: str, run_mgr: RunManager, timeout_seconds: int) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while True:
        run = run_mgr.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Gateway run disappeared")
        if str(run.status or "").lower() in TERMINAL_STATUSES:
            return run
        if time.monotonic() >= deadline:
            run_mgr.append_event(
                run_id,
                level="warning",
                type="gateway_request_timeout",
                message="Authority Gateway request timed out while waiting for run completion",
                payload={"timeout_seconds": timeout_seconds},
            )
            raise HTTPException(status_code=504, detail=f"Gateway run timed out after {timeout_seconds}s")
        await asyncio.sleep(0.5)


def _gateway_log_payload(
    *,
    request_id: str,
    key_name: str,
    profile: Dict[str, str],
    outcome: str,
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
    llm_call: Optional[Dict[str, Any]] = None,
    external_thread_id: Optional[str] = None,
    external_thread_source: Optional[str] = None,
    external_thread_value: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "timestamp": _now_iso(),
        "request_id": request_id,
        "key_name": key_name,
        "ecosystem": GATEWAY_ECOSYSTEM,
        "requested_model": profile.get("requested_model"),
        "profile": profile.get("profile_id"),
        "resolved_profile": profile.get("resolved_profile") or profile.get("profile_id"),
        "provider": profile.get("provider"),
        "model": profile.get("model"),
        "resolved_model": profile.get("resolved_model") or profile.get("model"),
        "routing_reason": profile.get("routing_reason"),
        "outcome": outcome,
        "status": status,
        "run_id": run_id,
    }
    if external_thread_id:
        payload["external_thread_id"] = external_thread_id
    if external_thread_source:
        payload["external_thread_source"] = external_thread_source
    if external_thread_value:
        payload["external_thread_value"] = external_thread_value
    if error:
        payload["error"] = error
    if llm_call:
        payload["ial_status"] = "llm_call_recorded"
        payload["provider"] = llm_call.get("provider") or payload["provider"]
        payload["model"] = llm_call.get("model") or payload["model"]
        payload["source"] = llm_call.get("source")
    else:
        payload["ial_status"] = "pending"
    return payload


def _completion_response(request_id: str, profile: Dict[str, str], run: Any, reply: str) -> Dict[str, Any]:
    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": profile["profile_id"],
        "amof": {
            "run_id": run.run_id,
            "ecosystem": GATEWAY_ECOSYSTEM,
            "requested_model": profile.get("requested_model"),
            "resolved_profile": profile.get("resolved_profile") or profile.get("profile_id"),
            "provider": _latest_llm_call(run).get("provider") or profile["provider"],
            "model": _latest_llm_call(run).get("model") or profile["model"],
            "resolved_model": _latest_llm_call(run).get("model") or profile["model"],
            "routing_reason": profile.get("routing_reason"),
            "status": run.status,
        },
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop" if run.status == RUN_STATUS_SUCCESS else "error",
            }
        ],
    }


def _streaming_response_from_completion(response: Dict[str, Any]) -> StreamingResponse:
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") if isinstance(choice, dict) else {}
    content = str((message or {}).get("content") or "")
    finish_reason = str(choice.get("finish_reason") or "stop") if isinstance(choice, dict) else "stop"
    created = int(response.get("created") or time.time())
    model = str(response.get("model") or "standard")
    completion_id = str(response.get("id") or f"chatcmpl-{uuid.uuid4()}")
    amof = response.get("amof") if isinstance(response.get("amof"), dict) else {}

    def event(data: Dict[str, Any]) -> str:
        return f"data: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n"

    def stream():
        base = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "amof": amof,
        }
        yield event({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        if content:
            yield event({**base, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
        yield event({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/status")
def gateway_status():
    return _status_payload()


@router.get("/v1/models")
def v1_models(authorization: Optional[str] = Header(default=None)):
    _require_gateway_key(authorization)
    return {"object": "list", "data": _model_entries()}


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_amof_thread_id: Optional[str] = Header(default=None, alias="X-AMOF-Thread-ID"),
    x_zed_thread_id: Optional[str] = Header(default=None, alias="X-Zed-Thread-ID"),
    x_openai_conversation_id: Optional[str] = Header(default=None, alias="X-OpenAI-Conversation-ID"),
    dispatcher: QueueDispatcher = Depends(get_queue_dispatcher),
    run_mgr: RunManager = Depends(get_run_manager),
):
    key_name = _require_gateway_key(authorization)
    if GATEWAY_ECOSYSTEM not in get_available_ecosystems():
        raise HTTPException(status_code=503, detail=f"Gateway ecosystem '{GATEWAY_ECOSYSTEM}' is not available")

    request_id = str(uuid.uuid4())
    profile = _resolve_profile(body.model)
    prompt = _prompt_from_messages(body.messages)
    timeout_seconds = int(os.environ.get("AMOF_GATEWAY_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
    external_thread_id, external_thread_source, external_thread_value = _resolve_external_thread(
        key_name=key_name,
        profile=profile,
        body=body,
        x_amof_thread_id=x_amof_thread_id,
        x_zed_thread_id=x_zed_thread_id,
        x_openai_conversation_id=x_openai_conversation_id,
    )
    dedup_key = _dedup_key(
        key_name=key_name,
        profile=profile,
        prompt=prompt,
        runtime_profile=body.runtime_profile,
        external_thread_id=external_thread_id,
    )
    cached = await _dedup_get(dedup_key)
    if cached is not None:
        cached_response = cached.get("response")
        if isinstance(cached_response, dict):
            response = copy.deepcopy(cached_response)
            response["id"] = f"chatcmpl-{request_id}"
            response.setdefault("amof", {})["deduplicated"] = True
            response["amof"]["duplicate_of"] = cached.get("request_id")
            response["amof"]["external_thread_id"] = cached.get("external_thread_id") or external_thread_id
            response["amof"]["external_thread_source"] = cached.get("external_thread_source") or external_thread_source
            if body.stream:
                return _streaming_response_from_completion(response)
            return response
        cached_run_id = str(cached.get("run_id") or "")
        if cached_run_id:
            run = await _wait_for_run(cached_run_id, run_mgr, timeout_seconds)
            reply = _assistant_reply(run)
            response = _completion_response(request_id, profile, run, reply)
            response["amof"]["session_id"] = cached.get("session_id")
            response["amof"]["deduplicated"] = True
            response["amof"]["duplicate_of"] = cached.get("request_id")
            response["amof"]["external_thread_id"] = cached.get("external_thread_id") or external_thread_id
            response["amof"]["external_thread_source"] = cached.get("external_thread_source") or external_thread_source
            if body.stream:
                return _streaming_response_from_completion(response)
            return response

    run_id, session_id = dispatcher.enqueue_agent(
        GATEWAY_ECOSYSTEM,
        prompt=prompt,
        mode="execute",
        runtime_profile=body.runtime_profile,
        session_id=external_thread_id,
        thread_id=external_thread_id,
        control_metadata={
            "gateway": {
                "request_id": request_id,
                "key_name": key_name,
                "external_thread_id": external_thread_id,
                "external_thread_source": external_thread_source,
                "external_thread_value": external_thread_value,
                "requested_model": profile["requested_model"],
                "resolved_profile": profile["resolved_profile"],
                "resolved_model": profile["resolved_model"],
                "routing_reason": profile["routing_reason"],
                "profile": profile["profile_id"],
                "provider": profile["provider"],
                "model": profile["model"],
                "client_host": request.client.host if request.client else None,
            }
        },
    )
    await _dedup_put(
        dedup_key,
        request_id=request_id,
        run_id=run_id,
        session_id=session_id,
        external_thread_id=external_thread_id,
        external_thread_source=external_thread_source,
        profile=profile,
    )
    run_mgr.append_event(
        run_id,
        level="info",
        type="gateway_request",
        message=f"Authority Gateway request queued for {profile['profile_id']}",
        payload=_gateway_log_payload(
            request_id=request_id,
            key_name=key_name,
            profile=profile,
            outcome="queued",
            run_id=run_id,
            status="queued",
            external_thread_id=external_thread_id,
            external_thread_source=external_thread_source,
            external_thread_value=external_thread_value,
        ),
    )

    async def finish() -> tuple[Any, str]:
        try:
            run = await _wait_for_run(run_id, run_mgr, timeout_seconds)
            reply = _assistant_reply(run)
            outcome = "success" if run.status == RUN_STATUS_SUCCESS else "failed"
            run_mgr.append_event(
                run_id,
                level="info" if outcome == "success" else "error",
                type="gateway_request",
                message=f"Authority Gateway request {outcome}",
                payload=_gateway_log_payload(
                    request_id=request_id,
                    key_name=key_name,
                    profile=profile,
                    outcome=outcome,
                    run_id=run_id,
                    status=run.status,
                    llm_call=_latest_llm_call(run),
                    external_thread_id=external_thread_id,
                    external_thread_source=external_thread_source,
                    external_thread_value=external_thread_value,
                ),
            )
            return run, reply
        except Exception as exc:
            run_mgr.append_event(
                run_id,
                level="error",
                type="gateway_request",
                message="Authority Gateway request failed",
                payload=_gateway_log_payload(
                    request_id=request_id,
                    key_name=key_name,
                    profile=profile,
                    outcome="failed",
                    run_id=run_id,
                    status="error",
                    error=str(exc),
                    external_thread_id=external_thread_id,
                    external_thread_source=external_thread_source,
                    external_thread_value=external_thread_value,
                ),
            )
            raise

    run, reply = await finish()
    response = _completion_response(request_id, profile, run, reply)
    response["amof"]["session_id"] = session_id
    response["amof"]["external_thread_id"] = external_thread_id
    response["amof"]["external_thread_source"] = external_thread_source
    await _dedup_put(dedup_key, response=response)
    if body.stream:
        return _streaming_response_from_completion(response)
    return response


@router.get("/requests")
def list_gateway_requests(limit: int = 50, run_mgr: RunManager = Depends(get_run_manager)):
    rows: List[Dict[str, Any]] = []
    for run in run_mgr.list_runs(ecosystem=GATEWAY_ECOSYSTEM, limit=200, include_archived=True):
        for event in run.events or []:
            if event.type != "gateway_request" or not isinstance(event.payload, dict):
                continue
            row = dict(event.payload)
            row.setdefault("run_id", run.run_id)
            row.setdefault("status", run.status)
            rows.append(row)
    rows.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    return {"requests": rows[: max(1, min(limit, 200))]}
