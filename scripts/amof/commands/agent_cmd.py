"""Agent command -- runs the AMOF orchestrator agent.

CLI entry point for `amof agent`.
Supports single-shot and interactive modes.
Supports model ladder for cost optimization (--model-ladder).
Supports plan-execute with interactive approval and checkpointing.
Supports resume on cost cap / crash via incremental session saves.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..app_paths import get_app_paths, indexes_dir, runs_dir, vector_store_dir

logger = logging.getLogger(__name__)

AMOF_RUNTIME_DEPENDENCIES = {
    "anthropic": "anthropic",
    "boto3": "boto3",
    "botocore": "botocore",
    "openai": "openai",
    "pydantic": "pydantic",
    "requests": "requests",
    "yaml": "PyYAML",
}
OPTIONAL_MEMORY_DEPENDENCIES = {"chromadb", "pysqlite3"}
SUPPORTED_PROFILE_PROVIDERS = {"anthropic", "openai", "openrouter", "bedrock", "local", "runpod"}

# Default model tiers when --model-ladder is enabled
DEFAULT_TIERS = {
    "fast":     "claude-haiku-4-5",
    "standard": "claude-sonnet-4-5",
    "strong":   "claude-opus-4-6",
}
PROVIDER_DEFAULT_MODELS = {
    "anthropic": {
        "worker": "claude-3-5-sonnet-latest",
        "planner": DEFAULT_TIERS["strong"],
    },
    "openai": {
        "worker": "gpt-4o",
        "planner": "gpt-4o",
    },
    "openrouter": {
        "worker": "anthropic/claude-sonnet-4.5",
        "planner": "anthropic/claude-sonnet-4.5",
    },
    "bedrock": {
        "worker": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        "planner": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    },
    "local": {
        "worker": "qwen2.5-coder:7b",
        "planner": "qwen2.5-coder:7b",
    },
    "runpod": {
        "worker": "deepseek-ai/DeepSeek-V4-Flash",
        "planner": "deepseek-ai/DeepSeek-V4-Flash",
    },
}
MUTATION_INTENT_RE = re.compile(
    r"\b(add|write|create|edit|modify|update|patch|replace|delete|remove|refactor|implement|fix|change)\b",
    re.IGNORECASE,
)
FULL_REWRITE_INTENT_RE = re.compile(
    r"\b(rewrite|replace|overwrite|regenerate)\b.{0,40}\b(entire|whole|file|from scratch)\b|"
    r"\b(entire|whole)\b.{0,40}\b(file)\b",
    re.IGNORECASE,
)
REQUESTED_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:md|markdown|py|js|jsx|ts|tsx|json|yaml|yml|toml|txt|rst|sh|css|html))"
)
LINE_BOUND_RE = re.compile(
    r"\b(?:under|less than|fewer than|no more than|max(?:imum)?|within|keep(?:\s+the\s+change)?\s+under)\s+(\d+)\s+lines?\b",
    re.IGNORECASE,
)


def _legacy_agent_dir(workspace_root: Path) -> Path:
    return workspace_root / ".amof"


def _safe_workspace_label(workspace_root: Path) -> str:
    raw = workspace_root.resolve(strict=False).name or "workspace"
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "-" for c in raw).strip("-")
    return safe or "workspace"


def _workspace_runtime_key(workspace_root: Path) -> str:
    resolved = workspace_root.resolve(strict=False)
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"{_safe_workspace_label(resolved)}-{digest}"


def _agent_config_path(workspace_root: Path) -> Path:
    app_config_path = get_app_paths().config_root / "agent.yaml"
    legacy_config_path = _legacy_agent_dir(workspace_root) / "agent.yaml"
    if app_config_path.exists() or not legacy_config_path.exists():
        return app_config_path
    return legacy_config_path


def _agent_rules_path(workspace_root: Path, filename: str) -> Path:
    app_rules_path = get_app_paths().config_root / "rules" / filename
    legacy_rules_path = _legacy_agent_dir(workspace_root) / "rules" / filename
    if app_rules_path.exists() or not legacy_rules_path.exists():
        return app_rules_path
    return legacy_rules_path


def _agent_allowed_rules_path() -> Path:
    return get_app_paths().config_root / "rules" / "allowed.yaml"


def _agent_runs_session_dir(session_id: str, *, session_subdir: str = "runs") -> Path:
    base_dir = runs_dir() if session_subdir == "runs" else runs_dir() / session_subdir
    return base_dir / session_id


def _legacy_session_dir(workspace_root: Path, session_id: str, *, session_subdir: str = "runs") -> Path:
    return _legacy_agent_dir(workspace_root) / session_subdir / session_id


def _resolve_session_dir(workspace_root: Path, session_id: str, *, session_subdir: str = "runs") -> Path:
    app_session_dir = _agent_runs_session_dir(session_id, session_subdir=session_subdir)
    legacy_dir = _legacy_session_dir(workspace_root, session_id, session_subdir=session_subdir)
    if app_session_dir.exists() or not legacy_dir.exists():
        return app_session_dir
    return legacy_dir


def _agent_vector_store_path(workspace_root: Path) -> Path:
    return vector_store_dir() / _workspace_runtime_key(workspace_root)


def _agent_index_path(workspace_root: Path, ecosystem_name: str) -> Path:
    return indexes_dir() / _workspace_runtime_key(workspace_root) / (ecosystem_name or "default")


def _is_amof_source_checkout(workspace_root: Path) -> bool:
    return (workspace_root / "scripts" / "amof.py").exists() and (workspace_root / "requirements.txt").exists()


def _is_appdata_adopted_manifest(manifest: Dict[str, Any]) -> bool:
    return str(manifest.get("manifest_source") or "").strip() == "appdata"


def _agent_journal_dir(manifest: Dict[str, Any], workspace_root: Path) -> Path:
    ecosystem_name = str(manifest.get("ecosystem") or "default").strip() or "default"
    if _is_appdata_adopted_manifest(manifest):
        return get_app_paths().data_root / "journals" / ecosystem_name
    from ..manifest import get_journal_dir

    return get_journal_dir(ecosystem_name, base=str(workspace_root))


def _agent_plans_dir(manifest: Dict[str, Any], workspace_root: Path) -> Path:
    ecosystem_name = str(manifest.get("ecosystem") or "default").strip() or "default"
    if _is_appdata_adopted_manifest(manifest):
        return get_app_paths().data_root / "plans" / ecosystem_name
    return workspace_root / "ecosystems" / ecosystem_name / "plans"


def _configure_guardrails(guardrails: Any, workspace_root: Path) -> None:
    app_allowed_path = _agent_allowed_rules_path()
    guardrails._allowed_path = app_allowed_path
    guardrails._always_allowed = guardrails._load_always_allowed()
    if guardrails._always_allowed:
        return
    legacy_allowed_path = _legacy_agent_dir(workspace_root) / "rules" / "allowed.yaml"
    if legacy_allowed_path.exists():
        guardrails._allowed_path = legacy_allowed_path
        legacy_allowed = guardrails._load_always_allowed()
        guardrails._allowed_path = app_allowed_path
        guardrails._always_allowed = legacy_allowed


def _auto_load_env(env_path: Path) -> None:
    """Auto-load .env file into os.environ if API keys aren't set.

    Handles KEY=value, KEY='value', KEY="value", and comments.
    Only loads variables that are not already in the environment.
    """
    if not env_path.exists():
        return
    # Skip if keys are already set
    if (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
    ):
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Strip surrounding quotes
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            # Only set if not already in env (don't override explicit exports)
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass  # Best-effort; if .env is unreadable, error message will guide user


def _missing_module_name(exc: BaseException) -> str:
    name = getattr(exc, "name", None)
    if isinstance(name, str) and name:
        return name.split(".", 1)[0]
    text = str(exc)
    marker = "No module named "
    if marker in text:
        return text.split(marker, 1)[1].strip().strip("'\"").split(".", 1)[0]
    return text


def _memory_dependency_guidance() -> str:
    return (
        "Vector memory is optional. For pipx installs, run:\n"
        "    pipx inject amof chromadb pysqlite3-binary\n"
        "For source checkouts, install the optional memory dependencies from the AMOF project environment."
    )


def _runtime_dependency_guidance(missing: str | None = None) -> str:
    missing_line = f"[agent] AMOF runtime dependency missing: {missing}\n" if missing else ""
    return (
        missing_line
        + "[agent] This dependency belongs to AMOF, not the adopted target repo.\n"
        "  Update or reinstall AMOF:\n"
        "    amof update\n"
        "  or:\n"
        "    pipx install --force git+https://github.com/marekhotshot/amof.git\n"
    )


def _check_amof_runtime_imports() -> tuple[list[str], list[str]]:
    missing_core: list[str] = []
    missing_memory: list[str] = []
    for module_name, package_name in AMOF_RUNTIME_DEPENDENCIES.items():
        try:
            __import__(module_name)
        except Exception:
            missing_core.append(package_name)
    for module_name in OPTIONAL_MEMORY_DEPENDENCIES:
        try:
            __import__(module_name)
        except Exception:
            missing_memory.append(module_name)
    return missing_core, missing_memory


def cmd_agent_install() -> int:
    """Set up the agent environment: create venv, install dependencies, verify API key.

    Usage: amof agent install
    """
    workspace_root = Path.cwd()
    req_file = workspace_root / "requirements.txt"
    venv_dir = workspace_root / ".venv"

    print("\n  AMOF Agent Environment Setup\n")

    if not _is_amof_source_checkout(workspace_root):
        missing_core, missing_memory = _check_amof_runtime_imports()
        if missing_core:
            sys.stderr.write(_runtime_dependency_guidance(", ".join(sorted(missing_core))))
            return 1
        print("  ✓ AMOF runtime dependencies are installed in this CLI environment.")
        if missing_memory:
            print("  ℹ Vector memory is optional and is not installed.")
            print("    For pipx installs, run: pipx inject amof chromadb pysqlite3-binary")
        print()
        print("  Next steps:")
        print("    amof setup provider openrouter --activate")
        print('    export OPENROUTER_API_KEY="<redacted>"')
        print('    amof agent --plan "Inspect this repo"')
        return 0

    # Step 1: Check requirements.txt
    if not req_file.exists():
        sys.stderr.write("  ✗ requirements.txt not found in workspace root\n")
        return 1
    print("  [1/4] requirements.txt found")

    # Step 2: Create venv if needed
    if venv_dir.exists():
        print(f"  [2/4] Virtual environment exists: {venv_dir}")
    else:
        print(f"  [2/4] Creating virtual environment: {venv_dir}")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                sys.stderr.write(f"  ✗ Failed to create venv:\n{result.stderr}\n")
                return 1
        except Exception as e:
            sys.stderr.write(f"  ✗ Failed to create venv: {e}\n")
            return 1

    # Step 3: Install dependencies into venv
    venv_pip = venv_dir / "bin" / "pip"
    if not venv_pip.exists():
        venv_pip = venv_dir / "Scripts" / "pip.exe"  # Windows

    if not venv_pip.exists():
        sys.stderr.write(f"  ✗ pip not found in venv: {venv_pip}\n")
        return 1

    print(f"  [3/4] Installing dependencies...")
    try:
        result = subprocess.run(
            [str(venv_pip), "install", "-r", str(req_file)],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            sys.stderr.write(f"  ✗ pip install failed:\n{result.stderr}\n")
            return 1
        # Count installed packages
        lines = [l for l in result.stdout.splitlines() if "Successfully installed" in l or "already satisfied" in l.lower()]
        if lines:
            print(f"        {lines[-1].strip()}")
        else:
            print("        Dependencies up to date")
    except subprocess.TimeoutExpired:
        sys.stderr.write("  ✗ pip install timed out after 180s\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"  ✗ pip install failed: {e}\n")
        return 1

    # Step 4: Verify key packages
    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():
        venv_python = venv_dir / "Scripts" / "python.exe"

    print("  [4/4] Verifying packages...")
    for pkg in ["anthropic", "openai"]:
        try:
            result = subprocess.run(
                [str(venv_python), "-c", f"import {pkg}; print({pkg}.__version__)"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                ver = result.stdout.strip()
                print(f"        ✓ {pkg} {ver}")
            else:
                print(f"        ⚠ {pkg} not installed (optional)")
        except Exception:
            print(f"        ⚠ {pkg} check failed (optional)")

    # Step 5: Check .env and API keys
    env_file = workspace_root / ".env"
    has_env = env_file.exists()
    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))

    if has_env and not has_api_key:
        # .env exists but keys not loaded -- try to read it
        try:
            env_text = env_file.read_text(encoding="utf-8")
            for line in env_text.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") and val:
                        has_api_key = True
                        break
        except Exception:
            pass

    api_status = "✓ API key found" if has_api_key else "⚠ No API key in .env (add ANTHROPIC_API_KEY or OPENAI_API_KEY)"
    print(f"  [5/5] {api_status}")

    # Summary
    print(f"""
  ✓ Agent environment ready!

  Run the agent:
    source .env && amof agent
""")
    if not has_api_key:
        print("  (first add your API key to .env)\n")
    return 0


def _load_agent_config(workspace_root: Path, config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load defaults from AMOF app-data, falling back to legacy workspace config.

    Returns a dict with config values. Missing keys are absent (not None).
    Uses proper YAML parsing to handle nested structures (provider_fallback,
    routing, budget_warning_thresholds, etc.).
    """
    config_path = config_path or _agent_config_path(workspace_root)
    if not config_path.exists():
        return {}

    try:
        import yaml
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        return config
    except ImportError:
        pass  # Fall back to simple parser if yaml not available
    except Exception:
        pass

    # Fallback: simple line-by-line parser (no nested structures)
    config: Dict[str, Any] = {}
    try:
        text = config_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if not value:
                continue  # skip keys with nested values
            # Strip inline comments
            if not (value.startswith('"') or value.startswith("'")):
                comment_idx = value.find("  #")
                if comment_idx < 0:
                    comment_idx = value.find(" #")
                if comment_idx > 0:
                    value = value[:comment_idx].rstrip()
            # Type coercion
            if value.lower() in ("true", "yes"):
                config[key] = True
            elif value.lower() in ("false", "no"):
                config[key] = False
            else:
                try:
                    config[key] = float(value)
                except ValueError:
                    config[key] = value
    except Exception:
        pass

    return config


def _active_provider_profile() -> dict[str, Any] | None:
    from ..app_config import get_provider_profile_refs, load_provider_profile

    refs = get_provider_profile_refs()
    if not refs:
        return None
    if len(refs) > 1:
        joined = ", ".join(refs)
        raise ValueError(
            f"Multiple active provider profiles configured: {joined}. "
            "Pass --provider explicitly or activate exactly one provider profile."
        )

    profile = load_provider_profile(refs[0])
    profile_name = str(profile.get("name") or refs[0])
    provider_name = str(profile.get("provider") or "").strip()
    if not provider_name:
        raise ValueError(f"Provider profile {profile_name} does not declare a provider.")
    if provider_name not in SUPPORTED_PROFILE_PROVIDERS:
        raise ValueError(
            f"Provider profile {profile_name} uses provider {provider_name}, "
            "but this provider is not supported by amof agent CLI yet."
        )
    return profile


def _profile_credential_env(profile: dict[str, Any] | None, key: str) -> str | None:
    if not profile:
        return None
    credential_refs = profile.get("credential_refs")
    if isinstance(credential_refs, dict):
        value = credential_refs.get(key)
        if value:
            return str(value).strip()
    value = profile.get(key)
    if value:
        return str(value).strip()
    return None


def _profile_model(profile: dict[str, Any] | None) -> str | None:
    if not profile:
        return None
    model_env = str(profile.get("model_env") or "").strip()
    if model_env and os.environ.get(model_env):
        return str(os.environ[model_env]).strip()
    for key in ("model", "default_model"):
        value = profile.get(key)
        if value:
            return str(value).strip()
    return None


def _profile_base_url(profile: dict[str, Any] | None) -> str | None:
    if not profile:
        return None
    base_url_env = _profile_credential_env(profile, "base_url_env")
    if base_url_env and os.environ.get(base_url_env):
        return str(os.environ[base_url_env]).strip()
    for key in ("base_url", "default_base_url"):
        value = profile.get(key)
        if value:
            return str(value).strip()
    return None


def _normalize_runpod_openai_base_url(base_url: str | None) -> str | None:
    """Normalize a RunPod OpenAI-compatible API root to exactly one `/v1` suffix."""
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return normalized
    while normalized.endswith("/v1/v1"):
        normalized = normalized[:-3]
    if not normalized.endswith("/v1"):
        normalized = f"{normalized}/v1"
    return normalized


def _provider_endpoint_diagnostics(
    *,
    provider: str,
    profile: dict[str, Any] | None,
    base_url: str | None,
    model: str | None,
    endpoint_family: str,
) -> str:
    profile_name = str((profile or {}).get("name") or "<none>")
    base = str(base_url or "").strip()
    parsed = urlparse(base)
    redacted_base = base
    if parsed.username or parsed.password:
        redacted_base = parsed._replace(netloc=parsed.hostname or "").geturl()
    final_path = "/chat/completions" if endpoint_family == "chat.completions" else "<unknown>"
    if parsed.path:
        final_path = f"{parsed.path.rstrip('/')}{final_path}"
    return (
        f"provider={provider} profile={profile_name} model={model or '<missing>'} "
        f"base_url={redacted_base or '<missing>'} "
        f"base_url_ends_with_v1={str(base.endswith('/v1')).lower()} "
        f"endpoint_family={endpoint_family} final_path={final_path}"
    )


def _profile_timeout_seconds(profile: dict[str, Any] | None) -> str | None:
    if profile:
        value = profile.get("timeout_seconds")
        if value is not None:
            return str(value).strip()
    value = os.environ.get("AMOF_LOCAL_PROVIDER_TIMEOUT_SECONDS")
    if value is not None:
        return value.strip()
    return None


def _default_worker_model(provider: str, model: str | None, profile_default_model: str | None) -> str:
    """Resolve the default worker/orchestrator model for a provider."""
    if model:
        return model
    if profile_default_model:
        return profile_default_model
    if provider == "openai":
        return os.environ.get("AMOF_OPENAI_MODEL", PROVIDER_DEFAULT_MODELS["openai"]["worker"])
    if provider == "openrouter":
        return os.environ.get(
            "AMOF_OPENROUTER_MODEL",
            os.environ.get("AMOF_OPENAI_MODEL", PROVIDER_DEFAULT_MODELS["openrouter"]["worker"]),
        )
    if provider == "bedrock":
        return os.environ.get(
            "AMOF_BEDROCK_STANDARD_MODEL_ID",
            PROVIDER_DEFAULT_MODELS["bedrock"]["worker"],
        )
    if provider == "local":
        return os.environ.get(
            "AMOF_LOCAL_QWEN_MODEL",
            os.environ.get("AMOF_LOCAL_MODEL", PROVIDER_DEFAULT_MODELS["local"]["worker"]),
        )
    if provider == "runpod":
        return os.environ.get(
            "AMOF_RUNPOD_MODEL",
            os.environ.get("RUNPOD_MODEL", PROVIDER_DEFAULT_MODELS["runpod"]["worker"]),
        )
    return os.environ.get("AMOF_ANTHROPIC_MODEL", PROVIDER_DEFAULT_MODELS["anthropic"]["worker"])


def _default_planner_model(
    provider: str,
    planner_model: str | None,
    profile_default_model: str | None = None,
) -> str:
    """Resolve a provider-compatible default planner model.

    Explicit CLI/env planner selections win. Defaults must be valid for the
    selected provider; OpenRouter cannot use Anthropic-native aliases directly.
    """
    if planner_model:
        return planner_model
    env_model = os.environ.get("AMOF_PLANNER_MODEL")
    if env_model:
        return env_model
    if provider == "local" and profile_default_model:
        return profile_default_model
    if provider == "runpod" and profile_default_model:
        return profile_default_model
    return PROVIDER_DEFAULT_MODELS.get(provider, PROVIDER_DEFAULT_MODELS["anthropic"])["planner"]


def _validate_local_base_url(base_url: str | None) -> str | None:
    normalized = str(base_url or "").strip()
    if not normalized:
        return "local provider profile requires base_url or default_base_url"
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return f"local provider base_url must be an http(s) URL, got: {normalized}"
    return None


def _validate_local_model(model: str | None) -> str | None:
    if str(model or "").strip():
        return None
    return "local provider profile requires model or default_model"


def _resolve_local_timeout_seconds(profile: dict[str, Any] | None) -> tuple[float | None, str | None]:
    raw_timeout = _profile_timeout_seconds(profile)
    if raw_timeout is None or raw_timeout == "":
        return None, None
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError:
        return None, (
            "local provider timeout_seconds must be a positive number "
            f"(got {raw_timeout!r})"
        )
    if timeout_seconds <= 0:
        return None, (
            "local provider timeout_seconds must be a positive number "
            f"(got {raw_timeout!r})"
        )
    return timeout_seconds, None


def _validate_runner_factory_for_plan(runner_factory: Any, plan: Any) -> str | None:
    expected = sorted({str(st.runner or "code") for st in getattr(plan, "subtasks", [])})
    if not expected:
        return None
    if runner_factory is None:
        return f"No runner factory available for plan execution. Expected runner: {expected[0]}."
    available = set(getattr(runner_factory, "runner_names", []))
    missing = [runner for runner in expected if runner not in available]
    if missing:
        return (
            f"No runner factory available for plan execution. Expected runner: {missing[0]}."
        )
    return None


def _agent_plan_checkpoints_dir(manifest: Dict[str, Any], workspace_root: Path) -> Path:
    return _agent_plans_dir(manifest, workspace_root) / "checkpoints"


def _run_plan_execute_readiness(
    goal: str,
    plan: Any,
    *,
    trust_state: Any,
    runner_factory: Any,
    guardrails: Any,
    tool_registry: Any,
) -> Any:
    from ..orchestrator.plan_execute_control import assess_execution_readiness

    parent_tools = set(getattr(tool_registry, "_tools", {}).keys())
    return assess_execution_readiness(
        goal,
        plan,
        trust_state=trust_state,
        runner_factory=runner_factory,
        guardrails=guardrails,
        parent_tool_names=parent_tools,
    )


def _parse_approve_capabilities(raw: Optional[List[str]]) -> set[str]:
    from ..orchestrator.plan_execute_control import parse_capability_names

    if not raw:
        return set()
    return set(parse_capability_names(list(raw)))


def _parse_budget_cli_flags(
    *,
    max_cost: Optional[float],
    budget: Optional[float],
    cost_limit: Optional[float],
    subtask_budget: Optional[float],
    add_budget: Optional[float],
    require_budget_approval: Optional[bool],
    budget_strict: Optional[bool],
    budget_status: Optional[bool],
) -> tuple[Any, Optional[str]]:
    """Validate budget CLI flags; return (BudgetOptions, error_message)."""
    from ..orchestrator.resume_control import BudgetOptions, parse_positive_budget

    alias_values: list[tuple[str, float]] = []
    try:
        if budget is not None:
            alias_values.append(("--budget", parse_positive_budget(budget, flag="--budget")))
        if max_cost is not None:
            alias_values.append(("--max-cost", parse_positive_budget(max_cost, flag="--max-cost")))
        if cost_limit is not None:
            alias_values.append((
                "--cost-limit",
                parse_positive_budget(cost_limit, flag="--cost-limit"),
            ))
        parsed_subtask = (
            parse_positive_budget(subtask_budget, flag="--subtask-budget")
            if subtask_budget is not None
            else None
        )
        parsed_add = (
            parse_positive_budget(add_budget, flag="--add-budget") if add_budget is not None else None
        )
    except ValueError as exc:
        return None, str(exc)

    canonical_budget: Optional[float] = None
    if alias_values:
        canonical_budget = alias_values[0][1]
        conflicts = [
            (flag, value)
            for flag, value in alias_values
            if abs(value - canonical_budget) > 1e-9
        ]
        if conflicts:
            all_values = ", ".join(f"{flag}={value:.2f}" for flag, value in alias_values)
            return None, f"Conflicting budget aliases: {all_values}"
        if len(alias_values) > 1:
            aliases = ", ".join(flag for flag, _ in alias_values if flag != "--budget")
            if aliases:
                verb = "is an alias" if "," not in aliases else "are aliases"
                sys.stderr.write(
                    f"[agent] {aliases} {verb} for --budget; "
                    f"using {canonical_budget:.2f}\n"
                )

    opts = BudgetOptions(
        budget=canonical_budget,
        cost_limit=None,
        subtask_budget=parsed_subtask,
        add_budget=parsed_add,
        require_budget_approval=bool(require_budget_approval),
        budget_strict=bool(budget_strict),
        budget_status=bool(budget_status),
    )
    return opts, None


def _resolve_effective_max_cost(
    max_cost: Optional[float],
    budget_options: Any,
) -> tuple[Optional[float], Optional[str]]:
    from ..orchestrator.resume_control import resolve_run_budget

    hard_budget, err = resolve_run_budget(budget_options)
    if err:
        return None, err
    if hard_budget is not None:
        return hard_budget, None
    if max_cost is not None:
        return max_cost, None
    return None, None


def _resume_readable_roots(workspace_root: Path, manifest: Dict[str, Any]) -> List[Path]:
    roots = [workspace_root.resolve()]
    if _is_appdata_adopted_manifest(manifest):
        roots.append(get_app_paths().data_root.resolve())
    return roots


def _load_resume_followup_for_session(
    *,
    resume_session: Optional[str],
    follow_up: Optional[str],
    follow_up_file: Optional[str],
    workspace_root: Path,
    manifest: Dict[str, Any],
) -> tuple[Any, Optional[str]]:
    from ..orchestrator.resume_control import load_resume_followup

    if not resume_session and (follow_up or follow_up_file):
        return None, "--follow-up and --follow-up-file require --resume."
    if not follow_up and not follow_up_file:
        return None, None
    return load_resume_followup(
        inline=follow_up,
        file_path=follow_up_file,
        readable_roots=_resume_readable_roots(workspace_root, manifest),
    )


def _log_resume_followup(events: Any, session: Any, followup: Any) -> None:
    payload = followup.to_event_dict(session.id)
    session.metadata["resume_followup"] = payload
    events.resume_followup(
        session_id=payload["session_id"],
        source=payload["source"],
        chars=payload["chars"],
        sha256=payload["sha256"],
        preview=payload["preview"],
    )


def _save_readiness_checkpoint(
    plan: Any,
    *,
    session: Any,
    manifest: Dict[str, Any],
    workspace_root: Path,
    goal: str,
    failure_type: str,
    failure_message: str,
    events: Any,
    telemetry: Any,
) -> Path:
    from ..orchestrator.plan_execute_control import build_checkpoint, save_plan_checkpoint

    checkpoint = build_checkpoint(
        plan,
        session_id=session.id,
        failure_type=failure_type,
        failure_message=failure_message,
        failed_subtask_id=None,
        goal=goal,
        budget_limit=telemetry.max_cost,
        spent_cost=telemetry.total_cost,
    )
    checkpoint_path = save_plan_checkpoint(
        checkpoint,
        _agent_plan_checkpoints_dir(manifest, workspace_root),
    )
    events.session_end(telemetry.to_dict())
    _save_session(session, telemetry, events, workspace_root)
    return checkpoint_path


def _gate_plan_execute_readiness(
    goal: str,
    plan: Any,
    *,
    session: Any,
    trust_state: Any,
    runner_factory: Any,
    guardrails: Any,
    tool_registry: Any,
    events: Any,
    telemetry: Any,
    manifest: Dict[str, Any],
    workspace_root: Path,
    approve_capabilities: Optional[List[str]] = None,
    approve_tool_packs: Optional[List[str]] = None,
    approve_writable_roots: Optional[List[str]] = None,
    no_follow_up: bool = False,
) -> tuple[Any, int | None]:
    """Run readiness; optionally elevate capabilities for this plan/session."""
    from ..orchestrator.plan_execute_control import (
        apply_capability_elevation,
        apply_tool_pack_approval,
        apply_writable_root_elevation,
        assess_execution_readiness,
        build_plan_capability_elevation,
        format_capability_elevation_prompt,
        format_elevation_scope,
        format_readiness_failure,
        format_readiness_success,
        parse_tool_pack_names,
        parse_capability_names,
        parse_writable_root_paths,
        readiness_is_capability_only_failure,
    )

    parent_tools = set(getattr(tool_registry, "_tools", {}).keys())
    base_ceiling = set(trust_state.trusted_intent_caps) if trust_state else {"read"}
    cli_caps: set[str] = set()
    if approve_capabilities:
        try:
            cli_caps = set(parse_capability_names(list(approve_capabilities)))
        except ValueError as exc:
            sys.stderr.write(f"[plan-execute] {exc}\n")
            return None, 1
        if cli_caps:
            print(
                "[plan-execute] CLI pre-approved capabilities for this run: "
                f"{', '.join(sorted(cli_caps))}"
            )
            if trust_state is not None:
                elevation = build_plan_capability_elevation(
                    session_id=session.id,
                    plan=plan,
                    goal=goal,
                    missing_caps=sorted(cli_caps),
                    base_ceiling=base_ceiling,
                    approval_source="cli_flag",
                    parent_tool_names=parent_tools,
                )
                apply_capability_elevation(trust_state, elevation)
                plan.capability_elevation = elevation.to_dict()  # type: ignore[attr-defined]
                session.metadata["plan_capability_elevation"] = elevation.to_dict()
                events.capability_elevation(
                    session_id=elevation.session_id,
                    plan_id=elevation.plan_id,
                    approved_capabilities=elevation.approved_capabilities,
                    base_ceiling=elevation.base_ceiling,
                    approved_tools=elevation.approved_tools,
                    approved_paths=elevation.approved_paths,
                    approval_source=elevation.approval_source,
                )

    cli_writable_roots: set[str] = set()
    if approve_writable_roots:
        try:
            cli_writable_roots = set(parse_writable_root_paths(list(approve_writable_roots)))
        except ValueError as exc:
            sys.stderr.write(f"[plan-execute] {exc}\n")
            return None, 1
        if cli_writable_roots:
            approved = apply_writable_root_elevation(guardrails, sorted(cli_writable_roots))
            session.metadata["plan_writable_roots"] = approved
            plan.writable_root_approvals = approved  # type: ignore[attr-defined]
            if hasattr(events, "writable_root_approval"):
                for root in approved:
                    events.writable_root_approval(
                        session_id=session.id,
                        path=root,
                        approval_source="cli_flag",
                    )
            print(
                "[plan-execute] CLI pre-approved writable roots for this run: "
                + ", ".join(approved)
            )

    cli_tool_packs: set[str] = set()
    if approve_tool_packs:
        try:
            cli_tool_packs = set(parse_tool_pack_names(list(approve_tool_packs)))
        except ValueError as exc:
            sys.stderr.write(f"[plan-execute] {exc}\n")
            return None, 1
        if cli_tool_packs:
            apply_tool_pack_approval(plan, cli_tool_packs)
            session.metadata["plan_tool_packs"] = sorted(cli_tool_packs)
            if hasattr(events, "tool_pack_approval"):
                for pack in sorted(cli_tool_packs):
                    events.tool_pack_approval(
                        session_id=session.id,
                        tool_pack=pack,
                        approval_source="cli_flag",
                    )
            print(
                "[plan-execute] CLI pre-approved tool packs for this run: "
                + ", ".join(sorted(cli_tool_packs))
            )

    while True:
        readiness = assess_execution_readiness(
            goal,
            plan,
            trust_state=trust_state,
            runner_factory=runner_factory,
            guardrails=guardrails,
            parent_tool_names=parent_tools,
            approved_writable_roots=cli_writable_roots,
            approved_tool_packs=cli_tool_packs,
            approved_capabilities=cli_caps,
            base_capability_ceiling=base_ceiling,
        )
        if readiness.ok:
            if cli_tool_packs or cli_caps or cli_writable_roots:
                print(
                    format_readiness_success(
                        approved_tool_packs=cli_tool_packs,
                        approved_capabilities=cli_caps,
                        approved_writable_roots=cli_writable_roots,
                        base_capability_ceiling=base_ceiling,
                    )
                )
            return readiness, None

        if not readiness_is_capability_only_failure(readiness):
            print(format_readiness_failure(readiness))
            checkpoint_path = _save_readiness_checkpoint(
                plan,
                session=session,
                manifest=manifest,
                workspace_root=workspace_root,
                goal=goal,
                failure_type=readiness.failure_type,
                failure_message=(
                    readiness.issues[0].message if readiness.issues else readiness.failure_type
                ),
                events=events,
                telemetry=telemetry,
            )
            print(f"Checkpoint saved: {checkpoint_path}")
            return readiness, 1

        issue = next(i for i in readiness.issues if i.kind == "missing_capability")
        missing = sorted(
            set(issue.detail.get("required") or [])
            - set(issue.detail.get("allowed_ceiling") or [])
        )
        if missing and set(missing).issubset(cli_caps) and trust_state is not None:
            elevation = build_plan_capability_elevation(
                session_id=session.id,
                plan=plan,
                goal=goal,
                missing_caps=missing,
                base_ceiling=base_ceiling,
                approval_source="cli_flag",
                parent_tool_names=parent_tools,
            )
            apply_capability_elevation(trust_state, elevation)
            plan.capability_elevation = elevation.to_dict()  # type: ignore[attr-defined]
            session.metadata["plan_capability_elevation"] = elevation.to_dict()
            events.capability_elevation(
                session_id=elevation.session_id,
                plan_id=elevation.plan_id,
                approved_capabilities=elevation.approved_capabilities,
                base_ceiling=elevation.base_ceiling,
                approved_tools=elevation.approved_tools,
                approved_paths=elevation.approved_paths,
                approval_source=elevation.approval_source,
            )
            print(format_elevation_scope(elevation))
            continue

        print(format_capability_elevation_prompt(readiness, goal=goal, plan=plan))

        if no_follow_up or not sys.stdin.isatty():
            checkpoint_path = _save_readiness_checkpoint(
                plan,
                session=session,
                manifest=manifest,
                workspace_root=workspace_root,
                goal=goal,
                failure_type=readiness.failure_type,
                failure_message="Capability elevation required but not approved.",
                events=events,
                telemetry=telemetry,
            )
            print(f"Checkpoint saved: {checkpoint_path}")
            print(
                "Re-run with explicit approval, e.g.: "
                f"--approve-capabilities {','.join(missing)}"
            )
            return readiness, 1

        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return readiness, 1

        if choice in ("y", "yes", "approve"):
            if trust_state is None:
                return readiness, 1
            elevation = build_plan_capability_elevation(
                session_id=session.id,
                plan=plan,
                goal=goal,
                missing_caps=missing,
                base_ceiling=base_ceiling,
                approval_source="interactive",
                parent_tool_names=parent_tools,
            )
            apply_capability_elevation(trust_state, elevation)
            plan.capability_elevation = elevation.to_dict()  # type: ignore[attr-defined]
            session.metadata["plan_capability_elevation"] = elevation.to_dict()
            events.capability_elevation(
                session_id=elevation.session_id,
                plan_id=elevation.plan_id,
                approved_capabilities=elevation.approved_capabilities,
                base_ceiling=elevation.base_ceiling,
                approved_tools=elevation.approved_tools,
                approved_paths=elevation.approved_paths,
                approval_source=elevation.approval_source,
            )
            print(format_elevation_scope(elevation))
            continue

        if choice in ("e", "edit"):
            print(f"[plan-execute] Edit plan file: {plan.file_path}")
            return readiness, 1

        if choice in ("r", "resume"):
            checkpoint_path = _save_readiness_checkpoint(
                plan,
                session=session,
                manifest=manifest,
                workspace_root=workspace_root,
                goal=goal,
                failure_type=readiness.failure_type,
                failure_message="Capability elevation not approved.",
                events=events,
                telemetry=telemetry,
            )
            print(f"Checkpoint saved: {checkpoint_path}")
            return readiness, 1

        # reject / default
        checkpoint_path = _save_readiness_checkpoint(
            plan,
            session=session,
            manifest=manifest,
            workspace_root=workspace_root,
            goal=goal,
            failure_type=readiness.failure_type,
            failure_message="Capability elevation rejected.",
            events=events,
            telemetry=telemetry,
        )
        print(f"Checkpoint saved: {checkpoint_path}")
        return readiness, 1


def _handle_plan_execute_fatal_stop(
    plan: Any,
    *,
    session: Any,
    telemetry: Any,
    events: Any,
    manifest: Dict[str, Any],
    workspace_root: Path,
    goal: str,
    stop: Any,
    no_follow_up: bool,
    continue_budget: float,
) -> int:
    from ..orchestrator.plan_execute_control import (
        build_checkpoint,
        format_fatal_stop_summary,
        save_plan_checkpoint,
    )

    skipped_count = sum(1 for st in plan.subtasks if st.status == "skipped")
    checkpoint = build_checkpoint(
        plan,
        session_id=session.id,
        failure_type=stop.failure_type,
        failure_message=stop.failure_message,
        failed_subtask_id=stop.failed_subtask_id,
        goal=goal,
        budget_limit=telemetry.max_cost,
        spent_cost=telemetry.total_cost,
    )
    checkpoint_path = save_plan_checkpoint(
        checkpoint,
        _agent_plan_checkpoints_dir(manifest, workspace_root),
    )
    try:
        if plan.file_path:
            plan.save_as_markdown(plan.file_path, session_id=session.id)
    except Exception:
        pass

    events.session_end(telemetry.to_dict())
    _save_session(session, telemetry, events, workspace_root)
    _generate_journal(
        session,
        goal,
        stop.failure_type,
        telemetry,
        events,
        manifest,
        workspace_root,
        plan,
    )

    print(
        format_fatal_stop_summary(
            stop,
            skipped_count=skipped_count,
            checkpoint_path=checkpoint_path,
        )
    )
    print(f"\nResume later:\n  {checkpoint.resume_command}")

    if no_follow_up or not sys.stdin.isatty():
        return 1

    try:
        choice = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return 1

    if choice in ("c", "continue"):
        telemetry.extend_budget(continue_budget)
        print(
            f"[plan-execute] Budget extended by ${continue_budget:.2f} "
            f"(new limit: ${telemetry.max_cost:.2f}). "
            f"Re-run: {checkpoint.resume_command}"
        )
        return 1
    if choice in ("e", "edit"):
        print(f"[plan-execute] Edit plan file: {plan.file_path}")
        return 1
    if choice in ("r", "resume"):
        print(f"[plan-execute] Checkpoint: {checkpoint_path}")
        return 1
    return 1


def _plan_has_mutation_intent(goal: str, plan: Any) -> bool:
    if MUTATION_INTENT_RE.search(goal or ""):
        return True
    parts: list[str] = []
    for st in getattr(plan, "subtasks", []) or []:
        parts.extend([str(getattr(st, "title", "") or ""), str(getattr(st, "description", "") or "")])
    return MUTATION_INTENT_RE.search("\n".join(parts)) is not None


def _git_probe(workspace_root: Path) -> dict[str, str]:
    def _run(args: list[str]) -> str:
        result = subprocess.run(
            args,
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return (result.stdout + result.stderr).strip()
        return result.stdout.strip()

    return {
        "status": _run(["git", "status", "--short"]),
        "diff": _run(["git", "diff", "--"]),
        "numstat": _run(["git", "diff", "--numstat", "--"]),
    }


def _extract_requested_paths(text: str) -> list[str]:
    paths: set[str] = set()
    for match in REQUESTED_PATH_RE.finditer(text or ""):
        path = match.group(1).strip("`'\".,:;)")
        if path and not path.startswith(("http://", "https://")):
            paths.add(path)
    return sorted(paths)


def _extract_line_bound(text: str) -> int | None:
    match = LINE_BOUND_RE.search(text or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _extract_exact_markdown_snippet(text: str) -> list[str]:
    if "exactly" not in (text or "").lower():
        return []
    lines = (text or "").splitlines()
    start: int | None = None
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            start = idx
            break
    if start is None:
        return []
    snippet: list[str] = []
    for line in lines[start:]:
        lowered = line.strip().lower()
        if snippet and (
            lowered.startswith("do not ")
            or lowered.startswith("keep ")
            or lowered.startswith("only ")
        ):
            break
        snippet.append(line.rstrip())
    return [line for line in snippet if line.strip()]


def _parse_numstat(numstat: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for line in (numstat or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_raw, deleted_raw, path = parts[0], parts[1], parts[-1]
        try:
            added = int(added_raw)
        except ValueError:
            added = 0
        try:
            deleted = int(deleted_raw)
        except ValueError:
            deleted = 0
        files.append({"path": path, "added": added, "deleted": deleted})
    return files


def _tracked_line_count(workspace_root: Path, rel_path: str) -> int | None:
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"],
        cwd=workspace_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    return len(result.stdout.splitlines())


def _worktree_line_count(workspace_root: Path, rel_path: str) -> int | None:
    path = workspace_root / rel_path
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except Exception:
        return None


def _evaluate_diff_guard(goal: str, workspace_root: Path, git_after: dict[str, str]) -> dict[str, Any]:
    changed = _parse_numstat(git_after.get("numstat", ""))
    changed_files = [entry["path"] for entry in changed]
    added = sum(int(entry["added"]) for entry in changed)
    deleted = sum(int(entry["deleted"]) for entry in changed)
    requested_paths = _extract_requested_paths(goal)
    requested_set = set(requested_paths)
    changed_set = set(changed_files)
    full_rewrite_intent = bool(FULL_REWRITE_INTENT_RE.search(goal or ""))
    line_bound = _extract_line_bound(goal)
    exact_snippet_lines = _extract_exact_markdown_snippet(goal)
    lower_goal = (goal or "").lower()
    docs_only = "docs-only" in lower_goal or "do not modify code" in lower_goal
    code_extensions = {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
        ".c", ".cc", ".cpp", ".h", ".hpp", ".sh", ".rb", ".php",
    }

    reasons: list[str] = []
    destructive_rewrite_detected = False

    if requested_set:
        extra = sorted(changed_set - requested_set)
        missing = sorted(requested_set - changed_set)
        if extra:
            reasons.append(f"requested_paths_mismatch:{','.join(extra)}")
        if missing:
            reasons.append(f"requested_paths_missing:{','.join(missing)}")

    if docs_only:
        code_changed = sorted(
            path for path in changed_files if Path(path).suffix.lower() in code_extensions
        )
        if code_changed:
            reasons.append(f"docs_only_code_files_changed:{','.join(code_changed)}")

    if line_bound is not None:
        changed_lines = added + deleted
        allowed_lines = max(line_bound * 3, line_bound + 20)
        if changed_lines > allowed_lines and not full_rewrite_intent:
            reasons.append(
                f"line_bound_exceeded:{changed_lines}>{allowed_lines} "
                f"(requested under {line_bound} lines)"
            )

    if exact_snippet_lines and changed_files:
        changed_text_parts: list[str] = []
        for path in changed_files:
            try:
                changed_text_parts.append((workspace_root / path).read_text(encoding="utf-8"))
            except Exception:
                continue
        changed_text = "\n".join(changed_text_parts)
        missing_exact_lines = [
            line for line in exact_snippet_lines
            if line.strip() and line not in changed_text
        ]
        if missing_exact_lines:
            preview = " | ".join(line[:80] for line in missing_exact_lines[:3])
            reasons.append(f"exact_text_missing:{preview}")

    for entry in changed:
        path = str(entry["path"])
        file_added = int(entry["added"])
        file_deleted = int(entry["deleted"])
        before_lines = _tracked_line_count(workspace_root, path)
        after_lines = _worktree_line_count(workspace_root, path)
        existing_tracked_file = before_lines is not None

        if not existing_tracked_file or full_rewrite_intent:
            continue
        if file_deleted > 20:
            destructive_rewrite_detected = True
            reasons.append(f"large_deletion:{path}:{file_deleted}>20")
        if file_deleted > max(file_added * 2, 5):
            destructive_rewrite_detected = True
            reasons.append(f"deletion_ratio:{path}:{file_deleted}>{file_added}*2")
        if before_lines and after_lines is not None and after_lines < before_lines * 0.70:
            destructive_rewrite_detected = True
            reasons.append(f"file_shrink:{path}:{before_lines}->{after_lines}")
        if before_lines is not None and after_lines is not None:
            growth_threshold = max(before_lines * 5, before_lines + 200, 250)
            if after_lines > growth_threshold:
                destructive_rewrite_detected = True
                reasons.append(f"file_growth:{path}:{before_lines}->{after_lines}>{growth_threshold}")
        if file_added > 500:
            destructive_rewrite_detected = True
            reasons.append(f"large_addition:{path}:{file_added}>500")

    requested_observed = not requested_set or requested_set.issubset(changed_set)
    status = "pass" if not reasons else "fail"
    return {
        "status": status,
        "reasons": reasons,
        "changed_files": changed_files,
        "added_lines": added,
        "deleted_lines": deleted,
        "destructive_rewrite_detected": destructive_rewrite_detected,
        "requested_paths": requested_paths,
        "requested_paths_observed": requested_observed,
    }


def _tool_failure_counts(telemetry: Any) -> tuple[int, int]:
    total = 0
    write_class = 0
    for tool_name, metrics in getattr(telemetry, "tool_metrics", {}).items():
        failures = int(getattr(metrics, "failures", 0) or 0)
        total += failures
        leaf_name = str(tool_name).split(":")[-1]
        if leaf_name in {"Write", "StrReplace", "InsertAfter", "Delete"}:
            write_class += failures
    return total, write_class


def _write_action_count(telemetry: Any) -> int:
    total = 0
    for tool_name, metrics in getattr(telemetry, "tool_metrics", {}).items():
        leaf_name = str(tool_name).split(":")[-1]
        if leaf_name in {"Write", "StrReplace", "InsertAfter", "Delete"}:
            total += int(getattr(metrics, "calls", 0) or 0)
    return total


def _verify_changed_python_files(workspace_root: Path, git_after: dict[str, str]) -> dict[str, Any]:
    changed = _parse_numstat(git_after.get("numstat", ""))
    python_files = [
        str(entry["path"])
        for entry in changed
        if Path(str(entry["path"])).suffix == ".py" and (workspace_root / str(entry["path"])).is_file()
    ]
    if not python_files:
        return {"status": "not_applicable", "files": [], "reasons": []}

    reasons: list[str] = []
    for rel_path in python_files:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", rel_path],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "compile failed").strip().splitlines()
            preview = detail[-1] if detail else "compile failed"
            reasons.append(f"py_compile:{rel_path}:{preview[:200]}")

    return {
        "status": "fail" if reasons else "pass",
        "files": python_files,
        "reasons": reasons,
    }


def _mark_plan_failed_for_verifier(plan: Any, reason: str) -> None:
    for st in getattr(plan, "subtasks", []) or []:
        if st.status == "completed":
            st.status = "failed"
            st.error = reason
            return
    for st in getattr(plan, "subtasks", []) or []:
        if st.status in {"pending", "running", "skipped"}:
            st.status = "failed"
            st.error = reason
            return


def _plan_execute_noninteractive(no_follow_up: Any, approve_plan: Any) -> bool:
    return bool(no_follow_up or approve_plan or not sys.stdin.isatty())


FATAL_AGENT_STOP_REASONS = {
    "cost_exceeded",
    "max_iterations",
    "circuit_breaker",
    "interrupted",
    "provider_payment_required",
    "provider_auth",
    "provider_rate_limit",
    "provider_server_error",
    "provider_network",
    "provider_api_error",
}


def _agent_stop_reason_exit_code(stop_reason: str | None) -> int:
    return 1 if str(stop_reason or "") in FATAL_AGENT_STOP_REASONS else 0


def _interactive_confirm(command: str, reason: str) -> str:
    """Prompt user for confirmation when a sensitive/dangerous command is detected.

    Returns "yes", "no", or "always".
    Uses colored output for visibility in the interactive shell.
    """
    try:
        # Import colors — may not be available if running outside interactive context
        from ..orchestrator import colors as c
    except ImportError:
        c = None

    # Formatting helpers
    def _warn(text: str) -> str:
        return f"{c.YELLOW}{text}{c.RESET}" if c else text

    def _cmd(text: str) -> str:
        return f"{c.BOLD}{text}{c.RESET}" if c else text

    def _dim(text: str) -> str:
        return f"{c.INFO}{text}{c.RESET}" if c else text

    # Truncate very long commands for display
    display_cmd = command.strip()
    if len(display_cmd) > 120:
        display_cmd = display_cmd[:117] + "..."

    print()
    print(_warn(f"  ⚠ Guardrail: {reason}"))
    print(f"  Command: {_cmd(display_cmd)}")
    print()
    print(f"  {_dim('[y]es')}  Allow this once")
    print(f"  {_dim('[n]o')}   Block this command")
    print(f"  {_dim('[a]lways')}  Allow and remember (saved to AMOF app-data rules/allowed.yaml)")
    print()

    try:
        choice = input(f"  Allow? [y/n/a] > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "no"

    if choice in ("a", "always"):
        return "always"
    elif choice in ("y", "yes"):
        return "yes"
    else:
        return "no"


def cmd_agent(
    manifest: Dict[str, Any],
    goal: Optional[str] = None,
    plan_mode: Optional[bool] = None,
    model: Optional[str] = None,
    verbose: Optional[bool] = None,
    max_cost: Optional[float] = None,
    budget: Optional[float] = None,
    cost_limit: Optional[float] = None,
    subtask_budget: Optional[float] = None,
    add_budget: Optional[float] = None,
    require_budget_approval: Optional[bool] = None,
    budget_strict: Optional[bool] = None,
    budget_status: Optional[bool] = None,
    model_ladder: Optional[bool] = None,
    fast_model: Optional[str] = None,
    strong_model: Optional[str] = None,
    plan_execute: Optional[bool] = None,
    planner_model: Optional[str] = None,
    provider: Optional[str] = None,
    resume_session: Optional[str] = None,
    follow_up: Optional[str] = None,
    follow_up_file: Optional[str] = None,
    plan_file: Optional[str] = None,
    no_follow_up: Optional[bool] = None,
    continue_budget: Optional[float] = None,
    approve_plan: Optional[bool] = None,
    approve_capabilities: Optional[List[str]] = None,
    approve_tool_packs: Optional[List[str]] = None,
    approve_writable_roots: Optional[List[str]] = None,
) -> int:
    """Run the AMOF coding agent.

    If goal is provided, runs in single-shot mode.
    If no goal, runs in interactive REPL mode.

    Loads defaults from AMOF app-data, with legacy workspace `.amof` config
    treated as a compatibility fallback when app-data config is absent.
    CLI flags always take priority over config file values.
    """
    workspace_root = Path.cwd()

    # ── Load config defaults from AMOF app-data, or legacy workspace config ──
    cfg = _load_agent_config(workspace_root)
    explicit_provider = provider is not None
    provider_profile: dict[str, Any] | None = None
    raw_default_max_cost = cfg.get("default_max_cost")
    config_default_max_cost = (
        float(raw_default_max_cost) if raw_default_max_cost is not None else None
    )

    # Apply config defaults where CLI args were not explicitly set (None)
    if verbose is None:
        verbose = cfg.get("verbose", False)
    if model_ladder is None:
        model_ladder = cfg.get("model_ladder", False)
    if provider is None:
        try:
            provider_profile = _active_provider_profile()
        except (FileNotFoundError, ValueError) as exc:
            sys.stderr.write(f"[agent] {exc}\n")
            return 1
        if provider_profile:
            provider = str(provider_profile.get("provider") or "").strip()
        else:
            provider = str(cfg.get("default_provider", "anthropic"))
    if plan_mode is None:
        plan_mode = False
    if plan_execute is None:
        plan_execute = False
    if no_follow_up is None:
        no_follow_up = False
    if approve_plan is None:
        approve_plan = False
    if continue_budget is None:
        continue_budget = 1.0

    budget_options, budget_err = _parse_budget_cli_flags(
        max_cost=max_cost,
        budget=budget,
        cost_limit=cost_limit,
        subtask_budget=subtask_budget,
        add_budget=add_budget,
        require_budget_approval=require_budget_approval,
        budget_strict=budget_strict,
        budget_status=budget_status,
    )
    if budget_err:
        sys.stderr.write(f"[agent] {budget_err}\n")
        return 1
    max_cost, max_cost_err = _resolve_effective_max_cost(max_cost, budget_options)
    if max_cost_err:
        sys.stderr.write(f"[agent] {max_cost_err}\n")
        return 1
    if max_cost is None:
        max_cost = config_default_max_cost
    if add_budget is not None and not resume_session:
        sys.stderr.write("[agent] --add-budget requires --resume.\n")
        return 1

    followup_obj, followup_err = _load_resume_followup_for_session(
        resume_session=resume_session,
        follow_up=follow_up,
        follow_up_file=follow_up_file,
        workspace_root=workspace_root,
        manifest=manifest,
    )
    if followup_err:
        sys.stderr.write(f"[agent] {followup_err}\n")
        return 1

    # Export thinking budget from config (picked up by AnthropicClient)
    thinking_budget = cfg.get("thinking_budget")
    if thinking_budget and not os.environ.get("AMOF_THINKING_BUDGET"):
        os.environ["AMOF_THINKING_BUDGET"] = str(int(thinking_budget))

    # Auto-load .env if API key isn't already in environment
    _auto_load_env(Path.cwd() / ".env")
    provider_base_url = _profile_base_url(provider_profile)
    if provider == "runpod":
        provider_base_url = _normalize_runpod_openai_base_url(provider_base_url)
    profile_default_model = _profile_model(provider_profile) if not explicit_provider else None
    local_timeout_seconds: float | None = None
    if provider in {"local", "runpod"}:
        local_base_url_error = _validate_local_base_url(provider_base_url)
        if local_base_url_error:
            sys.stderr.write(
                "[agent] "
                f"{local_base_url_error}; "
                f"{_provider_endpoint_diagnostics(provider=provider, profile=provider_profile, base_url=provider_base_url, model=profile_default_model or model, endpoint_family='chat.completions')}\n"
            )
            return 1
        local_model_error = _validate_local_model(profile_default_model or model)
        if local_model_error:
            sys.stderr.write(
                "[agent] "
                f"{local_model_error}; "
                f"{_provider_endpoint_diagnostics(provider=provider, profile=provider_profile, base_url=provider_base_url, model=profile_default_model or model, endpoint_family='chat.completions')}\n"
            )
            return 1
        local_timeout_seconds, local_timeout_error = _resolve_local_timeout_seconds(provider_profile)
        if local_timeout_error:
            sys.stderr.write(
                "[agent] "
                f"{local_timeout_error}; "
                f"{_provider_endpoint_diagnostics(provider=provider, profile=provider_profile, base_url=provider_base_url, model=profile_default_model or model, endpoint_family='chat.completions')} "
                f"sdk_max_retries=0\n"
            )
            return 1

    # Resolve API key based on provider
    if provider == "openai":
        api_key_env = _profile_credential_env(provider_profile, "api_key_env") if not explicit_provider else None
        api_key_env = api_key_env or "OPENAI_API_KEY"
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            sys.stderr.write(
                f"[agent] {api_key_env} not set.\n"
                f"  Export {api_key_env}=<provider-api-key> before running live agent calls.\n"
            )
            return 1
    elif provider == "openrouter":
        api_key_env = _profile_credential_env(provider_profile, "api_key_env") if not explicit_provider else None
        api_key_env = api_key_env or "OPENROUTER_API_KEY"
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            sys.stderr.write(
                f"[agent] {api_key_env} not set.\n"
                f"  Export {api_key_env}=<provider-api-key> before running live agent calls.\n"
            )
            return 1
    elif provider == "bedrock":
        api_key = ""
        region = (
            os.environ.get("AMOF_BEDROCK_REGION")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        )
        if not region:
            sys.stderr.write(
                "[agent] AWS_REGION not set for Bedrock.\n"
                "  Export AWS_REGION (or AMOF_BEDROCK_REGION) and optionally AWS_PROFILE.\n"
            )
            return 1
    elif provider == "local":
        api_key = ""
    elif provider == "runpod":
        api_key_env = _profile_credential_env(provider_profile, "api_key_env") if not explicit_provider else None
        api_key_env = api_key_env or "RUNPOD_API_KEY"
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            sys.stderr.write(
                f"[agent] {api_key_env} not set.\n"
                f"  Export {api_key_env}=<provider-api-key> before running live agent calls.\n"
            )
            return 1
    else:
        api_key_env = _profile_credential_env(provider_profile, "api_key_env") if not explicit_provider else None
        api_key_env = api_key_env or "ANTHROPIC_API_KEY"
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            sys.stderr.write(
                f"[agent] {api_key_env} not set.\n"
                f"  Export {api_key_env}=<provider-api-key> before running live agent calls.\n"
            )
            return 1

    # Early check: if .venv exists but we're not running from it, auto-relaunch
    venv_dir = Path.cwd() / ".venv"
    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():
        venv_python = venv_dir / "Scripts" / "python.exe"  # Windows
    if venv_dir.exists() and venv_python.exists():
        in_venv = (sys.prefix != sys.base_prefix) or str(venv_dir) in sys.prefix
        if not in_venv:
            # Check if the SDK is actually importable from system python
            try:
                import anthropic  # noqa: F401
            except ImportError:
                # Re-exec with the venv python instead of asking user to activate
                sys.stderr.write("[agent] Using virtual environment: .venv\n")
                sys.stderr.flush()
                os.execv(str(venv_python), [str(venv_python)] + sys.argv)

    # Lazy import to avoid import errors when SDK isn't installed.
    try:
        from ..orchestrator.llm.anthropic import AnthropicClient
        from ..orchestrator.tools import create_default_registry, Guardrails
        from ..orchestrator.agent import Agent
        from ..orchestrator.session import Session
        from ..orchestrator.telemetry import SessionTelemetry
        from ..orchestrator.events import EventLog
        from ..orchestrator.context.builder import ContextBuilder
        from ..orchestrator.context.summarizer import ContextSummarizer
        from ..orchestrator.model_router import ModelRouter
    except ImportError as e:
        missing = str(e)
        sys.stderr.write(f"[agent] Missing dependency: {missing}\n")
        missing_module = _missing_module_name(e)
        if missing_module in AMOF_RUNTIME_DEPENDENCIES or missing_module in OPTIONAL_MEMORY_DEPENDENCIES:
            if missing_module in OPTIONAL_MEMORY_DEPENDENCIES:
                sys.stderr.write(_memory_dependency_guidance() + "\n")
            else:
                package_name = AMOF_RUNTIME_DEPENDENCIES.get(missing_module, missing_module)
                sys.stderr.write(_runtime_dependency_guidance(package_name))
            return 1

        # Check if running from a venv
        venv_dir = Path.cwd() / ".venv"
        if venv_dir.exists():
            venv_python = venv_dir / "bin" / "python"
            if venv_python.exists():
                sys.stderr.write(
                    "\n[agent] A virtual environment exists but isn't activated.\n"
                    "  Run:\n"
                    "    source .venv/bin/activate\n"
                    "    amof agent\n"
                )
                return 1

        sys.stderr.write(
            "\n[agent] Dependency import failed before the agent could start.\n"
            "  If this is an AMOF runtime dependency, run: amof update\n"
            "  If this belongs to the target project, install that project dependency in the target environment.\n"
        )
        return 1

    # Provider-specific client factory
    def _make_client(mdl: str) -> Any:
        """Create an LLM client for the configured provider."""
        if provider in {"local", "runpod"}:
            from ..orchestrator.llm.local_openai_compatible import LocalOpenAICompatibleClient

            return LocalOpenAICompatibleClient(
                base_url=provider_base_url or "",
                model=mdl,
                api_key=api_key or None,
                timeout=local_timeout_seconds if local_timeout_seconds is not None else 60.0,
                provider_id="runpod" if provider == "runpod" else "local",
            )

        # Check if the model string is openrouter/ style
        if mdl.startswith("openrouter/"):
            from ..orchestrator.llm.openai_client import OpenAIClient
            return OpenAIClient(api_key=api_key, model=mdl, base_url=provider_base_url)
        
        if provider in ("openai", "openrouter"):
            from ..orchestrator.llm.openai_client import OpenAIClient
            if provider == "openrouter" and not mdl.startswith("openrouter/"):
                mdl = f"openrouter/{mdl}"
            return OpenAIClient(api_key=api_key, model=mdl, base_url=provider_base_url)
        if provider == "bedrock":
            from ..orchestrator.llm.bedrock_anthropic import BedrockAnthropicClient

            return BedrockAnthropicClient(model=mdl)
        else:
            return AnthropicClient(api_key=api_key, model=mdl)

    mode = "plan" if plan_mode else "build"

    # Set up guardrails from config + manifest
    from ..orchestrator.tools.base import GuardrailConfig

    no_touch = manifest.get("guardrails", {}).get("no_touch_paths", [])
    readonly_repos = {}
    for r in manifest.get("repos", []):
        if r.get("readonly"):
            readonly_repos[r["name"]] = Path(r["path"])
    writable_roots = [workspace_root] if _is_appdata_adopted_manifest(manifest) else []

    guardrail_config = GuardrailConfig.load(_agent_rules_path(workspace_root, "guardrails.yaml"))

    guardrails = Guardrails(
        no_touch_paths=no_touch,
        readonly_repos=readonly_repos,
        writable_roots=writable_roots,
        mode=mode,
        config=guardrail_config,
        confirm_fn=_interactive_confirm,
    )
    _configure_guardrails(guardrails, workspace_root)

    # Create components
    session = Session(session_id=resume_session, mode=mode)
    session.ecosystem = manifest.get("ecosystem")
    budget_thresholds = cfg.get("budget_warning_thresholds", [0.50, 0.75, 0.90])
    telemetry = SessionTelemetry(
        max_cost=max_cost,
        warning_thresholds=[float(t) for t in budget_thresholds],
    )
    events = EventLog(session_id=session.id)

    # ---- Model setup ----
    model_router = None
    context_summarizer = None

    # Default model depends on provider
    default_model = _default_worker_model(provider, model, profile_default_model)

    primary_llm = _make_client(default_model)

    if model_ladder:
        from ..orchestrator.llm.profile_catalog import (
            build_clients_from_selection,
            get_profile_selection,
        )

        # Check for new llm_ladder format
        llm_ladder_cfg = cfg.get("llm_ladder", {}).get("roles", {})
        orchestrator_cascade = llm_ladder_cfg.get("orchestrator", {}).get("cascade", [])
        worker_cascade = llm_ladder_cfg.get("worker", {}).get("cascade", [])

        models = {}

        profile_selection = get_profile_selection(cfg)
        if cfg.get("llm_profile_selection"):
            models = build_clients_from_selection(profile_selection)
            orchestrator_cascade = [slot for slot in ("fast", "standard", "strong") if slot in models]
            worker_cascade = list(orchestrator_cascade)
        elif orchestrator_cascade or worker_cascade:
            # New format: Instantiate clients for all unique models in both cascades
            for mdl in set(orchestrator_cascade + worker_cascade):
                models[mdl] = _make_client(mdl)
        else:
            # Fallback to legacy fast/standard/strong mapping
            if provider == "openai":
                fast_id = fast_model or os.environ.get("AMOF_FAST_MODEL", "gpt-4o-mini")
                standard_id = model or os.environ.get("AMOF_MODEL", "gpt-4o")
                strong_id = strong_model or os.environ.get("AMOF_STRONG_MODEL", "gpt-5.1-codex")
            elif provider == "openrouter":
                fast_id = fast_model or os.environ.get("AMOF_FAST_MODEL", "openrouter/openai/gpt-4o-mini")
                standard_id = model or os.environ.get("AMOF_MODEL", "openrouter/openai/gpt-4o")
                strong_id = strong_model or os.environ.get("AMOF_STRONG_MODEL", "openrouter/openai/gpt-4.1")
            else:
                fast_id = fast_model or os.environ.get("AMOF_FAST_MODEL", DEFAULT_TIERS["fast"])
                standard_id = model or os.environ.get("AMOF_MODEL", DEFAULT_TIERS["standard"])
                strong_id = strong_model or os.environ.get("AMOF_STRONG_MODEL", DEFAULT_TIERS["strong"])

            models = {
                "fast": _make_client(fast_id),
                "standard": _make_client(standard_id),
                "strong": _make_client(strong_id),
            }
            orchestrator_cascade = ["fast", "standard", "strong"]
            worker_cascade = ["fast", "standard", "strong"]

        # Build fallback models (alternate provider) for failover
        fallback_cfg = cfg.get("provider_fallback", {})
        fallback_provider_name = fallback_cfg.get("fallback", "openai" if provider != "openai" else "anthropic")
        fallback_cooldown = float(fallback_cfg.get("cooldown_seconds", 60))
        fallback_models: Dict[str, Any] = {}

        try:
            if fallback_provider_name == "openai" and provider != "openai":
                openai_key = os.environ.get("OPENAI_API_KEY", "")
                if openai_key:
                    from ..orchestrator.llm.openai_client import OpenAIClient
                    fallback_models = {
                        "fast": OpenAIClient(api_key=openai_key, model="gpt-4o-mini"),
                        "standard": OpenAIClient(api_key=openai_key, model="gpt-4o"),
                        "strong": OpenAIClient(api_key=openai_key, model="gpt-5.1-codex"),
                    }
            elif fallback_provider_name == "anthropic" and provider != "anthropic":
                anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
                if anthropic_key:
                    fallback_models = {
                        "fast": AnthropicClient(api_key=anthropic_key, model=DEFAULT_TIERS["fast"]),
                        "standard": AnthropicClient(api_key=anthropic_key, model=DEFAULT_TIERS["standard"]),
                        "strong": AnthropicClient(api_key=anthropic_key, model=DEFAULT_TIERS["strong"]),
                    }
        except Exception as e:
            # Mini Ultra Plan 2 / Phase L1: surface alternate-provider
            # build failures at WARNING (was DEBUG, which silently hid
            # missing-key situations from operators). The
            # `failure_class` tag aligns with the
            # ``classify_provider_status``-derived taxonomy so the
            # missing-fallback condition is greppable in operator logs
            # alongside real provider errors.
            logger.warning(
                "Fallback provider %s unavailable (failure_class=provider_fallback_unavailable): %s",
                fallback_provider_name,
                e,
            )

        routing_config = cfg.get("routing", {})

        # Default tier logic: use "standard" if present, else first in cascade
        def_tier = "standard" if "standard" in models else orchestrator_cascade[0] if orchestrator_cascade else next(iter(models))

        model_router = ModelRouter(
            models=models,
            default_tier=def_tier,
            fallback_models=fallback_models if fallback_models else None,
            provider_cooldown=fallback_cooldown,
            routing_config=routing_config,
            cascade=orchestrator_cascade,
        )
        primary_llm = model_router  # Router acts as LLMClient

        # ContextSummarizer uses the fast model for cheap compression
        fast_mdl_key = "fast" if "fast" in models else orchestrator_cascade[0] if orchestrator_cascade else next(iter(models.keys()))
        context_summarizer = ContextSummarizer(
            summarizer_llm=models[fast_mdl_key],
            threshold_pct=60.0,
            keep_recent=6,
        )

        if verbose:
            sys.stderr.write(
                f"[agent] Model ladder enabled ({provider})\n"
                f"  Orchestrator: {orchestrator_cascade}\n"
                f"  Worker:       {worker_cascade}\n"
            )

    from ..orchestrator.trust_boundary import create_trust_state

    trust_state = create_trust_state(goal or "") if (goal or "").strip() else None

    # ---- Runner factory + tool registry ----
    runner_factory = None
    model_clients = {"standard": _make_client(default_model)} if not model_ladder else models
    runners_config_path = _agent_rules_path(workspace_root, "runners.yaml")
    runner_cost_fraction = float(cfg.get("runner_cost_fraction", 0.3))
    runner_max_cost = (max_cost or 5.0) * runner_cost_fraction
    if budget_options.subtask_budget is not None:
        runner_max_cost = min(runner_max_cost, budget_options.subtask_budget)

    should_load_runners = runners_config_path.exists() or bool(plan_execute)
    if should_load_runners:
        try:
            from ..orchestrator.runners import PUBLIC_DEFAULT_RUNNERS_CONFIG, RunnerFactory
            # Build worker tool registry first (without delegate), then create factory
            _base_tools = create_default_registry(
                guardrails=guardrails,
                ops_tools=cfg.get("ops_tools", True),
                workspace_root=workspace_root,
                jenkins_jobs=cfg.get("jenkins_jobs"),
                deploy_presets=cfg.get("deploy_presets"),
                role="worker",
                events=events,
                trust_state=trust_state,
                policy_source="runner",
            )
            runner_factory = RunnerFactory.from_config(
                config_path=runners_config_path,
                model_clients=model_clients,
                parent_tools=_base_tools,
                guardrails=guardrails,
                workspace_root=workspace_root,
                max_cost_per_runner=runner_max_cost,
                verbose=verbose,
                cascade=worker_cascade if model_ladder else None,
                default_config=PUBLIC_DEFAULT_RUNNERS_CONFIG if not runners_config_path.exists() else None,
            )
            if verbose:
                sys.stderr.write(
                    f"[agent] Runners loaded: {', '.join(runner_factory.runner_names)}\n"
                )
        except Exception as e:
            sys.stderr.write(f"[agent] Runner factory init failed (non-fatal): {e}\n")
            runner_factory = None

    # ---- Vector Memory Setup ----
    vector_store = None
    ecosystem_name = manifest.get("ecosystem", "")
    memory_explicitly_enabled = bool(
        cfg.get("vector_memory")
        or cfg.get("memory_enabled")
        or (isinstance(cfg.get("memory"), dict) and cfg["memory"].get("enabled"))
    )
    try:
        from ..orchestrator.memory import VectorStore
        vector_store = VectorStore(persist_directory=_agent_vector_store_path(workspace_root))
        if verbose:
            sys.stderr.write("[agent] Vector memory initialized.\n")
    except Exception as e:
        if verbose or memory_explicitly_enabled:
            sys.stderr.write(
                f"[agent] Vector memory unavailable (non-fatal): {e}\n"
                f"{_memory_dependency_guidance()}\n"
            )

    # Create full tool registry (with DelegateTool if factory available)
    summarizer_llm = model_clients.get("fast") if model_ladder else None
    tools = create_default_registry(
        guardrails=guardrails,
        ops_tools=cfg.get("ops_tools", True),
        workspace_root=workspace_root,
        runner_factory=runner_factory,
        parent_telemetry=telemetry,
        summarizer_llm=summarizer_llm,
        jenkins_jobs=cfg.get("jenkins_jobs"),
        deploy_presets=cfg.get("deploy_presets"),
        role="orchestrator",
        vector_store=vector_store,
        ecosystem_name=ecosystem_name,
        events=events,
        trust_state=trust_state,
        policy_source="master",
    )

    # ---- Auto-index codebase (Merkle tree + incremental LLM index) ----
    codebase_index = None
    index_dir = _agent_index_path(workspace_root, ecosystem_name)

    repos_root = workspace_root / "repos"
    if repos_root.exists() and cfg.get("auto_index", True):
        try:
            from ..orchestrator.indexer import CodebaseIndexer
            from ..orchestrator.manifest_scope import resolve_scope

            scope = resolve_scope(manifest, workspace_root, ecosystem=ecosystem_name)
            if scope.is_empty():
                sys.stderr.write(
                    f"[agent] Indexing scope empty for ecosystem={ecosystem_name} "
                    f"(skipped={scope.skipped}); skipping auto-index\n"
                )
            else:
                indexer = CodebaseIndexer(
                    indexer_llm=primary_llm,
                    repos_root=repos_root,
                    index_dir=index_dir,
                    vector_store=vector_store,
                    ecosystem_name=ecosystem_name,
                    repo_roots=scope.repo_roots,
                )
                if verbose:
                    sys.stderr.write(
                        f"[agent] Indexing scope: {scope.repo_count} repo(s) "
                        f"({', '.join(p.name for p in scope.repo_roots)})\n"
                    )

                if indexer.index_path.exists() and indexer.tree_path.exists():
                    from ..orchestrator.merkle import MerkleTree
                    current_tree = MerkleTree.build_from_roots(scope.repo_roots)
                    cached_tree = MerkleTree.load(indexer.tree_path)

                    if current_tree.hash == cached_tree.hash:
                        codebase_index = indexer._load_cached()
                        if verbose:
                            sys.stderr.write(
                                f"[agent] Index up to date ({codebase_index.file_count} files)\n"
                            )
                    else:
                        diff = MerkleTree.diff(cached_tree, current_tree)
                        sys.stderr.write(
                            f"[agent] Index stale ({diff.summary()}), updating...\n"
                        )
                        codebase_index = indexer.index(force=False)
                        sys.stderr.write(
                            f"[agent] Index updated ({codebase_index.file_count} files, "
                            f"${codebase_index.indexing_cost:.4f})\n"
                        )
                else:
                    sys.stderr.write("[agent] No codebase index found, creating...\n")
                    codebase_index = indexer.index(force=True)
                    sys.stderr.write(
                        f"[agent] Indexed {codebase_index.file_count} files "
                        f"(${codebase_index.indexing_cost:.4f})\n"
                    )
        except Exception as e:
            sys.stderr.write(f"[agent] Auto-index failed (non-fatal): {e}\n")

    # Build context (with codebase index if available)
    context_builder = ContextBuilder(
        workspace_root=workspace_root,
        manifest=manifest,
        base_prompt_path=workspace_root / "prompts" / "master.md",
        codebase_index=codebase_index,
    )
    system_prompt = context_builder.build(mode=mode)

    # Create agent
    agent = Agent(
        llm=primary_llm,
        tools=tools,
        system_prompt=system_prompt,
        session=session,
        telemetry=telemetry,
        events=events,
        verbose=verbose,
        model_router=model_router,
        context_summarizer=context_summarizer,
    )

    # ---- Signal handler for graceful shutdown ----
    _shutdown_requested = False

    def _handle_signal(signum, frame):
        nonlocal _shutdown_requested
        if _shutdown_requested:
            # Second signal: force exit
            sys.exit(130)
        _shutdown_requested = True
        sys.stderr.write("\n[agent] Interrupt received — saving session & journal...\n")
        # Save session state
        _save_session(session, telemetry, events, workspace_root)
        # Write journal with interrupted status
        try:
            journal_goal = session.goal or "interactive-shell"
            if journal_goal == "interactive-shell" and session.messages:
                first_user = next((m.content for m in session.messages if m.role == "user"), None)
                if first_user:
                    journal_goal = first_user[:120]
            _generate_journal(
                session, journal_goal, "interrupted",
                telemetry, events, manifest, workspace_root,
            )
        except Exception:
            pass  # best-effort journal on interrupt
        sys.stderr.write(
            f"[agent] Session saved. To resume:\n"
            f"  amof agent --resume {session.id}\n"
        )
        sys.exit(130)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ---- Resume from previous session ----
    resume_checkpoint: Optional[Dict[str, Any]] = None
    if resume_session:
        session_dir = _resolve_session_dir(workspace_root, resume_session)
        messages_path = session_dir / "messages.jsonl"
        telemetry_path = session_dir / "telemetry.json"
        if messages_path.exists():
            _load_session_messages(session, messages_path)
            if verbose:
                sys.stderr.write(f"[agent] Resumed session {resume_session} ({session.turn_count} turns)\n")
        if telemetry_path.exists():
            from ..orchestrator.telemetry import SessionTelemetry as _SessionTelemetry

            restored = _SessionTelemetry.load(telemetry_path)
            telemetry.max_cost = restored.max_cost
            telemetry._restored_cost = restored._restored_cost
            if verbose:
                sys.stderr.write(
                    f"[agent] Restored telemetry (${telemetry.total_cost:.4f} spent, "
                    f"limit ${telemetry.max_cost or 0:.2f})\n"
                )
        if followup_obj:
            _log_resume_followup(events, session, followup_obj)
        if add_budget is not None:
            telemetry.extend_budget(add_budget)
            events.budget_approval(
                session_id=session.id,
                amount=add_budget,
                new_limit=float(telemetry.max_cost or add_budget),
                source="cli_flag",
            )
        from ..orchestrator.resume_control import (
            find_latest_plan_checkpoint,
            format_budget_status,
            update_checkpoint_budget,
        )

        resume_checkpoint = find_latest_plan_checkpoint(
            _agent_plan_checkpoints_dir(manifest, workspace_root),
            resume_session,
        )
        if add_budget is not None and resume_checkpoint is not None:
            cp_path = Path(resume_checkpoint["_checkpoint_path"])
            update_checkpoint_budget(
                cp_path,
                resume_checkpoint,
                add_budget=add_budget,
                new_limit=float(telemetry.max_cost or 0),
            )
        if budget_options.budget_status:
            print(
                format_budget_status(
                    resume_session,
                    telemetry,
                    resume_checkpoint,
                    subtask_budget=budget_options.subtask_budget,
                )
            )
            return 0
        if followup_obj and not resume_checkpoint:
            session.add_user_message(f"[Operator resume follow-up]\n{followup_obj.text}")

    # ---- Plan-execute mode ----
    plan_execute_resume = bool(resume_session and resume_checkpoint)
    if plan_execute_resume and not plan_execute:
        plan_execute = True
    if plan_execute and (goal or plan_execute_resume):
        from ..orchestrator.planner import TaskPlanner, ExecutionPlan
        from ..orchestrator.executor import SubtaskExecutor
        from ..orchestrator.llm.base import ProviderError

        # Determine planner model (provider-aware default; explicit flag/env wins).
        planner_model_id = _default_planner_model(
            provider,
            planner_model,
            profile_default_model if not explicit_provider else None,
        )
        planner_llm = _make_client(planner_model_id)

        if verbose:
            sys.stderr.write(f"[agent] Plan-execute mode | Planner: {planner_model_id}\n")

        if plan_execute_resume and resume_checkpoint:
            goal = goal or str(resume_checkpoint.get("goal") or "") or (session.goal or "")
        if not goal:
            sys.stderr.write("[plan-execute] Goal is required.\n")
            return 1

        events.session_start(mode="plan-execute", goal=goal, ecosystem=session.ecosystem)

        # Check if we're resuming from a plan file
        plan = None
        plans_dir = _agent_plans_dir(manifest, workspace_root)
        plan_execute_task_context = goal
        resume_next_subtask = ""

        if plan_file:
            plan_path = Path(plan_file)
            if plan_path.exists():
                plan = ExecutionPlan.load_from_markdown(plan_path)
                completed = sum(1 for st in plan.subtasks if st.status == "completed")
                print(f"[plan-execute] Resumed plan from {plan_path} ({completed}/{len(plan.subtasks)} tasks done)")
            else:
                sys.stderr.write(f"[plan-execute] Plan file not found: {plan_file}\n")
                return 1

        if plan is None and plan_execute_resume and resume_checkpoint:
            from ..orchestrator.resume_control import (
                append_followup_to_context,
                check_budget_before_execution,
                format_resume_summary,
                prepare_plan_for_resume,
            )

            cp_plan = plan_file or resume_checkpoint.get("plan_path")
            if not cp_plan:
                sys.stderr.write("[plan-execute] Resume checkpoint has no plan_path.\n")
                return 1
            cp_path = Path(cp_plan)
            if not cp_path.is_file():
                sys.stderr.write(f"[plan-execute] Plan file not found: {cp_path}\n")
                return 1
            plan = ExecutionPlan.load_from_markdown(cp_path)
            resume_next_subtask = prepare_plan_for_resume(plan, resume_checkpoint)
            plan_execute_task_context = append_followup_to_context(goal, followup_obj)
            completed_count = len(resume_checkpoint.get("completed_subtasks") or [])
            remaining_count = sum(1 for st in plan.subtasks if st.status == "pending")
            print(
                format_resume_summary(
                    session_id=session.id,
                    checkpoint=resume_checkpoint,
                    followup=followup_obj,
                    add_budget=add_budget,
                    telemetry=telemetry,
                    next_subtask_id=resume_next_subtask,
                    completed_count=completed_count,
                    remaining_count=remaining_count,
                )
            )
            elev = resume_checkpoint.get("capability_elevation")
            if elev and trust_state is not None:
                from ..orchestrator.plan_execute_control import (
                    PlanCapabilityElevation,
                    apply_capability_elevation,
                )

                elevation = PlanCapabilityElevation(
                    session_id=str(elev.get("session_id") or session.id),
                    plan_id=str(elev.get("plan_id") or ""),
                    approved_capabilities=list(elev.get("approved_capabilities") or []),
                    base_ceiling=list(elev.get("base_ceiling") or []),
                    approved_tools=list(elev.get("approved_tools") or []),
                    approved_paths=list(elev.get("approved_paths") or []),
                    rationales=dict(elev.get("rationales") or {}),
                    approval_source=str(elev.get("approval_source") or "checkpoint"),
                )
                apply_capability_elevation(trust_state, elevation)
                plan.capability_elevation = elev  # type: ignore[attr-defined]
            budget_block = check_budget_before_execution(
                telemetry,
                plan,
                budget_options,
                noninteractive=_plan_execute_noninteractive(no_follow_up, approve_plan),
            )
            if budget_block == "__prompt__":
                try:
                    choice = input("Estimated cost exceeds budget. Continue? [y/N] > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return 1
                if choice not in ("y", "yes"):
                    sys.stderr.write("[plan-execute] Execution not approved.\n")
                    return 1
            elif budget_block:
                sys.stderr.write(f"[plan-execute] {budget_block}\n")
                return 1

        if plan is None:
            # Build codebase context for the planner
            codebase_context = context_builder.build(mode="plan")

            # Build guardrail info text for the planner
            guardrail_info_parts = []
            if no_touch:
                guardrail_info_parts.append(f"no_touch_paths: {', '.join(no_touch)}")
            if readonly_repos:
                guardrail_info_parts.append(f"readonly repos: {', '.join(readonly_repos.keys())}")
            guardrail_info = "\n".join(guardrail_info_parts) if guardrail_info_parts else None

            # Interactive planning loop: plan -> questions -> user review
            planner = TaskPlanner(
                planner_llm=planner_llm,
                workspace_root=workspace_root,
            )

            task_with_answers = goal
            max_plan_retries = 3
            while True:
                print(f"[plan-execute] Planning with {planner_model_id}...")

                # Retry loop for transient parse / API errors
                plan = None
                for attempt in range(1, max_plan_retries + 1):
                    try:
                        plan = planner.plan(
                            task=task_with_answers,
                            codebase_context=codebase_context,
                            guardrail_info=guardrail_info,
                        )
                        break  # success
                    except ProviderError as e:
                        sys.stderr.write(f"[plan-execute] Planning provider error: {e}\n")
                        return 1
                    except Exception as e:
                        if attempt < max_plan_retries:
                            sys.stderr.write(
                                f"[plan-execute] Planning attempt {attempt}/{max_plan_retries} "
                                f"failed: {e}\n  Retrying...\n"
                            )
                        else:
                            sys.stderr.write(
                                f"[plan-execute] Planning failed after {max_plan_retries} "
                                f"attempts: {e}\n"
                            )

                if plan is None:
                    return 1

                # Display extended thinking (if produced by thinking model)
                thinking_text = planner.last_thinking
                if thinking_text:
                    sys.stderr.write("[plan-execute] Thinking:\n")
                    for tl in thinking_text.strip().splitlines()[:30]:
                        sys.stderr.write(f"  {tl}\n")
                    total_lines = len(thinking_text.strip().splitlines())
                    if total_lines > 30:
                        sys.stderr.write(f"  ... ({total_lines - 30} more lines)\n")

                # Handle planner questions
                if plan.questions:
                    print(f"\n[plan-execute] The planner has questions:\n")
                    for i, q in enumerate(plan.questions, 1):
                        print(f"  {i}. {q}")
                    print()
                    if _plan_execute_noninteractive(no_follow_up, approve_plan):
                        print("[plan-execute] Noninteractive mode enabled; skipping clarification questions.")
                    else:
                        try:
                            user_answers = input("Your answers (or 'skip' to plan without answering): ").strip()
                        except (EOFError, KeyboardInterrupt):
                            return 1
                        if user_answers.lower() != "skip":
                            task_with_answers = f"{goal}\n\nUser answers to planner questions:\n{user_answers}"
                            continue
                        # skip: proceed with current plan

                # Save plan to ecosystem plans folder
                slug = "-".join(goal.lower().split()[:6])
                slug = "".join(c for c in slug if c.isalnum() or c == "-")[:50]
                plan_path = plans_dir / f"{time.strftime('%Y-%m-%d')}-{slug}.md"
                plan.save_as_markdown(plan_path, session_id=session.id)
                print(f"\n[plan-execute] Plan saved to: {plan_path}")

                # Show plan summary
                print(f"\n=== Execution Plan ({len(plan.subtasks)} tasks, ${plan.planning_cost:.4f}) ===\n")
                print(f"Analysis: {plan.analysis[:200]}...")
                print()
                print(plan.summary())
                if plan.risks:
                    print(f"\nRisks: {', '.join(plan.risks)}")
                print()

                runner_error = _validate_runner_factory_for_plan(runner_factory, plan)
                if runner_error:
                    sys.stderr.write(f"[plan-execute] {runner_error}\n")
                    return 1

                # Interactive approval. --no-follow-up only skips the post-run
                # menu; it does not approve execution. Use --approve-plan for CI.
                if approve_plan:
                    choice = "approve"
                    print("[plan-execute] Plan approved via --approve-plan.")
                else:
                    try:
                        choice = input("[a]pprove  [e]dit  [r]eject  > ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        return 1

                if choice in ("a", "approve", ""):
                    # Re-read plan file in case user edited it
                    plan = ExecutionPlan.load_from_markdown(plan_path)
                    break
                elif choice in ("r", "reject"):
                    print("[plan-execute] Plan rejected.")
                    return 0
                elif choice in ("e", "edit"):
                    try:
                        feedback = input("Feedback (or edit the .md file directly, then press Enter): ").strip()
                    except (EOFError, KeyboardInterrupt):
                        return 1
                    if feedback:
                        task_with_answers = f"{goal}\n\nUser feedback on previous plan:\n{feedback}"
                    else:
                        # User edited file directly, re-read it
                        plan = ExecutionPlan.load_from_markdown(plan_path)
                        break
                    continue
                else:
                    print(f"Unknown choice: {choice}")
                    continue

        runner_error = _validate_runner_factory_for_plan(runner_factory, plan)
        if runner_error:
            sys.stderr.write(f"[plan-execute] {runner_error}\n")
            return 1

        if plan is not None and followup_obj and not plan_execute_resume:
            from ..orchestrator.resume_control import append_followup_to_context

            plan_execute_task_context = append_followup_to_context(goal, followup_obj)

        budget_block = None
        if plan is not None:
            from ..orchestrator.resume_control import check_budget_before_execution

            budget_block = check_budget_before_execution(
                telemetry,
                plan,
                budget_options,
                noninteractive=_plan_execute_noninteractive(no_follow_up, approve_plan),
            )
            if budget_block == "__prompt__":
                try:
                    choice = input("Estimated cost exceeds budget. Continue? [y/N] > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return 1
                if choice not in ("y", "yes"):
                    sys.stderr.write("[plan-execute] Execution not approved.\n")
                    return 1
                budget_block = None
            elif budget_block:
                sys.stderr.write(f"[plan-execute] {budget_block}\n")
                return 1

        _, readiness_exit = _gate_plan_execute_readiness(
            goal,
            plan,
            session=session,
            trust_state=trust_state,
            runner_factory=runner_factory,
            guardrails=guardrails,
            tool_registry=tools,
            events=events,
            telemetry=telemetry,
            manifest=manifest,
            workspace_root=workspace_root,
            approve_capabilities=approve_capabilities,
            approve_tool_packs=approve_tool_packs,
            approve_writable_roots=approve_writable_roots,
            no_follow_up=bool(no_follow_up),
        )
        if readiness_exit is not None:
            return readiness_exit

        # Record planning cost in telemetry
        if plan.planning_cost > 0:
            from ..orchestrator.llm.base import Usage
            planner_usage = Usage(
                model=plan.planner_model,
                prompt_tokens=0, completion_tokens=0,
                latency_ms=plan.planning_latency_ms,
                estimated_cost=plan.planning_cost,
            )
            telemetry.record_from_usage(planner_usage, tier="strong")

        mutation_intent = _plan_has_mutation_intent(goal, plan)
        git_before = _git_probe(workspace_root)

        # Phase 2: Execute
        executor = SubtaskExecutor(
            runner_factory=runner_factory,
            parent_telemetry=telemetry,
            verbose=verbose,
        )

        print("[plan-execute] Executing subtasks...")
        if resume_next_subtask:
            print(f"[plan-execute] Next subtask: {resume_next_subtask}")
        plan = executor.execute_plan(plan, task_context=plan_execute_task_context)
        fatal_stop = getattr(plan, "fatal_stop", None)
        if fatal_stop is not None:
            return _handle_plan_execute_fatal_stop(
                plan,
                session=session,
                telemetry=telemetry,
                events=events,
                manifest=manifest,
                workspace_root=workspace_root,
                goal=goal,
                stop=fatal_stop,
                no_follow_up=bool(no_follow_up),
                continue_budget=continue_budget,
            )
        git_after = _git_probe(workspace_root)
        diff_changed = git_before.get("diff") != git_after.get("diff")
        has_after_diff = bool(git_after.get("numstat") or git_after.get("diff"))
        failed_tool_calls, failed_write_tool_calls = _tool_failure_counts(telemetry)
        write_action_count = _write_action_count(telemetry)
        write_action_observed = write_action_count > 0
        diff_guard = {
            "status": "not_applicable",
            "reasons": [],
            "changed_files": [],
            "added_lines": 0,
            "deleted_lines": 0,
            "destructive_rewrite_detected": False,
            "requested_paths": [],
            "requested_paths_observed": True,
        }
        if mutation_intent and has_after_diff:
            diff_guard = _evaluate_diff_guard(goal, workspace_root, git_after)
        py_compile_guard = _verify_changed_python_files(workspace_root, git_after)

        verifier_failed = False
        verifier_reasons: list[str] = []
        if failed_tool_calls:
            verifier_failed = True
            verifier_reasons.append(f"failed_tool_calls:{failed_tool_calls}")
            _mark_plan_failed_for_verifier(
                plan,
                f"Verifier failed: {failed_tool_calls} tool call(s) failed.",
            )
        if mutation_intent and not write_action_observed:
            verifier_failed = True
            verifier_reasons.append("mutation-intent plan did not call a write-class tool")
            _mark_plan_failed_for_verifier(
                plan,
                "Verifier failed: mutation-intent plan did not call a write-class tool.",
            )
        if mutation_intent and (not diff_changed or not has_after_diff):
            verifier_failed = True
            verifier_reasons.append("mutation-intent plan produced no target repository diff")
            _mark_plan_failed_for_verifier(
                plan,
                "Verifier failed: mutation-intent plan produced no target repository diff.",
            )
        if mutation_intent and diff_guard["status"] == "fail":
            verifier_failed = True
            reasons = ", ".join(diff_guard["reasons"]) or "unknown"
            verifier_reasons.append(f"diff_guard:{reasons}")
            _mark_plan_failed_for_verifier(
                plan,
                f"Verifier failed: diff guard rejected target diff ({reasons}).",
            )
        if py_compile_guard["status"] == "fail":
            verifier_failed = True
            reasons = ", ".join(py_compile_guard["reasons"]) or "unknown"
            verifier_reasons.append(f"py_compile:{reasons}")
            _mark_plan_failed_for_verifier(
                plan,
                f"Verifier failed: Python compile check failed ({reasons}).",
            )

        # Summary
        completed = sum(1 for st in plan.subtasks if st.status == "completed")
        failed = sum(1 for st in plan.subtasks if st.status == "failed")
        skipped = sum(1 for st in plan.subtasks if st.status == "skipped")
        print(
            f"\n[plan-execute] Execution complete: "
            f"{completed}/{len(plan.subtasks)} completed, {failed} failed, {skipped} skipped"
        )
        print(
            "[plan-execute] Verification: "
            f"failed_tool_calls={failed_tool_calls}, "
            f"failed_write_tool_calls={failed_write_tool_calls}, "
            f"mutation_intent={str(mutation_intent).lower()}, "
            f"write_action_observed={str(write_action_observed).lower()}, "
            f"target_diff_changed={str(diff_changed).lower()}, "
            f"target_has_diff={str(has_after_diff).lower()}, "
            f"changed_files={','.join(diff_guard['changed_files']) or '-'}, "
            f"diff_added_lines={diff_guard['added_lines']}, "
            f"diff_deleted_lines={diff_guard['deleted_lines']}, "
            f"diff_guard_status={diff_guard['status']}, "
            f"diff_guard_reasons={';'.join(diff_guard['reasons']) or '-'}, "
            f"py_compile_status={py_compile_guard['status']}, "
            f"py_compile_files={','.join(py_compile_guard['files']) or '-'}, "
            f"py_compile_reasons={';'.join(py_compile_guard['reasons']) or '-'}, "
            f"verifier_reasons={';'.join(verifier_reasons) or '-'}, "
            f"destructive_rewrite_detected="
            f"{str(diff_guard['destructive_rewrite_detected']).lower()}, "
            f"requested_paths_observed="
            f"{str(diff_guard['requested_paths_observed']).lower()}"
        )
        print(plan.summary())
        if verifier_failed:
            try:
                plan.save_as_markdown(plan.file_path, session_id=session.id)  # type: ignore[arg-type]
            except Exception:
                pass

        # Save session and generate journal
        events.session_end(telemetry.to_dict())
        _save_session(session, telemetry, events, workspace_root)

        # Auto-journal
        _generate_journal(session, goal, agent.stop_reason if hasattr(agent, 'stop_reason') else "completed",
                          telemetry, events, manifest, workspace_root, plan)

        # Auto-tag if configured
        _auto_tag_if_configured(cfg, workspace_root)

        print(f"\n{telemetry.summary()}")
        print(f"Event log: {events.log_path}")

        # Post-run follow-up menu
        if not no_follow_up:
            return _post_run_menu(
                agent=agent, session=session, telemetry=telemetry, events=events,
                workspace_root=workspace_root, manifest=manifest, plan=plan,
                continue_budget=continue_budget, goal=goal,
            )

        return 0 if not plan.has_failures else 1

    if goal:
        # Single-shot mode
        events.session_start(mode=mode, goal=goal, ecosystem=session.ecosystem)

        model_info = primary_llm.model_name()
        if model_router:
            names = model_router.tier_model_names()
            model_info = " / ".join(f"{t}={n}" for t, n in names.items())
        print(f"[agent] Mode: {mode.upper()} | Models: {model_info} | Session: {session.id}")
        print(f"[agent] Goal: {goal}\n")

        response = agent.run(goal)
        print()
        for line in response.splitlines():
            print(f"[chat] {line}")
        print()

        # Save session and journal
        events.session_end(telemetry.to_dict())
        _save_session(session, telemetry, events, workspace_root)
        _generate_journal(session, goal, agent.stop_reason, telemetry, events, manifest, workspace_root)

        # Auto-tag if configured
        _auto_tag_if_configured(cfg, workspace_root)

        print(f"\n{telemetry.summary()}")
        print(f"Event log: {events.log_path}")

        exit_code = _agent_stop_reason_exit_code(agent.stop_reason)

        # Post-run follow-up menu
        if not no_follow_up:
            if exit_code:
                return exit_code
            return _post_run_menu(
                agent=agent, session=session, telemetry=telemetry, events=events,
                workspace_root=workspace_root, manifest=manifest,
                continue_budget=continue_budget, goal=goal,
            )
        return exit_code
    else:
        # Interactive chat shell (plan-execute by default)
        from ..orchestrator.planner import TaskPlanner, ExecutionPlan
        from ..orchestrator.executor import SubtaskExecutor

        planner_model_id = _default_planner_model(
            provider,
            planner_model,
            profile_default_model if not explicit_provider else None,
        )
        planner_llm = _make_client(planner_model_id)

        codebase_context = context_builder.build(mode="plan")
        guardrail_info_parts = []
        if no_touch:
            guardrail_info_parts.append(f"no_touch_paths: {', '.join(no_touch)}")
        if readonly_repos:
            guardrail_info_parts.append(f"readonly repos: {', '.join(readonly_repos.keys())}")
        guardrail_info = "\n".join(guardrail_info_parts) if guardrail_info_parts else None

        return _run_interactive_shell(
            agent=agent,
            planner_llm=planner_llm,
            planner_model_id=planner_model_id,
            runner_factory=runner_factory,
            session=session,
            telemetry=telemetry,
            events=events,
            guardrails=guardrails,
            workspace_root=workspace_root,
            manifest=manifest,
            codebase_context=codebase_context,
            guardrail_info=guardrail_info,
            verbose=verbose,
            continue_budget=continue_budget,
        )

    return 0


# ---- Helper functions ----


def _save_session(session, telemetry, events, workspace_root: Path, session_subdir: str = "runs") -> Path:
    """Save session state for resume capability.

    Saves:
    - messages.jsonl: conversation history (append-only for crash safety)
    - telemetry.json: cumulative metrics

    session_subdir: subdir under app-data runs (default "runs"). Use "sessions" for UI conversation persistence.
    """
    session_dir = _agent_runs_session_dir(session.id, session_subdir=session_subdir)
    session_dir.mkdir(parents=True, exist_ok=True)

    # Save messages as JSONL (each line is one message)
    messages_path = session_dir / "messages.jsonl"
    with open(messages_path, "w", encoding="utf-8") as f:
        for msg in session.get_messages_for_api():
            f.write(json.dumps(msg, default=str) + "\n")

    # Save telemetry
    telemetry_path = session_dir / "telemetry.json"
    telemetry_path.write_text(
        json.dumps(telemetry.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )

    return session_dir


def _load_session_messages(session, messages_path: Path) -> None:
    """Load conversation history from a saved session."""
    with open(messages_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                session.add_user_message(content)
            elif role == "assistant":
                session.add_assistant_message(content=content)


def _load_telemetry(telemetry, telemetry_path: Path) -> None:
    """Restore telemetry state from a saved session."""
    data = json.loads(telemetry_path.read_text(encoding="utf-8"))
    # We can't fully reconstruct CallMetrics, but we can set the cost baseline
    # so extend_budget works correctly
    if "total_cost" in data:
        telemetry._restored_cost = data["total_cost"]


def _generate_journal(session, goal, stop_reason, telemetry, events, manifest, workspace_root, plan=None):
    """Generate auto-journal entry after a run."""
    try:
        from ..orchestrator.journal import generate_entry
        eco_name = manifest.get("ecosystem", "default")
        journal_dir = _agent_journal_dir(manifest, workspace_root)
        entry_path = generate_entry(
            session_id=session.id,
            goal=goal,
            stop_reason=stop_reason,
            telemetry=telemetry,
            events=events,
            ecosystem=eco_name,
            output_dir=journal_dir,
            plan=plan,
            session=session,
        )
        print(f"Journal: {entry_path}")
    except Exception as e:
        sys.stderr.write(f"[agent] Journal generation failed: {e}\n")


def _auto_tag_if_configured(cfg: Dict[str, Any], workspace_root: Path) -> None:
    """Auto-tag a release if auto_tag_on_complete is configured.

    Reads the setting from agent config and tags as the specified pre-release stage.
    """
    auto_tag = cfg.get("auto_tag_on_complete", False)
    if not auto_tag or auto_tag is True:
        return  # False or True-without-stage: skip

    # auto_tag should be a string like "alpha", "beta", "rc"
    stage = str(auto_tag).lower()
    if stage not in ("alpha", "beta", "rc"):
        return

    try:
        from .release import release_from_agent
        tag = release_from_agent(workspace_root, bump="patch", pre=stage)
        if tag:
            print(f"[agent] Auto-tagged: {tag}")
    except Exception as e:
        sys.stderr.write(f"[agent] Auto-tag failed: {e}\n")


def _post_run_menu(
    agent=None, session=None, telemetry=None, events=None,
    workspace_root=None, manifest=None, plan=None,
    continue_budget=1.0, goal="",
) -> int:
    """Interactive post-run menu: continue, follow-up, review, merge, done.

    Returns exit code.
    """
    stop_reason = getattr(agent, "stop_reason", "completed") if agent else "completed"
    cost_str = f"${telemetry.total_cost:.2f}" if telemetry else "$0.00"

    print(f"\n--- Run complete ({stop_reason} | {cost_str} spent) ---")
    if plan and plan.file_path:
        completed = sum(1 for st in plan.subtasks if st.status == "completed")
        print(f"Plan:    {plan.file_path} ({completed}/{len(plan.subtasks)} tasks done)")
    print(f"Session: {_agent_runs_session_dir(session.id)}")
    print()

    while True:
        print("What next?")
        print(f"  [c] Continue with more budget (+${continue_budget:.2f})")
        print(f"  [f] Follow-up task (new goal, keeps context)")
        print(f"  [r] Review changes (git diff --stat)")
        print(f"  [t] Tag release (bump version, update docs, commit, tag, push)")
        print(f"  [m] Merge checkpoint to feature branch")
        print(f"  [q] Done")
        print()

        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0

        if choice in ("q", "done", ""):
            return 0

        elif choice in ("c", "continue"):
            if telemetry:
                telemetry.extend_budget(continue_budget)
                print(f"[agent] Budget extended by ${continue_budget:.2f} (new limit: ${telemetry.max_cost:.2f})")
            if agent:
                response = agent.run("Continue from where you left off. Check what was already done and continue with the next task.")
                print()
                for line in response.splitlines():
                    print(f"[chat] {line}")
                print()
                # Save after continue
                if session and workspace_root:
                    _save_session(session, telemetry, events, workspace_root)
                    _generate_journal(session, goal, agent.stop_reason, telemetry, events, manifest, workspace_root, plan)
                print(f"\n{telemetry.summary()}")

        elif choice in ("f", "follow-up"):
            try:
                new_goal = input("New goal: ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if not new_goal:
                continue
            if agent:
                response = agent.run(new_goal)
                print()
                for line in response.splitlines():
                    print(f"[chat] {line}")
                print()
                if session and workspace_root:
                    _save_session(session, telemetry, events, workspace_root)
                print(f"\n{telemetry.summary()}")

        elif choice in ("r", "review"):
            try:
                result = subprocess.run(
                    ["git", "diff", "--stat"],
                    capture_output=True, text=True, timeout=30,
                )
                print(f"\n{result.stdout}\n")
            except Exception as e:
                print(f"  Error: {e}")

        elif choice in ("m", "merge"):
            try:
                # Get current branch and try cherry-pick to feature branch
                current = subprocess.run(
                    ["git", "branch", "--show-current"],
                    capture_output=True, text=True, timeout=10,
                )
                current_branch = current.stdout.strip()
                print(f"  Current branch: {current_branch}")

                # Find the feature branch (strip -cp-... suffix)
                if "-cp-" in current_branch:
                    feature_branch = current_branch.rsplit("-cp-", 1)[0]
                    head_hash = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        capture_output=True, text=True, timeout=10,
                    ).stdout.strip()

                    print(f"  Cherry-picking {head_hash[:8]} to {feature_branch}...")
                    subprocess.run(["git", "checkout", feature_branch], capture_output=True, timeout=10)
                    result = subprocess.run(
                        ["git", "cherry-pick", "--no-commit", head_hash],
                        capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode == 0:
                        try:
                            msg = input("  Commit message: ").strip() or "feat: agent continuity features"
                        except (EOFError, KeyboardInterrupt):
                            msg = "feat: agent work"
                        subprocess.run(["git", "commit", "-m", msg], capture_output=True, timeout=30)
                        print(f"  Merged to {feature_branch}!")
                    else:
                        print(f"  Cherry-pick failed: {result.stderr}")
                        subprocess.run(["git", "cherry-pick", "--abort"], capture_output=True, timeout=10)
                        subprocess.run(["git", "checkout", current_branch], capture_output=True, timeout=10)
                else:
                    print("  Not on a checkpoint branch — nothing to merge.")
            except Exception as e:
                print(f"  Error: {e}")

        elif choice in ("t", "tag", "release"):
            try:
                from ..orchestrator import colors as c_mod
            except ImportError:
                c_mod = None
            _run_release_flow(c_mod, workspace_root or Path.cwd())

        else:
            print(f"  Unknown choice: {choice}")


# ── Release flow (used by interactive shell + post-run menu) ──


def _run_release_flow(c, workspace_root: Path) -> None:
    """Interactive release flow for the shell and post-run menu.

    Prompts user for bump type and pre-release stage, then runs the release.
    """
    from .release import cmd_release, _get_latest_tag, Version

    latest_tag = _get_latest_tag()
    if not latest_tag:
        print(c.error("  No existing tags found. Run: amof release patch --alpha"))
        return

    current = Version.parse(latest_tag)
    if not current:
        print(c.error(f"  Cannot parse tag '{latest_tag}'"))
        return

    # Show current version and options
    print()
    print(c.header("  Release"))
    print(f"  Current: {c.BOLD}{current.tag}{c.RESET}")
    print()

    # Build options based on current version
    options = []
    if current.pre:
        # Can increment pre, promote to next stage, or promote to stable
        next_pre = current.next_pre()
        options.append(("1", f"{next_pre.tag}", "patch", current.pre, None))

        stage_idx = ["alpha", "beta", "rc"].index(current.pre) if current.pre in ["alpha", "beta", "rc"] else -1
        if stage_idx < 2:
            next_stage = ["alpha", "beta", "rc"][stage_idx + 1]
            promoted = current.promote(next_stage)
            options.append(("2", f"{promoted.tag}", "promote", None, next_stage))

        stable = current.promote()
        options.append(("3", f"{stable.tag}", "promote", None, None))
    else:
        # Stable: offer patch/minor/major with alpha
        options.append(("1", f"{current.bump('patch', 'alpha').tag}", "patch", "alpha", None))
        options.append(("2", f"{current.bump('patch').tag}", "patch", None, None))
        options.append(("3", f"{current.bump('minor', 'alpha').tag}", "minor", "alpha", None))
        options.append(("4", f"{current.bump('major', 'alpha').tag}", "major", "alpha", None))

    for num, tag, _, _, _ in options:
        print(f"  [{num}] {tag}")
    print(f"  [c] Cancel")
    print()

    try:
        choice = input(f"  {c.USER}>{c.RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if choice.lower() in ("c", "cancel", ""):
        return

    # Find matching option
    selected = None
    for num, tag, bump, pre, promote_target in options:
        if choice == num:
            selected = (bump, pre, promote_target)
            break

    if not selected:
        print(c.error(f"  Unknown option: {choice}"))
        return

    bump, pre, promote_target = selected
    rc = cmd_release(
        bump=bump,
        pre=pre,
        promote_target=promote_target,
        push=True,
        dry_run=False,
        yes=True,
    )
    if rc != 0:
        print(c.error("  Release failed."))


# ── Interactive shell ──────────────────────────────────────────


def _run_interactive_shell(
    agent,
    planner_llm,
    planner_model_id: str,
    runner_factory: Any,
    session,
    telemetry,
    events,
    guardrails,
    workspace_root: Path,
    manifest: Dict[str, Any],
    codebase_context: str,
    guardrail_info: Optional[str],
    verbose: bool,
    continue_budget: float = 1.0,
) -> int:
    """Run interactive chat shell with two modes: execute (default) and plan.

    Execute mode (default):
        Type a task -> agent runs it directly. Continuous conversation.
        The master agent can delegate to specialized runners via the Delegate tool.

    Plan mode (/plan):
        Can be entered anytime. Uses current conversation context.
        Creates a plan file -> user refines -> agent enhances -> user approves.
        After approval, master starts fresh with the confirmed plan.

    Returns exit code.
    """
    from ..orchestrator.planner import TaskPlanner, ExecutionPlan
    from ..orchestrator.executor import SubtaskExecutor
    from ..orchestrator import colors as c

    eco = manifest.get("ecosystem", "")
    events.session_start(mode="interactive", goal="interactive-shell", ecosystem=eco)

    # Banner
    print()
    print(c.header(f"  AMOF Agent Shell ({eco})"))
    print()
    model_info = agent.llm.model_name()
    if agent.model_router:
        names = agent.model_router.tier_model_names()
        model_info = " / ".join(f"{t}={n}" for t, n in names.items())
    print(c.info(f"  Models:  {model_info}"))
    print(c.info(f"  Planner: {planner_model_id}"))
    print(c.info(f"  Session: {session.id}"))
    # Show runners if available
    delegate_tool = agent.tools.get("Delegate")
    if delegate_tool and hasattr(delegate_tool, '_factory'):
        runner_names = delegate_tool._factory.runner_names
        if runner_names:
            print(c.info(f"  Runners: {', '.join(runner_names)}"))
    print()
    print(c.info("  Mode: execute (default) — type a task, agent runs it."))
    print(c.info("  /plan <task>       enter plan mode (uses conversation context)"))
    print(c.info("  /checkpoints       show git checkpoints"))
    print(c.info("  /status            cost & telemetry"))
    print(c.info("  /review            git diff --stat"))
    print(c.info("  /release           tag a release"))
    print(c.info("  /help              all commands"))
    print(c.info("  Ctrl+C cancel      Ctrl+D exit"))
    print()

    planner = TaskPlanner(
        planner_llm=planner_llm,
        workspace_root=workspace_root,
    )

    plans_dir = _agent_plans_dir(manifest, workspace_root)

    try:
        while True:
            # Prompt
            try:
                line = input(f"{c.USER}>>> {c.RESET}").strip()
            except EOFError:
                print()
                break

            if not line:
                continue
            if line.lower() in ("exit", "quit", "q"):
                break

            # Multi-line: trailing backslash
            while line.endswith("\\"):
                line = line[:-1]
                try:
                    cont = input(f"{c.USER}... {c.RESET}")
                except EOFError:
                    break
                line = line + "\n" + cont
            line = line.strip()
            if not line:
                continue
            if line.lower() in ("exit", "quit", "q"):
                break

            # ── Slash commands ───────────────────────────────
            if line.startswith("/"):
                parts = line.split(None, 1)
                cmd = parts[0].lower()

                if cmd in ("/quit", "/exit", "/q"):
                    break

                elif cmd in ("/status", "/cost"):
                    print(f"\n{c.info(telemetry.summary())}\n")
                    continue

                elif cmd == "/help":
                    print()
                    print(c.header("  Modes"))
                    print("  execute (default)  Type a task, agent runs it. Continuous conversation.")
                    print("  plan (/plan)       Plan mode. Create, refine, and execute structured plans.")
                    print()
                    print(c.header("  Commands"))
                    print(f"  {c.USER}/plan <task>{c.RESET}       Enter plan mode (uses conversation context if available)")
                    print(f"  {c.USER}/p <task>{c.RESET}          Same as /plan")
                    print(f"  {c.USER}/checkpoints{c.RESET}       List git checkpoints on helper branch")
                    print(f"  {c.USER}/restore <hash>{c.RESET}    Restore to a checkpoint")
                    print(f"  {c.USER}/status{c.RESET}            Show session telemetry & cost")
                    print(f"  {c.USER}/review{c.RESET}            Show git diff --stat")
                    print(f"  {c.USER}/release{c.RESET}           Tag a release")
                    print(f"  {c.USER}/quit{c.RESET}              Exit the shell")
                    print()
                    print(c.header("  Tips"))
                    print("  - All inputs in execute mode are part of one conversation (follow-ups).")
                    print("  - /plan can be used anytime — it passes conversation context to the planner.")
                    print("  - After plan approval, master starts fresh with only the confirmed plan.")
                    print("  - End a line with \\ to continue on the next line.")
                    print("  - Ctrl+C cancels current run. Ctrl+D exits (writes journal).")
                    print()
                    continue

                elif cmd == "/review":
                    try:
                        result = subprocess.run(
                            ["git", "diff", "--stat"],
                            capture_output=True, text=True, timeout=30,
                        )
                        print(f"\n{result.stdout}\n")
                    except Exception as e:
                        print(c.error(f"  Error: {e}"))
                    continue

                elif cmd == "/release":
                    _run_release_flow(c, workspace_root)
                    continue

                elif cmd == "/checkpoints":
                    _show_checkpoints(agent, c)
                    continue

                elif cmd == "/restore":
                    commit_hash = parts[1].strip() if len(parts) > 1 else ""
                    if not commit_hash:
                        print(c.error("  Usage: /restore <commit-hash>"))
                        continue
                    _restore_checkpoint(agent, commit_hash, c)
                    continue

                elif cmd in ("/plan", "/p"):
                    task = parts[1] if len(parts) > 1 else ""
                    if not task:
                        print(c.error("  Usage: /plan <task description>"))
                        continue

                    # ── Plan mode: pass conversation context to planner ──
                    # If we have conversation history, summarize it for the planner
                    conversation_context = ""
                    if session.messages and len(session.messages) > 0:
                        conversation_context = _summarize_conversation_for_planner(
                            session, c
                        )

                    # Enter plan-edit-refine-execute flow
                    _run_plan_flow(
                        task=task,
                        planner=planner,
                        planner_model_id=planner_model_id,
                        agent=agent,
                        runner_factory=runner_factory,
                        guardrails=guardrails,
                        session=session,
                        telemetry=telemetry,
                        events=events,
                        workspace_root=workspace_root,
                        plans_dir=plans_dir,
                        codebase_context=codebase_context,
                        guardrail_info=guardrail_info,
                        verbose=verbose,
                        c=c,
                        conversation_context=conversation_context,
                    )
                    continue

                else:
                    print(c.error(f"  Unknown command: {cmd}. Type /help"))
                    continue

            # ── Execute mode (default): quick execution ──────
            # Run task directly. All inputs are part of one conversation.

            # Auto-extend budget if exhausted — user sending a message means "keep going"
            if telemetry.cost_exceeded:
                old_limit = telemetry.max_cost or 0
                extend_amount = max(continue_budget, 1.0)
                telemetry.extend_budget(extend_amount)
                print(c.info(
                    f"  [Budget was exhausted (${old_limit:.2f}). "
                    f"Auto-extended by ${extend_amount:.2f} → new limit ${telemetry.max_cost:.2f}]"
                ))

            print(f"\n{c.action('  Running...')}\n")
            try:
                response = agent.run(line)
                print(f"\n{c.agent(response)}\n")
            except KeyboardInterrupt:
                print(f"\n{c.info('  (cancelled)')}\n")
            cost = f"${telemetry.total_cost:.2f}"
            print(c.info(f"  [{cost} spent]"))
            print()

            # Save session periodically
            _save_session(session, telemetry, events, workspace_root)

    except KeyboardInterrupt:
        print()

    # Session end
    events.session_end(telemetry.to_dict())
    _save_session(session, telemetry, events, workspace_root)
    # Use the session goal if available, otherwise derive from first user message
    journal_goal = session.goal or "interactive-shell"
    if journal_goal == "interactive-shell" and session.messages:
        first_user = next((m.content for m in session.messages if m.role == "user"), None)
        if first_user:
            journal_goal = first_user[:120]
    _generate_journal(session, journal_goal, "completed", telemetry, events, manifest, workspace_root)

    print(f"\n{c.info(telemetry.summary())}")
    print(c.info(f"Event log: {events.log_path}"))
    # Show session ID for easy resume
    print(c.info(f"Session: {session.id}"))
    print(c.info(f"  To resume: amof agent --resume {session.id}"))
    return 0


def _show_checkpoints(agent, c) -> None:
    """Show git checkpoints from the helper branch."""
    checkpoint_tool = agent.tools.get("GitCheckpoint")
    if checkpoint_tool is None:
        print(c.error("  GitCheckpoint tool not available."))
        return

    result = checkpoint_tool.execute(action="list")
    if result.success:
        print(f"\n{result.output}\n")
    else:
        print(c.error(f"  {result.error or 'No checkpoints found.'}"))


def _restore_checkpoint(agent, commit_hash: str, c) -> None:
    """Restore to a git checkpoint."""
    checkpoint_tool = agent.tools.get("GitCheckpoint")
    if checkpoint_tool is None:
        print(c.error("  GitCheckpoint tool not available."))
        return

    result = checkpoint_tool.execute(action="restore", commit_hash=commit_hash)
    if result.success:
        print(f"\n{c.success(result.output)}\n")
    else:
        print(c.error(f"  {result.error}"))


def _summarize_conversation_for_planner(session, c) -> str:
    """Build a conversation summary to pass as context to the planner.

    Extracts key user messages and assistant responses (no tool output)
    to give the planner awareness of what has been discussed.
    """
    parts = []
    for msg in session.messages:
        if msg.role == "user" and msg.content:
            parts.append(f"User: {msg.content[:500]}")
        elif msg.role == "assistant" and msg.content:
            parts.append(f"Agent: {msg.content[:300]}")

    if not parts:
        return ""

    # Limit to last 10 exchanges to keep it manageable
    if len(parts) > 20:
        parts = parts[-20:]

    summary = "\n\n".join(parts)
    print(c.info(f"  (Including {len(parts)} conversation turns as context for planner)"))
    return f"\n\n## Conversation Context (from current session)\n{summary}"


def _run_plan_flow(
    task: str,
    planner,
    planner_model_id: str,
    agent,
    runner_factory: Any,
    guardrails,
    session,
    telemetry,
    events,
    workspace_root: Path,
    plans_dir: Path,
    codebase_context: str,
    guardrail_info: Optional[str],
    verbose: bool,
    c,
    conversation_context: str = "",
) -> None:
    """Plan-edit-refine-execute workflow.

    Flow:
    1. Agent creates a boilerplate plan file with task outline
    2. User opens the file in their editor and edits it
    3. User presses Enter → agent reads the file, refines it with the LLM
    4. Agent overwrites the file with a detailed plan (analysis, tasks, risks)
    5. User reviews: [a]pprove / [e]dit / [r]eject
       - If [e]dit: user edits the file again → goto step 3
       - If [a]pprove: master starts FRESH with confirmed plan → executes
       - If [r]eject: return to shell

    If conversation_context is provided (from mid-session /plan), it's included
    in the planner's input so it has awareness of what was discussed.
    """
    from ..orchestrator.planner import ExecutionPlan
    from ..orchestrator.executor import SubtaskExecutor

    # ── Step 1: Create boilerplate plan file ─────────────────
    slug = "-".join(task.lower().split()[:6])
    slug = "".join(ch for ch in slug if ch.isalnum() or ch == "-")[:50] or "plan"
    plan_path = plans_dir / f"{time.strftime('%Y-%m-%d')}-{slug}.md"
    plans_dir.mkdir(parents=True, exist_ok=True)

    # Avoid collision
    counter = 1
    while plan_path.exists():
        counter += 1
        plan_path = plans_dir / f"{time.strftime('%Y-%m-%d')}-{slug}-{counter}.md"

    boilerplate = _create_plan_boilerplate(task, session.id, plan_path)
    plan_path.write_text(boilerplate, encoding="utf-8")

    print(f"\n{c.plan('  Plan file created:')}")
    print(c.info(f"  {plan_path}"))
    print()
    print(c.plan("  Edit this file in your editor to describe what you want."))
    print(c.plan("  Add goals, constraints, files to change, anything relevant."))
    print(c.plan("  When done, come back here and press Enter."))
    print()

    # ── Step 2-4: Edit-refine loop ───────────────────────────
    plan = None
    while True:
        try:
            input(f"  {c.USER}Press Enter when ready (or 'q' to cancel): {c.RESET}")
        except (EOFError, KeyboardInterrupt):
            print(c.info("\n  Cancelled."))
            return

        # Check if user typed 'q'
        # (input() already consumed the line, but we can check the file)
        # Re-read the user's edits
        try:
            user_content = plan_path.read_text(encoding="utf-8")
        except Exception as e:
            print(c.error(f"  Could not read {plan_path}: {e}"))
            continue

        if not user_content.strip():
            print(c.error("  Plan file is empty. Please add your task description."))
            continue

        # ── Step 3: Agent refines the plan with LLM ──────────
        print(f"\n{c.plan(f'  Refining plan with {planner_model_id}...')}")

        plan = None
        # Extract the user's intent from the file
        # Pass original task + user's edits + conversation context (if mid-session)
        enriched_task = f"{task}\n\n---\nUser's plan notes:\n{user_content}"
        if conversation_context:
            enriched_task = f"{enriched_task}\n{conversation_context}"

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                plan = planner.plan(
                    task=enriched_task,
                    codebase_context=codebase_context,
                    guardrail_info=guardrail_info,
                )
                break
            except KeyboardInterrupt:
                print(f"\n{c.info('  (planning cancelled)')}")
                return
            except Exception as exc:
                if attempt < max_retries:
                    print(c.error(f"  Attempt {attempt}/{max_retries} failed: {exc}"))
                else:
                    print(c.error(f"  Planning failed: {exc}"))

        if plan is None:
            print(c.error("  Could not generate plan. Edit the file and try again."))
            continue

        # Display thinking if available
        thinking_text = planner.last_thinking
        if thinking_text:
            print(f"\n{c.info('  Thinking:')}")
            thinking_lines = thinking_text.strip().splitlines()
            for tl in thinking_lines[:20]:
                print(c.thinking(f"  {tl}"))
            if len(thinking_lines) > 20:
                print(c.info(f"  ... ({len(thinking_lines) - 20} more lines)"))

        # Record planning cost
        if plan.planning_cost > 0:
            from ..orchestrator.llm.base import Usage
            planner_usage = Usage(
                model=plan.planner_model,
                prompt_tokens=0, completion_tokens=0,
                latency_ms=plan.planning_latency_ms,
                estimated_cost=plan.planning_cost,
            )
            telemetry.record_from_usage(planner_usage, tier="strong")

        # Handle planner questions
        if plan.questions:
            print(f"\n{c.question('  The planner needs clarification:')}\n")
            for i, q in enumerate(plan.questions, 1):
                print(c.question(f"  {i}. {q}"))
            print()
            try:
                answers = input(f"{c.USER}  Your answer: {c.RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if answers and answers.lower() != "skip":
                enriched_task = f"{enriched_task}\n\nUser answers:\n{answers}"
                try:
                    plan = planner.plan(
                        task=enriched_task,
                        codebase_context=codebase_context,
                        guardrail_info=guardrail_info,
                    )
                except Exception as exc:
                    print(c.error(f"  Re-planning failed: {exc}"))
                    continue
                if plan is None:
                    continue

        # ── Step 4: Overwrite the file with the detailed plan ─
        plan.save_as_markdown(plan_path, session_id=session.id)

        # Show summary in shell
        print(f"\n{c.header(f'  Plan ({len(plan.subtasks)} tasks, ${plan.planning_cost:.4f})')}\n")
        if plan.analysis:
            for ln in plan.analysis.splitlines()[:5]:
                print(c.plan(f"  {ln}"))
            if len(plan.analysis.splitlines()) > 5:
                print(c.info("  ..."))
            print()

        for st in plan.subtasks:
            marker = {"pending": " ", "completed": "x"}.get(st.status, " ")
            tier_color = {
                "fast": c.GREEN, "standard": c.YELLOW, "strong": c.RED,
            }.get(st.model_tier, "")
            print(f"  [{marker}] {st.id}. {c.BOLD}{st.title}{c.RESET}  {tier_color}{st.model_tier}{c.RESET}")
        if plan.risks:
            print(f"\n{c.YELLOW}  Risks: {', '.join(plan.risks)}{c.RESET}")
        print(f"\n{c.info(f'  Plan updated: {plan_path}')}")

        # ── Step 5: Approve / Edit / Reject ──────────────────
        print()
        try:
            choice = input(
                f"  {c.USER}[a]{c.RESET}pprove  "
                f"{c.USER}[e]{c.RESET}dit  "
                f"{c.USER}[r]{c.RESET}eject  > "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice in ("r", "reject"):
            print(c.info("  Plan rejected."))
            return

        if choice in ("e", "edit"):
            print(c.plan("  Edit the plan file, then come back and press Enter."))
            continue  # back to "Press Enter when ready"

        # ── Approve (default) → Execute ──────────────────────
        # Re-read in case user made last-second edits
        try:
            plan = ExecutionPlan.load_from_markdown(plan_path)
        except Exception:
            pass  # use the in-memory plan

        break  # exit the edit-refine loop

    if plan is None:
        return

    # ── Phase 2: Execute the plan ────────────────────────────
    print(f"\n{c.header('  Executing...')}\n")

    executor = SubtaskExecutor(
        runner_factory=runner_factory,
        parent_telemetry=telemetry,
        verbose=verbose,
    )

    try:
        plan = executor.execute_plan(plan)
    except KeyboardInterrupt:
        print(f"\n{c.info('  (execution interrupted)')}")

    # Update the plan file with final checkbox state
    try:
        plan.save_as_markdown(plan_path, session_id=session.id)
    except Exception:
        pass

    # Summary
    completed = sum(1 for st in plan.subtasks if st.status == "completed")
    total = len(plan.subtasks)
    failed = sum(1 for st in plan.subtasks if st.status == "failed")

    print()
    if failed == 0 and completed == total:
        print(c.success(f"  Done: {completed}/{total} tasks completed ✓"))
    elif failed > 0:
        print(c.error(f"  Done: {completed}/{total} tasks, {failed} failed"))
    else:
        print(c.plan(f"  Done: {completed}/{total} tasks completed"))

    cost = f"${telemetry.total_cost:.2f}"
    print(c.info(f"  [{cost} spent]"))
    print(c.info(f"  Plan: {plan_path}"))
    print()

    # Save session
    _save_session(session, telemetry, events, workspace_root)


def _create_plan_boilerplate(task: str, session_id: str, plan_path: Path) -> str:
    """Create a boilerplate plan file for the user to edit.

    The file contains the task description and sections for the user to fill in.
    The agent will read this file, enhance it with a detailed plan, and
    overwrite it with the full plan.
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return f"""# {task[:120]}

**Created**: {now}
**Session**: {session_id}
**Status**: draft

## Goal

{task}

## Context / Constraints

<!-- Add any context the agent should know:
     - Which files/services are involved?
     - Any constraints or requirements?
     - What should NOT be changed?
     - Preferred approach?
-->



## Expected Outcome

<!-- What does "done" look like?
     - What should work after execution?
     - Any specific tests to verify?
-->



---

## Tasks

<!-- The agent will replace this section with a detailed task list.
     You can pre-fill tasks here if you have specific steps in mind.
     Format: - [ ] 1. **Task title** (model_tier)
-->

- [ ] 1. **TBD** (standard)

"""
