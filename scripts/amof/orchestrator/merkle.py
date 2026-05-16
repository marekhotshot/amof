"""Merkle tree for efficient codebase change detection.

Builds a hash tree over the repos/ folder:
- Leaf nodes: SHA256 of file content
- Interior nodes: SHA256 of sorted child_name:child_hash pairs
- Root node: fingerprint of all repos combined

Comparing two trees identifies exactly which files changed
without reading every file's content (just compare hashes level by level).

Usage:
    tree = MerkleTree.build(Path("repos"))
    old  = MerkleTree.load(Path("ecosystems/my-project/index/merkle-tree.json"))
    diff = MerkleTree.diff(old, tree)
    # diff.added, diff.modified, diff.deleted
    MerkleTree.save(tree, Path("ecosystems/my-project/index/merkle-tree.json"))
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Reuse filter constants from the indexer
SKIP_DIRS: Set[str] = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".amof", ".cursor", ".vscode",
}

INDEXABLE_EXTENSIONS: Set[str] = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
    ".rb", ".php", ".c", ".cpp", ".h", ".hpp", ".cs",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg",
    ".md", ".rst", ".txt",
    ".sh", ".bash",
    ".sql",
    ".html", ".css", ".scss",
    ".tf", ".hcl",
    ".Dockerfile", ".dockerignore",
}

KNOWN_FILENAMES: Set[str] = {"Dockerfile", "Makefile", "Jenkinsfile"}

# Skip files larger than this (bytes)
MAX_FILE_SIZE = 100_000


@dataclass
class MerkleNode:
    """A node in the Merkle tree (file or directory)."""

    name: str
    hash: str
    is_dir: bool
    children: Dict[str, "MerkleNode"] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        """Count total files in this subtree."""
        if not self.is_dir:
            return 1
        return sum(c.file_count for c in self.children.values())

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        d: dict = {
            "name": self.name,
            "hash": self.hash,
            "is_dir": self.is_dir,
        }
        if self.children:
            d["children"] = {
                k: v.to_dict() for k, v in sorted(self.children.items())
            }
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "MerkleNode":
        """Deserialize from dict."""
        children = {}
        for k, v in data.get("children", {}).items():
            children[k] = cls.from_dict(v)
        return cls(
            name=data["name"],
            hash=data["hash"],
            is_dir=data["is_dir"],
            children=children,
        )


@dataclass
class MerkleDiff:
    """Result of comparing two Merkle trees."""

    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted)

    @property
    def total_changes(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.modified:
            parts.append(f"{len(self.modified)} modified")
        if self.deleted:
            parts.append(f"{len(self.deleted)} deleted")
        return ", ".join(parts) if parts else "no changes"


class MerkleTree:
    """Build, compare, save, and load Merkle trees for codebases."""

    @staticmethod
    def build(
        root: Path,
        skip_dirs: Optional[Set[str]] = None,
        extensions: Optional[Set[str]] = None,
        max_file_size: int = MAX_FILE_SIZE,
    ) -> MerkleNode:
        """Build a Merkle tree from a directory.

        Walks the filesystem bottom-up, hashing file contents at the
        leaves and combining child hashes at interior nodes.

        Args:
            root: Directory to scan (typically repos/).
            skip_dirs: Directory names to skip (default: SKIP_DIRS).
            extensions: File extensions to include (default: INDEXABLE_EXTENSIONS).
            max_file_size: Skip files larger than this.

        Returns:
            Root MerkleNode of the tree.
        """
        if skip_dirs is None:
            skip_dirs = SKIP_DIRS
        if extensions is None:
            extensions = INDEXABLE_EXTENSIONS

        return MerkleTree._build_node(
            root, root.name, skip_dirs, extensions, max_file_size,
        )

    @staticmethod
    def build_from_roots(
        roots: List[Path],
        synthetic_root_name: str = "repos",
        skip_dirs: Optional[Set[str]] = None,
        extensions: Optional[Set[str]] = None,
        max_file_size: int = MAX_FILE_SIZE,
    ) -> MerkleNode:
        """Build a Merkle tree spanning a manifest-bounded set of roots.

        Used by ecosystem-bounded indexing: instead of walking the entire
        ``repos/`` folder, the indexer is handed only the repo roots that
        the ecosystem manifest enables, and they are stitched under one
        synthetic root so the existing tree/index/diff plumbing keeps
        working unchanged.

        The synthetic root's name (default ``repos``) is used purely as a
        label in serialized form; child hashing is identical to ``build``.

        Args:
            roots: Per-repo absolute paths (typically ``workspace/repos/<name>``).
                Empty list yields an empty tree, mirroring "no repos enabled".
            synthetic_root_name: Display name for the wrapper node.
            skip_dirs: Directory names to skip.
            extensions: File extensions to include.
            max_file_size: Skip files larger than this.
        """
        if skip_dirs is None:
            skip_dirs = SKIP_DIRS
        if extensions is None:
            extensions = INDEXABLE_EXTENSIONS

        children: Dict[str, MerkleNode] = {}
        # Stable child ordering by repo dir name so identical scopes hash
        # identically regardless of the manifest order.
        for repo_path in sorted(roots, key=lambda p: p.name):
            if not repo_path.exists():
                continue
            child = MerkleTree._build_node(
                repo_path, repo_path.name, skip_dirs, extensions, max_file_size,
            )
            if child.children or not child.is_dir:
                children[child.name] = child

        h = hashlib.sha256()
        for child_name in sorted(children.keys()):
            h.update(f"{child_name}:{children[child_name].hash}".encode())

        return MerkleNode(
            name=synthetic_root_name,
            hash=h.hexdigest()[:16],
            is_dir=True,
            children=children,
        )

    @staticmethod
    def _build_node(
        path: Path,
        name: str,
        skip_dirs: Set[str],
        extensions: Set[str],
        max_file_size: int,
    ) -> MerkleNode:
        """Recursively build a MerkleNode for a path."""
        if path.is_file():
            return MerkleTree._build_file_node(path, name)

        # Directory: process children
        children: Dict[str, MerkleNode] = {}

        try:
            entries = sorted(path.iterdir())
        except PermissionError:
            logger.debug("Permission denied: %s", path)
            return MerkleNode(
                name=name, hash=_empty_hash(), is_dir=True,
            )

        for entry in entries:
            entry_name = entry.name

            # Skip hidden files/dirs and excluded dirs
            if entry_name.startswith(".") and entry_name not in KNOWN_FILENAMES:
                if entry.is_dir() and entry_name in skip_dirs:
                    continue
                if entry.is_dir() and entry_name.startswith("."):
                    continue

            if entry.is_dir():
                if entry_name in skip_dirs:
                    continue
                child = MerkleTree._build_node(
                    entry, entry_name, skip_dirs, extensions, max_file_size,
                )
                # Only include non-empty directories
                if child.children or not child.is_dir:
                    children[entry_name] = child

            elif entry.is_file():
                # Check extension or known filename
                ext = entry.suffix.lower()
                if ext not in extensions and entry_name not in KNOWN_FILENAMES:
                    continue
                # Check size
                try:
                    if entry.stat().st_size > max_file_size:
                        continue
                except OSError:
                    continue

                child = MerkleTree._build_file_node(entry, entry_name)
                children[entry_name] = child

        # Compute directory hash from sorted children
        h = hashlib.sha256()
        for child_name in sorted(children.keys()):
            h.update(f"{child_name}:{children[child_name].hash}".encode())

        return MerkleNode(
            name=name,
            hash=h.hexdigest()[:16],
            is_dir=True,
            children=children,
        )

    @staticmethod
    def _build_file_node(path: Path, name: str) -> MerkleNode:
        """Build a leaf MerkleNode for a file."""
        try:
            content = path.read_bytes()
            file_hash = hashlib.sha256(content).hexdigest()[:16]
        except (OSError, PermissionError):
            file_hash = _empty_hash()

        return MerkleNode(
            name=name,
            hash=file_hash,
            is_dir=False,
        )

    @staticmethod
    def diff(old: MerkleNode, new: MerkleNode) -> MerkleDiff:
        """Compare two Merkle trees and return the differences.

        Walks both trees in parallel. When hashes match at a node,
        the entire subtree is skipped (the Merkle tree advantage).

        Returns:
            MerkleDiff with lists of added, modified, and deleted file paths.
        """
        result = MerkleDiff()
        MerkleTree._diff_recursive(old, new, "", result)
        return result

    @staticmethod
    def _diff_recursive(
        old: Optional[MerkleNode],
        new: Optional[MerkleNode],
        prefix: str,
        result: MerkleDiff,
    ) -> None:
        """Recursively diff two nodes."""
        if old is None and new is None:
            return

        # Node only in new tree → all files are added
        if old is None and new is not None:
            for path in _collect_files(new, prefix):
                result.added.append(path)
            return

        # Node only in old tree → all files are deleted
        if old is not None and new is None:
            for path in _collect_files(old, prefix):
                result.deleted.append(path)
            return

        # Both exist — compare hashes
        assert old is not None and new is not None

        # Hashes match → entire subtree is identical, skip
        if old.hash == new.hash:
            return

        # Both are files → file was modified
        if not old.is_dir and not new.is_dir:
            path = f"{prefix}{new.name}" if prefix else new.name
            result.modified.append(path)
            return

        # Type changed (file ↔ dir) — treat as delete + add
        if old.is_dir != new.is_dir:
            for path in _collect_files(old, prefix):
                result.deleted.append(path)
            for path in _collect_files(new, prefix):
                result.added.append(path)
            return

        # Both are directories with different hashes → recurse into children
        child_prefix = f"{prefix}{old.name}/" if prefix else f"{old.name}/"

        old_children = set(old.children.keys())
        new_children = set(new.children.keys())

        # Deleted children
        for name in sorted(old_children - new_children):
            for path in _collect_files(old.children[name], child_prefix):
                result.deleted.append(path)

        # Added children
        for name in sorted(new_children - old_children):
            for path in _collect_files(new.children[name], child_prefix):
                result.added.append(path)

        # Children in both → recurse (only if hashes differ)
        for name in sorted(old_children & new_children):
            old_child = old.children[name]
            new_child = new.children[name]
            if old_child.hash != new_child.hash:
                MerkleTree._diff_recursive(
                    old_child, new_child, child_prefix, result,
                )

    @staticmethod
    def save(node: MerkleNode, path: Path) -> None:
        """Save a Merkle tree to a JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "root_hash": node.hash,
            "file_count": node.file_count,
            "tree": node.to_dict(),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info(
            "Merkle tree saved: %s (%d files, root=%s)",
            path, node.file_count, node.hash,
        )

    @staticmethod
    def load(path: Path) -> MerkleNode:
        """Load a Merkle tree from a JSON file."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return MerkleNode.from_dict(data["tree"])


# ── Helpers ──────────────────────────────────────────────────────


def _empty_hash() -> str:
    """Hash for unreadable/empty content."""
    return hashlib.sha256(b"").hexdigest()[:16]


def _collect_files(node: MerkleNode, prefix: str) -> List[str]:
    """Collect all file paths under a node."""
    if not node.is_dir:
        path = f"{prefix}{node.name}" if prefix else node.name
        return [path]

    child_prefix = f"{prefix}{node.name}/" if prefix else f"{node.name}/"
    files: List[str] = []
    for child in node.children.values():
        files.extend(_collect_files(child, child_prefix))
    return files


def hash_snapshot(value: Any) -> str:
    """Hash an arbitrary JSON-serializable snapshot into a short Merkle leaf."""

    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def build_compact_merkle_root(
    family: str,
    entries: Dict[str, Any],
    *,
    updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a compact Merkle-style root for runtime families.

    This is intentionally lightweight: each named entry becomes a hashed leaf and the
    family root is the hash of the sorted `name:leaf_hash` pairs.
    """

    normalized_entries = {
        str(key): value
        for key, value in sorted((entries or {}).items())
        if value not in (None, "", [], {}, ())
    }
    child_hashes = {
        key: hash_snapshot(value)
        for key, value in normalized_entries.items()
    }
    accumulator = hashlib.sha256()
    for key, value_hash in child_hashes.items():
        accumulator.update(f"{key}:{value_hash}".encode("utf-8"))
    return {
        "family": family,
        "hash": accumulator.hexdigest()[:16],
        "item_count": len(child_hashes),
        "updated_at": updated_at,
        "child_hashes": child_hashes,
    }
