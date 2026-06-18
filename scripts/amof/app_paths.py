"""Resolve AMOF app-data paths independently from source workspaces."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


APP_NAME = "amof"
CANONICAL_REPO_WRITE_FORBIDDEN = "CANONICAL_REPO_WRITE_FORBIDDEN"
CANONICAL_REPO_MAINTENANCE_ENV = "AMOF_CANONICAL_REPO_MAINTENANCE"
CANONICAL_PATH_CLASSIFICATIONS = {
    "canonical_public_repo",
    "canonical_private_repo",
}


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


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class OperatorPathInfo:
    path: Path
    workspace_root: Path | None
    classification: str
    canonical_public_repo_root: Path | None
    canonical_private_repo_root: Path | None

    @property
    def is_canonical_repo(self) -> bool:
        return self.classification in CANONICAL_PATH_CLASSIFICATIONS


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
    ensure_canonical_repo_write_allowed(
        operation="create parent directory",
        target_path=normalized,
    )
    normalized.parent.mkdir(parents=True, exist_ok=True)
    return normalized


def classify_operator_path(path: str | Path, base: str | Path | None = None) -> OperatorPathInfo:
    normalized = _normalized(path)
    workspace_root = operator_workspace_root(base if base is not None else normalized)
    canonical_public_repo_root: Path | None = None
    canonical_private_repo_root: Path | None = None
    classification = "unknown"
    if workspace_root is not None:
        canonical_public_repo_root = (workspace_root / "repos" / "amof").resolve(strict=False)
        canonical_private_repo_root = (workspace_root / "repos" / "amof-private").resolve(strict=False)
        if _is_relative_to(normalized, canonical_public_repo_root):
            classification = "canonical_public_repo"
        elif _is_relative_to(normalized, canonical_private_repo_root):
            classification = "canonical_private_repo"
        elif _is_relative_to(normalized, workspace_root / "worktrees" / "public"):
            classification = "public_ticket_worktree"
        elif _is_relative_to(normalized, workspace_root / "worktrees" / "private"):
            classification = "private_ticket_worktree"
        elif _is_relative_to(normalized, workspace_root / "receipts"):
            classification = "operator_receipts"
    if classification == "unknown":
        app_paths = get_app_paths()
        if any(
            _is_relative_to(normalized, root)
            for root in (
                app_paths.config_root,
                app_paths.data_root,
                app_paths.cache_root,
                app_paths.state_root,
            )
        ):
            classification = "app_data"
    return OperatorPathInfo(
        path=normalized,
        workspace_root=workspace_root,
        classification=classification,
        canonical_public_repo_root=canonical_public_repo_root,
        canonical_private_repo_root=canonical_private_repo_root,
    )


def canonical_repo_write_hint(base: str | Path | None = None) -> str:
    workspace_root = operator_workspace_root(base)
    if workspace_root is None:
        return "Use a ticket worktree under ./worktrees/..."
    return f"Use a ticket worktree under {workspace_root / 'worktrees' / '...'}"


def ensure_canonical_repo_write_allowed(
    *,
    operation: str,
    target_path: str | Path,
    base: str | Path | None = None,
    maintenance_action: bool = False,
) -> Path:
    info = classify_operator_path(target_path, base=base)
    if not info.is_canonical_repo:
        return info.path
    if maintenance_action and os.environ.get(CANONICAL_REPO_MAINTENANCE_ENV) == "1":
        return info.path
    env_note = (
        f" Only narrow maintenance cleanup may use {CANONICAL_REPO_MAINTENANCE_ENV}=1."
        if maintenance_action
        else f" {CANONICAL_REPO_MAINTENANCE_ENV}=1 is not a general bypass for implementation work."
    )
    raise RuntimeError(
        f"{CANONICAL_REPO_WRITE_FORBIDDEN}: {operation} would touch {info.classification} at {info.path}. "
        f"{canonical_repo_write_hint(info.workspace_root)}.{env_note}"
    )


__all__ = [
    "APP_NAME",
    "AppPaths",
    "CANONICAL_REPO_MAINTENANCE_ENV",
    "CANONICAL_REPO_WRITE_FORBIDDEN",
    "CANONICAL_PATH_CLASSIFICATIONS",
    "OperatorPathInfo",
    "canonical_repo_write_hint",
    "classify_operator_path",
    "config_file",
    "contexts_file",
    "director_prepare_runs_dir",
    "director_run_local_dir",
    "downloads_dir",
    "ensure_canonical_repo_write_allowed",
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
