"""Base classes for the tool system.

Mirrors Cursor's tool interface: each tool has a name, description, JSON Schema
for parameters, and an execute method that returns a ToolResult.

Guardrail rules are loaded from .amof/rules/guardrails.yaml — no hardcoded
lists in Python. Edit that file to customize protections (similar to .cursor/rules).

In interactive mode, sensitive and dangerous commands prompt the user for
confirmation with an "always allow" option. Persistent allowances are saved
to .amof/rules/allowed.yaml.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

if TYPE_CHECKING:
    from ..linter import LinterRunner
    from ..events import EventLog

from ..trust_boundary import (
    MinimalToolPolicyGate,
    PolicyInput,
    TrustState,
    classify_tool_capabilities,
    create_trust_state,
    record_untrusted_tool_output,
)

logger = logging.getLogger(__name__)

# Default path for guardrails config (relative to workspace root)
_DEFAULT_RULES_PATH = ".amof/rules/guardrails.yaml"
_DEFAULT_ALLOWED_PATH = ".amof/rules/allowed.yaml"
_PUBLIC_DEFAULT_PROTECTED_PATHS = [
    ".git/**",
    "**/.git/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/*id_rsa*",
    "**/*id_ed25519*",
    "**/*private_key*",
    "**/*credentials*",
]
_PUBLIC_DEFAULT_PROTECTED_FRAGMENTS = [
    "/.git/",
    "/.ssh/",
    "/.gnupg/",
    "BEGIN PRIVATE KEY",
]
_PUBLIC_DEFAULT_PROTECTED_BASENAMES = [
    ".env",
    ".env.*",
    "credentials.json",
    "service-account.json",
]
_PUBLIC_DEFAULT_BLOCKED_COMMANDS = [
    "git push",
    "push --force",
    "rm -rf /",
    "curl | sh",
    "wget | sh",
]
_PUBLIC_DEFAULT_DANGEROUS_PATTERNS = [
    "rm -rf",
    "sudo ",
    "chmod -R 777",
    "chown -R",
]
_PUBLIC_DEFAULT_SENSITIVE_COMMANDS = [
    "git push",
    "git tag",
    "docker push",
    "kubectl delete",
    "terraform apply",
]


@dataclass
class ToolResult:
    """Result of a tool execution."""

    success: bool
    output: str
    error: Optional[str] = None
    cancelled: bool = False

    def to_text(self) -> str:
        """Convert to text for LLM consumption."""
        if self.success:
            return self.output
        return f"Error: {self.error}\n{self.output}" if self.output else f"Error: {self.error}"


@dataclass
class ToolCall:
    """A tool call from the LLM."""

    id: str
    name: str
    arguments: Dict[str, Any]


class Tool(ABC):
    """Abstract base class for all tools.

    Each tool mirrors a Cursor tool with the same name, parameter schema,
    and return format.
    """

    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema

    @abstractmethod
    def execute(self, **params: Any) -> ToolResult:
        """Execute the tool with given parameters."""

    def schema(self) -> Dict[str, Any]:
        """Return the tool definition for LLM consumption (Anthropic format)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


class GuardrailConfig:
    """Parsed guardrail configuration loaded from .amof/rules/guardrails.yaml.

    All rule lists come from the config file — Python has zero hardcoded lists.
    If the config file is missing, all lists default to empty (no protection).
    """

    def __init__(self) -> None:
        # Write protection
        self.protected_paths: List[str] = []
        self.protected_fragments: List[str] = []
        self.protected_basenames: List[str] = []
        self.protected_extensions: List[str] = []
        # Shell safety
        self.blocked_commands: List[str] = []
        self.dangerous_patterns: List[str] = []
        self.sensitive_commands: List[str] = []

    @classmethod
    def public_defaults(cls) -> "GuardrailConfig":
        cfg = cls()
        cfg.protected_paths = list(_PUBLIC_DEFAULT_PROTECTED_PATHS)
        cfg.protected_fragments = list(_PUBLIC_DEFAULT_PROTECTED_FRAGMENTS)
        cfg.protected_basenames = list(_PUBLIC_DEFAULT_PROTECTED_BASENAMES)
        cfg.blocked_commands = list(_PUBLIC_DEFAULT_BLOCKED_COMMANDS)
        cfg.dangerous_patterns = list(_PUBLIC_DEFAULT_DANGEROUS_PATTERNS)
        cfg.sensitive_commands = list(_PUBLIC_DEFAULT_SENSITIVE_COMMANDS)
        return cfg

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "GuardrailConfig":
        """Load guardrail config from YAML file.

        Args:
            config_path: Path to guardrails.yaml. If None, uses
                         .amof/rules/guardrails.yaml relative to cwd.

        Returns:
            GuardrailConfig populated from file, or packaged public defaults if missing.
        """
        cfg = cls()
        path = config_path or (Path.cwd() / _DEFAULT_RULES_PATH)

        if not path.exists():
            logger.info(
                "Guardrails config not found at %s; using packaged public defaults.",
                path,
            )
            return cls.public_defaults()

        try:
            text = path.read_text(encoding="utf-8")
            data = _parse_yaml(text)
        except Exception as e:
            logger.error("Failed to parse guardrails config %s: %s", path, e)
            return cfg

        def _get_list(key: str) -> List[str]:
            val = data.get(key, [])
            if isinstance(val, list):
                return [str(x) for x in val]
            return []

        cfg.protected_paths = _get_list("protected_paths")
        cfg.protected_fragments = _get_list("protected_fragments")
        cfg.protected_basenames = _get_list("protected_basenames")
        cfg.protected_extensions = _get_list("protected_extensions")
        cfg.blocked_commands = _get_list("blocked_commands")
        cfg.dangerous_patterns = _get_list("dangerous_patterns")
        cfg.sensitive_commands = _get_list("sensitive_commands")

        logger.info(
            "Loaded guardrails config from %s: %d protected paths, "
            "%d blocked commands, %d sensitive commands",
            path, len(cfg.protected_paths), len(cfg.blocked_commands),
            len(cfg.sensitive_commands),
        )
        return cfg


def _parse_yaml(text: str) -> Dict[str, Any]:
    """Parse YAML text, trying PyYAML first then falling back to simple parser."""
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        pass

    # Fallback: reuse the manifest's simple parser
    try:
        from ...manifest import simple_parse_yaml
        return simple_parse_yaml(text) or {}
    except Exception:
        pass

    # Minimal fallback: parse key: [list] lines
    result: Dict[str, Any] = {}
    current_key: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if current_key is not None:
                val = stripped[2:].strip().strip('"').strip("'")
                if val:
                    result.setdefault(current_key, []).append(val)
        elif ":" in stripped and not stripped.startswith("- "):
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()
            current_key = key
            if val:
                result[key] = val
            else:
                result.setdefault(key, [])
    return result


class Guardrails:
    """Enforces guardrail rules loaded from .amof/rules/guardrails.yaml.

    No hardcoded lists — all rules come from config. Edit the YAML file
    to customize protections (similar to .cursor/rules).

    Three enforcement levels:
    - HARD BLOCK: Prevented by config rules (protected paths, blocked commands)
    - SENSITIVE: Blocked in unattended mode, warned in interactive
    - MANIFEST: Blocked per ecosystem ecosystem.yaml config (no_touch_paths, readonly repos)
    """

    # Type alias for the confirmation callback.
    # Takes (command, reason) -> "yes" | "no" | "always"
    ConfirmFn = Callable[[str, str], str]

    def __init__(
        self,
        no_touch_paths: Optional[List[str]] = None,
        readonly_repos: Optional[Dict[str, Path]] = None,
        mode: str = "build",
        command_allowlist: Optional[List[str]] = None,
        unattended: bool = False,
        config: Optional[GuardrailConfig] = None,
        config_path: Optional[Path] = None,
        confirm_fn: Optional["Guardrails.ConfirmFn"] = None,
    ):
        self.no_touch_paths = no_touch_paths or []
        self.readonly_repos = readonly_repos or {}
        self.mode = mode
        self.command_allowlist = command_allowlist
        self.unattended = unattended
        self.confirm_fn = confirm_fn

        # Load config from file (or use provided config)
        self.config = config or GuardrailConfig.load(config_path)

        # Persistent "always allow" set — loaded from .amof/rules/allowed.yaml
        self._allowed_path = Path.cwd() / _DEFAULT_ALLOWED_PATH
        self._always_allowed: Set[str] = self._load_always_allowed()

        # Telemetry counters (read by agent/telemetry)
        self.hard_blocks = 0
        self.sensitive_blocks = 0
        self.manifest_blocks = 0
        self.user_confirmed = 0
        self.user_rejected = 0

    def check_write(self, path: str) -> Optional[str]:
        """Check if writing to path is allowed. Returns error message or None."""
        if self.mode == "plan":
            self.hard_blocks += 1
            return "Write operations are blocked in PLAN mode"

        from fnmatch import fnmatch

        # 1. Config: protected_paths (glob patterns)
        for pattern in self.config.protected_paths:
            if fnmatch(path, pattern) or fnmatch(path, f"**/{pattern}"):
                self.hard_blocks += 1
                return f"BLOCKED: Path '{path}' matches protected pattern '{pattern}'"

        # 2. Config: protected_fragments (substring match)
        normalized = ("/" + path.replace("\\", "/")).lower()
        for fragment in self.config.protected_fragments:
            frag_lower = fragment.lower()
            if frag_lower in normalized:
                self.hard_blocks += 1
                return f"BLOCKED: Path '{path}' contains protected fragment '{fragment}'"

        # 3. Config: protected_basenames
        basename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
        for pattern in self.config.protected_basenames:
            if pattern.endswith(".*"):
                prefix = pattern[:-2].lower()
                if basename == prefix or basename.startswith(prefix + "."):
                    self.hard_blocks += 1
                    return f"BLOCKED: Path '{path}' matches protected basename '{pattern}'"
            elif basename == pattern.lower():
                self.hard_blocks += 1
                return f"BLOCKED: Path '{path}' matches protected basename '{pattern}'"

        # 4. Config: protected_extensions
        for ext in self.config.protected_extensions:
            if normalized.endswith(ext.lower()):
                self.hard_blocks += 1
                return f"BLOCKED: Path '{path}' has protected extension '{ext}'"

        # 5. Manifest: no_touch_paths
        for pattern in self.no_touch_paths:
            if fnmatch(path, pattern) or fnmatch(path, f"**/{pattern}"):
                self.manifest_blocks += 1
                return f"Path '{path}' matches no_touch_path pattern '{pattern}'"

        # 6. Manifest: readonly repos
        abs_path = Path(path).resolve()
        for repo_name, repo_path in self.readonly_repos.items():
            try:
                abs_path.relative_to(repo_path.resolve())
                self.manifest_blocks += 1
                return f"Repository '{repo_name}' is readonly"
            except ValueError:
                continue

        return None

    def check_shell(self, command: str) -> Optional[str]:
        """Check if shell command is allowed. Returns error message or None."""
        if self.mode == "plan":
            self.hard_blocks += 1
            return "Shell operations are blocked in PLAN mode"

        cmd_lower = command.strip().lower()

        # 1. Config: blocked_commands (always blocked, substring match)
        for blocked in self.config.blocked_commands:
            if blocked.lower() in cmd_lower:
                self.hard_blocks += 1
                return f"BLOCKED: Command contains '{blocked}'"

        # 2. Config: dangerous_patterns (prompt in interactive, warn in unattended)
        for pattern in self.config.dangerous_patterns:
            if pattern.lower() in cmd_lower:
                result = self._confirm_or_block(
                    command, pattern, f"Dangerous: matches '{pattern}'"
                )
                if result is not None:
                    return result

        # 3. Config: sensitive_commands (prompt in interactive, block in unattended)
        cmd_stripped = command.strip()
        for sensitive in self.config.sensitive_commands:
            if cmd_stripped.startswith(sensitive) or f" && {sensitive}" in cmd_stripped:
                result = self._confirm_or_block(
                    command, sensitive, f"Sensitive: matches '{sensitive}'"
                )
                if result is not None:
                    return result

        # 4. Subtask-level command allowlist
        if self.command_allowlist is not None:
            cmd_prefix = command.strip().split()[0] if command.strip() else ""
            if not any(command.strip().startswith(allowed) for allowed in self.command_allowlist):
                self.manifest_blocks += 1
                return f"Command '{cmd_prefix}' not in allowlist"

        return None

    def _confirm_or_block(
        self, command: str, pattern: str, reason: str
    ) -> Optional[str]:
        """Prompt user in interactive mode or block in unattended mode.

        Returns error message to block, or None to allow.
        """
        # Check if already in "always allow" set
        if pattern.lower() in self._always_allowed:
            logger.debug("Auto-allowed (persistent): %s", pattern)
            return None

        # Unattended mode: block outright
        if self.unattended:
            self.sensitive_blocks += 1
            return (
                f"BLOCKED (unattended mode): '{pattern}' requires explicit permission. "
                f"Add to subtask allowed_commands or run in interactive mode."
            )

        # Interactive mode without confirm callback: allow with warning (legacy)
        if not self.confirm_fn:
            logger.warning("Sensitive command allowed (no confirm_fn): %s", command[:100])
            return None

        # Interactive mode: prompt user
        response = self.confirm_fn(command, reason)

        if response == "always":
            self._always_allowed.add(pattern.lower())
            self._save_always_allowed()
            self.user_confirmed += 1
            logger.info("Always-allowed: '%s'", pattern)
            return None
        elif response == "yes":
            self.user_confirmed += 1
            return None
        else:
            self.user_rejected += 1
            return f"REJECTED by user: {reason}"

    def is_dangerous(self, command: str) -> bool:
        """Check if a command matches dangerous patterns (for warning, not blocking)."""
        cmd_lower = command.strip().lower()
        for pattern in self.config.dangerous_patterns:
            if pattern.lower() in cmd_lower:
                return True
        return False

    # ── Always-allowed persistence ────────────────────────────────

    def _load_always_allowed(self) -> Set[str]:
        """Load persistent always-allowed patterns from .amof/rules/allowed.yaml."""
        if not self._allowed_path.exists():
            return set()
        try:
            text = self._allowed_path.read_text(encoding="utf-8")
            data = _parse_yaml(text)
            items = data.get("always_allowed", [])
            if isinstance(items, list):
                return {str(x).lower() for x in items}
        except Exception as e:
            logger.warning("Failed to load allowed.yaml: %s", e)
        return set()

    def _save_always_allowed(self) -> None:
        """Persist always-allowed patterns to .amof/rules/allowed.yaml."""
        try:
            self._allowed_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                "# Persistently allowed commands/patterns.",
                "# Added via interactive 'always allow' confirmation.",
                "# Remove entries to re-enable prompting.",
                "",
                "always_allowed:",
            ]
            for item in sorted(self._always_allowed):
                lines.append(f'  - "{item}"')
            lines.append("")
            self._allowed_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to save allowed.yaml: %s", e)

    @property
    def violation_counts(self) -> Dict[str, int]:
        """Return guardrail violation counters for telemetry."""
        return {
            "hard_blocks": self.hard_blocks,
            "sensitive_blocks": self.sensitive_blocks,
            "manifest_blocks": self.manifest_blocks,
            "user_confirmed": self.user_confirmed,
            "user_rejected": self.user_rejected,
            "total": self.hard_blocks + self.sensitive_blocks + self.manifest_blocks + self.user_rejected,
        }


class ToolRegistry:
    """Registry of available tools with guardrail enforcement.

    Manages tool registration, schema generation for LLM, and
    execution with guardrail checks.
    """

    def __init__(
        self,
        guardrails: Optional[Guardrails] = None,
        linter: Optional["LinterRunner"] = None,
        events: Optional["EventLog"] = None,
        trust_state: Optional[TrustState] = None,
        policy_gate: Optional[Any] = None,
        policy_source: str = "master",
    ):
        self._tools: Dict[str, Tool] = {}
        self.guardrails = guardrails or Guardrails()
        self.max_output_chars: int = 50_000
        self._linter: Optional["LinterRunner"] = linter
        self._modified_files: set = set()  # files changed during session
        self.events = events
        self.trust_state = trust_state
        self.policy_gate = policy_gate
        self.policy_source = policy_source

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def initialize_trust_state(self, task_text: str) -> None:
        """Derive trust state from a top-level task if none exists yet."""
        if self.trust_state is None:
            self.trust_state = create_trust_state(task_text)

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        """List registered tool names."""
        return list(self._tools.keys())

    def schemas(self) -> List[Dict[str, Any]]:
        """Return all tool schemas for LLM consumption."""
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, tool_call: ToolCall) -> ToolResult:
        """Execute a tool call with guardrail checks and output truncation."""
        tool = self._tools.get(tool_call.name)
        if not tool:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown tool: {tool_call.name}",
            )

        # Pre-execution guardrail checks
        policy_error = self._check_policy(tool_call)
        if policy_error:
            return ToolResult(success=False, output="", error=policy_error)

        error = self._check_guardrails(tool_call)
        if error:
            return ToolResult(success=False, output="", error=error)
        
        # Validate arguments against schema
        validation_error = self._validate_arguments(tool, tool_call.arguments)
        if validation_error:
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid arguments for {tool_call.name}: {validation_error}",
            )

        try:
            result = tool.execute(**tool_call.arguments)
        except TypeError as e:
            # Provide helpful error message about missing/extra parameters
            import inspect
            sig = inspect.signature(tool.execute)
            params = [p for p in sig.parameters.keys() if p != "self"]
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid arguments for {tool_call.name}: {e}. Expected parameters: {params}",
            )
        except Exception as e:
            logger.exception("Tool execution failed: %s", tool_call.name)
            return ToolResult(
                success=False,
                output="",
                error=f"Tool execution error: {type(e).__name__}: {e}",
            )

        if result.success:
            record_untrusted_tool_output(tool_call.name, self.trust_state)

        # Track modified files for end-of-task linting
        if result.success and tool_call.name in ("Write", "StrReplace"):
            file_path = tool_call.arguments.get("path", "")
            if file_path:
                self._modified_files.add(file_path)

        # Truncate large outputs
        if len(result.output) > self.max_output_chars:
            truncated = result.output[: self.max_output_chars]
            result = ToolResult(
                success=result.success,
                output=truncated + f"\n\n... (output truncated at {self.max_output_chars} chars, total was {len(result.output)})",
                error=result.error,
                cancelled=result.cancelled,
            )

        return result

    def _check_policy(self, tool_call: ToolCall) -> Optional[str]:
        if self.trust_state is None:
            return None

        policy = self.policy_gate or MinimalToolPolicyGate()
        requested_caps = classify_tool_capabilities(tool_call.name, tool_call.arguments)
        decision = policy.evaluate(
            PolicyInput(
                source=self.policy_source,
                tool_name=tool_call.name,
                tool_args=dict(tool_call.arguments or {}),
                requested_caps=requested_caps,
                trust_state=self.trust_state,
                mode=self.guardrails.mode,
            )
        )
        self._emit_policy_gate(tool_call.name, requested_caps, decision)
        if decision.allowed:
            return None
        return f"POLICY DENIED [{decision.reason_code}]: {decision.message}"

    def _emit_policy_gate(
        self,
        tool_name: str,
        requested_caps: Set[str],
        decision: Any,
    ) -> None:
        if self.events is None:
            return
        trusted_caps = sorted(self.trust_state.trusted_intent_caps) if self.trust_state else []
        untrusted_present = bool(self.trust_state.untrusted_context_present) if self.trust_state else False
        untrusted_sources = list(self.trust_state.untrusted_sources) if self.trust_state else []
        if hasattr(self.events, "policy_gate"):
            self.events.policy_gate(
                tool_name=tool_name,
                source=self.policy_source,
                requested_caps=sorted(requested_caps),
                trusted_intent_caps=trusted_caps,
                untrusted_context_present=untrusted_present,
                untrusted_sources=untrusted_sources,
                allowed=bool(decision.allowed),
                reason_code=str(decision.reason_code),
                matched_rule=str(decision.matched_rule),
                message=str(decision.message),
            )
        else:
            self.events.log(
                "policy_gate",
                tool=tool_name,
                source=self.policy_source,
                requested_caps=sorted(requested_caps),
                trusted_intent_caps=trusted_caps,
                untrusted_context_present=untrusted_present,
                untrusted_sources=untrusted_sources,
                allowed=bool(decision.allowed),
                reason_code=str(decision.reason_code),
                matched_rule=str(decision.matched_rule),
                message=str(decision.message),
            )

    def _check_guardrails(self, tool_call: ToolCall) -> Optional[str]:
        """Check guardrails before execution. Returns error or None."""
        name = tool_call.name
        args = tool_call.arguments

        # Write-class tools check path
        if name in ("Write", "StrReplace", "Delete"):
            path = args.get("path", "")
            return self.guardrails.check_write(path)

        # Shell checks command (dangerous + sensitive prompts handled inside check_shell)
        if name == "Shell":
            command = args.get("command", "")
            # Intercept raw kubectl: redirect agent to use the K8s tool instead
            cmd_base = command.strip().split()[0] if command.strip() else ""
            if cmd_base in ("kubectl", "k") and "K8s" in self._tools:
                return (
                    "BLOCKED: Do not use raw kubectl via Shell. Use the K8s tool instead — "
                    "it combines multiple kubectl calls into compound operations "
                    "(overview, diagnose, fix-pod, scale-and-wait) saving LLM round-trips. "
                    "For granular access: K8s(action='get'), K8s(action='logs'), "
                    "K8s(action='describe'). See the K8s tool parameters for details."
                )
            return self.guardrails.check_shell(command)

        # K8s destructive actions require confirmation in interactive mode.
        # These bypass Shell guardrails since K8s tool calls _kubectl directly.
        if name == "K8s":
            action = args.get("action", "")
            _K8S_DESTRUCTIVE = {"delete-pod", "fix-pod", "scale-and-wait"}
            if action in _K8S_DESTRUCTIVE:
                resource = args.get("resource_name") or args.get("component") or "?"
                ns = args.get("namespace", "mis")
                # Synthesize a kubectl-like command for the guardrail to check
                cmd_desc = f"kubectl delete pod {resource} -n {ns}"
                return self.guardrails.check_shell(cmd_desc)

        return None
    
    def _validate_arguments(self, tool: Tool, arguments: Dict[str, Any]) -> Optional[str]:
        """Validate arguments against tool schema. Returns error message or None."""
        schema = tool.parameters
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        
        # Check required parameters
        for param in required:
            if param not in arguments:
                return f"Missing required parameter: {param}"
        
        # Check for unknown parameters
        for param in arguments:
            if param not in properties:
                valid_params = list(properties.keys())
                return f"Unknown parameter: {param}. Valid parameters: {valid_params}"
        
        # Type validation (basic)
        for param, value in arguments.items():
            if param not in properties:
                continue
            
            expected_type = properties[param].get("type")
            if expected_type == "string" and not isinstance(value, str):
                return f"Parameter '{param}' must be a string, got {type(value).__name__}"
            elif expected_type == "integer" and not isinstance(value, int):
                return f"Parameter '{param}' must be an integer, got {type(value).__name__}"
            elif expected_type == "boolean" and not isinstance(value, bool):
                return f"Parameter '{param}' must be a boolean, got {type(value).__name__}"
        
        return None

    @property
    def modified_files(self) -> set:
        """Files modified during this session (for end-of-task linting)."""
        return self._modified_files

    def lint_modified_files(self) -> str:
        """Lint all files modified during the session.

        Called at end of task (not per-file). Returns formatted diagnostics
        or empty string if no issues found.
        """
        if not self._linter or not self._modified_files:
            return ""

        all_diagnostics: list = []
        for file_path in sorted(self._modified_files):
            try:
                output = self._linter.lint_file_formatted(file_path)
                if output:
                    all_diagnostics.append(output)
            except Exception as e:
                logger.debug("Lint failed for %s: %s", file_path, e)

        return "\n".join(all_diagnostics)


def create_default_registry(
    guardrails: Optional[Guardrails] = None,
    max_output_chars: int = 50_000,
    linter_config_path: Optional[Path] = None,
    ops_tools: bool = True,
    workspace_root: Optional[Path] = None,
    runner_factory: Optional[Any] = None,
    parent_telemetry: Optional[Any] = None,
    summarizer_llm: Optional[Any] = None,
    jenkins_jobs: Optional[Dict[str, Any]] = None,
    deploy_presets: Optional[Dict[str, Any]] = None,
    role: str = "all",
    vector_store: Optional[Any] = None,
    ecosystem_name: str = "default_ecosystem",
    ticket_cwd: Optional[Path] = None,
    stop_checker: Optional[Any] = None,
    events: Optional[Any] = None,
    trust_state: Optional[TrustState] = None,
    policy_gate: Optional[Any] = None,
    policy_source: str = "master",
) -> ToolRegistry:
    """Create a registry with tools appropriate for the given role.

    Args:
        role: "orchestrator" (only planning/delegation), "worker" (execution tools), or "all" (default).
    """
    from .read import ReadTool
    from .write import WriteTool
    from .str_replace import StrReplaceTool
    from .delete import DeleteTool
    from .shell import ShellTool
    from .grep import GrepTool
    from .glob_tool import GlobTool
    from .ls import LSTool
    from .git_checkpoint import GitCheckpointTool
    from .read_lints import ReadLintsTool
    from ..linter import LinterRunner, load_config

    # Load linter configuration
    linter_config = load_config(linter_config_path)
    linter = LinterRunner(config=linter_config)

    registry = ToolRegistry(
        guardrails=guardrails,
        linter=linter,
        events=events,
        trust_state=trust_state,
        policy_gate=policy_gate,
        policy_source=policy_source,
    )
    registry.max_output_chars = max_output_chars

    # Worker gets execution tools
    if role in ("worker", "all"):
        for tool_cls in [ReadTool, WriteTool, StrReplaceTool, DeleteTool, GrepTool, GlobTool, LSTool]:
            registry.register(tool_cls())
        registry.register(
            ShellTool(
                default_cwd=str(ticket_cwd) if ticket_cwd else None,
                stop_checker=stop_checker,
            )
        )
        registry.register(GitCheckpointTool())
        registry.register(ReadLintsTool(linter=linter))

    # Orchestrator gets delegation tool and read-only tools
    if role in ("orchestrator", "all"):
        if role == "orchestrator":
            # Orchestrator still needs read access to build context
            for tool_cls in [ReadTool, GrepTool, GlobTool, LSTool]:
                registry.register(tool_cls())
        
        # Register DelegateTool if a runner factory is provided
        if runner_factory is not None:
            try:
                from .delegate import DelegateTool
                delegate = DelegateTool(
                    runner_factory=runner_factory,
                    parent_telemetry=parent_telemetry,
                    summarizer_llm=summarizer_llm,
                )
                registry.register(delegate)
                logger.debug(
                    "Registered DelegateTool with runners: %s",
                    ", ".join(runner_factory.runner_names),
                )
            except Exception as e:
                logger.warning("Failed to register DelegateTool: %s", e)

        # Register MemorySearchTool if vector_store is provided
        if vector_store is not None:
            try:
                from .memory_search import MemorySearchTool
                registry.register(MemorySearchTool(vector_store, ecosystem_name))
                logger.debug("Registered MemorySearchTool")
            except Exception as e:
                logger.warning("Failed to register MemorySearchTool: %s", e)

    return registry
