"""Manifest-driven indexing scope.

Resolves the set of repository roots that the codebase indexer is allowed
to walk for a given ecosystem. The ecosystem manifest lists repositories
with a ``path`` and an ``enabled`` flag — only enabled repositories with
existing on-disk paths participate in indexing.

This replaces the previous behavior where the indexer walked the entire
``repos/`` folder unconditionally, which meant indexing for one ecosystem
silently included siblings (e.g. ``hotshot`` indexing pulling in the same
files as ``amof-platform``). Now the manifest is the single source of
truth for what each ecosystem's agent and indexer can see.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManifestScope:
    """Resolved indexing scope for one ecosystem.

    Attributes:
        ecosystem: Ecosystem name (for logging / diagnostics).
        repo_roots: Existing on-disk roots the indexer may walk. Each entry
            is a directory inside the workspace (typically ``repos/<name>``).
        skipped: Tuples of ``(repo_name, reason)`` for repos that were
            considered but not included (disabled, missing, malformed).
    """

    ecosystem: str
    repo_roots: List[Path]
    skipped: List[tuple]

    @property
    def repo_count(self) -> int:
        return len(self.repo_roots)

    def is_empty(self) -> bool:
        return not self.repo_roots


def manifest_repo_entries(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the manifest's ``repos`` list, normalized to dicts.

    Tolerant of missing/None and of plain string entries: a string is
    treated as a repo with that ``name`` (the legacy ad-hoc shape).
    """
    raw = manifest.get("repos") if isinstance(manifest, dict) else None
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append(entry)
        elif isinstance(entry, str):
            out.append({"name": entry})
    return out


def resolve_scope(
    manifest: Dict[str, Any],
    workspace_root: Path,
    ecosystem: Optional[str] = None,
    *,
    require_existing: bool = True,
) -> ManifestScope:
    """Resolve enabled, existing repo roots for the given manifest.

    Args:
        manifest: Parsed ecosystem manifest dict.
        workspace_root: Filesystem root used to resolve relative ``path``
            values. Repos are returned as ``workspace_root / repo.path``.
        ecosystem: Optional ecosystem name override (for diagnostics).
        require_existing: When True (default), repo paths that don't exist
            on disk are skipped. Tests may pass False to inspect the
            manifest-only intent.

    Returns:
        ManifestScope with the resolved roots and per-repo skip reasons.
    """
    eco_name = (
        ecosystem
        or (manifest.get("ecosystem") if isinstance(manifest, dict) else None)
        or (manifest.get("name") if isinstance(manifest, dict) else None)
        or "<unknown>"
    )

    workspace_root = Path(workspace_root)
    seen: set = set()
    roots: List[Path] = []
    skipped: List[tuple] = []

    for entry in manifest_repo_entries(manifest):
        name = str(entry.get("name") or "<unnamed>")

        enabled = entry.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
        if not enabled:
            skipped.append((name, "disabled"))
            continue

        rel_path = entry.get("path") or f"repos/{name}"
        if not isinstance(rel_path, str) or not rel_path.strip():
            skipped.append((name, "no path"))
            continue

        full_path = (workspace_root / rel_path).resolve()
        try:
            full_path.relative_to(workspace_root.resolve())
        except ValueError:
            skipped.append((name, f"path escapes workspace ({rel_path})"))
            continue

        if require_existing and not full_path.exists():
            skipped.append((name, f"missing on disk ({rel_path})"))
            continue

        if full_path in seen:
            skipped.append((name, "duplicate path"))
            continue
        seen.add(full_path)
        roots.append(full_path)

    if not roots:
        logger.warning(
            "Indexing scope for ecosystem %s resolved to zero repos (skipped=%s)",
            eco_name,
            skipped,
        )

    return ManifestScope(ecosystem=str(eco_name), repo_roots=roots, skipped=skipped)


def derive_repo_roots(
    manifest: Dict[str, Any],
    workspace_root: Path,
    ecosystem: Optional[str] = None,
) -> List[Path]:
    """Convenience wrapper returning only the resolved repo root paths."""

    return list(resolve_scope(manifest, workspace_root, ecosystem).repo_roots)
