"""Abstract LLM client interface.

Designed for pluggable backends (Anthropic, OpenAI, local models).
Each backend implements the same interface so the agent loop is
provider-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Usage:
    """Token usage and cost metrics for a single LLM call."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    estimated_cost: float = 0.0
    context_window: int = 200_000  # model's max context
    provider: Optional[str] = None
    upstream_provider: Optional[str] = None
    upstream_model: Optional[str] = None
    request_id: Optional[str] = None
    policy_decision: Optional[Dict[str, Any]] = None
    input_hash: Optional[str] = None
    output_hash: Optional[str] = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def context_used_pct(self) -> float:
        if self.context_window == 0:
            return 0.0
        return (self.prompt_tokens / self.context_window) * 100


@dataclass
class ToolCallRequest:
    """A tool call requested by the LLM."""

    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    """Response from an LLM call.

    Either text (final answer) or tool_calls (needs execution), or both.
    """

    text: Optional[str] = None
    tool_calls: Optional[List[ToolCallRequest]] = None
    usage: Optional[Usage] = None
    stop_reason: Optional[str] = None
    raw: Any = None  # raw API response for debugging
    thinking: Optional[str] = None  # extended thinking content (if model supports it)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class StructuredLLMResponse:
    """Typed response payload for structured output calls."""

    parsed: Any
    usage: Optional[Usage] = None
    stop_reason: Optional[str] = None
    raw: Any = None
    text: Optional[str] = None


# ---------- Structured provider failure taxonomy ----------
#
# Backends raise ``ProviderError`` (instead of opaque vendor exceptions) so
# the agent loop, telemetry and run record can attribute failures truthfully
# to the actual upstream provider, distinguish recoverable vs terminal cases,
# and expose a resumable hint to the UI. This replaces the prior behavior
# where every upstream HTTP failure (including OpenRouter 402 credit
# exhaustion) collapsed into a generic ``api_error`` / ``circuit_breaker``
# stop-reason mislabeled as ``OpenAI API error``.

# Stable failure classes used by stop_reason / loop_state / telemetry.
PROVIDER_FAILURE_AUTH = "auth"                       # 401, 403 -- not resumable
PROVIDER_FAILURE_PAYMENT_REQUIRED = "payment_required"  # 402     -- resumable after top-up
PROVIDER_FAILURE_RATE_LIMIT = "rate_limit"           # 429       -- resumable
PROVIDER_FAILURE_SERVER_ERROR = "server_error"       # 5xx       -- resumable
PROVIDER_FAILURE_NETWORK = "network"                 # timeout / connection -- resumable
PROVIDER_FAILURE_API_ERROR = "api_error"             # other     -- not classified

# Failure classes whose underlying cause is transient/recoverable from the
# operator's perspective (top up credits, wait, retry, fix auth) — the
# selected run record can advertise a resumable hint to the UI.
RESUMABLE_FAILURE_CLASSES = frozenset({
    PROVIDER_FAILURE_PAYMENT_REQUIRED,
    PROVIDER_FAILURE_RATE_LIMIT,
    PROVIDER_FAILURE_SERVER_ERROR,
    PROVIDER_FAILURE_NETWORK,
    PROVIDER_FAILURE_AUTH,
})


def classify_provider_status(status_code: Optional[int], error_name: str = "") -> str:
    """Map an HTTP status code / SDK exception name to a stable failure class.

    Network errors and timeouts come through without an HTTP status code so
    we also accept the SDK exception class name.
    """
    if status_code == 401 or status_code == 403:
        return PROVIDER_FAILURE_AUTH
    if status_code == 402:
        return PROVIDER_FAILURE_PAYMENT_REQUIRED
    if status_code == 429:
        return PROVIDER_FAILURE_RATE_LIMIT
    if status_code is not None and 500 <= int(status_code) < 600:
        return PROVIDER_FAILURE_SERVER_ERROR
    if error_name in (
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "PoolTimeout",
        "ReadTimeout",
        "Timeout",
        "WriteTimeout",
    ):
        return PROVIDER_FAILURE_NETWORK
    if error_name == "InternalServerError":
        return PROVIDER_FAILURE_SERVER_ERROR
    return PROVIDER_FAILURE_API_ERROR


def stop_reason_for_failure_class(failure_class: str) -> str:
    """Map a failure class to a stable agent ``stop_reason`` label.

    These labels surface in the runs list and in /runs/{id} so a 402 from
    OpenRouter no longer hides behind ``circuit_breaker`` or ``api_error``.
    """
    return {
        PROVIDER_FAILURE_AUTH: "provider_auth",
        PROVIDER_FAILURE_PAYMENT_REQUIRED: "provider_payment_required",
        PROVIDER_FAILURE_RATE_LIMIT: "provider_rate_limit",
        PROVIDER_FAILURE_SERVER_ERROR: "provider_server_error",
        PROVIDER_FAILURE_NETWORK: "provider_network",
    }.get(failure_class, "provider_api_error")


class ProviderError(Exception):
    """Truthful, attributed upstream provider failure.

    Wraps the original SDK exception so the agent loop can:

    - log the actual provider name (``openrouter``, ``openai``, ``anthropic``,
      ``local``) instead of always saying ``OpenAI API error``,
    - set a stable stop_reason and failure_class on the run record,
    - tell the UI whether the run is resumable from the user's perspective
      (e.g. add credits, wait out a 429) vs terminal (auth misconfig).
    """

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: Optional[int] = None,
        failure_class: Optional[str] = None,
        resumable: Optional[bool] = None,
        upstream_provider: Optional[str] = None,
        upstream_model: Optional[str] = None,
        request_id: Optional[str] = None,
        policy_decision: Optional[Dict[str, Any]] = None,
        input_hash: Optional[str] = None,
        output_hash: Optional[str] = None,
        original: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        if failure_class is None:
            failure_class = classify_provider_status(
                status_code,
                type(original).__name__ if original is not None else "",
            )
        self.failure_class = failure_class
        if resumable is None:
            resumable = failure_class in RESUMABLE_FAILURE_CLASSES
        self.resumable = bool(resumable)
        self.upstream_provider = upstream_provider
        self.upstream_model = upstream_model
        self.request_id = request_id
        self.policy_decision = policy_decision
        self.input_hash = input_hash
        self.output_hash = output_hash
        self.original = original

    def __str__(self) -> str:
        base = super().__str__()
        bits = [self.provider]
        if self.status_code is not None:
            bits.append(str(self.status_code))
        bits.append(self.failure_class)
        return f"[{'/'.join(bits)}] {base}"


class LLMClient(ABC):
    """Abstract base class for LLM providers.

    Implement this for each backend (Anthropic, OpenAI, etc.).
    """

    @abstractmethod
    def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send a conversation with tool definitions, return response.

        Args:
            system: System prompt text.
            messages: Conversation messages in provider-neutral format:
                [{"role": "user"|"assistant"|"tool", "content": ...}]
            tools: Tool schemas in Anthropic format:
                [{"name": ..., "description": ..., "input_schema": ...}]
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            LLMResponse with text and/or tool_calls.
        """

    @abstractmethod
    def model_name(self) -> str:
        """Return the model identifier being used."""

    @abstractmethod
    def context_window(self) -> int:
        """Return the model's context window size in tokens."""

    def chat_structured(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        response_model: Any,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> StructuredLLMResponse:
        """Request a response validated against a Pydantic model.

        Providers may override this using native structured output support.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support native structured output."
        )

    def supports_prefill(self) -> bool:
        """Whether the model supports assistant message prefill.

        Models with extended thinking enabled by default (e.g. claude-opus-4-6)
        do NOT support prefill. Override in subclass if needed.
        """
        return True

    def is_thinking_model(self) -> bool:
        """Whether the model uses extended thinking by default.

        Override in subclass to detect models with built-in thinking.
        """
        return False


# ----- Model pricing (USD per 1M tokens) -----
# Used for cost estimation. Update as pricing changes.
# Last updated: 2026-02-08

MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Anthropic Claude 4.6
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    # Anthropic Claude 4.5 family
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
    # Anthropic Claude 4 family (legacy)
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    # Anthropic Claude 3.5 family (legacy)
    "claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0},
    "claude-3-5-sonnet-20240620": {"input": 3.0, "output": 15.0},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.0},
    # Anthropic Claude 3 family (legacy)
    "claude-3-opus-20240229": {"input": 15.0, "output": 75.0},
    "claude-3-sonnet-20240229": {"input": 3.0, "output": 15.0},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
    # OpenAI GPT-5.x Codex (2026 pricing estimates)
    "gpt-5.2-codex": {"input": 5.0, "output": 20.0},
    "gpt-5.1-codex": {"input": 3.0, "output": 12.0},
    "gpt-5.1-codex-high": {"input": 3.0, "output": 12.0},
    "gpt-5.1-codex-medium": {"input": 1.50, "output": 6.0},
    "gpt-5.1-codex-max": {"input": 6.0, "output": 24.0},
    # OpenAI GPT-4o family
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    # OpenAI o-series
    "o1": {"input": 15.0, "output": 60.0},
    "o1-mini": {"input": 3.0, "output": 12.0},
    "o3-mini": {"input": 1.10, "output": 4.40},
    # OpenRouter-style aliases (canonical_model_name strips "openrouter/" -> provider/model)
    "anthropic/claude-4.6-sonnet": {"input": 3.0, "output": 15.0},
    "anthropic/claude-4.6-opus": {"input": 5.0, "output": 25.0},
    "anthropic/claude-4.5-sonnet": {"input": 3.0, "output": 15.0},
    "anthropic/claude-3.5-sonnet": {"input": 3.0, "output": 15.0},
    "openai/o3-mini": {"input": 1.10, "output": 4.40},
    "openai/gpt-5.1-codex": {"input": 3.0, "output": 12.0},
    "openai/gpt-5.3-codex": {"input": 5.0, "output": 20.0},
}

CONTEXT_WINDOWS: Dict[str, int] = {
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-5-20250929": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-sonnet-20240620": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    "claude-3-sonnet-20240229": 200_000,
    "claude-3-haiku-20240307": 200_000,
    # OpenAI GPT-5.x Codex
    "gpt-5.2-codex": 1_000_000,
    "gpt-5.1-codex": 1_000_000,
    "gpt-5.1-codex-high": 1_000_000,
    "gpt-5.1-codex-medium": 500_000,
    "gpt-5.1-codex-max": 1_000_000,
    # OpenAI GPT-4o
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    # OpenAI o-series
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3-mini": 200_000,
}


def _is_openai_model(model: str) -> bool:
    """Detect if a model belongs to OpenAI (vs Anthropic)."""
    if model.startswith("openrouter/"):
        return True
    return model.startswith(("gpt-", "o1", "o3"))


def canonical_model_name(model: str) -> str:
    """Normalize provider-prefixed model IDs for internal lookups.

    Examples:
        openrouter/meta-llama/llama-3-8b-instruct -> meta-llama/llama-3-8b-instruct
        gpt-4o -> gpt-4o
    """
    if model.startswith("openrouter/"):
        return model.split("/", 1)[1]
    return model


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Estimate cost in USD for a single LLM call.

    If prompt caching is active, the cost is split between cached and
    non-cached tokens. Discount rates differ by provider:

    **Anthropic:**
    - cache_creation_tokens: charged at 1.25x input price
    - cache_read_tokens: charged at 0.10x input price (90% discount)

    **OpenAI:**
    - cache_creation_tokens: no extra charge (automatic caching)
    - cache_read_tokens: charged at 0.50x input price (50% discount)
    """
    model_key = canonical_model_name(model)
    pricing = MODEL_PRICING.get(model_key)
    if not pricing:
        # Unknown model -- try prefix matching
        for name, p in MODEL_PRICING.items():
            if model_key.startswith(name.rsplit("-", 1)[0]):
                pricing = p
                break
    if not pricing:
        return 0.0

    input_price = pricing["input"]

    # Split input tokens into cached and non-cached
    non_cached_input = max(0, prompt_tokens - cache_creation_tokens - cache_read_tokens)
    input_cost = (non_cached_input / 1_000_000) * input_price

    # Provider-specific cache discount rates
    if _is_openai_model(model):
        # OpenAI: no write premium, 50% discount on reads
        cache_write_cost = (cache_creation_tokens / 1_000_000) * input_price  # 1.0x
        cache_read_cost = (cache_read_tokens / 1_000_000) * input_price * 0.50
    else:
        # Anthropic: 1.25x write premium, 90% discount on reads
        cache_write_cost = (cache_creation_tokens / 1_000_000) * input_price * 1.25
        cache_read_cost = (cache_read_tokens / 1_000_000) * input_price * 0.10

    output_cost = (completion_tokens / 1_000_000) * pricing["output"]

    return round(input_cost + cache_write_cost + cache_read_cost + output_cost, 6)


def get_context_window(model: str) -> int:
    """Get context window for a model."""
    model_key = canonical_model_name(model)
    if model_key in CONTEXT_WINDOWS:
        return CONTEXT_WINDOWS[model_key]
    # Default
    for name, window in CONTEXT_WINDOWS.items():
        if model_key.startswith(name.rsplit("-", 1)[0]):
            return window
    return 200_000
