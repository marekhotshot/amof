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
from .plan_execute_control import (
    PlanExecuteStop,
    is_fatal_failure,
    normalize_subtask_failure,
    skip_remaining_subtasks,
    skip_status_for_failure,
)
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
            setattr(subtask, "runner_event_log_path", result.runner_event_log_path)
            setattr(subtask, "tool_failures", list(result.tool_failures))
            setattr(subtask, "diagnostic_warnings", list(result.diagnostic_warnings))
            setattr(subtask, "failure_detail", result.primary_failure)
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
        task_context: Optional[str] = None,
    ) -> ExecutionPlan:
        """Execute all subtasks in an ExecutionPlan sequentially.

        Respects dependency order. Skips subtasks whose dependencies failed.

        Args:
            plan: The execution plan to run.
            max_iterations_per_subtask: Max iterations for each subtask's agent loop.
            task_context: Original top-level task text to preserve exact user
                instructions and quoted content for workers.

        Returns:
            The plan with all subtasks updated.
        """
        logger.info("Executing plan: %d subtasks", len(plan.subtasks))
        plan.fatal_stop = None  # type: ignore[attr-defined]

        while not plan.is_complete:
            if self._parent_telemetry is not None and self._parent_telemetry.cost_exceeded:
                failed = plan.subtasks[0] if plan.subtasks else None
                for st in plan.subtasks:
                    if st.status in {"running", "pending"}:
                        failed = st
                        break
                if failed is not None and failed.status == "pending":
                    failed.status = "failed"
                    failed.error = "cost_exceeded"
                stop = PlanExecuteStop(
                    fatal=True,
                    failure_type="cost_exceeded",
                    failure_message=(
                        f"Cost limit exceeded "
                        f"(${self._parent_telemetry.total_cost:.4f} "
                        f">= ${self._parent_telemetry.max_cost:.2f})."
                    ),
                    failed_subtask_id=failed.id if failed else None,
                    skip_status=skip_status_for_failure("cost_exceeded"),
                )
                skip_remaining_subtasks(
                    plan,
                    skip_status=stop.skip_status,
                    skip_message=f"Skipped: {stop.failure_type}",
                    after_task_id=stop.failed_subtask_id,
                )
                plan.fatal_stop = stop  # type: ignore[attr-defined]
                break

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
                plan_context_parts: list[str] = []
                if task_context:
                    plan_context_parts.append(f"Original user task:\n{task_context}")
                if plan.analysis:
                    plan_context_parts.append(f"Plan analysis:\n{plan.analysis}")
                self.execute(
                    subtask,
                    plan_context="\n\n".join(plan_context_parts) if plan_context_parts else None,
                    max_iterations=max_iterations_per_subtask,
                )

                # Update plan markdown immediately (crash-safe progress)
                if subtask.status == "completed":
                    plan.mark_task_complete(subtask.id)

                if subtask.status != "failed":
                    continue

                failure_type = normalize_subtask_failure(subtask.error, subtask.error)
                if (
                    self._parent_telemetry is not None
                    and self._parent_telemetry.cost_exceeded
                ):
                    failure_type = "cost_exceeded"
                    subtask.error = "cost_exceeded"

                logger.warning("Subtask %s failed: %s", subtask.id, failure_type)

                if not is_fatal_failure(
                    failure_type,
                    subtask_optional=subtask.optional,
                    continue_on_failure=getattr(plan, "continue_on_failure", False),
                ):
                    continue

                stop = PlanExecuteStop(
                    fatal=True,
                    failure_type=failure_type,
                    failure_message=subtask.error or failure_type,
                    failed_subtask_id=subtask.id,
                    skip_status=skip_status_for_failure(failure_type),
                )
                skip_remaining_subtasks(
                    plan,
                    skip_status=stop.skip_status,
                    skip_message=f"Skipped: {stop.failure_type}",
                    after_task_id=subtask.id,
                )
                plan.fatal_stop = stop  # type: ignore[attr-defined]
                break

            if getattr(plan, "fatal_stop", None) is not None:
                break

        # Log final status
        completed = sum(1 for st in plan.subtasks if st.status == "completed")
        failed = sum(1 for st in plan.subtasks if st.status == "failed")
        skipped = sum(1 for st in plan.subtasks if st.status == "skipped")
        logger.info(
            "Plan execution finished: %d completed, %d failed, %d skipped",
            completed, failed, skipped,
        )

        return plan
