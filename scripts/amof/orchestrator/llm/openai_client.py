"""OpenAI LLM backend.

Implements the LLMClient interface for OpenAI models (GPT-5.x Codex, GPT-4o, etc.).
Requires: pip install openai

Handles:
- Message format conversion (internal format ↔ OpenAI chat completion format)
- Tool calling (OpenAI uses JSON-string arguments, Anthropic uses dict)
- Retry with exponential backoff for transient errors
- Prompt caching support (where available)
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

from .base import (
    LLMClient,
    LLMResponse,
    ProviderError,
    StructuredLLMResponse,
    ToolCallRequest,
    Usage,
    canonical_model_name,
    classify_provider_status,
    estimate_cost_details,
    get_context_window,
)

logger = logging.getLogger(__name__)

# Default model
DEFAULT_MODEL = "gpt-4o"

# Retry configuration
MAX_RETRIES = 3
BASE_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 30.0

# HTTP status codes that warrant retry
RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


class OpenAIClient(LLMClient):
    """OpenAI API client implementing the AMOF LLMClient interface.

    Supports GPT-4o, GPT-4o-mini, GPT-5.x Codex, o1, o3-mini, etc.
    Automatically handles message format conversion between AMOF's internal
    format and OpenAI's chat completion format.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        max_retries: int = MAX_RETRIES,
        reasoning_effort: Optional[str] = None,
    ):
        """Initialize the OpenAI client.

        Args:
            api_key: OpenAI API key (or OPENAI_API_KEY env var).
            model: Model identifier (default: gpt-4o).
            max_retries: Maximum retry attempts for transient errors.
            reasoning_effort: For o-series models: "low", "medium", "high".
        """
        self._raw_model = model or os.environ.get("AMOF_OPENAI_MODEL", DEFAULT_MODEL)
        self._provider = "openrouter" if self._raw_model.startswith("openrouter/") else "openai"
        self._model = canonical_model_name(self._raw_model)
        key_env = "OPENROUTER_API_KEY" if self._provider == "openrouter" else "OPENAI_API_KEY"
        self._api_key = api_key or os.environ.get(key_env, "")
        self._base_url = base_url or (
            os.environ.get("OPENROUTER_OPENAI_BASE_URL")
            or os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
            if self._provider == "openrouter"
            else None
        )
        self._client = None
        self._max_retries = max_retries
        self._reasoning_effort = reasoning_effort

        if not self._api_key:
            if self._provider == "openrouter":
                raise ValueError(
                    "OPENROUTER_API_KEY not set. Add it to .env or pass api_key parameter."
                )
            raise ValueError(
                "OPENAI_API_KEY not set. Add it to .env or pass api_key parameter."
            )

    def _get_client(self) -> Any:
        """Lazy-init the OpenAI SDK client."""
        if self._client is None:
            try:
                import openai
            except ImportError:
                raise ImportError(
                    "openai package not installed. Run: pip install openai"
                )

            kwargs: Dict[str, Any] = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send conversation to OpenAI API.

        Converts from AMOF's internal message format to OpenAI's format
        and converts tool definitions from Anthropic schema to OpenAI function schema.
        """
        client = self._get_client()
        start = time.monotonic()

        # Convert messages to OpenAI format (system message first)
        api_messages = self._convert_messages(system, messages)

        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "temperature": temperature,
        }

        # max_tokens key differs for o-series models
        if self._model.startswith("o1") or self._model.startswith("o3"):
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens

        # Reasoning effort for o-series models
        if self._reasoning_effort and (
            self._model.startswith("o1") or self._model.startswith("o3")
        ):
            kwargs["reasoning_effort"] = self._reasoning_effort

        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        response = self._call_with_retry(client, kwargs)

        latency_ms = int((time.monotonic() - start) * 1000)

        return self._parse_response(response, latency_ms)

    def chat_structured(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        response_model: Any,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> StructuredLLMResponse:
        """Use OpenAI native structured output parsing (Pydantic response_format)."""
        client = self._get_client()
        start = time.monotonic()
        api_messages = self._convert_messages(system, messages)

        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "temperature": temperature,
            "response_format": response_model,
        }
        if self._model.startswith("o1") or self._model.startswith("o3"):
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens

        if self._reasoning_effort and (
            self._model.startswith("o1") or self._model.startswith("o3")
        ):
            kwargs["reasoning_effort"] = self._reasoning_effort

        response = self._call_parse_with_retry(client, kwargs)
        latency_ms = int((time.monotonic() - start) * 1000)

        choice = response.choices[0] if response.choices else None
        if not choice or not choice.message:
            raise ValueError(
                f"{self._provider_label()} structured output returned no choices."
            )

        parsed = getattr(choice.message, "parsed", None)
        if parsed is None:
            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise ValueError(
                    f"{self._provider_label()} structured output refusal: {refusal}"
                )
            raise ValueError(
                f"{self._provider_label()} structured output did not include parsed content."
            )

        usage = self._build_usage(response, latency_ms)
        return StructuredLLMResponse(
            parsed=parsed,
            usage=usage,
            stop_reason=choice.finish_reason,
            raw=response,
            text=choice.message.content,
        )

    @property
    def provider(self) -> str:
        """Truthful upstream provider label (``openai`` or ``openrouter``)."""
        return self._provider

    def _provider_label(self) -> str:
        """Truthful human-readable provider label for log/error messages.

        ``self._provider`` is set at construction time by the concrete
        subclass / model-id sniff in :meth:`__init__`. Subclasses that
        speak the OpenAI Chat Completions wire protocol but represent a
        different upstream (``LocalOpenAICompatibleClient`` for
        ``local`` / ``runpod``) inherit this method, so error and log
        text never says ``OpenAI`` for a non-OpenAI provider.
        """
        labels = {
            "openrouter": "OpenRouter",
            "openai": "OpenAI",
            "local": "Local",
            "runpod": "Runpod",
        }
        provider = self._provider or "provider"
        return labels.get(provider, provider.title())

    def _wrap_provider_error(self, exc: BaseException) -> ProviderError:
        status_code = getattr(exc, "status_code", None)
        failure_class = classify_provider_status(status_code, type(exc).__name__)
        return ProviderError(
            provider=self._provider,
            message=str(exc),
            status_code=status_code,
            failure_class=failure_class,
            original=exc,
        )

    def _call_with_retry(self, client: Any, kwargs: Dict[str, Any]) -> Any:
        """Call the API with exponential backoff retry for transient errors."""
        last_error: Optional[BaseException] = None
        retry_count = 0

        for attempt in range(self._max_retries + 1):
            try:
                response = client.chat.completions.create(**kwargs)
                response._amof_retry_count = retry_count
                return response
            except Exception as e:
                last_error = e
                status_code = getattr(e, "status_code", None)
                is_retryable = status_code in RETRYABLE_STATUS_CODES if status_code else False

                error_name = type(e).__name__
                if error_name in ("APIConnectionError", "Timeout", "InternalServerError"):
                    is_retryable = True

                if not is_retryable or attempt == self._max_retries:
                    logger.error(
                        "%s API error (attempt %d/%d, non-retryable): %s",
                        self._provider_label(),
                        attempt + 1,
                        self._max_retries + 1,
                        e,
                    )
                    raise self._wrap_provider_error(e) from e

                retry_count += 1
                delay = min(BASE_RETRY_DELAY * (2 ** attempt), MAX_RETRY_DELAY)
                jitter = delay * 0.25 * random.random()
                wait = delay + jitter

                logger.warning(
                    "%s API error (attempt %d/%d), retrying in %.1fs: %s",
                    self._provider_label(),
                    attempt + 1,
                    self._max_retries + 1,
                    wait,
                    e,
                )
                time.sleep(wait)

        raise self._wrap_provider_error(last_error) from last_error  # type: ignore[arg-type]

    def _call_parse_with_retry(self, client: Any, kwargs: Dict[str, Any]) -> Any:
        """Call beta chat.parse endpoint with retry."""
        last_error: Optional[BaseException] = None
        retry_count = 0

        for attempt in range(self._max_retries + 1):
            try:
                response = client.beta.chat.completions.parse(**kwargs)
                response._amof_retry_count = retry_count
                return response
            except Exception as e:
                last_error = e
                status_code = getattr(e, "status_code", None)
                is_retryable = status_code in RETRYABLE_STATUS_CODES if status_code else False
                error_name = type(e).__name__
                if error_name in ("APIConnectionError", "Timeout", "InternalServerError"):
                    is_retryable = True

                if not is_retryable or attempt == self._max_retries:
                    logger.error(
                        "%s parse API error (attempt %d/%d, non-retryable): %s",
                        self._provider_label(),
                        attempt + 1,
                        self._max_retries + 1,
                        e,
                    )
                    raise self._wrap_provider_error(e) from e

                retry_count += 1
                delay = min(BASE_RETRY_DELAY * (2 ** attempt), MAX_RETRY_DELAY)
                jitter = delay * 0.25 * random.random()
                wait = delay + jitter
                logger.warning(
                    "%s parse API error (attempt %d/%d), retrying in %.1fs: %s",
                    self._provider_label(),
                    attempt + 1,
                    self._max_retries + 1,
                    wait,
                    e,
                )
                time.sleep(wait)

        raise self._wrap_provider_error(last_error) from last_error  # type: ignore[arg-type]

    def model_name(self) -> str:
        return self._raw_model

    def context_window(self) -> int:
        return get_context_window(self._raw_model)

    def _convert_messages(
        self, system: str, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Convert AMOF internal messages to OpenAI chat format.

        AMOF format:
            {"role": "user", "content": "text"}
            {"role": "assistant", "content": "text", "tool_calls": [...]}
            {"role": "tool", "tool_call_id": "...", "content": "result text"}

        OpenAI format:
            {"role": "system", "content": "..."}
            {"role": "user", "content": "text"}
            {"role": "assistant", "content": "text", "tool_calls": [{type: "function", ...}]}
            {"role": "tool", "tool_call_id": "...", "content": "result text"}
        """
        api_messages: List[Dict[str, Any]] = []

        # System message first
        if system:
            api_messages.append({"role": "system", "content": system})

        for msg in messages:
            role = msg["role"]

            if role == "user":
                api_messages.append({"role": "user", "content": msg["content"]})

            elif role == "assistant":
                assistant_msg: Dict[str, Any] = {"role": "assistant"}
                tool_calls = msg.get("tool_calls")

                content = msg.get("content")
                if content:
                    assistant_msg["content"] = content
                elif tool_calls:
                    assistant_msg["content"] = ""
                else:
                    continue

                if tool_calls:
                    openai_tool_calls = []
                    for tc in tool_calls:
                        openai_tool_calls.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"]),
                            },
                        })
                    assistant_msg["tool_calls"] = openai_tool_calls

                api_messages.append(assistant_msg)

            elif role == "tool":
                # Tool results
                results = msg.get("results", [])
                if not results:
                    results = [{
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }]

                for r in results:
                    api_messages.append({
                        "role": "tool",
                        "tool_call_id": r.get("tool_call_id", r.get("id", "")),
                        "content": r.get("content", r.get("output", "")),
                    })

        return api_messages

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Anthropic-format tool definitions to OpenAI function format.

        Anthropic:
            {"name": "Read", "description": "...", "input_schema": {...}}

        OpenAI:
            {"type": "function", "function": {"name": "Read", "description": "...", "parameters": {...}}}
        """
        openai_tools = []
        for tool in tools:
            func_def: Dict[str, Any] = {
                "name": tool["name"],
                "description": tool.get("description", ""),
            }
            # input_schema → parameters
            schema = tool.get("input_schema", {})
            if schema:
                func_def["parameters"] = schema

            openai_tools.append({
                "type": "function",
                "function": func_def,
            })
        return openai_tools

    def _parse_response(self, response: Any, latency_ms: int) -> LLMResponse:
        """Parse OpenAI API response into AMOF LLMResponse format."""
        choice = response.choices[0] if response.choices else None
        if not choice:
            return LLMResponse(
                text=None, tool_calls=None, usage=None,
                stop_reason="no_choices", raw=response,
            )

        message = choice.message

        # Extract text
        text = message.content

        # Extract tool calls
        tool_calls = None
        if message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                # OpenAI sends arguments as a JSON string
                try:
                    arguments = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {"raw": tc.function.arguments}

                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=arguments,
                ))

        usage = self._build_usage(response, latency_ms)

        # Map OpenAI stop reasons to our format
        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "content_filter",
        }
        stop_reason = stop_reason_map.get(choice.finish_reason, choice.finish_reason)

        llm_response = LLMResponse(
            text=text,
            tool_calls=tool_calls if tool_calls else None,
            usage=usage,
            stop_reason=stop_reason,
            raw=response,
        )

        # Attach retry/cache metrics for telemetry (OpenRouter provides cache_write_tokens, cached_tokens)
        if hasattr(response, "_amof_retry_count"):
            llm_response._retry_count = response._amof_retry_count
        cache_creation = 0
        cache_read = 0
        if response.usage and hasattr(response.usage, "prompt_tokens_details"):
            details = response.usage.prompt_tokens_details
            if details:
                if hasattr(details, "cache_write_tokens"):
                    cache_creation = details.cache_write_tokens or 0
                if hasattr(details, "cached_tokens"):
                    cache_read = details.cached_tokens or 0
        llm_response._cache_creation_tokens = cache_creation
        llm_response._cache_read_tokens = cache_read

        return llm_response

    def _build_usage(self, response: Any, latency_ms: int) -> Usage:
        """Build normalized usage metrics from OpenAI/OpenRouter response."""
        prompt_tokens = response.usage.prompt_tokens if response.usage else 0
        completion_tokens = response.usage.completion_tokens if response.usage else 0

        cache_creation = 0
        cache_read = 0
        if response.usage and hasattr(response.usage, "prompt_tokens_details"):
            details = response.usage.prompt_tokens_details
            if details:
                if hasattr(details, "cache_write_tokens"):
                    cache_creation = details.cache_write_tokens or 0
                if hasattr(details, "cached_tokens"):
                    cache_read = details.cached_tokens or 0

        # OpenRouter returns usage.cost (in credits/USD); use it when present
        cost_from_provider = getattr(response.usage, "cost", None)
        if cost_from_provider is not None:
            try:
                estimated_cost = float(cost_from_provider)
                cost_status = "observed"
                cost_observed = True
            except (TypeError, ValueError):
                estimated_cost, cost_status, cost_observed = estimate_cost_details(
                    response.model or self._model,
                    prompt_tokens,
                    completion_tokens,
                    cache_creation_tokens=cache_creation,
                    cache_read_tokens=cache_read,
                )
        else:
            estimated_cost, cost_status, cost_observed = estimate_cost_details(
                response.model or self._model,
                prompt_tokens,
                completion_tokens,
                cache_creation_tokens=cache_creation,
                cache_read_tokens=cache_read,
            )

        return Usage(
            model=response.model or self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            estimated_cost=estimated_cost,
            context_window=get_context_window(response.model or self._model),
            cost_status=cost_status,
            cost_observed=cost_observed,
        )
