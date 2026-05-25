"""Canonical indexed planning context for read-only AMOF planning."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Optional

from ..app_paths import indexes_dir, planning_workspaces_dir
from ..manifest import resolve_workspace_root
from ..utils import get_git_toplevel
from .context.builder import ContextBuilder
from .indexer import CodebaseIndex, CodebaseIndexer
from .merkle import MerkleTree


CANONICAL_AMOF_REMOTE = "https://github.com/marekhotshot/amof.git"


class PlanningContextError(RuntimeError):
    """Raised when a canonical planning context cannot be prepared truthfully."""


@dataclass(frozen=True)
class PlanningContextReceipt:
    receipt_kind: str
    recorded_at: str
    source_repo_path: str
    source_git_root: str
    source_remote_url: str
    canonical_remote_url: str
    planning_workspace_root: str
    planning_repo_path: str
    planning_branch_ref: str
    origin_main_sha: str
    index_dir: str
    index_path: str
    tree_path: str
    merkle_root: str
    indexed_at: str
    freshness: str
    refresh_reason: str | None
    index_refreshed: bool
    repo_scope: list[str]
    files_to_inspect: list[str]
    planner_provenance: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanningContextResult:
    receipt: PlanningContextReceipt
    codebase_index: CodebaseIndex
    context_prompt: str
    repo_path: Path
    workspace_root: Path


def _build_planning_prompt(builder: ContextBuilder, index: CodebaseIndex) -> str:
    parts: list[str] = []
    repo_inventory = builder._build_manifest_repo_inventory()
    if repo_inventory:
        parts.append("# Ecosystem Repositories")
        parts.append(repo_inventory.strip())
    repo_snapshots = builder._build_manifest_repo_snapshots()
    if repo_snapshots:
        parts.append("# Ecosystem Repo Entrypoints")
        parts.append(repo_snapshots.strip())
    parts.append("# Codebase Index")
    parts.append(index.to_context_string().strip())
    return "\n\n".join(part for part in parts if part.strip()) + "\n"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_remote(url: str) -> str:
    value = str(url or "").strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value


def _workspace_runtime_key(workspace_root: Path) -> str:
    resolved = workspace_root.resolve(strict=False)
    raw = resolved.name or "workspace"
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "-" for c in raw).strip("-") or "workspace"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"{safe}-{digest}"


def planning_index_dir(workspace_root: Path, ecosystem_name: str) -> Path:
    return indexes_dir() / _workspace_runtime_key(workspace_root) / (ecosystem_name or "default")


def _planning_workspace_root(workspace_root: Path, repo_name: str) -> Path:
    return planning_workspaces_dir() / _workspace_runtime_key(workspace_root) / repo_name


def _run_git(repo_path: Path | None, *args: str) -> str:
    command = ["git", *args]
    completed = subprocess.run(
        command,
        cwd=str(repo_path) if repo_path is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "git command failed").strip()
        raise PlanningContextError(f"{' '.join(command)} failed: {message}")
    return completed.stdout.strip()


def _source_git_root(repo_path: Path) -> Path:
    git_root = get_git_toplevel(repo_path)
    if git_root is None:
        raise PlanningContextError(f"repo path is not inside a git checkout: {repo_path}")
    return git_root.resolve(strict=False)


def _origin_remote_url(repo_path: Path) -> str:
    return _run_git(repo_path, "remote", "get-url", "origin")


def _repo_name_from_remote(remote_url: str, fallback: str) -> str:
    tail = remote_url.rstrip("/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail or fallback


def _ensure_clean_planning_clone(source_git_root: Path) -> tuple[Path, str]:
    workspace_root = resolve_workspace_root()
    remote_url = _origin_remote_url(source_git_root)
    repo_name = _repo_name_from_remote(remote_url, source_git_root.name)
    planning_root = _planning_workspace_root(workspace_root, repo_name)
    repos_root = planning_root / "repos"
    planning_repo = repos_root / repo_name
    repos_root.mkdir(parents=True, exist_ok=True)

    if not planning_repo.exists():
        _run_git(None, "clone", remote_url, str(planning_repo))
    else:
        existing_remote = _origin_remote_url(planning_repo)
        if _normalize_remote(existing_remote) != _normalize_remote(remote_url):
            raise PlanningContextError(
                f"planning clone remote mismatch: expected {remote_url}, found {existing_remote}"
            )

    _run_git(planning_repo, "fetch", "origin", "main")
    origin_main_sha = _run_git(planning_repo, "rev-parse", "origin/main")
    _run_git(planning_repo, "checkout", "--detach", origin_main_sha)
    _run_git(planning_repo, "reset", "--hard", origin_main_sha)
    _run_git(planning_repo, "clean", "-fd")
    status = _run_git(planning_repo, "status", "--short")
    if status:
        raise PlanningContextError(f"planning clone is not clean after reset: {planning_repo}")
    return planning_repo, origin_main_sha


def _default_manifest(repo_name: str) -> dict[str, Any]:
    return {
        "ecosystem": repo_name,
        "name": repo_name,
        "manifest_source": "appdata",
        "repos": [
            {
                "name": repo_name,
                "path": f"repos/{repo_name}",
                "readonly": True,
                "enabled": True,
            }
        ],
    }


def _refresh_index_if_needed(
    *,
    indexer: CodebaseIndexer,
    repo_roots: list[Path],
) -> tuple[CodebaseIndex, str, bool]:
    current_tree = MerkleTree.build_from_roots(repo_roots)
    if indexer.index_path.exists() and indexer.tree_path.exists():
        cached_tree = MerkleTree.load(indexer.tree_path)
        if cached_tree.hash == current_tree.hash:
            return indexer._load_cached(), "fresh", False
        codebase_index = indexer.index(force=False)
        return codebase_index, "stale", True
    codebase_index = indexer.index(force=True)
    return codebase_index, "missing", True


def _indexed_files_for_objective(index: CodebaseIndex, objective: str, *, max_files: int) -> list[str]:
    matches = index.find_files_for(objective)
    if matches:
        return matches[:max_files]
    if index.entry_points:
        return list(index.entry_points[:max_files])
    if index.files:
        return sorted(index.files.keys())[:max_files]
    return []


def build_canonical_planning_context(
    *,
    repo: str | Path | None,
    objective: str,
    indexer_llm: Any,
    planner_provenance: Optional[dict[str, Any]] = None,
    max_files: int = 8,
) -> PlanningContextResult:
    source_path = Path(repo or ".").expanduser().resolve(strict=False)
    if not source_path.exists():
        raise PlanningContextError(f"repo path does not exist: {source_path}")
    source_git_root = _source_git_root(source_path)
    source_remote_url = _origin_remote_url(source_git_root)
    repo_name = _repo_name_from_remote(source_remote_url, source_git_root.name)
    planning_repo_path, origin_main_sha = _ensure_clean_planning_clone(source_git_root)
    planning_workspace_root = planning_repo_path.parent.parent
    repo_roots = [planning_repo_path]
    index_dir = planning_index_dir(planning_workspace_root, repo_name)
    indexer = CodebaseIndexer(
        indexer_llm=indexer_llm,
        repos_root=planning_workspace_root / "repos",
        index_dir=index_dir,
        ecosystem_name=repo_name,
        repo_roots=repo_roots,
    )
    codebase_index, freshness, refreshed = _refresh_index_if_needed(indexer=indexer, repo_roots=repo_roots)
    manifest = _default_manifest(repo_name)
    context_builder = ContextBuilder(
        workspace_root=planning_workspace_root,
        manifest=manifest,
        base_prompt_path=planning_repo_path / "prompts" / "master.md",
        codebase_index=codebase_index,
    )
    indexed_files = _indexed_files_for_objective(codebase_index, objective, max_files=max_files)
    receipt = PlanningContextReceipt(
        receipt_kind="planning_context_receipt",
        recorded_at=_now_iso(),
        source_repo_path=str(source_path),
        source_git_root=str(source_git_root),
        source_remote_url=source_remote_url,
        canonical_remote_url=CANONICAL_AMOF_REMOTE if repo_name == "amof" else source_remote_url,
        planning_workspace_root=str(planning_workspace_root),
        planning_repo_path=str(planning_repo_path),
        planning_branch_ref="origin/main",
        origin_main_sha=origin_main_sha,
        index_dir=str(index_dir),
        index_path=str(indexer.index_path),
        tree_path=str(indexer.tree_path),
        merkle_root=codebase_index.content_hash,
        indexed_at=codebase_index.indexed_at,
        freshness="fresh" if freshness == "fresh" else "refreshed",
        refresh_reason=None if freshness == "fresh" else freshness,
        index_refreshed=refreshed,
        repo_scope=[str(path.relative_to(planning_workspace_root)) for path in repo_roots],
        files_to_inspect=indexed_files,
        planner_provenance=planner_provenance,
    )
    context_prompt = _build_planning_prompt(context_builder, codebase_index)
    return PlanningContextResult(
        receipt=receipt,
        codebase_index=codebase_index,
        context_prompt=context_prompt,
        repo_path=planning_repo_path,
        workspace_root=planning_workspace_root,
    )


def write_planning_context_receipt(path: Path, receipt: PlanningContextReceipt) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


__all__ = [
    "PlanningContextError",
    "PlanningContextReceipt",
    "PlanningContextResult",
    "build_canonical_planning_context",
    "planning_index_dir",
    "write_planning_context_receipt",
]
