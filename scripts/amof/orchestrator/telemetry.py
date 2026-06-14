"""Telemetry tracking for agent sessions.

Records per-call and cumulative metrics: tokens, cost, latency, context usage.
Tracks tool-level success rates, usage frequency, and context growth over time.
Tracks per-model-tier costs for model ladder optimization.
Displayable via `amof status` (PRD section 9).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .llm.base import Usage, normalized_usage_cost


@dataclass
class CallMetrics:
    """Metrics for a single LLM call."""

    timestamp: float
    model: str
    prompt_tokens: int
    completion_tokens: int
    context_used_pct: float
    estimated_cost: float
    latency_ms: int
    tool_calls_count: int = 0
    tier: str = "default"  # model tier: fast/standard/strong/default
    # Mini Ultra Plan 2 / Phase L2: truthful upstream provider attribution
    # (anthropic / openai / openrouter / local / runpod). Default ``""`` so
    # legacy CLI emissions that bypass the InferenceAuthority do not have
    # to backfill — when the authority is wired (live API path) it always
    # supplies the provider extracted from the resolved client.
    provider: str = ""
    cost_status: str = "observed"

    def summary_line(self) -> str:
        provider_bit = f"{self.provider}/" if self.provider else ""
        cost_text = (
            f"${self.estimated_cost:.4f}"
            if self.cost_status == "observed"
            else "cost=unknown"
        )
        return (
            f"[{self.tier}:{provider_bit}{self.model}] "
            f"{self.prompt_tokens}+{self.completion_tokens} tokens "
            f"({self.context_used_pct:.1f}% ctx) {cost_text} "
            f"{self.latency_ms}ms"
        )


@dataclass
class ToolMetrics:
    """Aggregated metrics for a single tool."""

    calls: int = 0
    successes: int = 0
    failures: int = 0
    advisory_blocks: int = 0
    total_duration_ms: int = 0

    @property
    def success_rate(self) -> float:
        return (self.successes / self.calls * 100) if self.calls > 0 else 0.0

    @property
    def avg_duration_ms(self) -> int:
        return self.total_duration_ms // self.calls if self.calls > 0 else 0


@dataclass
class ContextSnapshot:
    """A point-in-time snapshot of context usage."""

    timestamp: float
    estimated_tokens: int
    context_used_pct: float
    message_count: int


@dataclass
class TierMetrics:
    """Aggregated cost metrics for a model tier."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    model_name: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class SessionTelemetry:
    """Cumulative telemetry for an agent session."""

    calls: List[CallMetrics] = field(default_factory=list)
    session_start: float = field(default_factory=time.time)
    max_cost: Optional[float] = None  # cost ceiling from config
    tool_metrics: Dict[str, ToolMetrics] = field(default_factory=lambda: defaultdict(ToolMetrics))
    inspected_files: List[str] = field(default_factory=list)
    context_history: List[ContextSnapshot] = field(default_factory=list)
    tier_metrics: Dict[str, TierMetrics] = field(default_factory=lambda: defaultdict(TierMetrics))
    _summarization_cost: float = 0.0
    _restored_cost: float = 0.0  # baseline from resumed session (no per-call history)

    # Enhanced tracking
    retry_count: int = 0
    timeout_count: int = 0
    empty_response_count: int = 0
    context_summarization_count: int = 0
    failure_categories: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Guardrail tracking
    guardrail_hard_blocks: int = 0
    guardrail_sensitive_blocks: int = 0
    guardrail_manifest_blocks: int = 0
    # Prompt caching tracking
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    # Budget early warnings
    warning_thresholds: List[float] = field(default_factory=lambda: [0.50, 0.75, 0.90])
    _budget_warnings_emitted: set = field(default_factory=set)

    # Per-agent cost isolation
    agent_costs: Dict[str, float] = field(default_factory=lambda: defaultdict(float))

    # Latency tracking (raw values for percentile calculation)
    _latency_values: List[int] = field(default_factory=list)
    _unknown_cost_calls: int = 0

    def record(self, metrics: CallMetrics) -> None:
        """Record metrics from an LLM call."""
        self.calls.append(metrics)
        self._latency_values.append(metrics.latency_ms)
        # Track context growth
        self.context_history.append(ContextSnapshot(
            timestamp=metrics.timestamp,
            estimated_tokens=metrics.prompt_tokens,
            context_used_pct=metrics.context_used_pct,
            message_count=len(self.calls),
        ))
        # Track per-tier costs
        tm = self.tier_metrics[metrics.tier]
        tm.calls += 1
        tm.input_tokens += metrics.prompt_tokens
        tm.output_tokens += metrics.completion_tokens
        if metrics.cost_status == "observed":
            tm.cost += metrics.estimated_cost
        else:
            self._unknown_cost_calls += 1
        tm.model_name = metrics.model

    def record_from_usage(
        self,
        usage,
        tier: str = "default",
        provider: str = "",
    ) -> CallMetrics:
        """Record metrics from an LLMResponse.usage object.

        Args:
            usage: The provider-reported usage object.
            tier: Model tier label (fast/standard/strong/default).
            provider: Truthful upstream provider id supplied by the
                InferenceAuthority (e.g. "anthropic", "openrouter",
                "local", "runpod"). Default ``""`` for legacy CLI callers
                that record telemetry directly without going through the
                authority — they do not have a single resolved provider
                handy and historically have not surfaced one.
        """
        metrics = CallMetrics(
            timestamp=time.time(),
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            context_used_pct=usage.context_used_pct,
            estimated_cost=0.0,
            latency_ms=usage.latency_ms,
            tier=tier,
            provider=provider,
            cost_status="unknown",
        )
        metrics.cost_status, normalized_cost = normalized_usage_cost(usage)
        metrics.estimated_cost = normalized_cost or 0.0
        self.record(metrics)
        return metrics

    def record_tool_call(
        self,
        tool_name: str,
        success: bool,
        duration_ms: int,
        metadata: Optional[Dict[str, object]] = None,
    ) -> None:
        """Record metrics from a tool execution."""
        tm = self.tool_metrics[tool_name]
        tm.calls += 1
        if success:
            tm.successes += 1
        elif metadata and metadata.get("advisory"):
            # Advisory guardrail redirect: the model is told to use a different
            # allowed approach. It is neither a success nor a genuine failure, so
            # it is bucketed separately and excluded from the fatal failure count.
            tm.advisory_blocks += 1
        else:
            tm.failures += 1
        tm.total_duration_ms += duration_ms
        if success and metadata:
            inspected = metadata.get("inspected_files")
            if isinstance(inspected, list):
                for path in inspected:
                    if isinstance(path, str) and path not in self.inspected_files:
                        self.inspected_files.append(path)

    def record_summarization_cost(self, cumulative_cost: float) -> None:
        """Record cumulative summarization cost (from ContextSummarizer)."""
        self._summarization_cost = cumulative_cost
    
    def record_retry(self) -> None:
        """Record an API retry attempt."""
        self.retry_count += 1
    
    def record_timeout(self) -> None:
        """Record an API timeout."""
        self.timeout_count += 1
    
    def record_empty_response(self) -> None:
        """Record an empty LLM response."""
        self.empty_response_count += 1
    
    def record_context_summarization(self) -> None:
        """Record a context summarization event."""
        self.context_summarization_count += 1
    
    def record_failure(self, category: str) -> None:
        """Record a failure by category (api, tool, timeout, etc.)."""
        self.failure_categories[category] += 1

    def record_guardrail_block(self, block_type: str) -> None:
        """Record a guardrail violation. block_type: hard, sensitive, manifest."""
        if block_type == "hard":
            self.guardrail_hard_blocks += 1
        elif block_type == "sensitive":
            self.guardrail_sensitive_blocks += 1
        elif block_type == "manifest":
            self.guardrail_manifest_blocks += 1

    def record_cache_usage(self, creation_tokens: int = 0, read_tokens: int = 0) -> None:
        """Record prompt cache token usage from an LLM call."""
        self.cache_creation_tokens += creation_tokens
        self.cache_read_tokens += read_tokens

    def check_budget_warning(self) -> Optional[str]:
        """Check if a new budget warning threshold has been crossed.

        Returns a warning message if a new threshold was crossed, None otherwise.
        Called after each LLM call in the agent loop.
        """
        if self.max_cost is None or self.max_cost <= 0:
            return None

        current_pct = self.total_cost / self.max_cost

        for threshold in sorted(self.warning_thresholds):
            if current_pct >= threshold and threshold not in self._budget_warnings_emitted:
                self._budget_warnings_emitted.add(threshold)
                return (
                    f"Budget warning: {threshold:.0%} used "
                    f"(${self.total_cost:.4f} of ${self.max_cost:.2f})"
                )

        return None

    def record_agent_cost(self, agent_name: str, cost: float) -> None:
        """Record cost for a specific agent (master or runner).

        Args:
            agent_name: e.g. "master" or "runner:k8s"
            cost: Cost in USD for this call.
        """
        self.agent_costs[agent_name] += cost

    def latency_percentile(self, p: float) -> float:
        """Compute latency percentile (e.g. p=0.50 for P50, p=0.95 for P95).

        Returns latency in ms at the given percentile, or 0.0 if no data.
        """
        if not self._latency_values:
            return 0.0
        sorted_vals = sorted(self._latency_values)
        idx = int(p * (len(sorted_vals) - 1))
        return float(sorted_vals[idx])

    @property
    def latency_p50(self) -> float:
        """Median latency in ms."""
        return self.latency_percentile(0.50)

    @property
    def latency_p95(self) -> float:
        """95th percentile latency in ms."""
        return self.latency_percentile(0.95)

    def extend_budget(self, additional: float) -> None:
        """Extend the cost budget without resetting stats.

        Args:
            additional: Amount in USD to add to the budget.
        """
        if self.max_cost is None:
            self.max_cost = additional
        else:
            self.max_cost += additional

    def save(self, path: Path) -> None:
        """Save telemetry state to a JSON file for resume capability."""
        data = self.to_dict()
        data["_max_cost"] = self.max_cost
        data["_session_start"] = self.session_start
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SessionTelemetry":
        """Load telemetry state from a saved JSON file.

        Note: This restores the summary metrics but not individual CallMetrics.
        Useful for resume to get the correct cost baseline.
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        telemetry = cls(max_cost=data.get("_max_cost"))
        telemetry.session_start = data.get("_session_start", time.time())
        # We can't fully reconstruct calls, but we can load aggregate counters
        telemetry.retry_count = data.get("reliability", {}).get("retries", 0)
        telemetry.timeout_count = data.get("reliability", {}).get("timeouts", 0)
        telemetry.empty_response_count = data.get("reliability", {}).get("empty_responses", 0)
        if "failure_categories" in data:
            telemetry.failure_categories = defaultdict(int, data["failure_categories"])
        if "prompt_cache" in data:
            telemetry.cache_creation_tokens = data["prompt_cache"].get("creation_tokens", 0)
            telemetry.cache_read_tokens = data["prompt_cache"].get("read_tokens", 0)
        telemetry._unknown_cost_calls = int(data.get("unknown_cost_calls") or 0)
        inspected_files = data.get("inspected_files", {}).get("files", [])
        if isinstance(inspected_files, list):
            telemetry.inspected_files = [path for path in inspected_files if isinstance(path, str)]
        total_cost = data.get("total_cost")
        telemetry._restored_cost = float(total_cost or 0.0) if total_cost is not None else 0.0
        return telemetry

    @property
    def total_guardrail_blocks(self) -> int:
        return self.guardrail_hard_blocks + self.guardrail_sensitive_blocks + self.guardrail_manifest_blocks

    @property
    def cache_hit_rate(self) -> float:
        """Percentage of cacheable tokens that were cache hits."""
        total = self.cache_creation_tokens + self.cache_read_tokens
        if total == 0:
            return 0.0
        return (self.cache_read_tokens / total) * 100

    @property
    def total_calls(self) -> int:
        return len(self.calls)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.calls)

    @property
    def total_completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.calls)

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    @property
    def total_cost(self) -> float:
        return (
            sum(c.estimated_cost for c in self.calls if c.cost_status == "observed")
            + self._summarization_cost
            + self._restored_cost
        )

    @property
    def unknown_cost_calls(self) -> int:
        return self._unknown_cost_calls

    @property
    def total_latency_ms(self) -> int:
        return sum(c.latency_ms for c in self.calls)

    @property
    def avg_latency_ms(self) -> int:
        if not self.calls:
            return 0
        return self.total_latency_ms // len(self.calls)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.session_start

    @property
    def cost_exceeded(self) -> bool:
        if self.max_cost is None:
            return False
        return self.total_cost >= self.max_cost

    @property
    def peak_context_pct(self) -> float:
        """Peak context window usage across all calls."""
        if not self.context_history:
            return 0.0
        return max(s.context_used_pct for s in self.context_history)

    @property
    def total_tool_calls(self) -> int:
        """Total number of tool executions."""
        return sum(tm.calls for tm in self.tool_metrics.values())

    @property
    def tool_success_rate(self) -> float:
        """Overall tool success rate."""
        total = self.total_tool_calls
        if total == 0:
            return 100.0
        successes = sum(tm.successes for tm in self.tool_metrics.values())
        return (successes / total) * 100

    @property
    def tokens_per_dollar(self) -> float:
        """Total tokens processed per dollar spent. Higher = more efficient."""
        cost = self.total_cost
        if cost <= 0:
            return 0.0
        return self.total_tokens / cost

    @property
    def cost_per_tool_call(self) -> float:
        """Average LLM cost per tool execution (proxy for efficiency)."""
        tools = self.total_tool_calls
        if tools <= 0:
            return 0.0
        return self.total_cost / tools

    @property
    def avg_tools_per_llm_call(self) -> float:
        """Average tool calls executed per LLM round-trip. Higher = better batching."""
        if self.total_calls == 0:
            return 0.0
        return self.total_tool_calls / self.total_calls

    def top_tools(self, n: int = 5) -> List[Tuple[str, ToolMetrics]]:
        """Get the N most-used tools."""
        sorted_tools = sorted(self.tool_metrics.items(), key=lambda x: x[1].calls, reverse=True)
        return sorted_tools[:n]

    def summary(self) -> str:
        """Human-readable session summary."""
        elapsed = self.elapsed_seconds
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        latency_line = f"  Avg latency:  {self.avg_latency_ms}ms"
        if len(self._latency_values) >= 2:
            latency_line += f"  P50={self.latency_p50:.0f}ms  P95={self.latency_p95:.0f}ms"

        lines = [
            f"Session Telemetry ({minutes}m {seconds}s)",
            f"  LLM calls:    {self.total_calls}",
            f"  Tokens:       {self.total_prompt_tokens:,} in + {self.total_completion_tokens:,} out = {self.total_tokens:,} total",
            f"  Cost:         ${self.total_cost:.4f}",
            latency_line,
            f"  Peak context: {self.peak_context_pct:.1f}%",
        ]
        if self.max_cost is not None:
            pct = (self.total_cost / self.max_cost) * 100 if self.max_cost > 0 else 0
            lines.append(f"  Budget used:  {pct:.1f}% (limit: ${self.max_cost:.2f})")
        if self.unknown_cost_calls > 0:
            lines.append(
                f"  Cost truth:   {self.unknown_cost_calls} call(s) reported unknown provider cost"
            )

        # Per-tier cost breakdown
        if len(self.tier_metrics) > 1 or (self.tier_metrics and "default" not in self.tier_metrics):
            lines.append("  Model tiers:")
            for tier, tm in sorted(self.tier_metrics.items()):
                lines.append(
                    f"    {tier:10s} {tm.calls:3d} calls  "
                    f"{tm.input_tokens:,}+{tm.output_tokens:,}tok  "
                    f"${tm.cost:.4f}"
                    + (f"  ({tm.model_name})" if tm.model_name else "")
                )

        # Summarization costs
        if self._summarization_cost > 0:
            lines.append(f"  Summarization: ${self._summarization_cost:.4f} ({self.context_summarization_count} times)")
        
        # Reliability metrics
        if self.retry_count > 0 or self.timeout_count > 0 or self.empty_response_count > 0:
            lines.append("  Reliability:")
            if self.retry_count > 0:
                lines.append(f"    Retries:        {self.retry_count}")
            if self.timeout_count > 0:
                lines.append(f"    Timeouts:       {self.timeout_count}")
            if self.empty_response_count > 0:
                lines.append(f"    Empty responses: {self.empty_response_count}")
        
        # Failure breakdown
        if self.failure_categories:
            lines.append("  Failures by type:")
            for category, count in sorted(self.failure_categories.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"    {category:15s} {count}")

        # Guardrail blocks
        if self.total_guardrail_blocks > 0:
            lines.append(f"  Guardrails:   {self.total_guardrail_blocks} blocked")
            if self.guardrail_hard_blocks > 0:
                lines.append(f"    Hard blocks:   {self.guardrail_hard_blocks}")
            if self.guardrail_sensitive_blocks > 0:
                lines.append(f"    Sensitive:     {self.guardrail_sensitive_blocks}")
            if self.guardrail_manifest_blocks > 0:
                lines.append(f"    Manifest:      {self.guardrail_manifest_blocks}")

        # Prompt caching
        if self.cache_read_tokens > 0 or self.cache_creation_tokens > 0:
            lines.append(f"  Prompt cache: {self.cache_hit_rate:.0f}% hit rate")
            lines.append(f"    Written:    {self.cache_creation_tokens:,} tokens")
            lines.append(f"    Read:       {self.cache_read_tokens:,} tokens")

        # Per-agent cost breakdown
        if self.agent_costs:
            lines.append("  Agent costs:")
            for agent_name, cost in sorted(self.agent_costs.items()):
                lines.append(f"    {agent_name:20s} ${cost:.4f}")

        # Efficiency metrics
        if self.total_calls > 0 and self.total_tool_calls > 0:
            lines.append(f"  Efficiency:")
            lines.append(f"    Tools/LLM call: {self.avg_tools_per_llm_call:.1f}")
            lines.append(f"    $/tool call:    ${self.cost_per_tool_call:.4f}")
            lines.append(f"    Tok/$:          {self.tokens_per_dollar:,.0f}")

        # Tool metrics
        if self.tool_metrics:
            lines.append(f"  Tool calls:   {self.total_tool_calls} ({self.tool_success_rate:.0f}% success)")
            for name, tm in self.top_tools(5):
                lines.append(f"    {name:15s} {tm.calls:3d} calls  {tm.success_rate:5.1f}% ok  avg {tm.avg_duration_ms}ms")

        if self.inspected_files:
            lines.append(f"  Inspected files: {len(self.inspected_files)}")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize for event log."""
        observed_cost = round(self.total_cost, 6)
        cost_status = (
            "unknown"
            if self.unknown_cost_calls > 0 and observed_cost == 0.0
            else "observed"
        )
        result = {
            "total_calls": self.total_calls,
            "total_tokens": {"in": self.total_prompt_tokens, "out": self.total_completion_tokens},
            "total_cost": None if cost_status == "unknown" else observed_cost,
            "cost_status": cost_status,
            "total_latency_ms": self.total_latency_ms,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "peak_context_pct": round(self.peak_context_pct, 1),
            "unknown_cost_calls": self.unknown_cost_calls,
        }
        if self.tier_metrics:
            result["tiers"] = {
                tier: {
                    "calls": tm.calls,
                    "tokens": tm.total_tokens,
                    "cost": round(tm.cost, 6),
                    "model": tm.model_name,
                }
                for tier, tm in self.tier_metrics.items()
            }
        if self._summarization_cost > 0:
            result["summarization_cost"] = round(self._summarization_cost, 6)
            result["summarization_count"] = self.context_summarization_count
        
        # Reliability metrics
        if self.retry_count > 0 or self.timeout_count > 0 or self.empty_response_count > 0:
            result["reliability"] = {
                "retries": self.retry_count,
                "timeouts": self.timeout_count,
                "empty_responses": self.empty_response_count,
            }
        
        if self.failure_categories:
            result["failure_categories"] = dict(self.failure_categories)
        
        if self.tool_metrics:
            result["tools"] = {
                name: {
                    "calls": tm.calls,
                    "success_rate": round(tm.success_rate, 1),
                    "avg_duration_ms": tm.avg_duration_ms,
                }
                for name, tm in self.tool_metrics.items()
            }
        if self.inspected_files:
            result["inspected_files"] = {
                "count": len(self.inspected_files),
                "files": list(self.inspected_files),
            }
        # Efficiency metrics
        if self.total_calls > 0:
            result["efficiency"] = {
                "avg_tools_per_llm_call": round(self.avg_tools_per_llm_call, 2),
                "cost_per_tool_call": round(self.cost_per_tool_call, 6),
                "tokens_per_dollar": round(self.tokens_per_dollar, 0),
            }
        # Guardrails
        if self.total_guardrail_blocks > 0:
            result["guardrails"] = {
                "hard_blocks": self.guardrail_hard_blocks,
                "sensitive_blocks": self.guardrail_sensitive_blocks,
                "manifest_blocks": self.guardrail_manifest_blocks,
            }
        # Prompt caching
        if self.cache_read_tokens > 0 or self.cache_creation_tokens > 0:
            result["prompt_cache"] = {
                "creation_tokens": self.cache_creation_tokens,
                "read_tokens": self.cache_read_tokens,
                "hit_rate_pct": round(self.cache_hit_rate, 1),
            }
        # Latency percentiles
        if len(self._latency_values) >= 2:
            result["latency_percentiles"] = {
                "p50_ms": round(self.latency_p50),
                "p95_ms": round(self.latency_p95),
            }
        # Per-agent cost breakdown
        if self.agent_costs:
            result["agent_costs"] = {k: round(v, 6) for k, v in self.agent_costs.items()}
        # Budget warnings emitted
        if self._budget_warnings_emitted:
            result["budget_warnings_emitted"] = sorted(self._budget_warnings_emitted)
        return result
