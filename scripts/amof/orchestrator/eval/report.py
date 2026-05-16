"""Eval report generator — produces markdown comparison reports.

Takes EvalRun results and generates a human-readable markdown report
with per-tier comparison tables.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .harness import EvalRun, TaskResult


def generate_report(run: EvalRun, output_path: Optional[Path] = None) -> str:
    """Generate a markdown eval report from run results.

    Args:
        run: Complete eval run with results.
        output_path: If provided, writes the report to this file.

    Returns:
        The markdown report as a string.
    """
    lines: List[str] = []
    ts = datetime.fromtimestamp(run.timestamp).strftime("%Y-%m-%d %H:%M:%S")

    lines.append(f"# AMOF Eval Report")
    lines.append(f"")
    lines.append(f"**Date:** {ts}")
    lines.append(f"**Tiers tested:** {', '.join(run.tiers_tested)}")
    lines.append(f"**Tasks:** {len(run.tasks)}")
    lines.append(f"**Total results:** {len(run.results)}")
    lines.append(f"")

    # ---- Summary table ----
    lines.append(f"## Summary by Tier")
    lines.append(f"")
    lines.append(f"| Tier | Tasks | Success | Fail | Total Cost | Avg Latency | Avg Cost/Task |")
    lines.append(f"|------|-------|---------|------|------------|-------------|---------------|")

    for tier in run.tiers_tested:
        tier_results = [r for r in run.results if r.tier == tier]
        total = len(tier_results)
        successes = sum(1 for r in tier_results if r.success)
        failures = total - successes
        total_cost = sum(r.cost for r in tier_results)
        avg_latency = (
            sum(r.latency_ms for r in tier_results) / total if total > 0 else 0
        )
        avg_cost = total_cost / total if total > 0 else 0

        lines.append(
            f"| {tier} | {total} | {successes} | {failures} | "
            f"${total_cost:.4f} | {avg_latency:.0f}ms | ${avg_cost:.4f} |"
        )

    lines.append(f"")

    # ---- Per-task comparison ----
    lines.append(f"## Task Results")
    lines.append(f"")

    # Group results by task
    task_results: Dict[str, List[TaskResult]] = {}
    for r in run.results:
        task_results.setdefault(r.task_id, []).append(r)

    for task in run.tasks:
        results = task_results.get(task.id, [])
        if not results:
            continue

        lines.append(f"### {task.id}")
        lines.append(f"")
        lines.append(f"**Task:** {task.description}")
        lines.append(f"**Runner:** {task.runner}")
        lines.append(f"**Expected tools:** {', '.join(task.expected_tools) or 'none'}")
        lines.append(f"**Max cost:** ${task.max_cost:.2f}")
        lines.append(f"")

        lines.append(f"| Tier | Status | Cost | Latency | Tools Used | Expected Tools Hit |")
        lines.append(f"|------|--------|------|---------|------------|-------------------|")

        for r in sorted(results, key=lambda x: run.tiers_tested.index(x.tier)):
            status = "PASS" if r.success else "FAIL"
            tools = ", ".join(r.tool_calls) or "none"
            expected = "Yes" if r.expected_tools_hit else "No"
            error_note = f" ({r.error[:40]})" if r.error else ""

            lines.append(
                f"| {r.tier} | {status}{error_note} | ${r.cost:.4f} | "
                f"{r.latency_ms}ms | {tools} | {expected} |"
            )

        lines.append(f"")

    # ---- Cost efficiency analysis ----
    lines.append(f"## Cost Efficiency Analysis")
    lines.append(f"")

    for tier in run.tiers_tested:
        tier_results = [r for r in run.results if r.tier == tier]
        if not tier_results:
            continue

        successes = [r for r in tier_results if r.success]
        if successes:
            cost_per_success = sum(r.cost for r in successes) / len(successes)
        else:
            cost_per_success = 0.0

        success_rate = (
            (len(successes) / len(tier_results)) * 100 if tier_results else 0
        )
        total_cost = sum(r.cost for r in tier_results)

        lines.append(f"**{tier}:** {success_rate:.0f}% success rate, "
                      f"${total_cost:.4f} total, "
                      f"${cost_per_success:.4f}/success")

    lines.append(f"")

    # ---- Recommendations ----
    lines.append(f"## Recommendations")
    lines.append(f"")

    # Find the cheapest tier with good success rate
    best_value = None
    best_score = -1

    for tier in run.tiers_tested:
        tier_results = [r for r in run.results if r.tier == tier]
        if not tier_results:
            continue
        success_rate = sum(1 for r in tier_results if r.success) / len(tier_results)
        avg_cost = sum(r.cost for r in tier_results) / len(tier_results)
        # Score = success_rate / avg_cost (higher = better value)
        score = success_rate / max(avg_cost, 0.001)
        if score > best_score:
            best_score = score
            best_value = tier

    if best_value:
        lines.append(
            f"- **Best value tier:** `{best_value}` "
            f"(highest success-to-cost ratio)"
        )

    # Check if fast tier handles simple tasks well
    fast_results = [r for r in run.results if r.tier == "fast"]
    fast_simple = [r for r in fast_results if r.task_id in ("simple-question", "shell-echo", "read-readme")]
    if fast_simple:
        fast_simple_success = sum(1 for r in fast_simple if r.success) / len(fast_simple)
        if fast_simple_success >= 0.8:
            lines.append(
                f"- **Fast tier** handles simple tasks well ({fast_simple_success:.0%} success). "
                f"Keep using it for exploration and summarization."
            )

    # Check if strong tier is needed for complex tasks
    strong_results = [r for r in run.results if r.tier == "strong"]
    strong_complex = [r for r in strong_results if r.task_id in ("complex-architecture",)]
    if strong_complex and all(r.success for r in strong_complex):
        lines.append(
            f"- **Strong tier** succeeds on complex architecture analysis. "
            f"Reserve for planning and high-complexity tasks."
        )

    lines.append(f"")

    report = "\n".join(lines)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")

    return report
