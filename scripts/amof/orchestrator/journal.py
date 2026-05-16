"""Auto-journal — generates a markdown entry after each agent run.

Captures: goal, outcome, metrics, model usage, files changed, checkpoints,
conversation summary, key findings/actions, and links to the plan file.

Journal entries are saved to: ecosystems/<eco>/journal/YYYY-MM-DD-HHMMSS-<slug>.md
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def generate_entry(
    session_id: str,
    goal: str,
    stop_reason: str,
    telemetry,
    events,
    ecosystem: str,
    output_dir: Path,
    plan=None,
    session=None,
) -> Path:
    """Generate a journal entry from telemetry, events, and conversation.

    Args:
        session_id: Agent session ID.
        goal: The user's goal/task description.
        stop_reason: How the run ended (completed, cost_exceeded, max_iterations, etc.).
        telemetry: SessionTelemetry instance.
        events: EventLog instance.
        ecosystem: Ecosystem name.
        output_dir: Directory to save the entry (e.g. ecosystems/<eco>/journal/).
        plan: Optional ExecutionPlan for plan-execute runs.
        session: Optional Session instance for conversation context.

    Returns:
        Path to the generated journal entry.
    """
    now = datetime.now()
    slug = _slugify(goal)
    # Include time in filename for chronological ordering
    timestamp = now.strftime('%Y-%m-%d-%H%M%S')
    filename = f"{timestamp}-{slug}.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename

    # Avoid overwriting (unlikely with seconds in name, but safe)
    counter = 1
    while path.exists():
        counter += 1
        path = output_dir / f"{timestamp}-{slug}-{counter}.md"

    # Gather metrics
    elapsed = telemetry.elapsed_seconds
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    lines = [
        f"# {goal[:120]}",
        "",
        f"**Date**: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Session**: {session_id}",
        f"**Ecosystem**: {ecosystem}",
        f"**Outcome**: {stop_reason}",
        f"**Duration**: {minutes}m {seconds}s",
        "",
    ]

    # Plan link
    if plan and plan.file_path:
        completed = sum(1 for st in plan.subtasks if st.status == "completed")
        total = len(plan.subtasks)
        try:
            rel_path = plan.file_path.relative_to(output_dir.parent)
            lines.append(f"**Plan**: [{rel_path}]({rel_path}) ({completed}/{total} tasks)")
        except ValueError:
            lines.append(f"**Plan**: {plan.file_path} ({completed}/{total} tasks)")
        lines.append("")

    # ── Conversation Summary ──────────────────────────────
    if session and hasattr(session, "messages") and session.messages:
        summary = _build_conversation_summary(session.messages)
        if summary:
            lines.extend([
                "## Summary",
                "",
                summary,
                "",
            ])

    # ── Key Actions & Findings ────────────────────────────
    if session and hasattr(session, "messages") and session.messages:
        actions = _extract_actions(session.messages)
        if actions:
            lines.extend([
                "## Key Actions",
                "",
            ])
            for action in actions[:15]:  # cap to avoid huge journals
                lines.append(f"- {action}")
            lines.append("")

    # ── Tools Used ────────────────────────────────────────
    if session and hasattr(session, "messages") and session.messages:
        tool_summary = _extract_tool_calls(session.messages)
        if tool_summary:
            lines.extend([
                "## Tools Executed",
                "",
            ])
            for tool_line in tool_summary[:20]:
                lines.append(f"- {tool_line}")
            lines.append("")

    # Metrics table
    lines.extend([
        "## Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| LLM calls | {telemetry.total_calls} |",
        f"| Tokens | {telemetry.total_prompt_tokens:,} in + {telemetry.total_completion_tokens:,} out |",
        f"| Cost | ${telemetry.total_cost:.4f} |",
        f"| Avg latency | {telemetry.avg_latency_ms}ms |",
        f"| Peak context | {telemetry.peak_context_pct:.1f}% |",
        f"| Tool calls | {telemetry.total_tool_calls} ({telemetry.tool_success_rate:.0f}% success) |",
    ])
    if telemetry.cache_read_tokens > 0 or telemetry.cache_creation_tokens > 0:
        lines.append(f"| Cache hit rate | {telemetry.cache_hit_rate:.0f}% |")
    if telemetry.max_cost:
        pct = (telemetry.total_cost / telemetry.max_cost) * 100 if telemetry.max_cost > 0 else 0
        lines.append(f"| Budget used | {pct:.1f}% of ${telemetry.max_cost:.2f} |")
    lines.append("")

    # Model usage by tier
    if telemetry.tier_metrics and len(telemetry.tier_metrics) > 1:
        lines.extend([
            "## Model Usage",
            "",
            "| Tier | Calls | Tokens | Cost | Model |",
            "|------|-------|--------|------|-------|",
        ])
        for tier, tm in sorted(telemetry.tier_metrics.items()):
            lines.append(
                f"| {tier} | {tm.calls} | {tm.input_tokens:,}+{tm.output_tokens:,} | "
                f"${tm.cost:.4f} | {tm.model_name} |"
            )
        lines.append("")

    # Plan task summary
    if plan:
        lines.extend(["## Tasks", ""])
        for st in plan.subtasks:
            check = "x" if st.status in ("completed", "skipped") else " "
            status_note = f" — {st.error}" if st.error else ""
            lines.append(f"- [{check}] {st.id}. {st.title}{status_note}")
        lines.append("")

    # Files changed
    files_diff = _git_diff_stat()
    if files_diff:
        lines.extend([
            "## Files Changed",
            "",
            "```",
            files_diff,
            "```",
            "",
        ])

    # Tool breakdown (from telemetry)
    if telemetry.tool_metrics:
        lines.extend(["## Tool Metrics", ""])
        for name, tm in telemetry.top_tools(8):
            lines.append(f"- **{name}**: {tm.calls} calls ({tm.success_rate:.0f}% ok, avg {tm.avg_duration_ms}ms)")
        lines.append("")

    # Footer
    try:
        from .. import __version__
        version = __version__
    except Exception:
        version = "unknown"
    lines.extend([
        "---",
        f"*Generated by AMOF orchestrator v{version}*",
    ])

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _build_conversation_summary(messages: list) -> str:
    """Build a concise summary of the conversation from messages.

    Extracts user questions and the final assistant response to create
    a readable narrative of what happened in the session.
    """
    parts = []

    # Collect user goals/questions
    user_msgs = [m for m in messages if m.role == "user"]
    assistant_msgs = [m for m in messages if m.role == "assistant" and m.content]

    if user_msgs:
        # First user message is the main goal
        main_goal = user_msgs[0].content
        if len(main_goal) > 200:
            main_goal = main_goal[:200] + "..."
        parts.append(f"**Task**: {main_goal}")

        # Follow-up questions
        if len(user_msgs) > 1:
            parts.append("")
            parts.append("**Follow-ups**:")
            for msg in user_msgs[1:]:
                text = msg.content
                if len(text) > 150:
                    text = text[:150] + "..."
                parts.append(f"- {text}")

    # Last assistant response (the final answer/conclusion)
    if assistant_msgs:
        last_response = assistant_msgs[-1].content
        if last_response:
            # Truncate very long responses but keep enough for context
            if len(last_response) > 800:
                last_response = last_response[:800] + "\n\n*[truncated]*"
            parts.append("")
            parts.append("**Agent conclusion**:")
            parts.append("")
            parts.append(last_response)

    return "\n".join(parts)


def _extract_actions(messages: list) -> List[str]:
    """Extract key actions performed by the agent from tool call messages."""
    actions = []
    for msg in messages:
        if msg.role != "assistant" or not msg.tool_calls:
            continue
        for tc in msg.tool_calls:
            name = tc.get("name", tc.get("function", {}).get("name", "unknown"))
            args = tc.get("input", tc.get("arguments", {}))
            if isinstance(args, str):
                try:
                    import json
                    args = json.loads(args)
                except Exception:
                    args = {}

            if name == "Shell":
                cmd = args.get("command", "")
                if len(cmd) > 120:
                    cmd = cmd[:117] + "..."
                actions.append(f"`{cmd}`")
            elif name == "Write":
                path = args.get("path", "")
                actions.append(f"Wrote file: `{path}`")
            elif name == "StrReplace":
                path = args.get("path", "")
                actions.append(f"Edited: `{path}`")
            elif name == "Read":
                path = args.get("path", "")
                actions.append(f"Read: `{path}`")
            elif name == "Delete":
                path = args.get("path", "")
                actions.append(f"Deleted: `{path}`")
            elif name == "K8s":
                action = args.get("action", "")
                component = args.get("component", "")
                actions.append(f"K8s {action} {component}".strip())
            elif name == "Grep":
                pattern = args.get("pattern", "")
                actions.append(f"Searched for: `{pattern}`")
            elif name == "Glob":
                pattern = args.get("glob_pattern", "")
                actions.append(f"Found files: `{pattern}`")
            else:
                actions.append(f"{name}({', '.join(f'{k}={repr(v)[:30]}' for k, v in list(args.items())[:3])})")
    return actions


def _extract_tool_calls(messages: list) -> List[str]:
    """Extract a deduplicated summary of tool types used."""
    tool_counts: Dict[str, int] = {}
    for msg in messages:
        if msg.role != "assistant" or not msg.tool_calls:
            continue
        for tc in msg.tool_calls:
            name = tc.get("name", tc.get("function", {}).get("name", "unknown"))
            tool_counts[name] = tool_counts.get(name, 0) + 1

    return [f"**{name}**: {count}x" for name, count in sorted(tool_counts.items(), key=lambda x: -x[1])]


def _slugify(text: str, max_words: int = 6, max_chars: int = 50) -> str:
    """Create a URL-safe slug from text."""
    words = text.lower().split()[:max_words]
    slug = "-".join(words)
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    return slug[:max_chars].rstrip("-") or "session"


def _git_diff_stat() -> str:
    """Get git diff --stat output, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""
