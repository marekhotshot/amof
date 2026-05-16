"""Delegate tool — master agent delegates tasks to specialized runner agents.

Two modes:
1. **Single**: Delegate(runner="k8s", task="check pods") → one runner, one result
2. **Batch**: Delegate(batch=[{runner:"k8s",task:"..."},{runner:"helm",task:"..."}])
   → multiple runners, results auto-summarized by a fast model

The DelegateTool holds a reference to the RunnerFactory (injected at registration)
and creates/runs runner agents on demand. Telemetry is rolled up to the parent.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)


class DelegateTool(Tool):
    """Delegate a task to a specialized runner agent.

    Single mode:
        Delegate(runner="k8s", task="check pod health in namespace test")

    Batch mode:
        Delegate(batch=[
            {"runner": "k8s", "task": "check pod health"},
            {"runner": "helm", "task": "verify chart values"},
        ])

    Batch mode auto-summarizes all runner results using a fast model.
    """

    name = "Delegate"
    description = (
        "Delegate a task to a specialized runner agent. "
        "Available runners: {runner_list}. "
        "Use 'runner' + 'task' for single delegation, or 'batch' for multiple tasks."
    )

    def __init__(
        self,
        runner_factory: Any,  # RunnerFactory (avoid circular import)
        parent_telemetry: Any = None,  # SessionTelemetry
        summarizer_llm: Any = None,  # LLMClient for batch summarization
    ) -> None:
        self._factory = runner_factory
        self._parent_telemetry = parent_telemetry
        self._summarizer_llm = summarizer_llm

        # Build dynamic parameters schema
        runner_names = runner_factory.runner_names if runner_factory else []
        self.parameters = self._build_parameters(runner_names)

        # Update description with available runners
        if runner_names:
            runner_descriptions = []
            for name in runner_names:
                cfg = runner_factory.available_runners.get(name)
                desc = cfg.description if cfg else name
                runner_descriptions.append(f"{name}: {desc}")
            self.description = (
                "Delegate a task to a specialized runner agent. "
                "Available runners:\n"
                + "\n".join(f"- {d}" for d in runner_descriptions)
                + "\n\nUse 'runner' + 'task' for single delegation, "
                "or 'batch' for multiple parallel tasks (results auto-summarized)."
            )

    @staticmethod
    def _build_parameters(runner_names: List[str]) -> Dict[str, Any]:
        """Build the JSON schema for parameters, with runner enum from config."""
        return {
            "type": "object",
            "properties": {
                "runner": {
                    "type": "string",
                    "enum": runner_names or ["code"],
                    "description": "Specialist runner type to delegate to.",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "Task description for the runner. Be specific: include "
                        "namespaces, pod names, file paths, error messages, expected outcomes."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Optional additional context from your current understanding "
                        "(findings so far, relevant info the runner needs)."
                    ),
                },
                "batch": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "runner": {"type": "string"},
                            "task": {"type": "string"},
                            "context": {"type": "string"},
                        },
                        "required": ["runner", "task"],
                    },
                    "description": (
                        "Batch mode: list of {runner, task, context?} objects. "
                        "All runners execute and results are auto-summarized."
                    ),
                },
            },
            # Neither runner+task nor batch is strictly required — we check in execute()
            "required": [],
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute single or batch delegation."""
        batch = kwargs.get("batch")
        runner = kwargs.get("runner")
        task = kwargs.get("task")

        if batch:
            return self._execute_batch(batch)
        elif runner and task:
            return self._execute_single(
                runner=runner,
                task=task,
                context=kwargs.get("context"),
            )
        else:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Delegate requires either (runner + task) for single mode "
                    "or (batch) for batch mode. "
                    f"Available runners: {', '.join(self._factory.runner_names)}"
                ),
            )

    def _execute_single(
        self,
        runner: str,
        task: str,
        context: Optional[str] = None,
    ) -> ToolResult:
        """Run a single runner and return its result."""
        logger.info("Delegating to %s runner: %s", runner, task[:80])

        result = self._factory.run_runner(
            name=runner,
            task=task,
            context=context,
            parent_telemetry=self._parent_telemetry,
        )

        status = "PASS" if result.success else "FAIL"
        output = (
            f"[Runner: {runner}] Status: {status} | "
            f"Stop: {result.stop_reason} | "
            f"Cost: ${result.telemetry.total_cost:.4f}\n\n"
            f"{result.response}"
        )

        return ToolResult(
            success=result.success,
            output=output,
        )

    def _execute_batch(self, batch: List[Dict[str, Any]]) -> ToolResult:
        """Run multiple runners and return consolidated summary.

        Steps:
        1. Execute each runner sequentially
        2. Collect all Execution Digests
        3. Summarize with fast model using prompts/batch-summarizer.md
        4. Return consolidated result
        """
        if not batch:
            return ToolResult(
                success=False,
                output="",
                error="Batch list is empty.",
            )

        # Execute each runner
        results = []
        for item in batch:
            runner_name = item.get("runner", "")
            task = item.get("task", "")
            context = item.get("context")

            if not runner_name or not task:
                results.append({
                    "runner": runner_name or "(missing)",
                    "status": "ERROR",
                    "response": "Missing runner or task in batch item.",
                })
                continue

            logger.info("Batch: delegating to %s runner", runner_name)
            result = self._factory.run_runner(
                name=runner_name,
                task=task,
                context=context,
                parent_telemetry=self._parent_telemetry,
            )
            results.append({
                "runner": runner_name,
                "status": "PASS" if result.success else "FAIL",
                "stop_reason": result.stop_reason,
                "cost": f"${result.telemetry.total_cost:.4f}",
                "response": result.response,
            })

        # Build combined digest text
        digest_parts = []
        for r in results:
            digest_parts.append(
                f"--- Runner: {r['runner']} | Status: {r['status']} ---\n"
                f"{r['response']}"
            )
        combined = "\n\n".join(digest_parts)

        # Summarize with fast model if available
        summary = combined  # fallback: raw combined
        if self._summarizer_llm is not None:
            try:
                from ..prompt_loader import load_prompt
                batch_prompt = load_prompt(
                    "batch-summarizer",
                    fallback=(
                        "Summarize the following runner results into a concise "
                        "consolidated report. Keep it under 500 words."
                    ),
                )
                response = self._summarizer_llm.chat(
                    system=batch_prompt,
                    messages=[{"role": "user", "content": combined}],
                    tools=None,
                    max_tokens=2048,
                )
                if response.text:
                    summary = response.text

                # Cost/telemetry recording is handled by the inference
                # authority adapter when the API runner wires one in
                # (source="summarizer"). The historical inline
                # `record_from_usage(tier="fast")` here over-attributed the
                # summarizer model as the "fast" tier regardless of what was
                # actually wired and double-counted when the summarizer LLM
                # was an authority adapter, so it is intentionally omitted.
                # CLI callers that pass a raw client still get the response
                # text; they just don't get split-out summarizer cost
                # attribution, matching the pre-IAL behaviour for that path.
            except Exception as e:
                logger.warning("Batch summarization failed: %s — using raw output", e)

        # Compute totals
        total_cost = sum(
            float(r.get("cost", "$0").replace("$", ""))
            for r in results
        )
        pass_count = sum(1 for r in results if r["status"] == "PASS")
        fail_count = len(results) - pass_count

        header = (
            f"[Batch: {len(results)} runners | "
            f"{pass_count} passed, {fail_count} failed | "
            f"Total cost: ${total_cost:.4f}]\n\n"
        )

        return ToolResult(
            success=fail_count == 0,
            output=header + summary,
        )
