"""Task planner — uses a strong model to analyze the full codebase and create an execution plan.

The planner receives:
1. The full codebase context (file tree, key file contents)
2. The user's high-level task description
3. Guardrail information (readonly repos, no_touch_paths)

It outputs a structured ExecutionPlan with ordered subtasks that can be
executed independently by cheaper models via SubtaskExecutor.

Cost profile: ONE expensive call (Opus/GPT-5.2 Codex with large context),
followed by many cheap calls (Haiku/Sonnet/GPT-4o-mini for execution).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .agent_models import PlannerOutputModel
from .llm.base import LLMClient, ProviderError

logger = logging.getLogger(__name__)
MAX_STRUCTURED_RETRIES = 3
PACKAGED_PLANNER_PROMPT = (
    "You are the AMOF planner. Create a concise, executable JSON plan for the user's task. "
    "Use runner 'code' for repository code changes unless another configured runner is clearly required. "
    "For mutation tasks, include subtasks that make concrete file edits and verification guidance. "
    "For bounded edits, additions, or docs-only changes, plan targeted insertions/replacements "
    "that preserve existing content. Exact user-provided text must be inserted as-is unless "
    "the user explicitly asks to rewrite it; do not paraphrase exact requested sections. "
    "Do not plan whole-file rewrites unless the task explicitly says to rewrite, replace, "
    "overwrite, or regenerate the whole file. "
    "Return only the structured schema requested by the caller."
)


@dataclass
class Subtask:
    """A single subtask in an execution plan."""

    id: str
    title: str
    description: str
    runner: str = "code"
    depends_on: List[str] = field(default_factory=list)
    optional: bool = False
    # Filled after execution
    status: str = "pending"  # pending, running, completed, failed, skipped
    result: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ExecutionPlan:
    """Structured plan produced by the TaskPlanner."""

    analysis: str
    subtasks: List[Subtask]
    execution_order: List[str]
    risks: List[str] = field(default_factory=list)
    verification: str = ""
    questions: List[str] = field(default_factory=list)
    # Metadata
    planner_model: str = ""
    planning_cost: float = 0.0
    planning_cost_status: str = "observed"
    planning_cost_observed: bool = True
    planning_latency_ms: int = 0
    file_path: Optional[Path] = None  # path to persisted .md file
    continue_on_failure: bool = False

    def next_runnable(self) -> Optional[Subtask]:
        """Return the next subtask that can run (dependencies met).

        Returns None if all subtasks are done or blocked.
        """
        completed_ids = {st.id for st in self.subtasks if st.status == "completed"}
        for task_id in self.execution_order:
            st = self.get_subtask(task_id)
            if st and st.status == "pending":
                deps_met = all(d in completed_ids for d in st.depends_on)
                if deps_met:
                    return st
        return None

    def runnable_batch(self) -> List[Subtask]:
        """Return all subtasks that can run in parallel right now."""
        completed_ids = {st.id for st in self.subtasks if st.status == "completed"}
        batch = []
        for task_id in self.execution_order:
            st = self.get_subtask(task_id)
            if st and st.status == "pending":
                deps_met = all(d in completed_ids for d in st.depends_on)
                if deps_met:
                    batch.append(st)
        return batch

    def get_subtask(self, task_id: str) -> Optional[Subtask]:
        """Get subtask by ID."""
        for st in self.subtasks:
            if st.id == task_id:
                return st
        return None

    @property
    def is_complete(self) -> bool:
        """True if all subtasks are completed or skipped."""
        return all(st.status in ("completed", "skipped") for st in self.subtasks)

    @property
    def has_failures(self) -> bool:
        return any(st.status == "failed" for st in self.subtasks)

    def summary(self) -> str:
        """Human-readable summary of the plan state."""
        lines = [f"Plan: {len(self.subtasks)} subtasks"]
        for st in self.subtasks:
            marker = {"pending": " ", "running": ">", "completed": "x",
                       "failed": "!", "skipped": "-"}.get(st.status, "?")
            lines.append(f"  [{marker}] {st.id}. {st.title} ({st.runner})")
        if self.risks:
            lines.append(f"  Risks: {', '.join(self.risks)}")
        return "\n".join(lines)

    # ---- Markdown persistence ----

    def save_as_markdown(self, path: Path, session_id: str = "") -> Path:
        """Save the plan as a user-editable markdown file.

        The markdown includes a task checklist at the bottom that
        can be updated by mark_task_complete() or edited manually.

        Args:
            path: File path to write (e.g. ecosystems/<eco>/plans/YYYY-MM-DD-slug.md)
            session_id: Optional session ID for metadata header.

        Returns:
            The path the file was written to.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        completed = sum(1 for st in self.subtasks if st.status == "completed")

        lines = [
            f"# {self.analysis[:80].strip()}" if self.analysis else "# Execution Plan",
            "",
            f"**Created**: {now}",
        ]
        if session_id:
            lines.append(f"**Session**: {session_id}")
        status = "completed" if self.is_complete else "in_progress" if completed > 0 else "pending"
        lines.append(f"**Status**: {status}")
        if self.planner_model:
            lines.append(f"**Planner model**: {self.planner_model}")
        if self.planning_cost > 0:
            lines.append(f"**Planning cost**: ${self.planning_cost:.4f}")
        elif self.planner_model and (
            self.planning_cost_status != "observed" or not self.planning_cost_observed
        ):
            lines.append("**Planning cost**: unknown")
        lines.append("")

        # Analysis
        if self.analysis:
            lines.extend(["## Analysis", "", self.analysis, ""])

        # Risks
        if self.risks:
            lines.append("## Risks")
            lines.append("")
            for risk in self.risks:
                lines.append(f"- {risk}")
            lines.append("")

        # Verification
        if self.verification:
            lines.extend(["## Verification", "", self.verification, ""])

        # Task checklist
        lines.extend(["---", "", "## Tasks", ""])
        for i, st in enumerate(self.subtasks):
            check = "x" if st.status in ("completed", "skipped") else " "
            lines.append(f"- [{check}] {st.id}. **{st.title}** ({st.runner})")

        lines.append("")  # trailing newline

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        self.file_path = path
        logger.info("Plan saved to %s", path)
        return path

    @classmethod
    def load_from_markdown(cls, path: Path) -> "ExecutionPlan":
        """Load an ExecutionPlan from a persisted markdown file.

        Parses the task checklist to determine which subtasks are completed.
        This enables resume: read the .md, find unchecked tasks, continue.

        Args:
            path: Path to the markdown plan file.

        Returns:
            ExecutionPlan with subtask statuses set from checkbox state.
        """
        text = path.read_text(encoding="utf-8")

        # Parse metadata from header
        analysis = ""
        risks: List[str] = []
        verification = ""
        planner_model = ""
        planning_cost = 0.0

        # Extract analysis section
        analysis_match = re.search(r"## Analysis\s*\n\s*\n(.*?)(?=\n##|\n---|\Z)", text, re.DOTALL)
        if analysis_match:
            analysis = analysis_match.group(1).strip()

        # Extract risks
        risks_match = re.search(r"## Risks\s*\n\s*\n(.*?)(?=\n##|\n---|\Z)", text, re.DOTALL)
        if risks_match:
            for line in risks_match.group(1).strip().splitlines():
                line = line.strip()
                if line.startswith("- "):
                    risks.append(line[2:])

        # Extract verification
        verif_match = re.search(r"## Verification\s*\n\s*\n(.*?)(?=\n##|\n---|\Z)", text, re.DOTALL)
        if verif_match:
            verification = verif_match.group(1).strip()

        # Extract metadata
        for line in text.splitlines():
            if line.startswith("**Planner model**:"):
                planner_model = line.split(":", 1)[1].strip()
            elif line.startswith("**Planning cost**:"):
                try:
                    planning_cost = float(line.split("$")[1].strip())
                except (IndexError, ValueError):
                    pass

        # Parse task checklist: - [x] or - [ ] lines
        subtasks: List[Subtask] = []
        task_pattern = re.compile(
            r"^- \[([ xX])\] (\S+)\.\s+\*\*(.+?)\*\*\s*\((\w+)\)",
            re.MULTILINE,
        )
        for match in task_pattern.finditer(text):
            checked = match.group(1).lower() == "x"
            task_id = match.group(2)
            title = match.group(3)
            runner = match.group(4)

            subtasks.append(Subtask(
                id=task_id,
                title=title,
                runner=runner,
                status="completed" if checked else "pending",
                description=title,  # minimal — executor will expand
            ))

        execution_order = [st.id for st in subtasks]

        plan = cls(
            analysis=analysis,
            subtasks=subtasks,
            execution_order=execution_order,
            risks=risks,
            verification=verification,
            planner_model=planner_model,
            planning_cost=planning_cost,
            file_path=path,
        )
        logger.info("Loaded plan from %s: %d subtasks (%d completed)", path, len(subtasks),
                     sum(1 for st in subtasks if st.status == "completed"))
        return plan

    def mark_task_complete(self, task_id: str) -> None:
        """Mark a task as completed in both the in-memory plan and the .md file.

        This is called immediately after each subtask completes so the .md file
        is always the crash-safe source of truth.

        Args:
            task_id: The subtask ID to mark as complete.
        """
        # Update in-memory
        st = self.get_subtask(task_id)
        if st:
            st.status = "completed"

        # Update the .md file on disk
        if self.file_path and self.file_path.exists():
            text = self.file_path.read_text(encoding="utf-8")
            # Replace - [ ] <id>. with - [x] <id>.
            updated = re.sub(
                rf"^(- )\[ \]( {re.escape(task_id)}\.)".replace("\\", "\\\\"),
                r"\1[x]\2",
                text,
                count=1,
                flags=re.MULTILINE,
            )
            if updated != text:
                self.file_path.write_text(updated, encoding="utf-8")
                logger.info("Checked off task %s in %s", task_id, self.file_path)

        # Update status header
        self._update_status_in_file()

    def _update_status_in_file(self) -> None:
        """Update the **Status** line in the .md file based on current task states."""
        if not self.file_path or not self.file_path.exists():
            return
        completed = sum(1 for st in self.subtasks if st.status == "completed")
        total = len(self.subtasks)
        if completed == total:
            new_status = "completed"
        elif completed > 0:
            new_status = f"in_progress ({completed}/{total})"
        else:
            new_status = "pending"

        text = self.file_path.read_text(encoding="utf-8")
        updated = re.sub(
            r"^\*\*Status\*\*:.*$",
            f"**Status**: {new_status}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if updated != text:
            self.file_path.write_text(updated, encoding="utf-8")


class PlannerSemanticRetryExhausted(ValueError):
    """Raised when bounded semantic repair retries still produce no usable plan."""


class TaskPlanner:
    """Creates an ExecutionPlan from a high-level task using a strong model.

    Designed for one-shot usage: create planner, call plan(), get ExecutionPlan.
    The planner reads the codebase context and guardrails, sends everything to
    a strong model with the planner prompt, and parses the JSON response.
    """

    def __init__(
        self,
        planner_llm: LLMClient,
        planner_prompt_path: Optional[Path] = None,
        workspace_root: Optional[Path] = None,
    ):
        self._llm = planner_llm
        self._workspace_root = workspace_root or Path.cwd()
        self._last_thinking: Optional[str] = None  # thinking from last plan() call

        # Load explicit planner prompt only when one is provided. Public defaults
        # use the packaged prompt instead of probing the target repo.
        prompt_path = planner_prompt_path
        if prompt_path and prompt_path.exists():
            self._system_prompt = prompt_path.read_text(encoding="utf-8")
        else:
            self._system_prompt = PACKAGED_PLANNER_PROMPT

    @property
    def last_thinking(self) -> Optional[str]:
        """Return the extended thinking text from the last plan() call, if any."""
        return self._last_thinking

    def plan(
        self,
        task: str,
        codebase_context: str,
        guardrail_info: Optional[str] = None,
    ) -> ExecutionPlan:
        """Create an execution plan for a task.

        Args:
            task: High-level task description from the user.
            codebase_context: Full codebase context (file tree, key contents).
            guardrail_info: Optional text describing guardrails (readonly repos, no_touch_paths).

        Returns:
            ExecutionPlan with ordered subtasks.

        Raises:
            ValueError: If the planner's response cannot be parsed.
        """
        # Build the user message with full context
        user_parts = [
            "## Task\n",
            task,
            "\n\n## Codebase Context\n",
            codebase_context,
        ]
        if guardrail_info:
            user_parts.extend(["\n\n## Guardrails\n", guardrail_info])

        user_message = "".join(user_parts)

        logger.info(
            "Planning task (context: ~%d chars, ~%d tokens)",
            len(user_message), len(user_message) // 4,
        )

        start = time.monotonic()

        structured, usage, latency_ms, response_stop_reason = self._request_structured_plan(
            user_message,
            start,
        )
        semantic_error = self._semantic_plan_error(structured, response_stop_reason)
        if semantic_error:
            raise ValueError(semantic_error)

        plan_data = structured.model_dump()

        # Build ExecutionPlan
        subtasks = []
        for st_data in plan_data.get("subtasks", []):
            subtasks.append(Subtask(
                id=str(st_data.get("id", "")),
                title=st_data.get("title", ""),
                description=st_data.get("description", ""),
                runner=st_data.get("runner", "code"),
                depends_on=st_data.get("depends_on", []),
            ))

        plan = ExecutionPlan(
            analysis=plan_data.get("analysis", ""),
            subtasks=subtasks,
            execution_order=[str(x) for x in plan_data.get("execution_order", [])],
            risks=plan_data.get("risks", []),
            verification=plan_data.get("verification", ""),
            questions=plan_data.get("questions", []),
            planner_model=usage.model if usage else self._llm.model_name(),
            planning_cost=(
                usage.estimated_cost
                if usage
                and getattr(usage, "cost_status", "observed") == "observed"
                and bool(getattr(usage, "cost_observed", True))
                else 0.0
            ),
            planning_cost_status=(
                str(getattr(usage, "cost_status", "observed") or "observed") if usage else "observed"
            ),
            planning_cost_observed=bool(getattr(usage, "cost_observed", True)) if usage else True,
            planning_latency_ms=latency_ms,
        )

        logger.info(
            "Plan created: %d subtasks, cost=$%.4f, %dms",
            len(subtasks), plan.planning_cost, latency_ms,
        )

        return plan

    def _request_structured_plan(
        self,
        user_message: str,
        started_at: float,
    ) -> tuple[PlannerOutputModel, Any, int, Optional[str]]:
        """Request a planner output validated by Pydantic with self-correction retries."""
        messages = [{"role": "user", "content": user_message}]
        last_error = ""
        semantic_feedback = ""
        last_failure_was_semantic = False

        def _handle_candidate_response(
            parsed: PlannerOutputModel,
            *,
            usage: Any,
            stop_reason: Optional[str],
            response_text: str,
            attempt: int,
        ) -> tuple[PlannerOutputModel, Any, int, Optional[str]] | None:
            nonlocal last_error, semantic_feedback, last_failure_was_semantic

            semantic_error = self._semantic_plan_error(parsed, stop_reason)
            if semantic_error:
                last_error = semantic_error
                semantic_feedback = self._semantic_repair_feedback(response_text, semantic_error)
                last_failure_was_semantic = True
                logger.warning(
                    "Planner semantic validation failed (attempt %d): %s",
                    attempt,
                    semantic_error,
                )
                return None

            semantic_feedback = ""
            last_failure_was_semantic = False
            latency_ms = int((time.monotonic() - started_at) * 1000)
            return parsed, usage, latency_ms, stop_reason

        for attempt in range(1, MAX_STRUCTURED_RETRIES + 1):
            if semantic_feedback:
                messages.append({
                    "role": "user",
                    "content": semantic_feedback,
                })
                semantic_feedback = ""
            elif last_error:
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response failed schema validation.\n"
                        f"Validation error:\n{last_error}\n\n"
                        "Return ONLY a valid JSON object matching the required schema."
                    ),
                })

            try:
                structured = self._llm.chat_structured(
                    system=self._system_prompt,
                    messages=messages,
                    response_model=PlannerOutputModel,
                    max_tokens=16384,
                    temperature=0.0,
                )
                self._last_thinking = None
                accepted = _handle_candidate_response(
                    structured.parsed,
                    usage=structured.usage,
                    stop_reason=getattr(structured, "stop_reason", None),
                    response_text=(getattr(structured, "text", None) or structured.parsed.model_dump_json()),
                    attempt=attempt,
                )
                if accepted is not None:
                    return accepted
            except NotImplementedError:
                # Fallback providers: strict JSON + Pydantic validation loop.
                response = self._llm.chat(
                    system=self._system_prompt
                    + "\n\nReturn ONLY a strict JSON object. Do not use markdown fences.",
                    messages=messages,
                    tools=None,
                    max_tokens=16384,
                    temperature=0.0,
                )
                self._last_thinking = response.thinking
                raw_text = (response.text or "").strip()
                if not raw_text:
                    last_error = "Empty response."
                    semantic_feedback = ""
                    last_failure_was_semantic = False
                    continue
                try:
                    parsed = PlannerOutputModel.model_validate_json(raw_text)
                    accepted = _handle_candidate_response(
                        parsed,
                        usage=response.usage,
                        stop_reason=response.stop_reason,
                        response_text=raw_text,
                        attempt=attempt,
                    )
                    if accepted is not None:
                        return accepted
                except ValidationError as e:
                    last_error = str(e)
                    semantic_feedback = ""
                    last_failure_was_semantic = False
                    logger.warning("Planner schema validation failed (attempt %d): %s", attempt, e)
                    if hasattr(self._llm, 'record_failure'):
                        self._llm.record_failure()
                    continue
            except ValidationError as e:
                last_error = str(e)
                semantic_feedback = ""
                last_failure_was_semantic = False
                logger.warning("Planner structured validation failed (attempt %d): %s", attempt, e)
                if hasattr(self._llm, 'record_failure'):
                    self._llm.record_failure()
                continue
            except ProviderError:
                raise
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                semantic_feedback = ""
                last_failure_was_semantic = False
                logger.warning("Planner structured request failed (attempt %d): %s", attempt, e)
                continue

        if last_failure_was_semantic:
            raise PlannerSemanticRetryExhausted(last_error)
        raise ValueError(
            "Planner failed to produce a valid structured response after "
            f"{MAX_STRUCTURED_RETRIES} attempts.\nLast error: {last_error}"
        )

    def _semantic_plan_error(
        self,
        structured: PlannerOutputModel,
        response_stop_reason: Optional[str],
    ) -> Optional[str]:
        """Return an error message when a schema-valid plan is still unusable."""
        plan_data = structured.model_dump()

        # Allow empty subtasks if the planner has questions (it's asking for clarification).
        has_subtasks = bool(plan_data.get("subtasks"))
        has_questions = bool(plan_data.get("questions"))
        if has_subtasks or has_questions:
            return None

        analysis_preview = (plan_data.get("analysis") or "")[:500]
        subtasks_raw = plan_data.get("subtasks")
        logger.warning(
            "Planner returned no subtasks. subtasks=%r, analysis=%s",
            subtasks_raw, analysis_preview,
        )
        message_parts = ["Planner returned no subtasks and no questions."]
        if response_stop_reason:
            message_parts.append(f"stop_reason={response_stop_reason}.")
        if analysis_preview:
            message_parts.append(f"Analysis: {analysis_preview}")
        return " ".join(message_parts)

    def _semantic_repair_feedback(self, response_text: str, semantic_error: str) -> str:
        """Ask the provider to repair a schema-valid but unusable plan."""
        previous_response = (response_text or "").strip() or "{}"
        return (
            "The previous structured plan was schema-valid but unusable because it contained "
            "no subtasks and no clarification questions.\n\n"
            f"Failure summary:\n{semantic_error}\n\n"
            "Previous structured response:\n"
            f"{previous_response}\n\n"
            "Return exactly one of:\n"
            "1. one or more bounded executable subtasks, or\n"
            "2. one or more clarification questions.\n\n"
            "Do not return analysis-only output.\n"
            "Return ONLY a valid JSON object matching the required schema."
        )
