"""Codebase indexer — generates a structured JSON index using a big-context model.

Indexes only the repos/ folder (ecosystem repos), NOT the AMOF framework.
Uses a Merkle tree for efficient incremental change detection:
- First run: full index (all files sent to LLM)
- Subsequent runs: Merkle diff identifies changed files, only those are re-indexed

The index is used by:
1. The TaskPlanner to understand the codebase without reading every file
2. The Agent to navigate quickly to relevant code
3. The ContextBuilder to include structured codebase context

Storage: ecosystems/<name>/index/codebase-index.json + merkle-tree.json

Typical cost: $0.10–0.50 for full index, $0.01–0.10 for incremental update.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .agent_models import CodebaseIndexOutputModel, IncrementalIndexUpdateModel
from .llm.base import LLMClient
from .merkle import MerkleDiff, MerkleNode, MerkleTree
from .prompt_loader import load_prompt

logger = logging.getLogger(__name__)

# Maximum files to include in a single indexing context
MAX_FILES_FOR_INDEXING = 200
# Hard cap: ~100k tokens to stay under 200k API limit (system + user + output)
MAX_CONTEXT_CHARS = 400_000

# Minimal fallbacks if prompt files are missing
_INDEXER_FALLBACK = (
    "You are a codebase analyst. Create a structured JSON index of the codebase. "
    "Respond with ONLY a JSON object containing: summary, architecture, files, "
    "dependency_graph, entry_points, key_abstractions."
)
_INCREMENTAL_FALLBACK = (
    "You are a codebase analyst. Produce an incremental JSON update with only "
    "changed files. Respond with ONLY the JSON object."
)
MAX_STRUCTURED_RETRIES = 3


@dataclass
class CodebaseIndex:
    """Structured index of a codebase."""

    summary: str = ""
    architecture: str = ""
    files: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    dependency_graph: Dict[str, List[str]] = field(default_factory=dict)
    entry_points: List[str] = field(default_factory=list)
    key_abstractions: List[Dict[str, Any]] = field(default_factory=list)
    # Metadata
    indexer_model: str = ""
    indexing_cost: float = 0.0
    indexing_latency_ms: int = 0
    file_count: int = 0
    content_hash: str = ""  # Merkle root hash
    indexed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON storage."""
        return {
            "summary": self.summary,
            "architecture": self.architecture,
            "files": self.files,
            "dependency_graph": self.dependency_graph,
            "entry_points": self.entry_points,
            "key_abstractions": self.key_abstractions,
            "_meta": {
                "indexer_model": self.indexer_model,
                "indexing_cost": self.indexing_cost,
                "indexing_latency_ms": self.indexing_latency_ms,
                "file_count": self.file_count,
                "content_hash": self.content_hash,
                "indexed_at": self.indexed_at,
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CodebaseIndex":
        """Deserialize from dict."""
        meta = data.get("_meta", {})
        return cls(
            summary=data.get("summary", ""),
            architecture=data.get("architecture", ""),
            files=data.get("files", {}),
            dependency_graph=data.get("dependency_graph", {}),
            entry_points=data.get("entry_points", []),
            key_abstractions=data.get("key_abstractions", []),
            indexer_model=meta.get("indexer_model", ""),
            indexing_cost=meta.get("indexing_cost", 0.0),
            indexing_latency_ms=meta.get("indexing_latency_ms", 0),
            file_count=meta.get("file_count", 0),
            content_hash=meta.get("content_hash", ""),
            indexed_at=meta.get("indexed_at", ""),
        )

    def find_files_for(self, topic: str) -> List[str]:
        """Find files related to a topic using the index."""
        topic_lower = topic.lower()
        matches = []
        for path, info in self.files.items():
            if topic_lower in info.get("purpose", "").lower():
                matches.append(path)
                continue
            for cls_info in info.get("classes", []):
                if topic_lower in cls_info.get("name", "").lower() or topic_lower in cls_info.get("description", "").lower():
                    matches.append(path)
                    break
            else:
                for func in info.get("functions", []):
                    if topic_lower in func.get("name", "").lower() or topic_lower in func.get("description", "").lower():
                        matches.append(path)
                        break
        return matches

    def get_dependencies(self, file_path: str, depth: int = 1) -> List[str]:
        """Get transitive dependencies of a file up to given depth."""
        visited = {file_path}
        current_level = [file_path]
        all_deps: List[str] = []
        for _ in range(depth):
            next_level = []
            for fp in current_level:
                for dep in self.dependency_graph.get(fp, []):
                    if dep not in visited:
                        visited.add(dep)
                        all_deps.append(dep)
                        next_level.append(dep)
            current_level = next_level
        return all_deps

    def high_risk_files(self, top_n: int = 10) -> List[Dict[str, Any]]:
        """Identify high-risk files based on complexity and dependencies.

        Risk factors:
        - High complexity (from LLM analysis)
        - Many dependents (other files import this)
        - Many dependencies (couples to many modules)
        - Entry point status

        Returns list of dicts: [{path, risk_score, reasons}] sorted by score desc.
        """
        # Build reverse dependency map (who depends on me)
        dependents: Dict[str, int] = {}
        for src, deps in self.dependency_graph.items():
            for dep in deps:
                dependents[dep] = dependents.get(dep, 0) + 1

        results: List[Dict[str, Any]] = []
        entry_set = set(self.entry_points)

        for path, info in self.files.items():
            score = 0.0
            reasons: List[str] = []

            # Complexity score
            complexity = info.get("complexity", "low")
            if complexity == "high":
                score += 3.0
                reasons.append("high complexity")
            elif complexity == "medium":
                score += 1.5

            # Dependent count (many files import this)
            dep_count = dependents.get(path, 0)
            if dep_count >= 5:
                score += 2.5
                reasons.append(f"{dep_count} dependents")
            elif dep_count >= 3:
                score += 1.5
                reasons.append(f"{dep_count} dependents")

            # Outgoing dependency count (couples to many)
            out_deps = len(self.dependency_graph.get(path, []))
            if out_deps >= 8:
                score += 1.5
                reasons.append(f"{out_deps} dependencies")

            # Entry point
            if path in entry_set:
                score += 1.0
                reasons.append("entry point")

            # Many classes/functions (large file)
            num_symbols = len(info.get("classes", [])) + len(info.get("functions", []))
            if num_symbols >= 10:
                score += 1.0
                reasons.append(f"{num_symbols} symbols")

            if score > 0:
                results.append({
                    "path": path,
                    "risk_score": round(score, 1),
                    "reasons": reasons,
                    "complexity": complexity,
                    "dependents": dep_count,
                })

        results.sort(key=lambda x: x["risk_score"], reverse=True)
        return results[:top_n]

    def to_context_string(self) -> str:
        """Format the index as a compact string for planner context.

        Includes: summary, architecture, per-file purpose + key symbols,
        dependency graph, and key abstractions.  Typically 2k-5k tokens.
        """
        parts: List[str] = []

        if self.summary:
            parts.append(f"## Codebase Summary\n{self.summary}\n")
        if self.architecture:
            parts.append(f"## Architecture\n{self.architecture}\n")

        if self.files:
            parts.append(f"## Files ({len(self.files)} indexed)\n")
            for path in sorted(self.files.keys()):
                info = self.files[path]
                purpose = info.get("purpose", "")
                line = f"- `{path}`: {purpose}"

                symbols: List[str] = []
                for cls_info in info.get("classes", []):
                    if isinstance(cls_info, str):
                        symbols.append(cls_info)
                        continue
                    name = cls_info.get("name", "")
                    methods = cls_info.get("key_methods", [])
                    if methods:
                        symbols.append(f"{name}({', '.join(methods[:3])})")
                    else:
                        symbols.append(name)
                for func in info.get("functions", []):
                    if isinstance(func, str):
                        symbols.append(func)
                        continue
                    symbols.append(func.get("name", ""))

                if symbols:
                    line += f" [{', '.join(symbols[:5])}]"
                parts.append(line)
            parts.append("")

        if self.entry_points:
            parts.append("## Entry Points\n" + ", ".join(f"`{e}`" for e in self.entry_points) + "\n")

        if self.key_abstractions:
            parts.append("## Key Abstractions\n")
            for ab in self.key_abstractions:
                name = ab.get("name", "")
                desc = ab.get("description", "")
                parts.append(f"- **{name}**: {desc}")
            parts.append("")

        if self.dependency_graph:
            parts.append("## Dependency Graph\n")
            for path in sorted(self.dependency_graph.keys()):
                deps = self.dependency_graph[path]
                if deps:
                    parts.append(f"- `{path}` -> {', '.join(f'`{d}`' for d in deps[:5])}")
            parts.append("")

        # High-risk files
        high_risk = self.high_risk_files(top_n=5)
        if high_risk:
            parts.append("## High-Risk Files (change with care)\n")
            for hr in high_risk:
                reasons_str = ", ".join(hr["reasons"])
                parts.append(f"- `{hr['path']}` (score {hr['risk_score']}): {reasons_str}")
            parts.append("")

        return "\n".join(parts)


class CodebaseIndexer:
    """Generates a CodebaseIndex using a big-context LLM.

    The indexer walks an explicit set of repository roots (manifest-bounded)
    and builds a Merkle tree for change detection. When ``repo_roots`` is
    provided, indexing is strictly limited to that set — no cross-ecosystem
    leakage. The legacy ``repos_root`` (parent dir scan) is still accepted
    for back-compat with callers that have not been migrated yet.
    """

    def __init__(
        self,
        indexer_llm: LLMClient,
        repos_root: Optional[Path] = None,
        index_dir: Optional[Path] = None,
        max_files: int = MAX_FILES_FOR_INDEXING,
        vector_store: Optional[Any] = None,
        ecosystem_name: str = "default_ecosystem",
        repo_roots: Optional[List[Path]] = None,
    ):
        """Initialize the indexer.

        Args:
            indexer_llm: LLM client for generating file descriptions.
            repos_root: Legacy single-root scan (default: cwd/repos). Used
                only when ``repo_roots`` is not provided.
            index_dir: Directory for index storage (default: cwd/.amof/index).
            max_files: Maximum files to include in indexing context.
            vector_store: Optional VectorStore for semantic indexing.
            ecosystem_name: Ecosystem name for vector store namespace.
            repo_roots: Manifest-bounded list of repository roots to walk.
                When provided, this is the canonical scope and the indexer
                will not look outside it. The synthetic Merkle root name
                stays ``repos`` so existing relative paths in the index
                payload remain stable.
        """
        self._llm = indexer_llm
        self._repos_root = repos_root or (Path.cwd() / "repos")
        # Normalize to absolute paths so equality/relativization is consistent.
        self._repo_roots: Optional[List[Path]] = (
            [Path(p).resolve() for p in repo_roots] if repo_roots is not None else None
        )
        self._index_dir = index_dir or (Path.cwd() / ".amof" / "index")
        self._max_files = max_files
        self._vector_store = vector_store
        self._ecosystem_name = ecosystem_name

    @property
    def manifest_bounded(self) -> bool:
        """True when this indexer was constructed with explicit repo_roots."""
        return self._repo_roots is not None

    @property
    def repo_roots(self) -> List[Path]:
        """The effective list of repo roots that will be walked."""
        if self._repo_roots is not None:
            return list(self._repo_roots)
        return [self._repos_root]

    def _build_current_tree(self) -> MerkleNode:
        """Build a Merkle tree over the configured scope.

        Manifest-bounded scope uses ``MerkleTree.build_from_roots`` so the
        tree is the union of only the enabled repos, not the entire repos/
        directory. Falls back to legacy single-root scan otherwise.
        """
        if self._repo_roots is not None:
            return MerkleTree.build_from_roots(self._repo_roots)
        return MerkleTree.build(self._repos_root)

    def _scope_exists(self) -> bool:
        if self._repo_roots is not None:
            return any(p.exists() for p in self._repo_roots)
        return self._repos_root.exists()

    @property
    def index_path(self) -> Path:
        return self._index_dir / "codebase-index.json"

    @property
    def tree_path(self) -> Path:
        return self._index_dir / "merkle-tree.json"

    def index(self, force: bool = False) -> CodebaseIndex:
        """Generate or incrementally update the codebase index.

        1. Build Merkle tree from repos/
        2. Compare with cached tree
        3. If unchanged: return cached index
        4. If changed: incremental update (or full if no cache / force)

        Args:
            force: Force full re-index even if cache exists.

        Returns:
            CodebaseIndex with file information.
        """
        if not self._scope_exists():
            logger.warning(
                "Indexing scope not found (repos_root=%s, repo_roots=%s)",
                self._repos_root,
                self._repo_roots,
            )
            return CodebaseIndex()

        # Step 1: Build current Merkle tree (manifest-bounded if configured)
        current_tree = self._build_current_tree()
        logger.info(
            "Merkle tree: %d files, root=%s, scope=%s",
            current_tree.file_count,
            current_tree.hash,
            "manifest-bounded" if self.manifest_bounded else "single-root",
        )

        # Step 2: Check cache
        cached_index = None
        cached_tree = None

        if not force and self.index_path.exists() and self.tree_path.exists():
            try:
                cached_tree = MerkleTree.load(self.tree_path)
                cached_index = self._load_cached()

                # Hashes match → no changes
                if cached_tree.hash == current_tree.hash:
                    logger.info(
                        "Index up to date (%d files, root=%s)",
                        cached_index.file_count, current_tree.hash,
                    )
                    return cached_index
            except Exception as e:
                logger.warning("Failed to load cache: %s", e)
                cached_index = None
                cached_tree = None

        # Step 3: Determine full vs incremental
        if cached_index and cached_tree and not force:
            # Incremental update
            diff = MerkleTree.diff(cached_tree, current_tree)
            logger.info(
                "Incremental update: %s", diff.summary(),
            )
            result = self._incremental_index(diff, cached_index, current_tree)
        else:
            # Full index
            result = self._full_index(current_tree)

        # Step 4: Save tree and index
        MerkleTree.save(current_tree, self.tree_path)
        self._save(result)

        return result

    def build_tree_only(self) -> MerkleNode:
        """Build and save just the Merkle tree (no LLM call).

        Useful during `amof install` when no API key is available.
        Honors the manifest-bounded scope when configured.
        """
        if not self._scope_exists():
            logger.warning(
                "Indexing scope not found (repos_root=%s, repo_roots=%s)",
                self._repos_root,
                self._repo_roots,
            )
            from .merkle import _empty_hash
            return MerkleNode(name="repos", hash=_empty_hash(), is_dir=True)

        tree = self._build_current_tree()
        MerkleTree.save(tree, self.tree_path)
        logger.info(
            "Merkle tree saved (no index): %d files, root=%s, scope=%s",
            tree.file_count,
            tree.hash,
            "manifest-bounded" if self.manifest_bounded else "single-root",
        )
        return tree

    def _full_index(self, tree: MerkleNode) -> CodebaseIndex:
        """Full index: read all files, send to LLM."""
        files = self._collect_files_from_tree(tree, str(self._repos_root.parent) + "/")
        if not files:
            return CodebaseIndex(content_hash=tree.hash)

        context = self._build_context(files)

        logger.info(
            "Full indexing: %d files, ~%d chars, ~%d tokens",
            len(files), len(context), len(context) // 4,
        )

        index_model, usage, latency_ms = self._request_structured(
            system_prompt=load_prompt("indexer", fallback=_INDEXER_FALLBACK),
            user_content=context,
            response_model=CodebaseIndexOutputModel,
            max_tokens=16384,
        )
        index_data = index_model.model_dump()

        idx = CodebaseIndex(
            summary=index_data.get("summary", ""),
            architecture=index_data.get("architecture", ""),
            files=index_data.get("files", {}),
            dependency_graph=index_data.get("dependency_graph", {}),
            entry_points=index_data.get("entry_points", []),
            key_abstractions=index_data.get("key_abstractions", []),
            indexer_model=usage.model if usage else self._llm.model_name(),
            indexing_cost=usage.estimated_cost if usage else 0.0,
            indexing_latency_ms=latency_ms,
            file_count=tree.file_count,
            content_hash=tree.hash,
            indexed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )

        self._upsert_to_vector_store(files, index_data)

        logger.info(
            "Full index created: %d file descriptions, cost=$%.4f, %dms",
            len(idx.files), idx.indexing_cost, latency_ms,
        )
        return idx

    def _incremental_index(
        self, diff: MerkleDiff, existing: CodebaseIndex, new_tree: MerkleNode,
    ) -> CodebaseIndex:
        """Incremental index: only re-index changed/added files."""
        # Remove deleted files from index
        for path in diff.deleted:
            existing.files.pop(path, None)
            existing.dependency_graph.pop(path, None)

        # Collect contents of changed + added files
        changed_paths = diff.added + diff.modified
        if not changed_paths:
            # Only deletions — just update metadata
            existing.content_hash = new_tree.hash
            existing.file_count = new_tree.file_count
            existing.indexed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            return existing

        # Read changed files from disk
        files: Dict[str, str] = {}
        for rel_path in changed_paths:
            # rel_path is like "repos/xpc-helm/values.yaml"
            abs_path = self._repos_root.parent / rel_path
            if abs_path.exists() and abs_path.is_file():
                try:
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
                    files[rel_path] = content
                except Exception:
                    continue

        if not files:
            existing.content_hash = new_tree.hash
            existing.indexed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            return existing

        # Build incremental context (cap to avoid 200k token limit)
        context_parts = [
            f"# Incremental Update: {len(files)} changed files\n",
            f"## Existing codebase summary\n{existing.summary}\n",
            f"## Existing architecture\n{existing.architecture}\n",
            "## Changed/Added Files\n",
        ]
        total_chars = sum(len(p) for p in context_parts)
        for path in sorted(files.keys()):
            content = files[path]
            if len(content) > 5000:
                content = content[:5000] + f"\n... (truncated, {len(files[path])} chars total)"
            block = f"\n### {path}\n```\n{content}\n```\n"
            if total_chars + len(block) > MAX_CONTEXT_CHARS:
                context_parts.append(f"\n### {path}\n```\n{content[:1000]}\n...\n```\n")
                context_parts.append("\n[WARNING: Context truncated due to length limitations.]\n")
                break
            context_parts.append(block)
            total_chars += len(block)

        context = "\n".join(context_parts)

        logger.info(
            "Incremental indexing: %d files, ~%d chars",
            len(files), len(context),
        )

        update_model, usage, latency_ms = self._request_structured(
            system_prompt=load_prompt("indexer-incremental", fallback=_INCREMENTAL_FALLBACK),
            user_content=context,
            response_model=IncrementalIndexUpdateModel,
            max_tokens=8192,
        )
        update_data = update_model.model_dump()
        for path, info in update_data.get("files", {}).items():
            existing.files[path] = info
        for path, deps in update_data.get("dependency_graph_updates", {}).items():
            existing.dependency_graph[path] = deps

        # Update metadata
        cost = usage.estimated_cost if usage else 0.0
        existing.indexing_cost += cost
        existing.indexing_latency_ms = latency_ms
        existing.content_hash = new_tree.hash
        existing.file_count = new_tree.file_count
        existing.indexed_at = time.strftime("%Y-%m-%dT%H:%M:%S")

        self._upsert_to_vector_store(files, update_data)

        logger.info(
            "Incremental index: %d files updated, cost=$%.4f, %dms",
            len(files), cost, latency_ms,
        )
        return existing

    def _upsert_to_vector_store(self, files_content: Dict[str, str], index_data: Dict[str, Any]) -> None:
        """Upsert file contents or summaries to the vector database."""
        if not self._vector_store:
            return
            
        for path, content in files_content.items():
            ext = Path(path).suffix.lower()
            if ext in (".md", ".txt", ".json", ".yaml", ".yml") or "kb/" in path or "journal/" in path or "docs/" in path:
                # Upsert raw text for docs/config
                self._vector_store.upsert_document(
                    doc_id=path,
                    text=content,
                    metadata={"type": "doc", "path": path},
                    ecosystem_name=self._ecosystem_name,
                )
            else:
                # Upsert summary for source code
                file_info = index_data.get("files", {}).get(path, {})
                if file_info:
                    purpose = file_info.get("purpose", "")
                    classes = file_info.get("classes", [])
                    functions = file_info.get("functions", [])
                    deps = index_data.get("dependency_graph", {}).get(path, [])
                    
                    summary_parts = [f"File: {path}"]
                    if purpose:
                        summary_parts.append(f"Purpose: {purpose}")
                    if classes:
                        class_names = [c.get("name") if isinstance(c, dict) else str(c) for c in classes]
                        summary_parts.append(f"Classes: {', '.join(class_names)}")
                    if functions:
                        func_names = [f.get("name") if isinstance(f, dict) else str(f) for f in functions]
                        summary_parts.append(f"Functions: {', '.join(func_names)}")
                    if deps:
                        summary_parts.append(f"Dependencies: {', '.join(deps)}")
                        
                    summary_text = "\n".join(summary_parts)
                    self._vector_store.upsert_document(
                        doc_id=path,
                        text=summary_text,
                        metadata={"type": "code_summary", "path": path},
                        ecosystem_name=self._ecosystem_name,
                    )

    def _collect_files_from_tree(
        self, node: MerkleNode, base_path: str,
    ) -> Dict[str, str]:
        """Read file contents for all files in the Merkle tree."""
        files: Dict[str, str] = {}
        self._collect_recursive(node, base_path, "", files)
        return files

    def _collect_recursive(
        self, node: MerkleNode, base_path: str, prefix: str,
        files: Dict[str, str],
    ) -> None:
        """Recursively collect file contents."""
        if len(files) >= self._max_files:
            return

        rel = f"{prefix}{node.name}" if prefix else node.name

        if not node.is_dir:
            abs_path = Path(base_path) / rel
            try:
                if abs_path.exists() and abs_path.stat().st_size > 50_000:
                    files[rel] = "[File skipped: Exceeds 50KB size limit]"
                else:
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
                    files[rel] = content
            except Exception:
                pass
            return

        child_prefix = f"{rel}/"
        for child in sorted(node.children.values(), key=lambda c: c.name):
            if len(files) >= self._max_files:
                break
            self._collect_recursive(child, base_path, child_prefix, files)

    @staticmethod
    def _build_context(files: Dict[str, str]) -> str:
        """Build the context string sent to the indexer LLM."""
        parts = [f"# Codebase ({len(files)} files)\n"]
        parts.append("## File Tree\n")

        for path in sorted(files.keys()):
            parts.append(f"  {path}")
        parts.append("\n\n## File Contents\n")

        total_chars = 0
        for path in sorted(files.keys()):
            content = files[path]
            if len(content) > 5000:
                content = content[:5000] + f"\n... (truncated, {len(files[path])} chars total)"
            chars_in_file = len(content)
            if total_chars + chars_in_file > MAX_CONTEXT_CHARS:
                parts.append(f"\n### {path}\n```\n{content[:1000]}\n...\n```\n")
                parts.append("\n[WARNING: Context truncated due to length limitations.]\n")
                break
            parts.append(f"\n### {path}\n```\n{content}\n```\n")
            total_chars += chars_in_file

        return "\n".join(parts)

    def _save(self, index: CodebaseIndex) -> None:
        """Save index to disk."""
        self._index_dir.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(index.to_dict(), indent=2),
            encoding="utf-8",
        )
        logger.info("Index saved to %s", self.index_path)

    def _load_cached(self) -> CodebaseIndex:
        """Load index from disk cache."""
        data = json.loads(self.index_path.read_text(encoding="utf-8"))
        return CodebaseIndex.from_dict(data)

    @staticmethod
    def load(index_path: Path) -> CodebaseIndex:
        """Load a CodebaseIndex from a JSON file (for external use)."""
        data = json.loads(index_path.read_text(encoding="utf-8"))
        return CodebaseIndex.from_dict(data)

    def _request_structured(
        self,
        system_prompt: str,
        user_content: str,
        response_model: Any,
        max_tokens: int,
    ) -> tuple[Any, Any, int]:
        """Request schema-validated output with retries and self-correction."""
        start = time.monotonic()
        messages = [{"role": "user", "content": user_content}]
        last_error = ""

        for attempt in range(1, MAX_STRUCTURED_RETRIES + 1):
            if last_error:
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response failed schema validation.\n"
                        f"Validation error:\n{last_error}\n\n"
                        "Return ONLY a valid JSON object for the schema."
                    ),
                })

            try:
                structured = self._llm.chat_structured(
                    system=system_prompt,
                    messages=messages,
                    response_model=response_model,
                    max_tokens=max_tokens,
                    temperature=0.0,
                )
                latency_ms = int((time.monotonic() - start) * 1000)
                return structured.parsed, structured.usage, latency_ms
            except NotImplementedError:
                response = self._llm.chat(
                    system=system_prompt
                    + "\n\nReturn ONLY a strict JSON object. Do not use markdown fences.",
                    messages=messages,
                    tools=None,
                    max_tokens=max_tokens,
                    temperature=0.0,
                )
                raw_text = (response.text or "").strip()
                if not raw_text:
                    last_error = "Empty response."
                    continue
                try:
                    parsed = response_model.model_validate_json(raw_text)
                    latency_ms = int((time.monotonic() - start) * 1000)
                    return parsed, response.usage, latency_ms
                except ValidationError as e:
                    last_error = str(e)
                    logger.warning("Indexer schema validation failed (attempt %d): %s", attempt, e)
                    if hasattr(self._llm, 'record_failure'):
                        self._llm.record_failure()
                    continue
            except ValidationError as e:
                last_error = str(e)
                logger.warning("Indexer structured validation failed (attempt %d): %s", attempt, e)
                if hasattr(self._llm, 'record_failure'):
                    self._llm.record_failure()
                continue
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning("Indexer structured request failed (attempt %d): %s", attempt, e)
                continue

        raise ValueError(
            f"Indexer failed to produce valid structured output after {MAX_STRUCTURED_RETRIES} attempts. "
            f"Last error: {last_error}"
        )
