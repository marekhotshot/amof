"""Runner factory — spawns specialized runner agents from config.

Runners are domain-specific agents (k8s, helm, jenkins, debug, code) that
the master agent delegates to via the DelegateTool. Each runner gets:

1. A focused system prompt from prompts/runners/<name>.md
2. A filtered tool registry (only the tools it needs)
3. A clean session (no master context leaks in)
4. A ModelRouter (shared tiers, independent state tracking)
5. Its own telemetry (rolled up to parent after completion)

Runner definitions are config-driven via .amof/rules/runners.yaml — no
tool lists or model tiers are hardcoded in Python.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .agent import Agent
from .events import EventLog
from .llm.base import LLMClient
from .model_router import ModelRouter
from .prompt_loader import load_prompt
from .session import Session
from .telemetry import SessionTelemetry
from .plan_execute_control import (
    FATAL_STOP_REASONS,
    is_read_only_repository_inspection,
    normalize_subtask_failure,
)
from .tools.base import Guardrails, Tool, ToolRegistry
from .tool_failure_semantics import (
    analyze_tool_call_events,
    enrich_repo_inspection_response,
    repo_inspection_runner_tools,
    repo_inspection_task_guidance,
)

logger = logging.getLogger(__name__)
PACKAGED_CODE_RUNNER_PROMPT = (
    "You are AMOF's bounded public code runner. Make only the minimal file edits "
    "required by the assigned subtask. Read files before editing. Use only the "
    "provided tools; Shell, Delete, and GitCheckpoint are intentionally unavailable. "
    "If a missing read-only capability is needed, use ToolProposal with bounded "
    "metadata; never request arbitrary shell. "
    "Do not commit or push. Preserve existing file content. Never rewrite a whole "
    "existing file for small edits, additions, docs-only edits, or bounded changes. "
    "Before editing existing files, inspect them first. Prefer InspectFiles when "
    "you need related files such as app.py and tests/test_app.py in one call. Never "
    "invent old_string or anchor_string values; copy them exactly from Read or "
    "InspectFiles output. Use InsertAfter for small additions after a unique anchor, "
    "StrReplace for targeted replacement, "
    "and use Write only to create new files unless the top-level task explicitly asks to "
    "rewrite or overwrite the entire file. If a docs insertion point is ambiguous, "
    "fail or ask in interactive mode rather than replacing the document with generic "
    "content. For docs edits, prefer the smallest reviewable diff and insert exact "
    "user-provided text as-is unless asked to rewrite it. When a task says to add "
    "exactly a section, copy that heading and body verbatim; do not paraphrase, "
    "rename headings, add checklists, or invent alternate wording. Report exactly "
    "which files changed."
)

PUBLIC_DEFAULT_RUNNERS_CONFIG: Dict[str, Any] = {
    "runners": {
        "code": {
            "prompt": "__packaged__/runners/code.md",
            "tools": ["Read", "InspectFiles", "ToolProposal", "Write", "StrReplace", "InsertAfter", "Glob", "LS", "ReadLints"],
            "description": "Safe public code-edit runner without shell, delete, checkpoint, or git push tools.",
            "default_tier": "standard",
            "max_iterations": 20,
        },
    },
}


@dataclass
class RunnerConfig:
    """Configuration for a single runner type, loaded from runners.yaml."""

    name: str
    prompt_path: str  # relative to workspace root, e.g. "prompts/runners/k8s.md"
    tool_names: List[str]
    description: str
    default_tier: str = "standard"
    max_iterations: int = 50

    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> RunnerConfig:
        return cls(
            name=name,
            prompt_path=data.get("prompt", f"prompts/runners/{name}.md"),
            tool_names=data.get("tools", []),
            description=data.get("description", f"{name} runner"),
            default_tier=data.get("default_tier", "standard"),
            max_iterations=data.get("max_iterations", 50),
        )


@dataclass
class RunnerResult:
    """Result returned by a runner after execution."""

    runner_name: str
    success: bool
    response: str
    stop_reason: str
    telemetry: SessionTelemetry
    checkpoint_count: int = 0  # for code runner
    failed_tool_calls: int = 0
    failed_write_tool_calls: int = 0
    runner_event_log_path: str | None = None
    tool_failures: List[Dict[str, Any]] = field(default_factory=list)
    diagnostic_warnings: List[str] = field(default_factory=list)
    primary_failure: Dict[str, Any] | None = None


class RunnerFactory:
    """Creates and runs specialized runner agents from config.

    Usage:
        factory = RunnerFactory.from_config(config_path, ...)
        result = factory.run_runner("k8s", task="check pod health", context="...")
    """

    def __init__(
        self,
        runners: Dict[str, RunnerConfig],
        model_clients: Dict[str, LLMClient],
        parent_tools: ToolRegistry,
        guardrails: Optional[Guardrails] = None,
        workspace_root: Optional[Path] = None,
        max_cost_per_runner: float = 2.00,
        verbose: bool = False,
        cascade: Optional[List[str]] = None,
        inference_authority: Optional[Any] = None,
        runner_local_clients: Optional[Dict[str, LLMClient]] = None,
    ):
        """Initialize the factory.

        Args:
            runners: Runner configs keyed by name.
            model_clients: Tier -> LLMClient mapping (shared with master).
            parent_tools: Master's tool registry (runners get filtered subsets).
            guardrails: Guardrail config (inherited by runners).
            workspace_root: Workspace root for prompt loading.
            max_cost_per_runner: Cost cap per runner invocation.
            verbose: Show per-iteration tool call details.
            cascade: Optional cascade of model identifiers for the worker.
            inference_authority: Mini Ultra Plan 2 / Phase L2 — the run's
                shared :class:`InferenceAuthority`. When supplied, each
                runner Agent is constructed with a sibling authority via
                ``inference_authority.with_source(f"runner:{name}")`` so
                per-call telemetry / events / SSE rebroadcasts attribute
                to ``runner:<name>`` end to end. When ``None`` (CLI
                back-compat), runners use the legacy in-Agent telemetry
                emission and source attribution defaults to ``"master"``.
            runner_local_clients: Mini Ultra Plan 2 / Phase L2 opt-in
                map of ``{runner_name: LLMClient}``. When a runner is
                spawned and its name is present here, the local client
                replaces ``model_clients["standard"]`` and ``["fast"]``
                for that runner only. Other runners keep the unchanged
                cloud cascade. Empty/None = no override (default).
        """
        self._runners = runners
        self._model_clients = model_clients
        self._parent_tools = parent_tools
        self._guardrails = guardrails
        self._workspace_root = workspace_root or Path.cwd()
        self._max_cost_per_runner = max_cost_per_runner
        self._verbose = verbose
        self._cascade = cascade
        self._inference_authority = inference_authority
        self._runner_local_clients: Dict[str, LLMClient] = (
            dict(runner_local_clients) if runner_local_clients else {}
        )

    @classmethod
    def from_config(
        cls,
        config_path: Path,
        model_clients: Dict[str, LLMClient],
        parent_tools: ToolRegistry,
        guardrails: Optional[Guardrails] = None,
        workspace_root: Optional[Path] = None,
        max_cost_per_runner: float = 2.00,
        verbose: bool = False,
        cascade: Optional[List[str]] = None,
        inference_authority: Optional[Any] = None,
        runner_local_clients: Optional[Dict[str, LLMClient]] = None,
        default_config: Optional[Dict[str, Any]] = None,
    ) -> RunnerFactory:
        """Load runner definitions from a YAML config file.

        Args:
            config_path: Path to runners.yaml.
            inference_authority: see :meth:`__init__`.
            runner_local_clients: see :meth:`__init__`.
        """
        runners: Dict[str, RunnerConfig] = {}

        if config_path.is_file():
            with open(config_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        elif default_config is not None:
            data = default_config
            logger.info("Runners config not found at %s; using packaged public defaults.", config_path)
        else:
            data = {}
            logger.warning("Runners config not found: %s", config_path)

        for name, rdata in data.get("runners", {}).items():
            if isinstance(rdata, dict):
                runners[name] = RunnerConfig.from_dict(name, rdata)
                logger.debug("Loaded runner config: %s", name)

        return cls(
            runners=runners,
            model_clients=model_clients,
            parent_tools=parent_tools,
            guardrails=guardrails,
            workspace_root=workspace_root,
            max_cost_per_runner=max_cost_per_runner,
            verbose=verbose,
            cascade=cascade,
            inference_authority=inference_authority,
            runner_local_clients=runner_local_clients,
        )

    @property
    def available_runners(self) -> Dict[str, RunnerConfig]:
        """All configured runner types."""
        return dict(self._runners)

    @property
    def runner_names(self) -> List[str]:
        """Names of all configured runners (for DelegateTool enum)."""
        return list(self._runners.keys())

    def run_runner(
        self,
        name: str,
        task: str,
        context: Optional[str] = None,
        parent_telemetry: Optional[SessionTelemetry] = None,
    ) -> RunnerResult:
        """Spawn a runner agent, execute the task, and return the result.

        Args:
            name: Runner type (must be in available_runners).
            task: The task description for the runner.
            context: Optional additional context from the master.
            parent_telemetry: Parent telemetry to roll up costs into.

        Returns:
            RunnerResult with the runner's response and telemetry.
        """
        if name not in self._runners:
            return RunnerResult(
                runner_name=name,
                success=False,
                response=f"Unknown runner type: '{name}'. Available: {', '.join(self._runners.keys())}",
                stop_reason="error",
                telemetry=SessionTelemetry(),
            )

        config = self._runners[name]
        task_scope_text = task if not context else f"{task}\n\n{context}"
        repo_inspection_mode = (
            name == "code"
            and is_read_only_repository_inspection(task_scope_text)
        )

        # 1. Load runner system prompt
        if config.prompt_path == "__packaged__/runners/code.md":
            system_prompt = PACKAGED_CODE_RUNNER_PROMPT
        else:
            prompt_name = config.prompt_path.replace("prompts/", "").replace(".md", "")
            try:
                system_prompt = load_prompt(
                    prompt_name,
                    prompts_dir=self._workspace_root / "prompts",
                    fallback=f"You are the {name} runner agent. Execute the task given to you.",
                )
            except Exception as e:
                logger.warning("Failed to load runner prompt %s: %s", prompt_name, e)
                system_prompt = f"You are the {name} runner agent. Execute the task given to you."
        if repo_inspection_mode:
            system_prompt = f"{system_prompt}\n\n{repo_inspection_task_guidance()}"

        # 2. Build filtered tool registry
        tool_names = config.tool_names
        if repo_inspection_mode:
            tool_names = repo_inspection_runner_tools(tool_names)
        filtered_registry = self._build_filtered_registry(
            tool_names,
            policy_source=f"runner:{name}",
        )

        # 3. Create fresh session and telemetry
        session = Session(mode="runner")
        session.metadata["runner_type"] = name
        telemetry = SessionTelemetry(max_cost=self._max_cost_per_runner)
        events = EventLog(session_id=session.id)

        # Mini Ultra Plan 2 / Phase L2: opt-in local-client override.
        # When ``runner_local_clients[name]`` is set (gated on
        # runtime_profile + allowed_runners back in agent_runner.py), the
        # local OpenAI-compatible client replaces the standard tier for
        # this runner only. Default routing for every other runner is
        # untouched. We also surface ``provider="local"`` (or whichever
        # provider_id the override carries) into telemetry/events because
        # the runner Agent uses the override directly without going
        # through the InferenceAuthority dispatch path.
        override_client = self._runner_local_clients.get(name)
        local_clients_for_router = dict(self._model_clients)
        if override_client is not None:
            local_clients_for_router["standard"] = override_client
            local_clients_for_router["fast"] = override_client
            logger.info(
                "RunnerFactory: runner=%s using local override client model=%s",
                name,
                override_client.model_name(),
            )

        # 4. Create a ModelRouter with independent state but shared clients
        # Use the worker cascade if provided, otherwise default logic.
        # When an override is present we deliberately drop the cascade so
        # the local client is the only path — cascade fallbacks would
        # silently re-introduce the cloud provider, breaking the L2
        # "no silent fallback" rule.
        router = ModelRouter(
            models=local_clients_for_router,
            default_tier=config.default_tier,
            cascade=None if override_client is not None else self._cascade,
        )

        if override_client is not None:
            primary_llm = override_client
        elif self._cascade:
            primary_llm = router.select_for_phase("explore")
        else:
            primary_llm = local_clients_for_router.get(
                config.default_tier, local_clients_for_router.get("standard")
            )

        # Mini Ultra Plan 2 / Phase L2: thread a source-scoped sibling
        # InferenceAuthority into the runner Agent so per-call telemetry
        # and ``llm_call`` events attribute to ``runner:<name>`` end to
        # end, including the truthful upstream provider extracted from
        # the (possibly overridden) primary_llm. When the parent did not
        # supply an authority (CLI back-compat), the runner Agent stays
        # on the legacy in-Agent telemetry emission path.
        runner_authority = None
        if self._inference_authority is not None:
            try:
                runner_authority = self._inference_authority.with_source(
                    f"runner:{name}"
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "RunnerFactory: with_source failed for runner=%s (%s); "
                    "falling back to legacy telemetry emission",
                    name,
                    exc,
                )
                runner_authority = None

        # 5. Create the runner agent
        agent = Agent(
            llm=primary_llm,
            tools=filtered_registry,
            system_prompt=system_prompt,
            session=session,
            telemetry=telemetry,
            events=events,
            max_iterations=config.max_iterations,
            verbose=self._verbose,
            model_router=router,
            inference_authority=runner_authority,
        )

        # 6. Build user message with task + optional context
        user_message = task
        if context:
            user_message = f"{task}\n\n## Context from master\n{context}"

        # 7. Execute
        logger.info("Running %s runner: %s", name, task[:80])
        try:
            response = agent.run(user_message)
        except Exception as e:
            logger.error("Runner %s failed: %s", name, e)
            response = f"Runner execution failed: {type(e).__name__}: {e}"
        if repo_inspection_mode:
            response = enrich_repo_inspection_response(
                response,
                workspace_root=self._workspace_root,
            )

        tool_events = events.query(event_type="tool_call")
        tool_failure_analysis = analyze_tool_call_events(
            tool_events,
            task_text=task_scope_text,
            final_response=response,
        )
        self._mirror_tool_events_to_parent(
            runner_name=name,
            runner_session_id=session.id,
            tool_events=tool_events,
            analysis=tool_failure_analysis,
        )

        # 8. Roll up telemetry to parent
        if parent_telemetry is not None:
            self._rollup_telemetry(parent_telemetry, telemetry, name)

        # 9. Get checkpoint count (for code runner)
        checkpoint_count = 0
        for tool in filtered_registry._tools.values():
            if hasattr(tool, "checkpoint_count"):
                checkpoint_count = tool.checkpoint_count
                break

        failed_tool_calls = sum(metrics.failures for metrics in telemetry.tool_metrics.values())
        failed_write_tool_calls = sum(
            metrics.failures
            for tool_name, metrics in telemetry.tool_metrics.items()
            if tool_name in {"Write", "StrReplace", "Delete"}
        )
        fatal_tool_failures = tool_failure_analysis["fatal_failures"]
        repo_validation = tool_failure_analysis["repo_validation"]
        stop_reason = agent.stop_reason or "tool_failed"
        if stop_reason in FATAL_STOP_REASONS:
            runner_success = False
        else:
            if repo_inspection_mode:
                if stop_reason == "completed" and fatal_tool_failures:
                    stop_reason = normalize_subtask_failure("tool_failed", events=events)
                    runner_success = False
                elif stop_reason == "completed" and getattr(repo_validation, "conflict", None):
                    stop_reason = "findings_conflict"
                    runner_success = False
                elif stop_reason == "completed" and not repo_validation.ok:
                    stop_reason = "incomplete_findings"
                    runner_success = False
                else:
                    runner_success = stop_reason == "completed"
            elif stop_reason == "completed" and failed_tool_calls:
                stop_reason = normalize_subtask_failure("tool_failed", events=events)
                runner_success = False
            else:
                runner_success = stop_reason == "completed" and failed_tool_calls == 0

        return RunnerResult(
            runner_name=name,
            success=runner_success,
            response=response,
            stop_reason=stop_reason,
            telemetry=telemetry,
            checkpoint_count=checkpoint_count,
            failed_tool_calls=failed_tool_calls,
            failed_write_tool_calls=failed_write_tool_calls,
            runner_event_log_path=str(events.log_path),
            tool_failures=[failure.to_failure_dict() for failure in tool_failure_analysis["failures"]],
            diagnostic_warnings=list(tool_failure_analysis["diagnostic_warnings"]),
            primary_failure=(
                tool_failure_analysis["fatal_failures"][0].to_failure_dict()
                if tool_failure_analysis["fatal_failures"]
                else None
            ),
        )

    def _build_filtered_registry(
        self,
        tool_names: List[str],
        *,
        policy_source: str = "master",
    ) -> ToolRegistry:
        """Create a ToolRegistry containing only the named tools.

        Tools are cloned from the parent registry by name lookup.
        """
        registry = ToolRegistry(
            guardrails=self._parent_tools.guardrails,
            linter=self._parent_tools._linter,
            events=self._parent_tools.events,
            trust_state=self._parent_tools.trust_state,
            policy_gate=self._parent_tools.policy_gate,
            policy_source=policy_source,
        )
        registry.max_output_chars = self._parent_tools.max_output_chars

        for name in tool_names:
            tool = self._parent_tools.get(name)
            if tool is not None:
                registry.register(tool)
            else:
                logger.warning(
                    "Runner tool '%s' not found in parent registry — skipping",
                    name,
                )

        return registry

    def _mirror_tool_events_to_parent(
        self,
        *,
        runner_name: str,
        runner_session_id: str,
        tool_events: List[Dict[str, Any]],
        analysis: Dict[str, Any],
    ) -> None:
        parent_events = getattr(self._parent_tools, "events", None)
        if parent_events is None:
            return
        failures_by_event_id = {
            failure.tool_id: failure
            for failure in analysis.get("failures", [])
        }
        for call_index, event in enumerate(tool_events, start=1):
            tool_id = str(event.get("tool_id") or event.get("event_id") or f"{runner_session_id}:{call_index}")
            failure = failures_by_event_id.get(tool_id) or failures_by_event_id.get(
                str(event.get("event_id") or "")
            )
            metadata = dict(event.get("metadata") or {})
            metadata["runner_session_id"] = runner_session_id
            parent_events.tool_call(
                tool_name=str(event.get("tool") or ""),
                arguments=dict(event.get("args") or {}),
                success=bool(event.get("success")),
                duration_ms=int(event.get("duration_ms") or 0),
                output_preview=event.get("output_preview"),
                error=event.get("error"),
                metadata=metadata,
                tool_id=tool_id,
                call_index=call_index,
                required_or_optional=(
                    failure.required_or_optional if failure is not None else "required"
                ),
                failure_class=(failure.failure_class if failure is not None else None),
                safe_next_action=(
                    failure.safe_next_action if failure is not None else None
                ),
                runner_name=runner_name,
                runner_session_id=runner_session_id,
            )

    @staticmethod
    def _rollup_telemetry(
        parent: SessionTelemetry,
        child: SessionTelemetry,
        runner_name: str,
    ) -> None:
        """Merge child runner telemetry into parent.

        Adds child calls, cost, and tool metrics to the parent's totals.
        Also records per-agent cost isolation.
        """
        # Merge LLM call records
        for call_metric in child.calls:
            parent.calls.append(call_metric)

        # Merge latency values for percentile calculations
        parent._latency_values.extend(child._latency_values)

        # Per-agent cost isolation
        parent.record_agent_cost(f"runner:{runner_name}", child.total_cost)

        # Merge tool metrics
        for tool_name, metrics in child.tool_metrics.items():
            prefixed = f"runner:{runner_name}:{tool_name}"
            if prefixed not in parent.tool_metrics:
                parent.tool_metrics[prefixed] = metrics
            else:
                existing = parent.tool_metrics[prefixed]
                existing.calls += metrics.calls
                existing.successes += metrics.successes
                existing.failures += metrics.failures
                existing.total_duration_ms += metrics.total_duration_ms

        for path in child.inspected_files:
            if path not in parent.inspected_files:
                parent.inspected_files.append(path)

        logger.info(
            "Runner %s telemetry rolled up: $%.4f, %d LLM calls",
            runner_name, child.total_cost, len(child.calls),
        )
