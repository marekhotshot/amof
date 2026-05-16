"""Local generated-build artifact store.

The store is deliberately local-only and filesystem-backed:

    .amof/generated-builds/<repo-id>/<service-or-root>/artifact.json
    .amof/generated-builds/index.json

No API, UI, deploy, or release state is touched here. Producer
commands (detect/render/build-proof/runtime-proof) may persist their
artifact; list/show commands are read-only.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


INDEX_VERSION = 1


def store_root() -> Path:
    """Return the local generated-build store root.

    `AMOF_GENERATED_BUILDS_ROOT` exists primarily for tests. Otherwise
    we anchor at the nearest ancestor with `.amof/state.json`, falling
    back to the current working directory.
    """
    override = os.environ.get("AMOF_GENERATED_BUILDS_ROOT")
    if override:
        return Path(override).resolve()

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".amof" / "state.json").exists():
            return candidate / ".amof" / "generated-builds"
    return cwd / ".amof" / "generated-builds"


def persist_artifact(artifact: Dict[str, Any], *, service: Optional[str] = None) -> Path:
    """Persist an artifact and upsert its index entry.

    The artifact is written without changing its proof status. When a
    service is supplied and the artifact does not already carry one, a
    shallow copy is enriched with `service` so subsequent `show` calls
    can resolve the deterministic path.
    """
    to_write = dict(artifact)
    if service and not to_write.get("service"):
        to_write["service"] = service

    root = store_root()
    artifact_path = artifact_path_for(to_write, service=service, root=root)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(to_write, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    _upsert_index(root, to_write, artifact_path, service=service)
    return artifact_path


def artifact_path_for(
    artifact_or_repo: Dict[str, Any] | str | Path,
    *,
    service: Optional[str] = None,
    root: Optional[Path] = None,
) -> Path:
    base = root or store_root()
    if isinstance(artifact_or_repo, dict):
        repo_path = str(artifact_or_repo.get("source_repo", {}).get("host_path") or "")
        resolved_service = service or artifact_or_repo.get("service") or "root"
    else:
        repo_path = str(artifact_or_repo)
        resolved_service = service or "root"
    if not repo_path:
        raise ValueError("source_repo.host_path is required to resolve generated-build artifact path")
    return base / _repo_id(repo_path) / _slug(str(resolved_service)) / "artifact.json"


def load_artifact(repo_path: str | Path, *, service: Optional[str] = None) -> Dict[str, Any]:
    path = artifact_path_for(repo_path, service=service)
    return json.loads(path.read_text(encoding="utf-8"))


def load_index() -> Dict[str, Any]:
    path = store_root() / "index.json"
    if not path.exists():
        return {"version": INDEX_VERSION, "updated_at": None, "items": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _upsert_index(root: Path, artifact: Dict[str, Any], artifact_path: Path, *, service: Optional[str]) -> None:
    index_path = root / "index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        index = {"version": INDEX_VERSION, "updated_at": None, "items": []}

    repo_path = str(artifact.get("source_repo", {}).get("host_path") or "")
    repo_id = _repo_id(repo_path)
    resolved_service = str(service or artifact.get("service") or "root")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rel_artifact_path = str(artifact_path)
    try:
        rel_artifact_path = str(artifact_path.relative_to(root.parent.parent))
    except ValueError:
        pass

    entry = {
        "repo_id": repo_id,
        "repo_path": repo_path,
        "service": resolved_service,
        "runtime_family": artifact.get("runtime_family"),
        "status": artifact.get("status"),
        "artifact_path": rel_artifact_path,
        "updated_at": now,
    }

    items: List[Dict[str, Any]] = [
        row
        for row in list(index.get("items") or [])
        if not (row.get("repo_path") == repo_path and row.get("service") == resolved_service)
    ]
    items.append(entry)
    items.sort(key=lambda row: (str(row.get("repo_id") or ""), str(row.get("service") or "")))
    index["version"] = INDEX_VERSION
    index["updated_at"] = now
    index["items"] = items
    root.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _repo_id(repo_path: str) -> str:
    return _slug(Path(repo_path).resolve().name or "repo")


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return slug or "root"
