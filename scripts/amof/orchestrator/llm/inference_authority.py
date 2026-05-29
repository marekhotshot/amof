"""Inference Authority — shared chokepoint for live controlplane inference.

Audit context (D1 verdict was PARTIAL — see
`docs/audit/runtime-truth-ial-reality.md`):

- Phase 1 routed the off-funnel callsites (DelegateTool batch summarizer,
  CodebaseIndexer) through this authority via ``with_source(...)`` adapters.
- Phase 1b additionally routes the **master Agent path** through the same
  authority via :meth:`record_external_call`. The Agent still owns model
  selection (``ModelRouter``) and the actual ``chat()`` invocation so cascade
  / failover / circuit-breaker behaviour stays in one place, but emission
  (telemetry recording + ``llm_call`` event) is now uniformly owned by the
  authority. Raw ``events.jsonl`` master entries therefore now carry
  ``source="master"`` instead of relying on a UI-side default.
- Phase 2a adds **per-source client resolution** so non-master sources
  (currently ``summarizer`` and ``indexer``) can be routed to a local
  OpenAI-compatible inference endpoint instead of the cloud underlying
  client. Master is intentionally never registered in the source map in
  this slice; it always uses ``self._underlying``. Fallback policy is
  explicit per source (``"cloud"`` falls back to the underlying on local
  failure, ``"none"`` re-raises) so silent invented behaviour is impossible.

The authority is an `LLMClient`-shaped adapter that wraps an underlying
client (typically the run's `ModelRouter`) and centralises:

  * source attribution     (``master``, ``summarizer``, ``indexer``,
                            ``runner:<name>``, ...)
  * normalised telemetry   (``SessionTelemetry.record_from_usage(tier=...)``
                            + ``record_agent_cost(source, cost)``)
  * normalised events      (``EventLog.log("llm_call", ..., source=source)``
                            so ``UIRunEventLog`` rebroadcasts uniformly)
  * failure handoff        (exceptions propagate unchanged so the Agent
                            loop's existing circuit breaker still owns the
                            terminal classification)
  * per-source routing     (Phase 2a — opt-in via
                            :meth:`register_source_client`)

What this is NOT:

- Not a replacement for ``LLMClient`` / ``ModelRouter``. Provider SDK calls,
  cascade and fallback all stay in the underlying client.
- Not a budgeting layer. Cost ceilings still live in ``SessionTelemetry`` /
  ``Agent.run``.
- Not a redesign of every caller. Phase 1b touches only the master agent
  emission path; ``_handle_tool_calls``, retry / cache bookkeeping, and
  budget-warning surfaces remain in the Agent.
- Not a model-quality router. Phase 2a deliberately limits local routing
  to ``summarizer`` and ``indexer`` and does not introduce master fallback
  logic. Master path resolution stays unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import LLMClient, LLMResponse, StructuredLLMResponse, Usage

logger = logging.getLogger(__name__)


# Per-source fallback policy when a registered client raises.
#
# - ``"cloud"`` (default): on failure of the source-specific client, fall
#   back to the underlying cloud client and re-emit telemetry under the
#   same source label (cost will reflect the cloud call). A
#   ``llm_local_fallback`` event is logged for operator visibility so the
#   fallback is never silent.
# - ``"none"``: re-raise the original exception. Use this for
#   local-only experiments where masking failures would hide config drift.
SOURCE_FALLBACK_CLOUD = "cloud"
SOURCE_FALLBACK_NONE = "none"
_VALID_FALLBACKS = {SOURCE_FALLBACK_CLOUD, SOURCE_FALLBACK_NONE}


def _client_provider(client: Any) -> str:
    """Best-effort truthful provider id for a resolved client.

    Returns ``""`` when no truthful provider can be derived (legacy
    custom clients used in tests). Order of preference:

    1. ``client.provider`` (the canonical attribute used by
       :class:`OpenAIClient` and :class:`LocalOpenAICompatibleClient`).
    2. ``client._provider`` (back-compat for older client subclasses).
    3. ``client.detect_active_provider()`` (ModelRouter exposes this so
       cascaded calls report the cascade-elected upstream).

    No string heuristics on class names — those would silently
    misattribute (the IAL Phase 2a refactor explicitly removed them).
    """
    if client is None:
        return ""
    for attr in ("provider", "_provider"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    detect = getattr(client, "detect_active_provider", None)
    if callable(detect):
        try:
            value = detect()
            if isinstance(value, str) and value:
                return value
        except Exception:
            pass
    return ""


class InferenceAuthority(LLMClient):
    """Thin shared dispatch layer for live controlplane inference.

    Wraps an underlying ``LLMClient`` (almost always the run's
    ``ModelRouter``) and forwards every call while uniformly recording
    source-attributed telemetry and ``llm_call`` events.

    Use :meth:`with_source` to obtain a sibling adapter bound to a different
    source label without re-wiring telemetry/events. The underlying client
    is shared, so failover/cascade state stays single-sourced.

    Phase 2a — call-site dispatch:
        :meth:`register_source_client` lets the API runner attach a
        per-source ``LLMClient`` (typically a
        :class:`~amof.orchestrator.llm.local_openai_compatible.LocalOpenAICompatibleClient`)
        for ``summarizer`` and ``indexer``. ``chat`` / ``chat_structured``
        consult that map (keyed by the bound ``default_source``) before
        falling back to ``self._underlying``.
    """

    def __init__(
        self,
        underlying: LLMClient,
        *,
        telemetry: Optional[Any] = None,
        events: Optional[Any] = None,
        default_source: str = "master",
        source_clients: Optional[Dict[str, LLMClient]] = None,
        source_fallback: Optional[Dict[str, str]] = None,
    ) -> None:
        self._underlying = underlying
        self._telemetry = telemetry
        self._events = events
        self._default_source = default_source
        # Shared mutable maps so siblings created via ``with_source`` see
        # the same registrations without re-wiring. The API runner builds
        # the root authority once and registers local clients before
        # handing ``with_source(...)`` adapters to indexer / summarizer.
        self._source_clients: Dict[str, LLMClient] = (
            source_clients if source_clients is not None else {}
        )
        self._source_fallback: Dict[str, str] = (
            source_fallback if source_fallback is not None else {}
        )

    # ------------------------------------------------------------------ #
    # Attribution helpers
    # ------------------------------------------------------------------ #

    def with_source(self, source: str) -> "InferenceAuthority":
        """Return a sibling authority bound to a specific source label.

        The sibling shares the underlying client / telemetry / events sink
        and the per-source client + fallback maps so cost rolls into the
        same ``SessionTelemetry``, provider cascade state is not
        duplicated, and Phase 2a local routing decisions stay consistent
        across all callers.
        """
        return InferenceAuthority(
            self._underlying,
            telemetry=self._telemetry,
            events=self._events,
            default_source=source,
            source_clients=self._source_clients,
            source_fallback=self._source_fallback,
        )

    # ------------------------------------------------------------------ #
    # Phase 2a — per-source client registration
    # ------------------------------------------------------------------ #

    def register_source_client(
        self,
        source: str,
        client: LLMClient,
        *,
        fallback: str = SOURCE_FALLBACK_CLOUD,
    ) -> None:
        """Register a per-source ``LLMClient`` (e.g. local inference).

        Args:
            source: Source label this client handles, e.g. ``"summarizer"``
                or ``"indexer"``. The master path is intentionally not a
                supported registration target in Phase 2a; callers should
                not register ``"master"``.
            client: An ``LLMClient``-shaped instance. Typically a
                :class:`~amof.orchestrator.llm.local_openai_compatible.LocalOpenAICompatibleClient`.
            fallback: Either ``"cloud"`` (fall back to ``self._underlying``
                if the registered client raises) or ``"none"`` (re-raise).

        Raises:
            ValueError: if ``fallback`` is not a recognised policy.
        """

        if fallback not in _VALID_FALLBACKS:
            raise ValueError(
                f"Unknown fallback policy {fallback!r}; "
                f"expected one of {sorted(_VALID_FALLBACKS)}"
            )
        if source == "master":
            # Defensive guard: Phase 2a explicitly does not route master
            # through alternate clients. Callers should never request this;
            # log loudly so the misconfiguration is visible.
            logger.warning(
                "InferenceAuthority.register_source_client called with "
                "source='master'; ignoring (Phase 2a forbids master local "
                "routing)."
            )
            return
        self._source_clients[source] = client
        self._source_fallback[source] = fallback

    def has_source_client(self, source: str) -> bool:
        return source in self._source_clients

    def _resolve_client(self, source: str) -> LLMClient:
        """Return the per-source client if one is registered, else underlying."""
        return self._source_clients.get(source, self._underlying)

    @property
    def underlying(self) -> LLMClient:
        return self._underlying

    @property
    def default_source(self) -> str:
        return self._default_source

    # ------------------------------------------------------------------ #
    # LLMClient interface
    # ------------------------------------------------------------------ #

    def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        source: Optional[str] = None,
    ) -> LLMResponse:
        attrib = source or self._default_source
        response, used_fallback, served_provider = self._dispatch(
            attrib,
            lambda c: c.chat(
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        self._record(
            getattr(response, "usage", None),
            source=attrib,
            provider=served_provider,
        )
        if used_fallback:
            self._emit_fallback_event(attrib)
        return response

    def chat_structured(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        response_model: Any,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        source: Optional[str] = None,
    ) -> StructuredLLMResponse:
        attrib = source or self._default_source
        response, used_fallback, served_provider = self._dispatch(
            attrib,
            lambda c: c.chat_structured(
                system=system,
                messages=messages,
                response_model=response_model,
                max_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        self._record(
            getattr(response, "usage", None),
            source=attrib,
            provider=served_provider,
        )
        if used_fallback:
            self._emit_fallback_event(attrib)
        return response

    # ------------------------------------------------------------------ #
    # Phase 2a — dispatch with explicit fallback
    # ------------------------------------------------------------------ #

    def _dispatch(self, source: str, call):
        """Invoke ``call`` against the resolved client with fallback policy.

        Returns ``(response, used_fallback, served_provider)`` where
        ``served_provider`` is the truthful upstream provider id of the
        client that actually served the call (after any fallback). If a
        per-source client is registered and raises, the configured
        fallback policy decides whether to re-raise or retry against
        ``self._underlying``. The fallback path is deliberately explicit
        — there is no silent provider-swap and no synthesised response.
        Mini Ultra Plan 2 / Phase L2: the served provider is propagated
        into telemetry + ``llm_call`` events so per-call attribution is
        truthful even when fallback flipped from local/runpod back to
        the cloud cascade.
        """

        client = self._resolve_client(source)
        if client is self._underlying:
            return call(client), False, _client_provider(client)

        try:
            return call(client), False, _client_provider(client)
        except Exception as exc:
            policy = self._source_fallback.get(source, SOURCE_FALLBACK_CLOUD)
            if policy == SOURCE_FALLBACK_NONE:
                logger.warning(
                    "InferenceAuthority: source=%s %s client failed and "
                    "fallback='none' — re-raising: %s",
                    source,
                    _client_provider(client) or "alt",
                    exc,
                )
                raise
            logger.warning(
                "InferenceAuthority: source=%s %s client failed; "
                "falling back to underlying cloud client: %s",
                source,
                _client_provider(client) or "alt",
                exc,
            )
            return call(self._underlying), True, _client_provider(self._underlying)

    def _emit_fallback_event(self, source: str) -> None:
        """Record a ``llm_local_fallback`` event for operator visibility."""
        if self._events is None:
            return
        try:
            self._events.log("llm_local_fallback", source=source)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "authority fallback event emission failed for source=%s: %s",
                source,
                exc,
            )

    # ------------------------------------------------------------------ #
    # External-call hook (Phase 1b — master agent path)
    # ------------------------------------------------------------------ #

    def record_external_call(
        self,
        usage: Optional[Usage],
        *,
        source: Optional[str] = None,
        tier: Optional[str] = None,
        tool_calls: int = 0,
        provider: Optional[str] = None,
    ) -> None:
        """Record a call that was already invoked outside the authority.

        The master Agent loop continues to call ``active_llm.chat(...)``
        directly so the existing ``ModelRouter`` tier selection, retry, and
        cache-token bookkeeping in ``Agent.run`` stay untouched. After the
        response is in hand, the Agent hands the resulting ``Usage`` to
        the authority via this method so the same telemetry + event
        emission path is exercised as for ``chat()`` / ``chat_structured``
        calls. This is the seam that makes raw ``events.jsonl`` master /
        runner entries carry their truthful ``source`` without
        double-emitting.

        Args:
            usage: Provider-reported usage for the just-completed call.
                ``None`` is a no-op (e.g. dry-run / timeout responses).
            source: Source attribution to record under. Defaults to the
                authority's ``default_source`` (``"master"`` for the root
                authority; ``"runner:<name>"`` for sibling authorities
                obtained via :meth:`with_source` and handed to a runner
                Agent in Phase L2).
            tier: Optional tier label for ``record_from_usage``. Falls
                back to ``source`` when not provided so back-compat with
                the bypass callsites is preserved.
            tool_calls: Number of tool calls produced by this response.
            provider: Optional explicit provider id. When omitted, the
                authority extracts it from ``self._underlying`` (the
                client that actually served the call from Agent.run).
                This makes ``llm_call`` events truthful about which
                upstream answered even on the master/runner path.
        """

        if provider is None:
            provider = _client_provider(self._underlying)
        self._record(
            usage,
            source=source or self._default_source,
            tier=tier,
            tool_calls=tool_calls,
            provider=provider,
        )

    def model_name(self) -> str:
        return self._underlying.model_name()

    def context_window(self) -> int:
        return self._underlying.context_window()

    def supports_prefill(self) -> bool:
        try:
            return self._underlying.supports_prefill()
        except Exception:
            return True

    def is_thinking_model(self) -> bool:
        try:
            return self._underlying.is_thinking_model()
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Provider-health passthrough
    # ------------------------------------------------------------------ #

    def record_failure(self) -> None:
        """Forward provider failure to the underlying router if supported.

        The indexer calls ``self._llm.record_failure()`` when schema
        validation fails repeatedly; preserve that hook so cascade /
        circuit-breaker behaviour stays intact when an authority adapter
        is plugged in instead of the raw router.
        """

        rec = getattr(self._underlying, "record_failure", None)
        if callable(rec):
            try:
                rec()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("authority record_failure passthrough failed: %s", exc)

    def report_provider_failure(self) -> None:
        rec = getattr(self._underlying, "report_provider_failure", None)
        if callable(rec):
            try:
                rec()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("authority report_provider_failure passthrough failed: %s", exc)

    def report_provider_success(self) -> None:
        rec = getattr(self._underlying, "report_provider_success", None)
        if callable(rec):
            try:
                rec()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("authority report_provider_success passthrough failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _record(
        self,
        usage: Optional[Usage],
        *,
        source: str,
        tier: Optional[str] = None,
        tool_calls: int = 0,
        provider: str = "",
    ) -> None:
        """Record source-attributed telemetry + a normalised llm_call event.

        All emission is best-effort: if the bound telemetry / events sink
        is missing or raises, the call is silently dropped (logged at
        DEBUG) so authority failures cannot break in-flight inference.

        ``tier`` defaults to ``source`` to preserve the Phase 1 behaviour
        for off-funnel callers (summarizer / indexer), where source and
        tier are the same logical bucket. The master path supplies the
        real ``ModelRouter`` tier so per-tier metrics remain truthful.

        ``provider`` is the truthful upstream id of the client that
        served the call (``"local"``, ``"runpod"``, ``"openrouter"``,
        ``"anthropic"``, etc.). Mini Ultra Plan 2 / Phase L2 added this
        so ``CallMetrics`` and the ``llm_call`` SSE rebroadcast carry
        per-call provider attribution end to end.
        """

        if usage is None:
            return

        effective_tier = tier or source

        if self._telemetry is not None:
            try:
                self._telemetry.record_from_usage(
                    usage, tier=effective_tier, provider=provider
                )
            except Exception as exc:
                logger.debug(
                    "authority telemetry record_from_usage failed for source=%s tier=%s: %s",
                    source,
                    effective_tier,
                    exc,
                )
            try:
                if bool(getattr(usage, "cost_observed", True)):
                    self._telemetry.record_agent_cost(
                        source, float(getattr(usage, "estimated_cost", 0.0) or 0.0)
                    )
            except Exception as exc:
                logger.debug(
                    "authority telemetry record_agent_cost failed for source=%s: %s",
                    source,
                    exc,
                )

        if self._events is not None:
            try:
                self._events.log(
                    "llm_call",
                    model=getattr(usage, "model", None),
                    tokens={
                        "in": getattr(usage, "prompt_tokens", 0) or 0,
                        "out": getattr(usage, "completion_tokens", 0) or 0,
                    },
                    cost=(
                        round(float(getattr(usage, "estimated_cost", 0.0) or 0.0), 6)
                        if bool(getattr(usage, "cost_observed", True))
                        else None
                    ),
                    cost_status=getattr(usage, "cost_status", None),
                    latency_ms=getattr(usage, "latency_ms", 0) or 0,
                    tool_calls=tool_calls,
                    source=source,
                    provider=provider,
                    upstream_provider=getattr(usage, "upstream_provider", None),
                    upstream_model=getattr(usage, "upstream_model", None),
                    request_id=getattr(usage, "request_id", None),
                    policy_decision=getattr(usage, "policy_decision", None),
                    input_hash=getattr(usage, "input_hash", None),
                    output_hash=getattr(usage, "output_hash", None),
                    provider_generation_id=getattr(usage, "provider_generation_id", None),
                    provider_generation_ref=getattr(usage, "provider_generation_ref", None),
                )
            except Exception as exc:
                logger.debug(
                    "authority event emission failed for source=%s: %s", source, exc
                )
