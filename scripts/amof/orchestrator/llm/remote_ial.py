"""Remote private IAL gateway client for local AMOF calls."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from .base import (
    LLMClient,
    LLMResponse,
    PROVIDER_FAILURE_AUTH,
    PROVIDER_FAILURE_NETWORK,
    ProviderError,
    ToolCallRequest,
    Usage,
    classify_provider_status,
    get_context_window,
)

DEFAULT_REMOTE_IAL_TIMEOUT_SECONDS = 90.0


def _normalize_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "RemoteIALClient requires a valid http(s) base_url for the remote IAL gateway."
        )
    return normalized


def _parse_json_response(response: requests.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


class RemoteIALClient(LLMClient):
    """Route chat requests through a private remote IAL gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_REMOTE_IAL_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._model = str(model or "").strip()
        self._api_key = str(api_key or "").strip()
        self._timeout = float(timeout)
        if self._timeout <= 0:
            raise ValueError("RemoteIALClient timeout must be a positive number")
        self._provider = "remote-ial"

    @property
    def provider(self) -> str:
        return self._provider

    def model_name(self) -> str:
        if self._model:
            return self._model
        host = urlparse(self._base_url).netloc or "remote-ial"
        return f"remote-ial/{host}"

    def context_window(self) -> int:
        if self._model:
            return get_context_window(self._model)
        return 200_000

    def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        payload: Dict[str, Any] = {
            "system": system,
            "messages": messages,
            "tools": tools or [],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self._model:
            payload["model"] = self._model

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            response = requests.post(
                f"{self._base_url}/v1/ial/chat",
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise ProviderError(
                provider=self._provider,
                message=f"Remote IAL network failure: {exc}",
                failure_class=PROVIDER_FAILURE_NETWORK,
                original=exc,
            ) from exc

        body = _parse_json_response(response)
        if response.status_code >= 400:
            detail = body.get("detail")
            if not isinstance(detail, dict):
                detail = body
            message = str(
                detail.get("message")
                or detail.get("detail")
                or response.text.strip()
                or f"remote IAL request failed with status {response.status_code}"
            )
            detail_code = str(detail.get("code") or "").strip()
            upstream_provider = str(
                detail.get("upstream_provider") or detail.get("provider") or ""
            ).strip() or None
            upstream_model = str(
                detail.get("upstream_model") or detail.get("model") or self._model or ""
            ).strip() or None
            request_id = str(detail.get("request_id") or "").strip() or None
            policy_decision = detail.get("policy_decision") if isinstance(detail.get("policy_decision"), dict) else None
            input_hash = str(detail.get("input_hash") or "").strip() or None
            output_hash = str(detail.get("output_hash") or "").strip() or None
            if response.status_code in {401, 403} and detail_code in {
                "ial_auth_invalid",
                "ial_auth_unconfigured",
            }:
                raise ProviderError(
                    provider=self._provider,
                    message=message,
                    status_code=response.status_code,
                    failure_class=PROVIDER_FAILURE_AUTH,
                    request_id=request_id,
                    policy_decision=policy_decision,
                    input_hash=input_hash,
                    output_hash=output_hash,
                )
            failure_class = str(detail.get("failure_class") or "").strip() or None
            raise ProviderError(
                provider=self._provider,
                message=message,
                status_code=int(detail.get("status_code") or response.status_code),
                failure_class=failure_class
                or classify_provider_status(
                    int(detail.get("status_code") or response.status_code),
                    "",
                ),
                upstream_provider=upstream_provider,
                upstream_model=upstream_model,
                request_id=request_id,
                policy_decision=policy_decision,
                input_hash=input_hash,
                output_hash=output_hash,
            )

        tool_calls: list[ToolCallRequest] = []
        raw_tool_calls = body.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for index, item in enumerate(raw_tool_calls, start=1):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                arguments = item.get("arguments")
                if not name or not isinstance(arguments, dict):
                    continue
                tool_calls.append(
                    ToolCallRequest(
                        id=str(item.get("id") or f"remote-tool-{index}"),
                        name=name,
                        arguments=arguments,
                    )
                )

        tokens = body.get("tokens") if isinstance(body.get("tokens"), dict) else {}
        input_tokens = int(tokens.get("input") or 0)
        output_tokens = int(tokens.get("output") or 0)
        upstream_provider = str(
            body.get("upstream_provider") or body.get("provider") or ""
        ).strip() or None
        upstream_model = str(
            body.get("upstream_model") or body.get("model") or self._model or ""
        ).strip() or None
        resolved_model = upstream_model or "remote-ial"

        usage = Usage(
            model=resolved_model,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            latency_ms=int(body.get("latency_ms") or 0),
            estimated_cost=float(body.get("estimated_cost") or 0.0),
            context_window=get_context_window(resolved_model),
            provider=self._provider,
            upstream_provider=upstream_provider,
            upstream_model=upstream_model,
            request_id=str(body.get("request_id") or "").strip() or None,
            policy_decision=body.get("policy_decision")
            if isinstance(body.get("policy_decision"), dict)
            else None,
            input_hash=str(body.get("input_hash") or "").strip() or None,
            output_hash=str(body.get("output_hash") or "").strip() or None,
        )
        return LLMResponse(
            text=body.get("text") if isinstance(body.get("text"), str) else None,
            tool_calls=tool_calls or None,
            usage=usage,
            stop_reason=str(body.get("stop_reason") or "").strip() or None,
            raw=body,
            thinking=body.get("thinking") if isinstance(body.get("thinking"), str) else None,
        )

    def chat_structured(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        response_model: Any,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> Any:
        raise NotImplementedError(
            "RemoteIALClient does not yet support native structured output calls."
        )
