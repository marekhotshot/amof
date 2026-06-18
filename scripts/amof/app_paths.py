"""Resolve AMOF app-data paths independently from source workspaces."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


APP_NAME = "amof"


def _normalized(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _xdg_base(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name)
    if value and value.strip():
        return _normalized(value)
    return _normalized(fallback)


def _looks_like_operator_workspace_root(path: Path) -> bool:
    return (
        (path / "compat" / "public-private.lock.yaml").is_file()
        and (path / "repos").is_dir()
    )


@dataclass(frozen=True)
class AppPaths:
    config_root: Path
    data_root: Path
    cache_root: Path
    state_root: Path

    def as_dict(self) -> dict[str, Path]:
        return {
            "config_root": self.config_root,
            "data_root": self.data_root,
            "cache_root": self.cache_root,
            "state_root": self.state_root,
        }


def get_app_paths() -> AppPaths:
    """Return resolved AMOF app-data roots using AMOF_HOME or XDG defaults."""
    amof_home = os.environ.get("AMOF_HOME")
    if amof_home and amof_home.strip():
        root = _normalized(amof_home)
        return AppPaths(
            config_root=root / "config",
            data_root=root / "share",
            cache_root=root / "cache",
            state_root=root / "state",
        )

    home = Path.home()
    config_base = _xdg_base("AMOF_CONFIG_HOME", _xdg_base("XDG_CONFIG_HOME", home / ".config"))
    data_base = _xdg_base("AMOF_DATA_HOME", _xdg_base("XDG_DATA_HOME", home / ".local" / "share"))
    cache_base = _xdg_base("AMOF_CACHE_HOME", _xdg_base("XDG_CACHE_HOME", home / ".cache"))
    state_base = _xdg_base("AMOF_STATE_HOME", _xdg_base("XDG_STATE_HOME", home / ".local" / "state"))
    return AppPaths(
        config_root=config_base / APP_NAME,
        data_root=data_base / APP_NAME,
        cache_root=cache_base / APP_NAME,
        state_root=state_base / APP_NAME,
    )


def operator_workspace_root(base: str | Path | None = None) -> Path | None:
    """Return the operator workspace root when it can be detected safely."""
    explicit = os.environ.get("AMOF_OPERATOR_WORKSPACE_ROOT")
    if explicit and explicit.strip():
        return _normalized(explicit)
    start = _normalized(
        base
        if base is not None
        else os.environ.get("AMOF_WORKSPACE_ROOT")
        or os.environ.get("AMOF_CWD")
        or Path.cwd()
    )
    for candidate in (start, *start.parents):
        if _looks_like_operator_workspace_root(candidate):
            return candidate
    return None


def operator_receipts_root(base: str | Path | None = None) -> Path | None:
    """Return the preferred operator receipts root when configured or detectable."""
    explicit = os.environ.get("AMOF_RECEIPTS_ROOT")
    if explicit and explicit.strip():
        return _normalized(explicit)
    root = operator_workspace_root(base)
    if root is None:
        return None
    return root / "receipts"


def ensure_app_roots() -> AppPaths:
    """Create the top-level AMOF app-data roots when they do not exist."""
    paths = get_app_paths()
    for root in paths.as_dict().values():
        root.mkdir(parents=True, exist_ok=True)
    return paths


def config_file() -> Path:
    return get_app_paths().config_root / "config.yaml"


def contexts_file() -> Path:
    return get_app_paths().config_root / "contexts.yaml"


def workspaces_registry_file() -> Path:
    return get_app_paths().config_root / "workspaces.yaml"


def workspace_state_file() -> Path:
    return get_app_paths().config_root / "state.json"


def kubeconfigs_dir() -> Path:
    return get_app_paths().config_root / "kubeconfigs"


def provider_profiles_dir() -> Path:
    return get_app_paths().config_root / "provider-profiles"


def runs_dir() -> Path:
    return get_app_paths().data_root / "runs"


def studio_dir() -> Path:
    return get_app_paths().data_root / "studio"


def director_prepare_runs_dir() -> Path:
    return get_app_paths().data_root / "evidence" / "prepare-runs"


def director_run_local_dir() -> Path:
    return get_app_paths().data_root / "evidence" / "run-local"


def evidence_dir() -> Path:
    return get_app_paths().data_root / "evidence"


def workspaces_dir() -> Path:
    return get_app_paths().data_root / "workspaces"


def materialized_runs_dir() -> Path:
    return workspaces_dir() / "materialized-runs"


def ticket_worktrees_dir() -> Path:
    return workspaces_dir() / "ticket-worktrees"


def planning_workspaces_dir() -> Path:
    return workspaces_dir() / "planning"


def receipts_dir() -> Path:
    return get_app_paths().data_root / "receipts"


def templates_dir() -> Path:
    return get_app_paths().data_root / "templates"


def indexes_dir() -> Path:
    return get_app_paths().cache_root / "indexes"


def downloads_dir() -> Path:
    return get_app_paths().cache_root / "downloads"


def vector_store_dir() -> Path:
    return get_app_paths().cache_root / "vector_store"


def tmp_dir() -> Path:
    return get_app_paths().cache_root / "tmp"


def logs_dir() -> Path:
    return get_app_paths().state_root / "logs"


def locks_dir() -> Path:
    return get_app_paths().state_root / "locks"


def queue_dir() -> Path:
    return get_app_paths().state_root / "queue"


def ensure_parent_dir(path: str | Path) -> Path:
    """Ensure the parent directory exists and return the normalized path."""
    normalized = _normalized(path)
    normalized.parent.mkdir(parents=True, exist_ok=True)
    return normalized


__all__ = [
    "APP_NAME",
    "AppPaths",
    "config_file",
    "contexts_file",
    "director_prepare_runs_dir",
    "director_run_local_dir",
    "downloads_dir",
    "ensure_app_roots",
    "ensure_parent_dir",
    "evidence_dir",
    "get_app_paths",
    "indexes_dir",
    "kubeconfigs_dir",
    "locks_dir",
    "logs_dir",
    "materialized_runs_dir",
    "operator_receipts_root",
    "operator_workspace_root",
    "planning_workspaces_dir",
    "provider_profiles_dir",
    "queue_dir",
    "receipts_dir",
    "runs_dir",
    "studio_dir",
    "templates_dir",
    "ticket_worktrees_dir",
    "tmp_dir",
    "vector_store_dir",
    "workspace_state_file",
    "workspaces_dir",
    "workspaces_registry_file",
]
