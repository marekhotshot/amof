"""Read-only AMOF app-data path reporting."""

from __future__ import annotations

import json
from typing import Any

from ..app_paths import (
    config_file,
    contexts_file,
    director_prepare_runs_dir,
    director_run_local_dir,
    downloads_dir,
    ensure_app_roots,
    evidence_dir,
    get_app_paths,
    indexes_dir,
    kubeconfigs_dir,
    locks_dir,
    logs_dir,
    materialized_runs_dir,
    provider_profiles_dir,
    queue_dir,
    receipts_dir,
    runs_dir,
    templates_dir,
    ticket_worktrees_dir,
    tmp_dir,
    vector_store_dir,
    workspace_state_file,
    workspaces_registry_file,
)
from ..version_metadata import install_metadata_file, load_install_metadata


def _path_report() -> dict[str, Any]:
    ensure_app_roots()
    roots = get_app_paths()
    payload: dict[str, Any] = {
        "config_root": str(roots.config_root),
        "data_root": str(roots.data_root),
        "cache_root": str(roots.cache_root),
        "state_root": str(roots.state_root),
        "config_file": str(config_file()),
        "contexts_file": str(contexts_file()),
        "workspace_state_file": str(workspace_state_file()),
        "workspaces_registry_file": str(workspaces_registry_file()),
        "kubeconfigs_dir": str(kubeconfigs_dir()),
        "provider_profiles_dir": str(provider_profiles_dir()),
        "runs_dir": str(runs_dir()),
        "evidence_dir": str(evidence_dir()),
        "director_prepare_runs_dir": str(director_prepare_runs_dir()),
        "director_run_local_dir": str(director_run_local_dir()),
        "materialized_runs_dir": str(materialized_runs_dir()),
        "ticket_worktrees_dir": str(ticket_worktrees_dir()),
        "receipts_dir": str(receipts_dir()),
        "templates_dir": str(templates_dir()),
        "indexes_dir": str(indexes_dir()),
        "downloads_dir": str(downloads_dir()),
        "vector_store_dir": str(vector_store_dir()),
        "tmp_dir": str(tmp_dir()),
        "logs_dir": str(logs_dir()),
        "locks_dir": str(locks_dir()),
        "queue_dir": str(queue_dir()),
        "install_metadata_file": str(install_metadata_file()),
    }
    metadata = load_install_metadata()
    if metadata is not None:
        payload["install_metadata"] = metadata
    return payload


def cmd_paths(args: Any) -> int:
    payload = _path_report()
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2))
        return 0

    width = max(len(key) for key in payload)
    for key, value in payload.items():
        rendered = json.dumps(value) if isinstance(value, dict) else value
        print(f"{key:<{width}}  {rendered}")
    return 0


__all__ = ["cmd_paths"]
