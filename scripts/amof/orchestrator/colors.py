"""ANSI color helpers for the AMOF agent shell.

Provides color constants and formatting functions for terminal output.
Automatically disables colors when stdout is not a TTY (piped/redirected).
"""

from __future__ import annotations

import os
import sys


def _supports_color() -> bool:
    """Check if the terminal supports ANSI colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


_COLOR = _supports_color()


# ── ANSI codes ──────────────────────────────────────────────────

RESET = "\033[0m" if _COLOR else ""
BOLD = "\033[1m" if _COLOR else ""
DIM = "\033[2m" if _COLOR else ""
ITALIC = "\033[3m" if _COLOR else ""

# Foreground colors
BLACK = "\033[30m" if _COLOR else ""
RED = "\033[31m" if _COLOR else ""
GREEN = "\033[32m" if _COLOR else ""
YELLOW = "\033[33m" if _COLOR else ""
BLUE = "\033[34m" if _COLOR else ""
MAGENTA = "\033[35m" if _COLOR else ""
CYAN = "\033[36m" if _COLOR else ""
WHITE = "\033[37m" if _COLOR else ""

# Bright variants
BRIGHT_BLACK = "\033[90m" if _COLOR else ""  # gray
BRIGHT_RED = "\033[91m" if _COLOR else ""
BRIGHT_GREEN = "\033[92m" if _COLOR else ""
BRIGHT_YELLOW = "\033[93m" if _COLOR else ""
BRIGHT_BLUE = "\033[94m" if _COLOR else ""
BRIGHT_MAGENTA = "\033[95m" if _COLOR else ""
BRIGHT_CYAN = "\033[96m" if _COLOR else ""
BRIGHT_WHITE = "\033[97m" if _COLOR else ""


# ── Semantic aliases ────────────────────────────────────────────
# Used throughout the shell for consistent theming.

USER = GREEN                  # user prompt and input
AGENT = CYAN                  # agent replies
ACTION = DIM                  # tool calls, progress
PLAN = BRIGHT_YELLOW          # planning phase
THINKING = DIM + ITALIC       # extended thinking (faded italic)
QUESTION = MAGENTA            # questions from planner/agent
ERROR = RED                   # errors
SUCCESS = BRIGHT_GREEN        # success messages
INFO = BRIGHT_BLACK           # dim informational text
HEADER = BOLD + BRIGHT_WHITE  # banners, section headers


# ── Formatting helpers ──────────────────────────────────────────


def user(text: str) -> str:
    """Format text as user message."""
    return f"{USER}{text}{RESET}"


def agent(text: str) -> str:
    """Format text as agent reply."""
    return f"{AGENT}{text}{RESET}"


def action(text: str) -> str:
    """Format text as agent action (tool call, progress)."""
    return f"{ACTION}{text}{RESET}"


def plan(text: str) -> str:
    """Format text as planning phase output."""
    return f"{PLAN}{text}{RESET}"


def question(text: str) -> str:
    """Format text as a question/prompt."""
    return f"{QUESTION}{text}{RESET}"


def error(text: str) -> str:
    """Format text as error."""
    return f"{ERROR}{text}{RESET}"


def success(text: str) -> str:
    """Format text as success."""
    return f"{SUCCESS}{text}{RESET}"


def info(text: str) -> str:
    """Format text as dim info."""
    return f"{INFO}{text}{RESET}"


def header(text: str) -> str:
    """Format text as bold header."""
    return f"{HEADER}{text}{RESET}"


def thinking(text: str) -> str:
    """Format text as extended thinking (faded italic)."""
    return f"{THINKING}{text}{RESET}"


def status_tag(tag: str, color: str = "") -> str:
    """Format a bracketed status tag like [OK] or [FAIL]."""
    c = color or ACTION
    return f"{c}[{tag}]{RESET}"
