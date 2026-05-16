"""Subtask executor — runs a single subtask from an ExecutionPlan.

Each executor instance gets:
1. A focused subtask with specific instructions
2. Only the files it needs (read_files + write_files)
3. A restricted tool set (guardrails + command allowlist from the subtask)
4. A cheap model appropriate for the subtask's complexity

This is the "worker" in the planner-executor architecture. The planner
decides WHAT to do; the executor decides HOW to do each piece.

Key constraints:
- Executors have limited context (only relevant files, not full codebase)
- Executors inherit guardrails from the parent agent
- Shell commands are restricted to the subtask's allowed_commands list
- Each executor runs a mini agent loop (message → LLM → tools → result)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent import Agent
from .events import EventLog
from .llm.base import LLMClient
from .planner import ExecutionPlan, Subtask
from .prompt_loader import load_prompt
from .session import Session
from .telemetry import SessionTelemetry
from .tools.base import Guardrails, ToolRegistry, create_default_registry

logger = logging.getLogger(__name__)

# Minimal fallback if prompts/executor.md is missing
_EXECUTOR_FALLBACK = (
    "You are a focused coding agent executing a specific subtask.\n\n"
    "## Your Task\n{task_description}\n\n"
    "## Files You Can Read\n{read_files}\n\n"
    "## Files You Can Write\n{write_files}\n\n"
    "## Allowed Shell Commands\n{allowed_commands}\n\n"
    "## Rules\n1. ONLY modify listed files\n2. ONLY run listed commands\n"
    "3. Read files first\n4. Minimal changes\n5. Report what you did\n"
)


class SubtaskExecutor:
    """Executes a single subtask from an ExecutionPlan.

    Delegates the subtask to the RunnerFactory to spawn the appropriate runner agent.
    """

    def __init__(
        self,
        runner_factory: Any,  # RunnerFactory
        parent_telemetry: Optional[SessionTelemetry] = None,
        verbose: bool = False,
    ):
        """Initialize executor with runner factory.

        Args:
            runner_factory: Factory to spawn runners.
            parent_telemetry: Telemetry to roll up costs into.
            verbose: Print status to stderr.
        """
        self._runner_factory = runner_factory
        self._parent_telemetry = parent_telemetry
        self._verbose = verbose

    def execute(
        self,
        subtask: Subtask,
        plan_context: Optional[str] = None,
        max_iterations: int = 20,
    ) -> Subtask:
        """Execute a single subtask via RunnerFactory and update its status."""
        subtask.status = "running"
        start = time.monotonic()

        logger.info(
            "Executing subtask %s: %s (runner=%s)",
            subtask.id, subtask.title, subtask.runner,
        )

        try:
            # Add plan context to the subtask description if provided
            context_str = plan_context if plan_context else None

            # Delegate to runner factory
            result = self._runner_factory.run_runner(
                name=subtask.runner,
                task=subtask.description,
                context=context_str,
                parent_telemetry=self._parent_telemetry,
            )

            duration_ms = int((time.monotonic() - start) * 1000)

            subtask.status = "completed" if result.success else "failed"
            subtask.result = result.response
            if not result.success:
                subtask.error = result.stop_reason

            logger.info(
                "Subtask %s %s: cost=$%.4f, %dms",
                subtask.id, subtask.status, result.telemetry.total_cost, duration_ms,
            )

        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            subtask.status = "failed"
            subtask.error = str(e)

            logger.error(
                "Subtask %s failed after %dms: %s",
                subtask.id, duration_ms, e,
            )

        return subtask

    def execute_plan(
        self,
        plan: ExecutionPlan,
        max_iterations_per_subtask: int = 20,
    ) -> ExecutionPlan:
        """Execute all subtasks in an ExecutionPlan sequentially.

        Respects dependency order. Skips subtasks whose dependencies failed.

        Args:
            plan: The execution plan to run.
            max_iterations_per_subtask: Max iterations for each subtask's agent loop.

        Returns:
            The plan with all subtasks updated.
        """
        logger.info("Executing plan: %d subtasks", len(plan.subtasks))

        while not plan.is_complete:
            # Get next batch of runnable subtasks
            batch = plan.runnable_batch()
            if not batch:
                # No more runnable subtasks — remaining are blocked by failures
                failed_deps = []
                for st in plan.subtasks:
                    if st.status == "pending":
                        st.status = "skipped"
                        st.error = "Skipped: dependency failed"
                        failed_deps.append(st.id)
                if failed_deps:
                    logger.warning("Skipped subtasks due to dependency failures: %s", failed_deps)
                break

            # Execute sequentially (could be parallelized for independent subtasks)
            for subtask in batch:
                self.execute(
                    subtask,
                    plan_context=plan.analysis,
                    max_iterations=max_iterations_per_subtask,
                )

                # Update plan markdown immediately (crash-safe progress)
                if subtask.status == "completed":
                    plan.mark_task_complete(subtask.id)

                # If a subtask fails, check if any remaining subtasks depend on it
                if subtask.status == "failed":
                    logger.warning(
                        "Subtask %s failed: %s", subtask.id, subtask.error,
                    )

        # Log final status
        completed = sum(1 for st in plan.subtasks if st.status == "completed")
        failed = sum(1 for st in plan.subtasks if st.status == "failed")
        skipped = sum(1 for st in plan.subtasks if st.status == "skipped")
        logger.info(
            "Plan execution finished: %d completed, %d failed, %d skipped",
            completed, failed, skipped,
        )

        return plan
