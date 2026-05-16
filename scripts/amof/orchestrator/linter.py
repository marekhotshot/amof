"""Linter runner — runs configured linters on files and returns diagnostics.

All linter definitions come from .amof/rules/linters.yaml.
Python code contains zero hardcoded linter names or commands.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Regex to strip ANSI escape sequences from linter output
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;;[^\x1b]*\x1b\\")

# Summary lines from linters that are not actual diagnostics
_SKIP_PATTERNS = re.compile(
    r"^(All checks passed|Found \d+ error|"
    r"\[\*\] \d+ fixable|\s*$)",
    re.IGNORECASE,
)

_DEFAULT_CONFIG_PATH = ".amof/rules/linters.yaml"


# ── Data classes ─────────────────────────────────────────────────


@dataclass
class Diagnostic:
    """A single linter diagnostic."""

    file: str
    line: int
    column: int
    severity: str  # "error", "warning", "info"
    message: str
    linter: str

    def __str__(self) -> str:
        loc = f"{self.file}:{self.line}"
        if self.column > 0:
            loc += f":{self.column}"
        return f"{loc}: {self.severity}: {self.message} [{self.linter}]"


@dataclass
class LinterDef:
    """A linter definition loaded from config."""

    name: str
    command: str  # template with {file} placeholder
    extensions: List[str] = field(default_factory=list)
    timeout: int = 10
    optional: bool = True


@dataclass
class LinterConfig:
    """Parsed linter configuration from .amof/rules/linters.yaml."""

    auto_lint: bool = True
    linters: List[LinterDef] = field(default_factory=list)
    max_lines_per_file: int = 30
    min_severity: str = "warning"


# ── YAML parsing ────────────────────────────────────────────────


def _parse_yaml(text: str) -> Dict[str, Any]:
    """Parse YAML text, trying PyYAML first then falling back to simple parser."""
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        pass

    # Fallback: simple line-based parser for linters.yaml structure.
    # Handles top-level keys, nested dicts (2-space indent), and inline lists.
    result: Dict[str, Any] = {}
    current_section: Optional[str] = None
    current_item: Optional[str] = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Calculate indent level
        indent = len(line) - len(line.lstrip())

        if indent == 0 and ":" in stripped:
            # Top-level key
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()
            current_section = key
            current_item = None
            if val:
                # Inline value
                if val.lower() == "true":
                    result[key] = True
                elif val.lower() == "false":
                    result[key] = False
                elif val.isdigit():
                    result[key] = int(val)
                else:
                    result[key] = val.strip('"').strip("'")
            else:
                result.setdefault(key, {})

        elif indent == 2 and ":" in stripped and current_section:
            # Second-level key (e.g. linter name)
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()
            current_item = key
            parent = result.get(current_section)
            if isinstance(parent, dict):
                if val:
                    parent[key] = val.strip('"').strip("'")
                else:
                    parent.setdefault(key, {})

        elif indent == 4 and ":" in stripped and current_section and current_item:
            # Third-level key (e.g. command, extensions, timeout)
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()
            parent = result.get(current_section)
            if isinstance(parent, dict):
                item = parent.get(current_item)
                if isinstance(item, dict):
                    if val.startswith("[") and val.endswith("]"):
                        # Inline list: [".py", ".yaml"]
                        inner = val[1:-1]
                        items = [
                            s.strip().strip('"').strip("'")
                            for s in inner.split(",")
                            if s.strip()
                        ]
                        item[key] = items
                    elif val.lower() == "true":
                        item[key] = True
                    elif val.lower() == "false":
                        item[key] = False
                    elif val.isdigit():
                        item[key] = int(val)
                    elif val:
                        item[key] = val.strip('"').strip("'")

    return result


# ── Config loading ───────────────────────────────────────────────


def load_config(config_path: Optional[Path] = None) -> LinterConfig:
    """Load linter configuration from YAML file.

    Args:
        config_path: Path to linters.yaml. If None, uses
                     .amof/rules/linters.yaml relative to cwd.

    Returns:
        LinterConfig populated from file, or defaults if file missing.
    """
    cfg = LinterConfig()
    path = config_path or (Path.cwd() / _DEFAULT_CONFIG_PATH)

    if not path.exists():
        logger.info(
            "Linters config not found at %s — auto-lint disabled. "
            "Create this file to enable linting.",
            path,
        )
        cfg.auto_lint = False
        return cfg

    try:
        text = path.read_text(encoding="utf-8")
        data = _parse_yaml(text)
    except Exception as e:
        logger.error("Failed to parse linters config %s: %s", path, e)
        cfg.auto_lint = False
        return cfg

    # Top-level settings
    cfg.auto_lint = bool(data.get("auto_lint", True))
    cfg.max_lines_per_file = int(data.get("max_lines_per_file", 30))
    cfg.min_severity = str(data.get("min_severity", "warning")).lower()

    # Parse linter definitions
    linters_data = data.get("linters", {})
    if isinstance(linters_data, dict):
        for name, ldef in linters_data.items():
            if not isinstance(ldef, dict):
                continue
            command = ldef.get("command", "")
            if not command:
                logger.warning("Linter '%s' has no command — skipping.", name)
                continue

            extensions = ldef.get("extensions", [])
            if isinstance(extensions, str):
                extensions = [extensions]

            cfg.linters.append(
                LinterDef(
                    name=name,
                    command=str(command),
                    extensions=[str(e) for e in extensions],
                    timeout=int(ldef.get("timeout", 10)),
                    optional=bool(ldef.get("optional", True)),
                )
            )

    linter_names = [ldef.name for ldef in cfg.linters]
    logger.info(
        "Loaded linters config from %s: auto_lint=%s, linters=%s",
        path,
        cfg.auto_lint,
        linter_names,
    )
    return cfg


# ── Severity helpers ─────────────────────────────────────────────

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _severity_rank(severity: str) -> int:
    """Return numeric rank for severity (lower = more severe)."""
    return _SEVERITY_ORDER.get(severity.lower(), 2)


def _passes_severity_filter(diagnostic_line: str, min_severity: str) -> bool:
    """Check if a diagnostic line meets the minimum severity threshold.

    Tries to detect severity keywords in the line. If no severity keyword
    is found, the line passes (included by default).
    """
    line_lower = diagnostic_line.lower()
    min_rank = _severity_rank(min_severity)

    # Check for explicit severity keywords
    for sev, rank in _SEVERITY_ORDER.items():
        if sev in line_lower:
            return rank <= min_rank

    # No severity keyword found — include by default
    return True


# ── LinterRunner ─────────────────────────────────────────────────


class LinterRunner:
    """Runs configured linters on files and returns diagnostics.

    Used by both the post-edit hook (auto-lint) and the ReadLints tool.
    """

    def __init__(self, config: Optional[LinterConfig] = None, config_path: Optional[Path] = None):
        """Initialize with a config or load from path.

        Args:
            config: Pre-loaded LinterConfig. Takes priority over config_path.
            config_path: Path to linters.yaml. Used if config is None.
        """
        self.config = config or load_config(config_path)
        self._binary_cache: Dict[str, Optional[str]] = {}
        # Build an extended PATH that includes the current Python's venv bin dir.
        # This ensures linters installed in the venv (e.g. ruff) are found even
        # when the shell PATH doesn't include the venv.
        self._env = os.environ.copy()
        venv_bin = os.path.join(sys.prefix, "bin")
        current_path = self._env.get("PATH", "")
        if venv_bin not in current_path.split(os.pathsep):
            self._env["PATH"] = venv_bin + os.pathsep + current_path

    @property
    def auto_lint_enabled(self) -> bool:
        """Whether auto-lint after edits is enabled."""
        return self.config.auto_lint

    def _find_binary(self, command_template: str) -> Optional[str]:
        """Extract binary name from command template and check if it exists.

        Searches both the system PATH and the venv bin directory.
        """
        binary = command_template.split()[0] if command_template else ""
        if not binary:
            return None

        if binary not in self._binary_cache:
            # Search with extended PATH that includes venv
            self._binary_cache[binary] = shutil.which(
                binary, path=self._env.get("PATH")
            )

        return self._binary_cache[binary]

    def _matching_linters(self, file_path: str) -> List[LinterDef]:
        """Return linters whose extensions match the given file."""
        ext = os.path.splitext(file_path)[1].lower()
        if not ext:
            return []
        return [ld for ld in self.config.linters if ext in ld.extensions]

    def lint_file(self, file_path: str) -> List[str]:
        """Run all matching linters on a single file.

        Returns:
            List of diagnostic lines (human-readable, one per issue).
        """
        path = Path(file_path)
        if not path.is_file():
            return []

        linters = self._matching_linters(file_path)
        if not linters:
            return []

        all_lines: List[str] = []

        for ldef in linters:
            binary_path = self._find_binary(ldef.command)
            if not binary_path:
                if ldef.optional:
                    logger.debug(
                        "Linter '%s' binary not found — skipping (optional).",
                        ldef.name,
                    )
                else:
                    logger.warning(
                        "Linter '%s' binary not found and is NOT optional.",
                        ldef.name,
                    )
                    all_lines.append(
                        f"[{ldef.name}] ERROR: binary not found. "
                        f"Install it or set optional: true in linters.yaml."
                    )
                continue

            # Build command from template
            cmd_str = ldef.command.replace("{file}", str(path))

            try:
                proc = subprocess.run(
                    cmd_str,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=ldef.timeout,
                    cwd=Path.cwd(),
                    env=self._env,
                )

                # Most linters use exit code 1 for "issues found" (not a failure)
                output = proc.stdout.strip()
                if proc.returncode > 1 and proc.stderr.strip():
                    # Actual error running the linter
                    all_lines.append(
                        f"[{ldef.name}] runner error (exit {proc.returncode}): "
                        f"{proc.stderr.strip()[:200]}"
                    )
                elif output:
                    # Strip ANSI escape codes and filter
                    clean_output = _ANSI_ESCAPE.sub("", output)
                    for line in clean_output.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        # Skip summary/meta lines (e.g. "Found 2 errors.")
                        if _SKIP_PATTERNS.match(line):
                            continue
                        if _passes_severity_filter(line, self.config.min_severity):
                            all_lines.append(line)

            except subprocess.TimeoutExpired:
                all_lines.append(
                    f"[{ldef.name}] timed out after {ldef.timeout}s on {file_path}"
                )
            except Exception as e:
                logger.exception("Linter '%s' failed on %s", ldef.name, file_path)
                all_lines.append(f"[{ldef.name}] error: {type(e).__name__}: {e}")

        # Truncate if too many lines
        max_lines = self.config.max_lines_per_file
        if len(all_lines) > max_lines:
            truncated = all_lines[:max_lines]
            truncated.append(
                f"... ({len(all_lines) - max_lines} more diagnostics truncated)"
            )
            return truncated

        return all_lines

    def lint_files(self, file_paths: List[str]) -> Dict[str, List[str]]:
        """Run linters on multiple files.

        Returns:
            Dict mapping file path -> list of diagnostic lines.
            Only files with diagnostics are included.
        """
        results: Dict[str, List[str]] = {}
        for fp in file_paths:
            diags = self.lint_file(fp)
            if diags:
                results[fp] = diags
        return results

    def format_diagnostics(self, file_diagnostics: Dict[str, List[str]]) -> str:
        """Format diagnostics for human/LLM consumption.

        Args:
            file_diagnostics: Dict from lint_files().

        Returns:
            Formatted string with diagnostics grouped by file.
        """
        if not file_diagnostics:
            return ""

        parts: List[str] = []
        for fp, lines in file_diagnostics.items():
            parts.append(fp)
            for line in lines:
                parts.append(f"  {line}")
        return "\n".join(parts)

    def lint_file_formatted(self, file_path: str) -> str:
        """Lint a single file and return formatted output.

        Convenience method for the post-edit hook.
        Returns empty string if no diagnostics.
        """
        lines = self.lint_file(file_path)
        if not lines:
            return ""
        return "\n".join(lines)
