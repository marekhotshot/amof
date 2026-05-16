"""Prompt loader — loads system prompts from the prompts/ folder.

All system prompts used by any component (master, planner, runners, summarizer,
indexer, executor, etc.) are stored as .md files in the prompts/ folder.

This allows fine-tuning agent behavior by editing markdown files without
touching Python code. Prompts are loaded from disk at use time (not cached)
so changes are picked up immediately.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default prompts directory (relative to workspace root)
DEFAULT_PROMPTS_DIR = "prompts"


def load_prompt(
    name: str,
    prompts_dir: Optional[Path] = None,
    fallback: Optional[str] = None,
) -> str:
    """Load a system prompt from the prompts/ folder.

    Args:
        name: Prompt name without extension (e.g. 'master', 'runners/k8s',
              'summarizer'). Will load from prompts/<name>.md.
        prompts_dir: Absolute path to prompts directory. If None, searches
                     common locations relative to the module.
        fallback: If provided and the file is missing, return this string
                  instead of raising. A warning is logged.

    Returns:
        The prompt text (file contents).

    Raises:
        FileNotFoundError: If the prompt file doesn't exist and no fallback.
    """
    if prompts_dir is None:
        prompts_dir = _find_prompts_dir()

    prompt_path = prompts_dir / f"{name}.md"

    if prompt_path.is_file():
        text = prompt_path.read_text(encoding="utf-8").strip()
        if text:
            return text
        logger.warning("Prompt file is empty: %s", prompt_path)

    if fallback is not None:
        logger.warning(
            "Prompt file not found: %s — using inline fallback", prompt_path
        )
        return fallback

    raise FileNotFoundError(f"Prompt file not found: {prompt_path}")


def _find_prompts_dir() -> Path:
    """Locate the prompts/ directory by walking up from this module's location.

    Searches: module dir → parent → grandparent → ... up to 5 levels.
    Falls back to cwd/prompts if nothing found.
    """
    # Start from this file's directory
    here = Path(__file__).resolve().parent

    # Walk up to find a prompts/ dir (should be at workspace root)
    for _ in range(6):
        candidate = here / DEFAULT_PROMPTS_DIR
        if candidate.is_dir():
            return candidate
        here = here.parent

    # Fallback: cwd / prompts
    cwd_prompts = Path.cwd() / DEFAULT_PROMPTS_DIR
    if cwd_prompts.is_dir():
        return cwd_prompts

    # Last resort: return a path that will fail gracefully
    return Path.cwd() / DEFAULT_PROMPTS_DIR
