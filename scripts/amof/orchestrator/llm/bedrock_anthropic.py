"""Anthropic Claude via AWS Bedrock.

This is a narrow provider adapter for the meeting slice: it reuses the
Anthropic message/tool path, but authenticates through AWS Bedrock using the
operator's local AWS profile + region instead of an Anthropic API key.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .anthropic import AnthropicClient, DEFAULT_THINKING_BUDGET, _resolve_ca_bundle
from .base import PROVIDER_FAILURE_NETWORK, ProviderError


class BedrockAnthropicClient(AnthropicClient):
    """Anthropic-compatible client backed by AWS Bedrock."""

    def __init__(
        self,
        *,
        model: str,
        aws_profile: Optional[str] = None,
        aws_region: Optional[str] = None,
        max_retries: int = 3,
        thinking_budget: Optional[int] = None,
    ) -> None:
        self._aws_profile = (
            aws_profile
            or os.environ.get("AMOF_BEDROCK_AWS_PROFILE")
            or os.environ.get("AWS_PROFILE")
            or None
        )
        self._aws_region = (
            aws_region
            or os.environ.get("AMOF_BEDROCK_REGION")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or None
        )
        self._model = model or os.environ.get("AMOF_BEDROCK_MODEL", "")
        self._client = None
        self._max_retries = max_retries
        self._provider = "bedrock"

        env_budget = os.environ.get("AMOF_THINKING_BUDGET")
        self._thinking_budget = (
            thinking_budget
            or (int(env_budget) if env_budget else None)
            or DEFAULT_THINKING_BUDGET
        )

        if not self._model:
            raise ValueError("Bedrock model id is required.")
        if not self._aws_region:
            raise ValueError(
                "AWS region not set for Bedrock. Export AWS_REGION or AMOF_BEDROCK_REGION."
            )

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ImportError(
                    "anthropic package not installed. Run: pip install anthropic"
                ) from exc

            kwargs = {"aws_region": self._aws_region, "max_retries": self._max_retries}
            if self._aws_profile:
                kwargs["aws_profile"] = self._aws_profile
            ca_bundle = _resolve_ca_bundle()
            if ca_bundle:
                import httpx

                kwargs["http_client"] = httpx.Client(verify=ca_bundle)
            self._client = anthropic.AnthropicBedrock(**kwargs)
        return self._client

    def _wrap_provider_error(self, exc: BaseException) -> ProviderError:
        wrapped = super()._wrap_provider_error(exc)
        if wrapped.failure_class != PROVIDER_FAILURE_NETWORK:
            return wrapped
        return ProviderError(
            provider=wrapped.provider,
            message=(
                f"{exc}. Corporate TLS note: set SSL_CERT_FILE or REQUESTS_CA_BUNDLE for "
                "Anthropic/httpx trust, and set AWS_CA_BUNDLE for AWS SDK trust when needed."
            ),
            status_code=wrapped.status_code,
            failure_class=wrapped.failure_class,
            original=wrapped.original,
        )
