"""CLI command: amof eval — run the eval harness.

Runs predefined tasks through the agent at different model tiers,
comparing cost, latency, and success rate. Generates a markdown report.
"""

from __future__ import annotations

import os
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def cmd_eval(
    manifest: Dict[str, Any],
    tiers: Optional[List[str]] = None,
    tasks_file: Optional[str] = None,
    task_filter: Optional[List[str]] = None,
    provider: Optional[str] = None,
    verbose: bool = False,
    output_dir: Optional[str] = None,
) -> int:
    """Run the eval harness.

    Args:
        manifest: Ecosystem manifest.
        tiers: Tiers to test (default: all available).
        tasks_file: Custom tasks YAML (default: built-in).
        task_filter: Only run these task IDs.
        provider: LLM provider (anthropic/openai/openrouter).
        verbose: Show per-step output.
        output_dir: Directory for report output.

    Returns:
        Exit code (0=success).
    """
    from ..orchestrator.eval.harness import EvalHarness
    from ..orchestrator.eval.report import generate_report
    from ..orchestrator.llm.base import LLMClient
    from ..orchestrator.tools.base import (
        Guardrails,
        GuardrailConfig,
        ToolRegistry,
        create_default_registry,
    )

    workspace_root = Path.cwd()

    # Load agent config
    agent_cfg_path = workspace_root / ".amof" / "agent.yaml"
    cfg: Dict[str, Any] = {}
    if agent_cfg_path.exists():
        import yaml
        with open(agent_cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    # Determine provider
    if provider is None:
        provider = cfg.get("default_provider", "anthropic")

    # Load .env
    env_path = workspace_root / ".env"
    if env_path.exists():
        _load_env(env_path)

    api_key = _get_api_key(provider)
    if not api_key:
        sys.stderr.write(
            f"[eval] No API key found for provider '{provider}'. "
            f"Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY in .env\n"
        )
        return 1

    # Build model clients for each tier
    model_clients = _build_model_clients(provider, api_key, cfg)

    if tiers:
        # Filter to requested tiers
        model_clients = {t: c for t, c in model_clients.items() if t in tiers}

    if not model_clients:
        sys.stderr.write("[eval] No model clients available for requested tiers.\n")
        return 1

    # Set up guardrails and tool registry
    guardrail_config = GuardrailConfig.load(
        workspace_root / ".amof" / "rules" / "guardrails.yaml"
    )
    guardrails = Guardrails(
        no_touch_paths=[],
        readonly_repos={},
        mode="eval",
        config=guardrail_config,
    )
    tool_registry = create_default_registry(guardrails=guardrails)

    # Load runner factory if available
    runner_factory = None
    runners_config = workspace_root / ".amof" / "rules" / "runners.yaml"
    if runners_config.exists():
        from ..orchestrator.runners import RunnerFactory

        runner_factory = RunnerFactory.from_config(
            config_path=runners_config,
            model_clients=model_clients,
            parent_tools=tool_registry,
            guardrails=guardrails,
            workspace_root=workspace_root,
            verbose=verbose,
        )

    # Load system prompt
    from ..orchestrator.prompt_loader import load_prompt

    system_prompt = load_prompt(
        "master",
        prompts_dir=workspace_root / "prompts",
        fallback="You are a helpful AI assistant.",
    )

    # Create harness
    harness = EvalHarness(
        model_clients=model_clients,
        tool_registry=tool_registry,
        runner_factory=runner_factory,
        system_prompt=system_prompt,
        verbose=verbose,
    )

    # Load tasks
    tasks_path = Path(tasks_file) if tasks_file else None
    tasks = harness.load_tasks(tasks_path)

    if task_filter:
        filtered_count = sum(1 for t in tasks if t.id in task_filter)
        sys.stderr.write(
            f"[eval] Running {filtered_count}/{len(tasks)} tasks "
            f"(filtered) across tiers: {', '.join(model_clients.keys())}\n"
        )
    else:
        sys.stderr.write(
            f"[eval] Running {len(tasks)} tasks across tiers: "
            f"{', '.join(model_clients.keys())}\n"
        )

    # Run
    run = harness.run_all(tasks=tasks, task_filter=task_filter)

    # Generate report
    ecosystem = manifest.get("ecosystem", "default")
    if output_dir:
        report_dir = Path(output_dir)
    else:
        report_dir = workspace_root / "ecosystems" / ecosystem / "reports"

    ts = datetime.now().strftime("%Y-%m-%d")
    report_path = report_dir / f"eval-{ts}.md"

    report = generate_report(run, output_path=report_path)

    # Print summary to stdout
    print(report)

    sys.stderr.write(f"\n[eval] Report saved to: {report_path}\n")

    # Exit code based on overall success rate
    total = len(run.results)
    successes = sum(1 for r in run.results if r.success)
    if total > 0 and successes / total < 0.5:
        sys.stderr.write(
            f"[eval] Warning: low success rate ({successes}/{total})\n"
        )
        return 1

    return 0


def _load_env(path: Path) -> None:
    """Simple .env file loader."""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _get_api_key(provider: str) -> Optional[str]:
    """Get API key for provider from environment."""
    if provider == "openai":
        return os.environ.get("OPENAI_API_KEY")
    if provider == "openrouter":
        return os.environ.get("OPENROUTER_API_KEY")
    return os.environ.get("ANTHROPIC_API_KEY")


def _build_model_clients(
    provider: str, api_key: str, cfg: Dict[str, Any]
) -> Dict[str, Any]:
    """Build model clients for all tiers."""
    from ..orchestrator.llm.anthropic import AnthropicClient

    if provider in ("openai", "openrouter"):
        from ..orchestrator.llm.openai_client import OpenAIClient

        if provider == "openrouter":
            fast_model = cfg.get("openrouter_fast", "openrouter/openai/gpt-4o-mini")
            standard_model = cfg.get("openrouter_standard", "openrouter/openai/gpt-4o")
            strong_model = cfg.get("openrouter_strong", "openrouter/openai/gpt-4.1")
        else:
            fast_model = cfg.get("openai_fast", "gpt-4o-mini")
            standard_model = cfg.get("openai_standard", "gpt-4o")
            strong_model = cfg.get("openai_strong", "gpt-5.1-codex")

        return {
            "fast": OpenAIClient(api_key=api_key, model=fast_model),
            "standard": OpenAIClient(api_key=api_key, model=standard_model),
            "strong": OpenAIClient(api_key=api_key, model=strong_model),
        }
    else:
        fast = cfg.get("anthropic_fast", "claude-haiku-4-5")
        standard = cfg.get("anthropic_standard", "claude-sonnet-4-5")
        strong = cfg.get("anthropic_strong", "claude-opus-4-6")
        return {
            "fast": AnthropicClient(api_key=api_key, model=fast),
            "standard": AnthropicClient(api_key=api_key, model=standard),
            "strong": AnthropicClient(api_key=api_key, model=strong),
        }
