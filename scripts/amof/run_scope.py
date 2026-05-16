"""Normalized ad-hoc run scope helpers for Director local runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .app_config import get_registered_workspace


@dataclass(frozen=True)
class ScopeRepo:
    name: str
    source: str
    source_kind: str
    default_ref: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScopeRef:
    repo: str
    ref: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ScopeResolvedSha:
    repo: str
    sha: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RunScope:
    scope_id: str
    scope_kind: str
    repos: list[ScopeRepo]
    refs: list[ScopeRef]
    resolved_shas: list[ScopeResolvedSha]
    policy: dict[str, Any]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "scope_kind": self.scope_kind,
            "repos": [repo.to_dict() for repo in self.repos],
            "refs": [ref.to_dict() for ref in self.refs],
            "resolved_shas": [sha.to_dict() for sha in self.resolved_shas],
            "policy": dict(self.policy),
            "metadata": dict(self.metadata),
        }


def parse_ad_hoc_repo_spec(repo_spec: str, explicit_ref: str | None = None) -> dict[str, str]:
    """Parse `name=path-or-url@ref` or `path-or-url@ref` ad-hoc repo inputs."""
    normalized = str(repo_spec or "").strip()
    if not normalized:
        raise ValueError("repo spec is required")

    alias: str | None = None
    source_part = normalized
    if "=" in normalized:
        alias, source_part = normalized.split("=", 1)
        alias = alias.strip() or None
        source_part = source_part.strip()

    source = source_part
    ref = str(explicit_ref or "").strip() or None
    if ref is None:
        source_candidate, at, ref_candidate = source_part.rpartition("@")
        if at and ref_candidate.strip():
            source = source_candidate.strip()
            ref = ref_candidate.strip()

    if not source:
        raise ValueError("repo source is required")

    if alias is None and _looks_like_alias(source):
        try:
            entry = get_registered_workspace(source)
        except KeyError:
            entry = None
        if entry is not None:
            alias = entry["name"]
            source = entry["path"]
            if ref is None:
                ref = str(entry.get("default_ref") or "main")

    if ref is None:
        ref = "main"

    name = alias or _infer_repo_name(source)
    source_kind = "local-path" if Path(source).expanduser().exists() else "git-url"
    return {
        "name": name,
        "source": source,
        "source_kind": source_kind,
        "ref": ref,
    }


def build_ad_hoc_run_scope(
    *,
    scope_id: str,
    context_name: str,
    repo_name: str,
    repo_source: str,
    source_kind: str,
    ref: str,
    resolved_sha: str,
) -> RunScope:
    return RunScope(
        scope_id=scope_id,
        scope_kind="ad_hoc",
        repos=[
            ScopeRepo(
                name=repo_name,
                source=repo_source,
                source_kind=source_kind,
                default_ref=ref,
            )
        ],
        refs=[ScopeRef(repo=repo_name, ref=ref)],
        resolved_shas=[ScopeResolvedSha(repo=repo_name, sha=resolved_sha)],
        policy={
            "allow_promotion": False,
            "require_clean_workspace": True,
            "materialize_exact_sha": True,
        },
        metadata={
            "created_by": "cli",
            "context": context_name,
        },
    )


def _looks_like_alias(value: str) -> bool:
    return "/" not in value and ":" not in value and value not in {".", ".."}


def _infer_repo_name(source: str) -> str:
    candidate = source.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    candidate = candidate.strip()
    if not candidate:
        raise ValueError("could not infer repo name from source")
    return candidate


__all__ = [
    "RunScope",
    "build_ad_hoc_run_scope",
    "parse_ad_hoc_repo_spec",
]
