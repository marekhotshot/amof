"""Pydantic models for structured agent outputs.

These models serve two purposes:
1) Runtime validation for robust autonomous behavior.
2) Prompt schema for LLM structured-output APIs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class AgentThought(BaseModel):
    """High-level reasoning state for a single agent step."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(
        ...,
        description="Current objective the agent is trying to achieve in this step.",
        min_length=3,
    )
    observations: List[str] = Field(
        default_factory=list,
        description="Key facts observed from tools, files, or prior responses.",
    )
    constraints: List[str] = Field(
        default_factory=list,
        description="Guardrails, no-touch paths, readonly repos, or other constraints that must be respected.",
    )
    next_action: str = Field(
        ...,
        description="Concrete next action to execute immediately.",
        min_length=3,
    )
    confidence: float = Field(
        0.5,
        ge=0.0,
        le=1.0,
        description="Confidence score (0-1) that this plan is correct and safe.",
    )
    needs_clarification: bool = Field(
        False,
        description="True when user clarification is required before acting safely.",
    )


class EcosystemCommand(BaseModel):
    """Validated command envelope for safe subprocess execution."""

    model_config = ConfigDict(extra="forbid")

    target_repo: Optional[str] = Field(
        default=None,
        description="Repository or working-directory target for the command, if known.",
    )
    command: str = Field(
        ...,
        description="Exact shell/CLI command to execute.",
        min_length=1,
    )
    is_destructive: bool = Field(
        ...,
        description="Whether the command can delete data, rewrite history, or otherwise make irreversible changes.",
    )
    rationale: Optional[str] = Field(
        default=None,
        description="Short reason why this command is needed.",
    )


class JournalEntry(BaseModel):
    """Structured journal payload for ecosystem run logs."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        ...,
        description="Short title for the journal entry.",
        min_length=3,
    )
    goal: str = Field(
        ...,
        description="The user goal that this run attempted to satisfy.",
        min_length=3,
    )
    outcome: Literal[
        "completed",
        "interrupted",
        "cost_exceeded",
        "max_iterations",
        "failed",
    ] = Field(
        ...,
        description="Final run outcome status.",
    )
    summary: str = Field(
        ...,
        description="Concise summary of what was done and why.",
        min_length=3,
    )
    actions: List[str] = Field(
        default_factory=list,
        description="List of key actions or commands executed.",
    )
    risks: List[str] = Field(
        default_factory=list,
        description="Residual risks, caveats, or follow-up concerns.",
    )
    changed_files: List[str] = Field(
        default_factory=list,
        description="Files that were modified or generated.",
    )
    next_steps: List[str] = Field(
        default_factory=list,
        description="Recommended follow-up steps after this run.",
    )


class PlanSubtaskModel(BaseModel):
    """Pydantic schema for one planned subtask."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Stable task identifier like '1' or '2a'.")
    title: str = Field(..., description="Short task title.")
    description: str = Field(..., description="Detailed execution instructions.")
    runner: str = Field(default="code", description="Type of worker/runner to use.")
    depends_on: List[str] = Field(
        default_factory=list,
        description="Task IDs that must be completed before this subtask.",
    )

class DelegateTaskModel(BaseModel):
    """Orchestrator delegation of a subtask to a worker."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Stable task identifier like '1' or '2a'.")
    runner: str = Field(..., description="Type of worker/runner to use.")
    task: str = Field(..., description="Detailed execution instructions for the worker.")
    context: Optional[str] = Field(default=None, description="Additional context from the orchestrator.")

class SubtaskResult(BaseModel):
    """Result returned by a worker after execution."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether the subtask succeeded.")
    output: str = Field(..., description="Final output, summary, or logs from the worker.")
    error_logs: Optional[str] = Field(default=None, description="Error logs if it failed.")

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Stable task identifier like '1' or '2a'.")
    title: str = Field(..., description="Short task title.")
    description: str = Field(..., description="Detailed execution instructions.")
    depends_on: List[str] = Field(
        default_factory=list,
        description="Task IDs that must be completed before this subtask.",
    )
    read_files: List[str] = Field(
        default_factory=list,
        description="Files to inspect before making changes.",
    )
    write_files: List[str] = Field(
        default_factory=list,
        description="Files that may be modified in this subtask.",
    )
    allowed_commands: List[str] = Field(
        default_factory=list,
        description="Shell commands explicitly permitted for this subtask.",
    )
    model_tier: Literal["fast", "standard", "strong"] = Field(
        "standard",
        description="Recommended model tier for executing this subtask.",
    )
    estimated_complexity: Literal["low", "medium", "high"] = Field(
        "medium",
        description="Estimated implementation complexity.",
    )


class PlannerOutputModel(BaseModel):
    """Structured planner response model."""

    model_config = ConfigDict(extra="forbid")

    analysis: str = Field(..., description="Overall analysis of the task.")
    subtasks: List[PlanSubtaskModel] = Field(
        default_factory=list,
        description="Ordered set of subtasks to complete the goal.",
    )
    execution_order: List[str] = Field(
        default_factory=list,
        description="Task IDs in preferred execution order.",
    )
    risks: List[str] = Field(
        default_factory=list,
        description="Potential risks, regressions, or blockers.",
    )
    verification: str = Field(
        default="",
        description="How to verify the final result.",
    )
    questions: List[str] = Field(
        default_factory=list,
        description="Clarifying questions when requirements are incomplete.",
    )


class FileSymbolModel(BaseModel):
    """Symbol description for indexed code."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., description="Class or function name.")
    description: str = Field(default="", description="What this symbol is responsible for.")
    key_methods: List[str] = Field(
        default_factory=list,
        description="Notable methods for classes.",
    )
    params: List[str] = Field(
        default_factory=list,
        description="Function parameters (optional, from LLM).",
    )


class IndexedFileModel(BaseModel):
    """Per-file index record."""

    model_config = ConfigDict(extra="allow")

    purpose: str = Field(
        default="",
        description=(
            "Primary role of this file. Optional with empty default so a partial "
            "structured response (e.g. an incremental update where the LLM only "
            "filled symbols/imports) doesn't fail the entire batch validation."
        ),
    )
    complexity: Literal["low", "medium", "high"] = Field(
        "medium",
        description="Estimated implementation complexity of this file.",
    )
    classes: List[Union[FileSymbolModel, str]] = Field(
        default_factory=list,
        description="Classes found in the file.",
    )
    functions: List[Union[FileSymbolModel, str]] = Field(
        default_factory=list,
        description="Functions found in the file.",
    )
    imports_from: List[str] = Field(
        default_factory=list,
        description="Modules imported from (optional, from LLM).",
    )


class CodebaseIndexOutputModel(BaseModel):
    """Structured full index output from the indexing model."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", description="High-level summary of the codebase.")
    architecture: str = Field(default="", description="Architecture overview and major components.")
    files: Dict[str, IndexedFileModel] = Field(
        default_factory=dict,
        description="Map of file path to structured file analysis.",
    )
    dependency_graph: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Map of file path to its direct dependencies/import targets.",
    )
    entry_points: List[str] = Field(
        default_factory=list,
        description="Important runtime entrypoints.",
    )
    key_abstractions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Core abstractions and their responsibilities.",
    )


class IncrementalIndexUpdateModel(BaseModel):
    """Structured incremental index delta."""

    model_config = ConfigDict(extra="forbid")

    files: Dict[str, IndexedFileModel] = Field(
        default_factory=dict,
        description="Updated file analyses for changed files.",
    )
    dependency_graph_updates: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Dependency graph patches for changed files.",
    )
