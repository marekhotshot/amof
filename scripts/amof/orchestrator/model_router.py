"""Model router -- selects the cheapest adequate model for each agent step.

Three tiers:
- fast:     Haiku-class ($0.80/M). Used for exploration (LS, Read, Grep),
            context summarization, and simple routing decisions.
- standard: Sonnet-class ($3/M). Default for analysis, planning, code edits.
- strong:   Opus-class ($5/M). Promoted to on repeated failures or explicit request.

Promotion triggers:
- 2 consecutive failures on the same step → promote to next tier
- LLM returns empty/confused response → promote
- User explicitly sets --model flag → override

Provider failover:
- Tracks health per provider (consecutive failures, cooldown).
- On provider-level error (429/5xx/timeout), marks provider degraded.
- Degraded providers recover after a configurable cooldown.
- Falls back to alternate provider at the same tier.

Advanced routing:
- select_for_task(): Picks tier based on complexity/risk signals.

The router is transparent to the agent loop: it exposes the same LLMClient
interface and delegates to the appropriate backend.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .llm.base import LLMClient, LLMResponse

logger = logging.getLogger(__name__)

# Tools that indicate an "exploration" phase (cheap model is fine)
EXPLORATION_TOOLS = {"LS", "Read", "InspectFiles", "Glob", "Grep"}

# Maximum consecutive failures before promoting to next tier
MAX_FAILURES_BEFORE_PROMOTE = 2

# Keywords that signal high complexity tasks
_HIGH_COMPLEXITY_KEYWORDS = re.compile(
    r"\b(refactor|architect|migrat|redesign|overhaul|rewrite)\b", re.IGNORECASE,
)
# Keywords that signal medium complexity
_MEDIUM_COMPLEXITY_KEYWORDS = re.compile(
    r"\b(debug|fix|error|exception|traceback|stack\s*trace|crash)\b", re.IGNORECASE,
)


# ---------- Provider Health ----------

@dataclass
class ProviderHealth:
    """Tracks health of a single LLM provider for failover decisions.

    A provider is marked degraded after ``max_failures`` consecutive failures
    and recovers automatically after ``cooldown_seconds``.
    """

    provider: str
    consecutive_failures: int = 0
    last_success: float = field(default_factory=time.time)
    last_failure: float = 0.0
    cooldown_seconds: float = 60.0
    max_failures: int = 3

    @property
    def available(self) -> bool:
        """True if the provider is healthy or its cooldown has elapsed."""
        if self.consecutive_failures < self.max_failures:
            return True
        # Check cooldown
        if self.last_failure > 0 and (time.time() - self.last_failure) > self.cooldown_seconds:
            return True
        return False

    def report_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure = time.time()
        if self.consecutive_failures >= self.max_failures:
            logger.warning(
                "Provider %s marked degraded (%d consecutive failures)",
                self.provider,
                self.consecutive_failures,
            )

    def report_success(self) -> None:
        if self.consecutive_failures > 0:
            logger.info("Provider %s recovered", self.provider)
        self.consecutive_failures = 0
        self.last_success = time.time()


class ModelRouter:
    """Selects the appropriate model tier for each agent step.

    Acts as an LLMClient itself so the Agent doesn't need to change
    its interface -- just pass a ModelRouter where it would pass an LLMClient.
    """

    DEMOTE_AFTER_SUCCESSES = 5  # demote back to default after N consecutive successes

    def __init__(
        self,
        models: Dict[str, LLMClient],
        default_tier: str = "standard",
        fallback_models: Optional[Dict[str, LLMClient]] = None,
        provider_cooldown: float = 60.0,
        routing_config: Optional[Dict[str, Any]] = None,
        cascade: Optional[List[str]] = None,
    ):
        """Initialize with a dict of tier -> LLMClient.

        Args:
            models: {"fast": haiku_client, "standard": sonnet_client, "strong": opus_client}
            default_tier: Starting tier for code generation / editing.
            fallback_models: Alternate provider's tier -> LLMClient mapping for failover.
            provider_cooldown: Seconds before a degraded provider is retried.
            routing_config: Advanced routing thresholds from agent.yaml.
            cascade: Ordered list of tiers from cheapest/fastest to most capable.
        """
        self._cascade = cascade or [t for t in ("fast", "standard", "strong") if t in models]
        if not self._cascade:
            self._cascade = list(models.keys())
            
        if not self._cascade:
            raise ValueError("ModelRouter requires at least one model in 'models'")
            
        if default_tier not in models and "standard" in models:
            default_tier = "standard"
        elif default_tier not in models:
            default_tier = self._cascade[0]

        self._models = models
        self._fallback_models = fallback_models or {}
        self._default_tier = default_tier
        self._current_tier = default_tier
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._phase = "standard"  # current detected phase

        # Track which tier is used for each call (for telemetry)
        self._last_tier_used: Optional[str] = None

        # Provider health tracking
        self._provider_health: Dict[str, ProviderHealth] = {}
        self._primary_provider = self._detect_provider(models.get("standard"))
        if self._primary_provider:
            self._provider_health[self._primary_provider] = ProviderHealth(
                provider=self._primary_provider,
                cooldown_seconds=provider_cooldown,
            )
        fallback_provider = self._detect_provider(
            self._fallback_models.get("standard")
        )
        if fallback_provider:
            self._provider_health[fallback_provider] = ProviderHealth(
                provider=fallback_provider,
                cooldown_seconds=provider_cooldown,
            )
        self._fallback_provider = fallback_provider

        # Advanced routing config
        rc = routing_config or {}
        self._complexity_low = rc.get("complexity_low_threshold", 0.20)
        self._complexity_high = rc.get("complexity_high_threshold", 0.60)
        self._large_context_tokens = rc.get("large_context_tokens", 200_000)
        self._risk_escalation = rc.get("risk_escalation", True)

    @staticmethod
    def _detect_provider(client: Optional[LLMClient]) -> Optional[str]:
        """Truthful provider attribution.

        Prefers the client's explicit ``_provider`` / ``provider`` attribute
        so an OpenAI-SDK client pointed at OpenRouter is correctly attributed
        as ``openrouter`` (not ``openai``). Falls back to the class name only
        for older clients that have not set a provider attribute.
        """
        if client is None:
            return None

        explicit = getattr(client, "_provider", None) or getattr(client, "provider", None)
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip().lower()

        cls_name = type(client).__name__.lower()
        if "anthropic" in cls_name:
            return "anthropic"
        if "openai" in cls_name:
            return "openai"
        if "gemini" in cls_name:
            return "gemini"
        return cls_name

    def _resolve(self, tier: str) -> LLMClient:
        """Get client for a tier, falling back to default.

        If the primary provider is degraded and a fallback is configured,
        try the fallback at the same tier.
        """
        primary = self._models.get(tier, self._models.get(self._default_tier, next(iter(self._models.values()))))

        # Check provider health — if primary is degraded, try fallback
        if self._primary_provider and self._fallback_models:
            health = self._provider_health.get(self._primary_provider)
            if health and not health.available:
                fallback = self._fallback_models.get(tier) or self._fallback_models.get("standard")
                if fallback:
                    logger.info(
                        "Primary provider %s degraded, using fallback for tier '%s'",
                        self._primary_provider, tier,
                    )
                    return fallback

        return primary

    @property
    def active_tier(self) -> str:
        """The tier that was last used."""
        return self._last_tier_used or self._current_tier

    @property
    def has_fast(self) -> bool:
        """Whether a fast-tier model is configured."""
        return len(self._cascade) > 0

    @property
    def has_strong(self) -> bool:
        """Whether a strong-tier model is configured."""
        return len(self._cascade) > 1

    def select_for_phase(self, phase: str) -> LLMClient:
        """Select model for an explicit phase.

        Phases:
        - "explore": Use fast tier (LS, Read, Grep routing)
        - "summarize": Use fast tier (context compression)
        - "plan": Use standard tier
        - "edit": Use current tier (may be promoted)
        - "strong": Force strong tier
        """
        self._phase = phase

        if phase in ("explore", "summarize"):
            tier = self._cascade[0]
        elif phase == "strong":
            tier = self._cascade[-1]
        elif self._consecutive_failures >= MAX_FAILURES_BEFORE_PROMOTE:
            tier = self._promote()
        else:
            tier = self._current_tier

        self._last_tier_used = tier
        return self._resolve(tier)

    def select_for_tools(self, last_tool_calls: Optional[List[str]] = None) -> LLMClient:
        """Auto-detect phase from recent tool usage and select model.

        If the last LLM turn only used exploration tools (LS, Read, Grep, Glob),
        the next call is likely also exploration → use fast tier.
        If it used Write/StrReplace/Shell, we're in editing phase → use standard/strong.
        """
        if last_tool_calls is not None:
            tool_set = set(last_tool_calls)
            if tool_set and tool_set.issubset(EXPLORATION_TOOLS):
                return self.select_for_phase("explore")

        # Check for failure-based promotion
        if self._consecutive_failures >= MAX_FAILURES_BEFORE_PROMOTE:
            tier = self._promote()
        else:
            tier = self._current_tier

        self._last_tier_used = tier
        return self._resolve(tier)

    def record_success(self) -> None:
        """Record a successful LLM call. Resets failure counter and may demote.

        After DEMOTE_AFTER_SUCCESSES consecutive successes while promoted,
        drop back to the default tier to save cost.
        """
        self._consecutive_failures = 0
        self._consecutive_successes += 1

        # Demote back to default if we've been promoted and had enough successes
        if (
            self._current_tier != self._default_tier
            and self._consecutive_successes >= self.DEMOTE_AFTER_SUCCESSES
        ):
            logger.info(
                "Demoting model tier: %s → %s (after %d consecutive successes)",
                self._current_tier, self._default_tier, self._consecutive_successes,
            )
            self._current_tier = self._default_tier
            self._consecutive_successes = 0

    def record_failure(self) -> None:
        """Record a failed LLM call (tool errors or empty response)."""
        self._consecutive_failures += 1
        self._consecutive_successes = 0
        if self._consecutive_failures >= MAX_FAILURES_BEFORE_PROMOTE:
            next_tier = self._next_tier(self._current_tier)
            if next_tier != self._current_tier:
                logger.info(
                    "Promoting model tier: %s → %s (after %d consecutive failures)",
                    self._current_tier, next_tier, self._consecutive_failures,
                )

    def reset_tier(self) -> None:
        """Reset to default tier (e.g., after successful recovery)."""
        self._current_tier = self._default_tier
        self._consecutive_failures = 0

    def _promote(self) -> str:
        """Promote to the next tier and return it."""
        next_tier = self._next_tier(self._current_tier)
        if next_tier != self._current_tier:
            logger.info("Model promoted: %s → %s", self._current_tier, next_tier)
            self._current_tier = next_tier
            self._consecutive_failures = 0
        return self._current_tier

    def _next_tier(self, current: str) -> str:
        """Get the next tier up, or stay at current if already at top."""
        try:
            idx = self._cascade.index(current)
        except ValueError:
            return self._default_tier
        next_idx = min(idx + 1, len(self._cascade) - 1)
        # Only promote if the target tier has a model
        candidate = self._cascade[next_idx]
        if candidate in self._models:
            return candidate
        return current

    # ---- Provider health ----

    def report_provider_failure(self, provider: Optional[str] = None) -> None:
        """Report a provider-level failure (429, 5xx, timeout).

        If no provider specified, uses the primary provider.
        """
        provider = provider or self._primary_provider
        if provider and provider in self._provider_health:
            self._provider_health[provider].report_failure()

    def report_provider_success(self, provider: Optional[str] = None) -> None:
        """Report a successful API call to a provider."""
        provider = provider or self._primary_provider
        if provider and provider in self._provider_health:
            self._provider_health[provider].report_success()

    def provider_status(self) -> Dict[str, Dict[str, Any]]:
        """Return health status of all tracked providers."""
        result = {}
        for name, health in self._provider_health.items():
            result[name] = {
                "available": health.available,
                "consecutive_failures": health.consecutive_failures,
                "cooldown_seconds": health.cooldown_seconds,
            }
        return result

    # ---- Advanced task-based routing ----

    def select_for_task(
        self,
        task_kind: str = "default",
        complexity: Optional[float] = None,
        risk_level: str = "normal",
        context_tokens: int = 0,
    ) -> LLMClient:
        """Select model based on task characteristics.

        Higher-level routing than phase-based selection: considers the
        complexity and risk of the entire task, not just what tools were
        recently called.

        Args:
            task_kind: One of "tool_call", "plan", "summarize", "debug", "edit", "default".
            complexity: 0.0 (trivial) to 1.0 (very complex). None = auto-detect.
            risk_level: "low", "normal", "high". High risk forces strong tier.
            context_tokens: Current context size in tokens.

        Returns:
            The selected LLMClient.
        """
        # Risk escalation overrides everything
        if self._risk_escalation and risk_level == "high":
            self._last_tier_used = "strong"
            return self._resolve("strong")

        # Large context -> prefer strong model (better at long-context reasoning)
        if context_tokens > self._large_context_tokens:
            self._last_tier_used = "strong"
            return self._resolve("strong")

        # Determine tier based on complexity + task_kind
        c = complexity if complexity is not None else 0.3  # default medium-low

        if c <= self._complexity_low:
            # Low complexity
            if task_kind in ("tool_call", "summarize"):
                tier = self._cascade[0]
            else:
                tier = self._current_tier
        elif c >= self._complexity_high:
            # High complexity
            tier = self._cascade[-1]
        else:
            # Medium complexity
            if task_kind == "plan":
                tier = self._cascade[-1]
            elif task_kind == "debug":
                tier = self._default_tier
            else:
                tier = self._current_tier

        self._last_tier_used = tier
        return self._resolve(tier)

    @staticmethod
    def estimate_complexity(
        user_message: str,
        tool_results: Optional[List[str]] = None,
        context_tokens: int = 0,
    ) -> float:
        """Heuristic estimation of task complexity (0.0 - 1.0).

        Used by select_for_task() when no explicit complexity is provided.

        Factors:
        - Message length: very short messages are likely simple
        - Keywords: refactor/architecture/migrate signal high complexity
        - Error traces in tool results signal medium complexity
        - Large context bumps complexity (more to reason about)
        """
        score = 0.3  # baseline

        # Short messages with no special keywords -> lower complexity
        if len(user_message) < 100:
            score -= 0.15

        # High-complexity keywords
        if _HIGH_COMPLEXITY_KEYWORDS.search(user_message):
            score += 0.35

        # Medium-complexity keywords
        if _MEDIUM_COMPLEXITY_KEYWORDS.search(user_message):
            score += 0.15

        # Error traces in tool results
        if tool_results:
            combined = " ".join(tool_results)
            if re.search(r"(traceback|exception|error|stack\s*trace)", combined, re.IGNORECASE):
                score += 0.15

        # Large context bump
        if context_tokens > 100_000:
            score += 0.20

        return max(0.0, min(1.0, score))

    # ---- LLMClient-compatible interface ----
    # These allow ModelRouter to be used as a drop-in replacement for LLMClient
    # in the Agent constructor. The agent just calls router.chat() and
    # the router delegates to the right tier.

    def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Delegate to the currently selected model."""
        client = self._resolve(self._last_tier_used or self._current_tier)
        return client.chat(system, messages, tools, max_tokens, temperature)

    def chat_structured(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        response_model: Any,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> Any:
        """Delegate structured chat to the currently selected model."""
        client = self._resolve(self._last_tier_used or self._current_tier)
        return client.chat_structured(system, messages, response_model, max_tokens, temperature)

    def model_name(self) -> str:
        """Return the model name of the currently active tier."""
        client = self._resolve(self._last_tier_used or self._current_tier)
        return client.model_name()

    def detect_active_provider(self) -> str:
        """Return the provider id of the currently elected upstream client.

        Mini Ultra Plan 2 / Phase L2: the InferenceAuthority's
        ``_client_provider`` helper consults this method to derive a
        truthful per-call provider attribution when the LLMClient handed
        to ``Agent`` is a router rather than a single client. Without
        this hook, telemetry and ``llm_call`` events on the cloud master
        path would record an empty provider, defeating the truthful-
        attribution contract introduced for runner overrides.

        Returns ``""`` when no per-tier client exposes a provider — the
        helper treats that as "no truthful answer" and avoids inventing
        one.
        """
        client = self._resolve(self._last_tier_used or self._current_tier)
        for attr in ("provider", "_provider"):
            value = getattr(client, attr, None)
            if isinstance(value, str) and value:
                return value
        primary = getattr(self, "_primary_provider", "") or ""
        return primary if isinstance(primary, str) else ""

    def context_window(self) -> int:
        """Return context window of the currently active tier."""
        client = self._resolve(self._last_tier_used or self._current_tier)
        return client.context_window()

    def tier_model_names(self) -> Dict[str, str]:
        """Return model names for all configured tiers."""
        return {tier: client.model_name() for tier, client in self._models.items()}
