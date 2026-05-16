"""Read/write agent config (.amof/agent.yaml) for control plane settings."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from amof.api.command_builder import get_workspace_root


def _parse_bool(value: str) -> Optional[bool]:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def resolve_agent_dry_run(cfg: Dict[str, Any]) -> tuple[bool, str]:
    """Resolve effective agent dry-run mode and its source.

    Precedence:
    1. AMOF_AGENT_DRY_RUN env var (operator/runtime override)
    2. .amof/agent.yaml dry_run key
    3. Safe runtime default (False)
    """
    env_value = os.environ.get("AMOF_AGENT_DRY_RUN")
    if env_value is not None:
        parsed = _parse_bool(env_value)
        if parsed is not None:
            return parsed, "env:AMOF_AGENT_DRY_RUN"
    cfg_value = cfg.get("dry_run")
    if isinstance(cfg_value, bool):
        return cfg_value, "config:dry_run"
    return False, "default:false"


def get_agent_config_path() -> Path:
    return get_workspace_root() / ".amof" / "agent.yaml"


def get_agent_config() -> Dict[str, Any]:
    path = get_agent_config_path()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def update_agent_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    path = get_agent_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = get_agent_config()
    deep_merge(current, updates)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(current, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return current


def deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> None:
    for k, v in updates.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
