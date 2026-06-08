"""Anthropic Claude LLM backend.

First implementation of the pluggable LLM client.
Requires: pip install anthropic

Includes retry with exponential backoff for transient API errors.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .base import (
    LLMClient,
    LLMResponse,
    ProviderError,
    StructuredLLMResponse,
    ToolCallRequest,
    Usage,
    classify_provider_status,
    estimate_cost_details,
    get_context_window,
)

logger = logging.getLogger(__name__)

# Default model
DEFAULT_MODEL = "claude-sonnet-4-5"

# Retry configuration
MAX_RETRIES = 3
BASE_RETRY_DELAY = 1.0  # seconds
MAX_RETRY_DELAY = 30.0  # seconds

# HTTP status codes that warrant retry
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}


# Models that have extended thinking enabled by default.
# These models: require temperature=1, do NOT support assistant prefill,
# and return "thinking" content blocks in responses.
THINKING_MODELS = {
    "claude-opus-4-6",
}

# Default thinking budget (tokens) when thinking is auto-enabled.
# Can be overridden via AMOF_THINKING_BUDGET env var or agent.yaml.
# 16k is reasonable for planning tasks with large codebase context.
DEFAULT_THINKING_BUDGET = 16_000


def _resolve_ca_bundle() -> Optional[str]:
    ca_bundle = (
        os.environ.get("SSL_CERT_FILE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("AWS_CA_BUNDLE")
    )
    if ca_bundle:
        return ca_bundle
    for ca_path in ["/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"]:
        if os.path.exists(ca_path):
            return ca_path
    return None


class AnthropicClient(LLMClient):
    """Anthropic Claude API client.

    Uses the anthropic Python SDK with automatic retry for transient errors.
    Automatically handles extended thinking for models that require it.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: int = MAX_RETRIES,
        thinking_budget: Optional[int] = None,
    ):
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model or os.environ.get("AMOF_MODEL", DEFAULT_MODEL)
        self._client = None
        self._max_retries = max_retries
        self._provider = "anthropic"

        # Thinking budget (only used for thinking models)
        env_budget = os.environ.get("AMOF_THINKING_BUDGET")
        self._thinking_budget = (
            thinking_budget
            or (int(env_budget) if env_budget else None)
            or DEFAULT_THINKING_BUDGET
        )

        if not self._api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not set. Add it to .env or pass api_key parameter."
            )

    def _get_client(self) -> Any:
        """Lazy-init the Anthropic SDK client."""
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise ImportError(
                    "anthropic package not installed. Run: pip install anthropic"
                )

            # Auto-detect system CA bundle for corporate proxy environments (Zscaler, etc.)
            import httpx
            ca_bundle = _resolve_ca_bundle()
            if ca_bundle:
                http_client = httpx.Client(verify=ca_bundle)
                self._client = anthropic.Anthropic(api_key=self._api_key, http_client=http_client)
            else:
                self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send conversation to Claude API.

        Automatically applies prompt caching to the system prompt and
        tool definitions (last block gets cache_control: ephemeral).
        This can reduce input token costs by up to 90% on cache hits.

        For extended thinking models (e.g. claude-opus-4-6), automatically:
        - Enables thinking with configured budget
        - Sets temperature to 1 (required by API)
        - Strips assistant prefill messages (not supported with thinking)
        """
        client = self._get_client()
        start = time.monotonic()

        # Convert messages to Anthropic format
        api_messages = self._convert_messages(messages)

        # Build system prompt with cache_control for prompt caching
        system_blocks = self._build_cached_system(system)

        thinking = self.is_thinking_model()

        kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": api_messages,
        }

        if thinking:
            # Extended thinking models: temperature must be 1, enable thinking
            kwargs["temperature"] = 1
            # Opus 4.6+: adaptive thinking (no budget_tokens -- use effort param)
            # Older models: enabled thinking with explicit budget
            if "opus-4-6" in self._model or "opus-4-5" in self._model:
                kwargs["thinking"] = {"type": "adaptive"}
                logger.debug("Adaptive thinking enabled for %s", self._model)
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self._thinking_budget,
                }
                logger.debug(
                    "Thinking enabled for %s (budget=%d tokens)",
                    self._model, self._thinking_budget,
                )
            # Ensure conversation ends with a user message (no prefill)
            if api_messages and api_messages[-1]["role"] == "assistant":
                logger.debug(
                    "Stripping assistant prefill message for thinking model %s",
                    self._model,
                )
                kwargs["messages"] = api_messages[:-1]
        else:
            kwargs["temperature"] = temperature

        if tools:
            # Apply cache_control to the last tool definition
            kwargs["tools"] = self._build_cached_tools(tools)

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
        """Use Anthropic native structured outputs via tool_choice.

        Anthropic's native path uses a synthetic tool with input_schema derived
        from the Pydantic model. The model is forced to call that tool.
        """
        client = self._get_client()
        start = time.monotonic()
        api_messages = self._convert_messages(messages)
        system_blocks = self._build_cached_system(system)
        tool_name = f"emit_{response_model.__name__.lower()}"
        structured_tool = self._build_structured_tool(response_model, tool_name)
        thinking = self.is_thinking_model()

        kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": api_messages,
            "tools": [structured_tool],
            "tool_choice": {"type": "tool", "name": tool_name},
        }

        if thinking:
            kwargs["temperature"] = 1
            if "opus-4-6" in self._model or "opus-4-5" in self._model:
                kwargs["thinking"] = {"type": "adaptive"}
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self._thinking_budget,
                }
            if api_messages and api_messages[-1]["role"] == "assistant":
                kwargs["messages"] = api_messages[:-1]
        else:
            kwargs["temperature"] = temperature

        response = self._call_with_retry(client, kwargs)
        latency_ms = int((time.monotonic() - start) * 1000)

        tool_payload = None
        text_parts: List[str] = []
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                tool_payload = block.input
                break
            if block.type == "text":
                text_parts.append(block.text)

        if tool_payload is None:
            raise ValueError(
                f"Anthropic structured output did not include expected tool call '{tool_name}'."
            )

        try:
            parsed = response_model.model_validate(tool_payload)
        except ValidationError as exc:
            raise ValueError(
                f"Anthropic structured output failed Pydantic validation: {exc}"
            ) from exc

        usage = self._build_usage(response, latency_ms)
        return StructuredLLMResponse(
            parsed=parsed,
            usage=usage,
            stop_reason=response.stop_reason,
            raw=response,
            text="\n".join(text_parts) if text_parts else None,
        )

    @staticmethod
    def _build_cached_system(system: str) -> List[Dict[str, Any]]:
        """Wrap system prompt in cache-enabled content blocks.

        Anthropic prompt caching requires system to be a list of content
        blocks, with cache_control on the last block. This marks the system
        prompt as cacheable, so subsequent calls with the same prompt prefix
        pay only 10% of the input token cost for cached tokens.
        """
        return [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    @staticmethod
    def _build_cached_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Apply cache_control to the last tool definition.

        Tool definitions are typically static across calls, so caching them
        saves significant tokens on every call after the first.
        """
        if not tools:
            return tools

        # Deep copy to avoid mutating the original
        cached = [dict(t) for t in tools]
        cached[-1] = dict(cached[-1])
        cached[-1]["cache_control"] = {"type": "ephemeral"}
        return cached

    @property
    def provider(self) -> str:
        """Truthful upstream provider label."""
        return self._provider

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
        """Call the API with exponential backoff retry for transient errors.
        
        Returns tuple of (response, retry_count) for telemetry tracking.
        """
        last_error: Optional[BaseException] = None
        retry_count = 0

        for attempt in range(self._max_retries + 1):
            try:
                response = client.messages.create(**kwargs)
                # Store retry count in response for telemetry
                response._amof_retry_count = retry_count
                return response
            except Exception as e:
                last_error = e
                status_code = getattr(e, "status_code", None)
                is_retryable = status_code in RETRYABLE_STATUS_CODES if status_code else False

                # Also retry on connection/timeout errors
                error_name = type(e).__name__
                if error_name in ("ConnectError", "ReadTimeout", "WriteTimeout", "PoolTimeout"):
                    is_retryable = True

                if not is_retryable or attempt == self._max_retries:
                    logger.error(
                        "Anthropic API error (attempt %d/%d, non-retryable): %s",
                        attempt + 1, self._max_retries + 1, e,
                    )
                    raise self._wrap_provider_error(e) from e

                # Exponential backoff with jitter
                retry_count += 1
                delay = min(BASE_RETRY_DELAY * (2 ** attempt), MAX_RETRY_DELAY)
                jitter = delay * 0.25 * random.random()
                wait = delay + jitter

                logger.warning(
                    "Anthropic API error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, self._max_retries + 1, wait, e,
                )
                time.sleep(wait)

        raise self._wrap_provider_error(last_error) from last_error  # type: ignore[arg-type]

    def model_name(self) -> str:
        return self._model

    def context_window(self) -> int:
        return get_context_window(self._model)

    def is_thinking_model(self) -> bool:
        """Check if current model has extended thinking enabled by default."""
        for prefix in THINKING_MODELS:
            if self._model == prefix or self._model.startswith(prefix):
                return True
        return False

    def supports_prefill(self) -> bool:
        """Thinking models do not support assistant prefill."""
        return not self.is_thinking_model()

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert internal message format to Anthropic API format.

        Internal format:
            {"role": "user", "content": "text"}
            {"role": "assistant", "content": "text", "tool_calls": [...]}
            {"role": "tool", "tool_call_id": "...", "content": "result text"}

        Anthropic format:
            {"role": "user", "content": "text"}
            {"role": "assistant", "content": [{"type": "text"}, {"type": "tool_use", ...}]}
            {"role": "user", "content": [{"type": "tool_result", ...}]}
        """
        api_messages = []

        for msg in messages:
            role = msg["role"]

            if role == "user":
                api_messages.append({"role": "user", "content": msg["content"]})

            elif role == "assistant":
                content_blocks = []
                if msg.get("content") and isinstance(msg["content"], str):
                    content_blocks.append({"type": "text", "text": msg["content"]})
                elif msg.get("content") and isinstance(msg["content"], list):
                    content_blocks = msg["content"]

                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["arguments"],
                        })

                # Skip empty assistant messages (API rejects empty content blocks)
                if not content_blocks:
                    continue

                api_messages.append({"role": "assistant", "content": content_blocks})

            elif role == "tool":
                # Tool results go in a user message with tool_result blocks
                results = msg.get("results", [])
                if not results:
                    results = [{"tool_call_id": msg.get("tool_call_id", ""), "content": msg.get("content", "")}]

                tool_result_blocks = []
                for r in results:
                    block = {
                        "type": "tool_result",
                        "tool_use_id": r.get("tool_call_id", r.get("id", "")),
                        "content": r.get("content", r.get("output", "")),
                    }
                    if r.get("is_error"):
                        block["is_error"] = True
                    tool_result_blocks.append(block)

                # Merge into previous user message if it exists, or create new
                if api_messages and api_messages[-1]["role"] == "user" and isinstance(api_messages[-1]["content"], list):
                    api_messages[-1]["content"].extend(tool_result_blocks)
                else:
                    api_messages.append({"role": "user", "content": tool_result_blocks})

        return api_messages

    def _parse_response(self, response: Any, latency_ms: int) -> LLMResponse:
        """Parse Anthropic API response into our format.

        Extracts:
        - Text content blocks → text
        - Thinking content blocks → thinking (for extended thinking models)
        - Tool use blocks → tool_calls
        - Prompt cache metrics for cost estimation
        """
        text_parts = []
        thinking_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                thinking_parts.append(block.thinking)
            elif block.type == "tool_use":
                tool_calls.append(ToolCallRequest(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,
                ))

        thinking_text = "\n".join(thinking_parts) if thinking_parts else None
        if thinking_text:
            logger.debug(
                "Extended thinking: %d chars across %d blocks",
                len(thinking_text), len(thinking_parts),
            )

        # Extract cache metrics for accurate cost estimation
        cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0

        usage = self._build_usage(response, latency_ms)

        llm_response = LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls if tool_calls else None,
            usage=usage,
            stop_reason=response.stop_reason,
            raw=response,
            thinking=thinking_text,
        )

        # Attach retry count if available (for telemetry)
        if hasattr(response, "_amof_retry_count"):
            llm_response._retry_count = response._amof_retry_count

        # Attach prompt cache metrics (for agent-level telemetry)
        llm_response._cache_creation_tokens = cache_creation
        llm_response._cache_read_tokens = cache_read

        if cache_read > 0 or cache_creation > 0:
            logger.debug(
                "Prompt cache: created=%d read=%d (hit_rate=%.0f%%)",
                cache_creation, cache_read,
                (cache_read / (cache_creation + cache_read) * 100) if (cache_creation + cache_read) else 0,
            )

        return llm_response

    @staticmethod
    def _build_structured_tool(response_model: Any, tool_name: str) -> Dict[str, Any]:
        """Create Anthropic tool schema from a Pydantic response model."""
        input_schema = response_model.model_json_schema()
        return {
            "name": tool_name,
            "description": f"Emit {response_model.__name__} as structured JSON.",
            "input_schema": input_schema,
        }

    def _build_usage(self, response: Any, latency_ms: int) -> Usage:
        """Build normalized usage metrics from Anthropic response."""
        cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        estimated_cost, cost_status, cost_observed = estimate_cost_details(
            response.model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
        )
        return Usage(
            model=response.model,
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
            estimated_cost=estimated_cost,
            context_window=get_context_window(response.model),
            cost_status=cost_status,
            cost_observed=cost_observed,
        )
