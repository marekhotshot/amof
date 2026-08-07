"""Fail-closed path safety for trust authority storage."""

from __future__ import annotations

import os
from pathlib import Path

from ..trust_layer import TrustIntegrityError


def assert_not_symlink(path: Path, *, what: str) -> Path:
    """Reject symlink paths (and symlink parents of the leaf)."""
    resolved_leaf = Path(path)
    if resolved_leaf.is_symlink():
        raise TrustIntegrityError(
            f"{what} must not be a symlink: {resolved_leaf}",
            code="unsafe_symlink",
        )
    # Walk parents up to filesystem root; do not follow the leaf if missing.
    current = resolved_leaf.parent if not resolved_leaf.exists() else resolved_leaf
    seen: set[Path] = set()
    while True:
        if current in seen:
            break
        seen.add(current)
        if current.is_symlink():
            raise TrustIntegrityError(
                f"{what} path traverses a symlink: {current}",
                code="unsafe_symlink",
            )
        if current.parent == current:
            break
        current = current.parent
    return resolved_leaf


def assert_private_mode(path: Path, *, what: str) -> None:
    """Require owner-only access bits (no group/other)."""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise TrustIntegrityError(
            f"{what} unreadable: {path}",
            code="missing_key",
        ) from exc
    if mode & 0o077:
        raise TrustIntegrityError(
            f"{what} permissions too open ({oct(mode)}); require 0o600/0o700",
            code="insecure_permissions",
        )


def write_bytes_exclusive(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Atomic create-only write; refuse overwrite."""
    assert_not_symlink(path, what=str(path.name))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, mode)
    try:
        os.write(fd, data)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
        os.chmod(path, mode)
