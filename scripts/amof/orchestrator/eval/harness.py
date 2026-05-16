"""Eval harness — runs tasks through the agent/runners and records results.

Usage:
    from scripts.amof.orchestrator.eval.harness import EvalHarness
    harness = EvalHarness(...)
    results = harness.run_all()
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..agent import Agent
from ..events import EventLog
from ..llm.base import LLMClient
from ..model_router import ModelRouter
from ..runners import RunnerFactory
from ..session import Session
from ..telemetry import SessionTelemetry
from ..tools.base import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class TaskDefinition:
    """A single eval task loaded from YAML."""

    id: str
    description: str
    runner: str  # "code", "k8s", "helm", "debug", "master"
    expected_tools: List[str] = field(default_factory=list)
    max_cost: float = 0.10
    timeout_s: int = 30
    success_check: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TaskDefinition:
        return cls(
            id=data["id"],
            description=data["description"],
            runner=data.get("runner", "master"),
            expected_tools=data.get("expected_tools", []),
            max_cost=float(data.get("max_cost", 0.10)),
            timeout_s=int(data.get("timeout_s", 30)),
            success_check=data.get("success_check"),
        )


@dataclass
class TaskResult:
    """Result of running a single eval task."""

    task_id: str
    tier: str  # e.g. "fast", "standard", "strong"
    runner: str
    success: bool
    response: str
    cost: float
    latency_ms: int
    tool_calls: List[str]
    expected_tools_hit: bool  # whether expected tools were used
    success_check_passed: Optional[bool] = None
    error: Optional[str] = None
    timed_out: bool = False


@dataclass
class EvalRun:
    """Full eval run results across multiple tiers."""

    timestamp: float = field(default_factory=time.time)
    tasks: List[TaskDefinition] = field(default_factory=list)
    results: List[TaskResult] = field(default_factory=list)
    tiers_tested: List[str] = field(default_factory=list)


class EvalHarness:
    """Runs eval tasks through agents/runners and collects metrics.

    Can run same tasks across multiple model tiers for comparison.
    """

    def __init__(
        self,
        model_clients: Dict[str, LLMClient],
        tool_registry: ToolRegistry,
        runner_factory: Optional[RunnerFactory] = None,
        system_prompt: str = "You are a helpful AI assistant.",
        verbose: bool = False,
    ):
        """Initialize the eval harness.

        Args:
            model_clients: Tier -> LLMClient mapping (fast, standard, strong).
            tool_registry: Full tool registry for master agent.
            runner_factory: For runner-based tasks.
            system_prompt: System prompt for master agent.
            verbose: Show per-step output.
        """
        self._model_clients = model_clients
        self._tool_registry = tool_registry
        self._runner_factory = runner_factory
        self._system_prompt = system_prompt
        self._verbose = verbose

    @staticmethod
    def load_tasks(tasks_path: Optional[Path] = None) -> List[TaskDefinition]:
        """Load task definitions from a YAML file.

        If no path provided, uses the built-in tasks.yaml.
        """
        if tasks_path is None:
            tasks_path = Path(__file__).parent / "tasks.yaml"

        with open(tasks_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return [TaskDefinition.from_dict(t) for t in data.get("tasks", [])]

    def run_all(
        self,
        tasks: Optional[List[TaskDefinition]] = None,
        tiers: Optional[List[str]] = None,
        task_filter: Optional[List[str]] = None,
    ) -> EvalRun:
        """Run all tasks across specified tiers.

        Args:
            tasks: Task definitions (loads built-in if not provided).
            tiers: Tiers to test (default: all available).
            task_filter: If provided, only run tasks with these IDs.

        Returns:
            EvalRun with all results.
        """
        if tasks is None:
            tasks = self.load_tasks()

        if task_filter:
            tasks = [t for t in tasks if t.id in task_filter]

        if tiers is None:
            tiers = list(self._model_clients.keys())

        run = EvalRun(tasks=tasks, tiers_tested=tiers)

        for tier in tiers:
            logger.info("--- Eval tier: %s ---", tier)
            for task in tasks:
                logger.info("  Task: %s (%s)", task.id, task.runner)
                result = self._run_task(task, tier)
                run.results.append(result)
                status = "OK" if result.success else "FAIL"
                logger.info(
                    "  [%s] %s: $%.4f, %dms, tools=%s",
                    status, task.id, result.cost, result.latency_ms,
                    ",".join(result.tool_calls) or "none",
                )

        return run

    def _run_task(self, task: TaskDefinition, tier: str) -> TaskResult:
        """Run a single task at a specific tier.

        Returns a TaskResult with metrics.
        """
        start_ms = int(time.monotonic() * 1000)

        try:
            if task.runner == "master":
                return self._run_master_task(task, tier, start_ms)
            elif self._runner_factory and task.runner in self._runner_factory.runner_names:
                return self._run_runner_task(task, tier, start_ms)
            else:
                # Fall back to master for unknown runners
                return self._run_master_task(task, tier, start_ms)
        except Exception as e:
            elapsed = int(time.monotonic() * 1000) - start_ms
            return TaskResult(
                task_id=task.id,
                tier=tier,
                runner=task.runner,
                success=False,
                response="",
                cost=0.0,
                latency_ms=elapsed,
                tool_calls=[],
                expected_tools_hit=False,
                error=str(e),
            )

    def _run_master_task(
        self, task: TaskDefinition, tier: str, start_ms: int
    ) -> TaskResult:
        """Run a task through the master agent."""
        client = self._model_clients.get(tier, self._model_clients.get("standard"))
        if client is None:
            # Use any available client
            client = next(iter(self._model_clients.values()))

        session = Session(mode="eval")
        telemetry = SessionTelemetry(max_cost=task.max_cost)
        events = EventLog(session_id=session.id)

        # Single-tier router — use explicit cascade so ModelRouter is happy
        router = ModelRouter(
            models={tier: client, "standard": client},
            default_tier=tier,
            cascade=[tier],
        )

        agent = Agent(
            llm=client,
            tools=self._tool_registry,
            system_prompt=self._system_prompt,
            session=session,
            telemetry=telemetry,
            events=events,
            max_iterations=20,
            verbose=self._verbose,
            model_router=router,
        )

        response = agent.run(task.description)
        elapsed = int(time.monotonic() * 1000) - start_ms

        # Collect tools used
        tools_used = list(telemetry.tool_metrics.keys())

        # Check expected tools
        expected_hit = all(t in tools_used for t in task.expected_tools)

        # Success check (regex)
        check_passed = None
        if task.success_check:
            check_passed = bool(re.search(task.success_check, response, re.IGNORECASE))

        success = (
            agent.stop_reason == "completed"
            and (check_passed is None or check_passed)
        )

        return TaskResult(
            task_id=task.id,
            tier=tier,
            runner=task.runner,
            success=success,
            response=response[:500],
            cost=telemetry.total_cost,
            latency_ms=elapsed,
            tool_calls=tools_used,
            expected_tools_hit=expected_hit,
            success_check_passed=check_passed,
        )

    def _run_runner_task(
        self, task: TaskDefinition, tier: str, start_ms: int
    ) -> TaskResult:
        """Run a task through a specialized runner."""
        assert self._runner_factory is not None

        # Ensure the runner factory has a "standard" client mapped
        # (required by ModelRouter). Map the test tier client to standard if needed.
        if "standard" not in self._runner_factory._model_clients:
            client = self._model_clients.get(tier) or next(iter(self._model_clients.values()))
            self._runner_factory._model_clients["standard"] = client

        # Create a temporary telemetry for tracking
        telemetry = SessionTelemetry(max_cost=task.max_cost)

        result = self._runner_factory.run_runner(
            name=task.runner,
            task=task.description,
            parent_telemetry=telemetry,
        )

        elapsed = int(time.monotonic() * 1000) - start_ms

        tools_used = list(telemetry.tool_metrics.keys())
        # Strip runner prefix for expected tool matching (e.g. "runner:code:Shell" -> "Shell")
        base_tools = {t.rsplit(":", 1)[-1] for t in tools_used}
        expected_hit = all(t in base_tools for t in task.expected_tools)

        check_passed = None
        if task.success_check:
            check_passed = bool(
                re.search(task.success_check, result.response, re.IGNORECASE)
            )

        success = result.success and (check_passed is None or check_passed)

        return TaskResult(
            task_id=task.id,
            tier=tier,
            runner=task.runner,
            success=success,
            response=result.response[:500],
            cost=telemetry.total_cost,
            latency_ms=elapsed,
            tool_calls=tools_used,
            expected_tools_hit=expected_hit,
            success_check_passed=check_passed,
        )
