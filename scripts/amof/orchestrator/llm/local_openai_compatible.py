"""Local OpenAI-compatible LLM backend (IAL Phase 2a).

Phase 2a context (see `docs/audit/runtime-truth-ial-reality.md` and the
follow-on Phase 1/1b notes in :mod:`inference_authority`):

- IAL Phase 1 routed the off-funnel callsites (DelegateTool batch summarizer
  and CodebaseIndexer) through the shared ``InferenceAuthority``.
- IAL Phase 1b additionally routed the master Agent emission path through the
  same authority, so every live runtime LLM call now carries a truthful
  ``source`` label.
- Phase 2a is the first slice that lets *non-master* sources resolve to a
  **local** OpenAI-compatible inference endpoint instead of the cloud
  ``ModelRouter``. Master deliberately stays on the existing cloud path in
  this slice; only ``summarizer`` and ``indexer`` are eligible for local
  routing.

This client is a thin subclass of :class:`OpenAIClient` because every viable
local LLM server (Ollama, vLLM, LM Studio, llama.cpp / llamafile,
text-generation-webui, ...) speaks the OpenAI Chat Completions / structured
output protocol. We intentionally reuse the OpenAI message + tool schema
conversion code instead of writing yet another adapter, and we override
exactly two seams:

1. ``__init__`` / ``_get_client`` — accept an explicit ``base_url`` and
   tolerate a missing API key (Ollama and llama.cpp commonly accept any
   non-empty token), and pass the ``base_url`` through to the OpenAI SDK.
2. ``_build_usage`` — report ``estimated_cost = 0.0`` and prefix the model
   identifier with ``local/<host>/`` so downstream telemetry and the UI can
   see the call was served locally without inventing a fake cloud price.

Truthful semantics this client preserves:

- prompt / completion tokens are taken straight from the provider response
  (most OpenAI-compatible local servers do report them; if absent, both
  fields collapse to 0 — same behaviour as the cloud client).
- latency is captured client-side around the SDK call.
- ``estimated_cost`` is **always** zero for local calls. We do not synthesise
  a cloud-equivalent price; that would lie to the operator about real spend.

Out of scope for this client:

- No quantisation / model-loading orchestration. The local server is assumed
  to be already running and reachable at ``base_url``.
- No model auto-discovery. The model identifier is config-driven.
- No fallback handling here. Fallback policy lives in
  :class:`InferenceAuthority` (Phase 2a per-source routing) so it can be
  expressed explicitly per source.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .base import LLMResponse, ToolCallRequest, Usage, get_context_window
from .openai_client import OpenAIClient

logger = logging.getLogger(__name__)

# Marker prefix the authority + UI use to distinguish local-served calls.
# Phase R1 reuses this client for Runpod with provider_id="runpod"; the
# matching prefix is built dynamically (see _build_model_label).
LOCAL_MODEL_PREFIX = "local/"

# Default timeout (seconds) for local SDK calls. Local servers are usually
# fast but quantised models can spike — keep a generous ceiling so the agent
# loop's circuit-breaker still has room to engage on real hangs.
DEFAULT_LOCAL_TIMEOUT_SECONDS = 60.0

# Provider IDs that this OpenAI-compatible client is allowed to claim.
# "local" is the IAL Phase 2a default; "runpod" is added in Phase R1 so the
# same wire-protocol adapter can carry truthful Runpod attribution without a
# parallel client class. Reject anything else so a config typo cannot
# silently masquerade as another provider.
_ALLOWED_PROVIDER_IDS = frozenset({"local", "runpod"})


class LocalOpenAICompatibleClient(OpenAIClient):
    """OpenAI-compatible LLM client for local-shaped inference servers.

    Originally introduced for IAL Phase 2a to talk to Ollama / vLLM / LM
    Studio / llama.cpp etc. Phase R1 widened the contract so the same
    adapter can carry a different provider attribution (``"runpod"``)
    without a separate client class — the wire protocol is the same OpenAI
    Chat Completions schema in both cases. Provider identity is set at
    construction time via ``provider_id`` and is never inferred from
    request shape; this keeps attribution truthful in logs, events,
    telemetry, and SSE rebroadcasts.

    Construction is config-driven by the API runner — callers (indexer,
    summarizer, runners) never instantiate this directly; they always go
    through the
    :class:`~amof.orchestrator.llm.inference_authority.InferenceAuthority`.

    Args:
        base_url: Full URL to the OpenAI-compatible API root,
            e.g. ``http://ollama.amof-system.svc:11434/v1`` for an in-cluster
            Ollama or ``https://api.runpod.ai/v2/<endpoint>/openai/v1`` for
            a Runpod Serverless vLLM endpoint. Required.
        model: Model identifier the server expects, e.g.
            ``qwen2.5-coder:7b`` for Ollama or
            ``Qwen/Qwen2.5-Coder-7B-Instruct`` for vLLM. Required.
        api_key: Optional bearer token. Many local servers ignore this but
            still require *some* non-empty value because the OpenAI SDK
            refuses to send a request without one. Defaults to the literal
            string ``"local"`` which is harmless and explicit. Runpod
            requires a real API key.
        timeout: Per-request timeout in seconds. Defaults to
            :data:`DEFAULT_LOCAL_TIMEOUT_SECONDS`.
        model_label: Optional override for the ``Usage.model`` string the
            authority emits to telemetry. Defaults to
            ``<provider_id>/<host>/<model>``. Override only when an
            operator needs a custom dashboard label.
        provider_id: Truthful provider identity. ``"local"`` (default) for
            operator-hosted endpoints; ``"runpod"`` for Runpod Serverless.
            Must be in :data:`_ALLOWED_PROVIDER_IDS` — anything else is a
            config error and raises ``ValueError`` to prevent silent
            misattribution.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_LOCAL_TIMEOUT_SECONDS,
        model_label: Optional[str] = None,
        provider_id: str = "local",
    ) -> None:
        if not base_url:
            raise ValueError(
                "LocalOpenAICompatibleClient requires an explicit base_url"
            )
        if not model:
            raise ValueError(
                "LocalOpenAICompatibleClient requires an explicit model"
            )
        if provider_id not in _ALLOWED_PROVIDER_IDS:
            raise ValueError(
                f"LocalOpenAICompatibleClient: provider_id={provider_id!r} "
                f"is not allowed; expected one of "
                f"{sorted(_ALLOWED_PROVIDER_IDS)}"
            )

        self._raw_model = model
        # We bypass canonical_model_name on purpose — the model identifier
        # is whatever the server expects (e.g. an Ollama tag, a Runpod
        # template model name) and must be sent verbatim. Cloud-style
        # provider/model splitting does not apply.
        self._model = model
        self._provider = provider_id
        # OpenAI SDK requires a non-empty api_key even when the server does
        # not enforce auth; "local" is an explicit, audit-friendly
        # placeholder so logs do not show empty-string credentials. For
        # Runpod the caller must always pass a real key — we still tolerate
        # the SDK requirement here and let the upstream HTTP 401 surface
        # truthfully as a ProviderError(provider="runpod", failure_class=
        # "auth").
        self._api_key = api_key or os.environ.get("AMOF_LOCAL_LLM_API_KEY", "") or "local"
        self._base_url = base_url
        self._client = None
        # Keep both AMOF-level and SDK-level retries explicit for local-shaped
        # providers. The OpenAI SDK defaults to retrying internally, which can
        # turn one local timeout into several minutes of wall time.
        self._max_retries = 0
        self._sdk_max_retries = 0
        self._reasoning_effort = None
        self._timeout = float(timeout)
        if self._timeout <= 0:
            raise ValueError("LocalOpenAICompatibleClient timeout must be a positive number")
        self._model_label = model_label or self._build_model_label(
            provider_id, base_url, model
        )
        # Some local OpenAI-compatible stacks surface would-be tool calls as
        # plain JSON in ``message.content`` instead of populating
        # ``message.tool_calls``. We only consider the strict textual fallback
        # when this instance actually sent tool schemas on the request.
        self._allow_textual_tool_fallback = False

    @staticmethod
    def _build_model_label(provider_id: str, base_url: str, model: str) -> str:
        """Return ``<provider_id>/<host>/<model>`` for unambiguous attribution."""
        try:
            parsed = urlparse(base_url)
            host = parsed.netloc or parsed.path or "unknown-host"
        except Exception:
            host = "unknown-host"
        return f"{provider_id}/{host}/{model}"

    def _get_client(self) -> Any:
        """Lazy-init the OpenAI SDK client pointed at the local base_url."""
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - depends on env
                raise ImportError(
                    "openai package not installed. Run: pip install openai"
                ) from exc

            kwargs: Dict[str, Any] = {
                "api_key": self._api_key,
                "base_url": self._base_url,
                "max_retries": self._sdk_max_retries,
            }
            # The SDK accepts a `timeout` kwarg; forward it so connection
            # hangs to a downed local server surface quickly instead of
            # blocking the agent loop.
            try:
                self._client = openai.OpenAI(timeout=self._timeout, **kwargs)
            except TypeError:
                # Older SDK versions without a `timeout` kwarg — fall back
                # to the default constructor.
                self._client = openai.OpenAI(**kwargs)
        return self._client

    def model_name(self) -> str:
        """Return the local model identifier (with ``local/`` prefix).

        The authority uses this for log/telemetry attribution; we expose the
        prefixed label so operator-facing surfaces can immediately tell that
        the call was served locally.
        """

        return self._model_label

    def _wrap_provider_error(self, exc: BaseException) -> Any:
        provider_error = super()._wrap_provider_error(exc)
        provider_error.args = (
            f"{provider_error.args[0]} "
            f"(provider={self._provider}, base_url={self._base_url}, "
            f"timeout_seconds={self._timeout:g}, sdk_max_retries={self._sdk_max_retries})",
        )
        return provider_error

    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        # Only enable the textual fallback on local runs that actually
        # provided tools. This keeps the scope local-only and avoids treating
        # arbitrary JSON final answers as tool intents.
        self._allow_textual_tool_fallback = self._provider == "local" and bool(tools)
        try:
            return super().chat(
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        finally:
            self._allow_textual_tool_fallback = False

    def context_window(self) -> int:
        """Best-effort context window for the configured local model.

        OpenAI-compatible local servers do not advertise this number, so we
        look it up via the shared registry and fall back to a conservative
        32k default — enough for indexer/summarizer payloads without
        promising a window we cannot keep.
        """

        try:
            cw = get_context_window(self._model)
            if cw and cw > 0:
                return cw
        except Exception:
            pass
        return 32_000

    def _build_usage(self, response: Any, latency_ms: int) -> Usage:
        """Build truthful usage metrics for an OpenAI-compatible call.

        Differences vs the cloud parent:

        - ``model`` is the prefixed provider label
          (``local/<host>/<model>`` or ``runpod/<host>/<model>``) so
          operators can see in the run timeline which provider served the
          call.
        - ``estimated_cost`` is **always** ``0.0``. We deliberately do not
          fall back to ``estimate_cost(...)`` (which would attach a fake
          cloud-equivalent price) because that would lie to the operator
          about real spend. For Runpod the real spend is per-second GPU
          billing, not per-token; surfacing it requires a separate
          billing-side integration that lives outside this client.
        """

        prompt_tokens = response.usage.prompt_tokens if response.usage else 0
        completion_tokens = response.usage.completion_tokens if response.usage else 0

        return Usage(
            model=self._model_label,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            estimated_cost=0.0,
            context_window=self.context_window(),
        )

    def _parse_response(self, response: Any, latency_ms: int) -> LLMResponse:
        parsed = super()._parse_response(response, latency_ms)
        if (
            parsed.tool_calls
            or not self._allow_textual_tool_fallback
            or self._provider != "local"
        ):
            return parsed

        content = parsed.text
        fallback_tool_calls = self._parse_textual_tool_calls(content)
        if not fallback_tool_calls:
            return parsed

        parsed.tool_calls = fallback_tool_calls
        parsed.text = None
        return parsed

    @staticmethod
    def _parse_textual_tool_calls(
        content: Optional[str],
    ) -> Optional[list[ToolCallRequest]]:
        """Strictly parse a JSON-only textual tool call fallback.

        Local Qwen via Ollama currently returns a would-be tool call as
        ``message.content`` JSON instead of native ``message.tool_calls``.
        This fallback is intentionally narrow:

        - content must be a single JSON object or array
        - every object must contain exactly a string ``name`` and dict
          ``arguments`` payload
        - anything else returns ``None`` so prose or mixed output stays text
        """

        if not isinstance(content, str):
            return None

        stripped = content.strip()
        if not stripped or stripped[0] not in "{[":
            return None

        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None

        if isinstance(payload, dict):
            items = [payload]
        elif isinstance(payload, list):
            items = payload
        else:
            return None

        tool_calls: list[ToolCallRequest] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                return None
            name = item.get("name")
            arguments = item.get("arguments")
            if not isinstance(name, str) or not name.strip():
                return None
            if not isinstance(arguments, dict):
                return None
            tool_id = item.get("id")
            if not isinstance(tool_id, str) or not tool_id.strip():
                tool_id = f"textual-tool-{index}"
            tool_calls.append(
                ToolCallRequest(id=tool_id, name=name.strip(), arguments=arguments)
            )

        return tool_calls or None

    def supports_prefill(self) -> bool:
        # Most local OpenAI-compatible servers don't support assistant
        # prefill in the same way Anthropic does. Default to False so the
        # caller does not assume prefill semantics.
        return False

    def is_thinking_model(self) -> bool:
        # Local quantised reasoning models exist but we don't auto-detect.
        # Opt-in only via explicit caller logic.
        return False
