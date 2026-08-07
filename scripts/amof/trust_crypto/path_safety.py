"""Fail-closed path safety for trust authority storage and hermetic packages."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path

from ..trust_layer import TrustIntegrityError

# BL-4: O_NOFOLLOW / O_CLOEXEC are POSIX; when absent, lstat pre-checks remain primary.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_HARDLINK_NLINK_DETECTABLE = True  # POSIX st_nlink; some network FS always report 1.


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


# --- BL-4: hermetic export-package boundary (no symlink / traversal escape) ---


def assert_safe_package_member_name(name: str, *, what: str = "package member") -> str:
    """Reject absolute paths, ``..``, separators, and NUL in a single member name."""
    if not isinstance(name, str) or name == "":
        raise TrustIntegrityError(f"{what}: empty name", code="unsafe_path")
    if name in (".", ".."):
        raise TrustIntegrityError(f"{what}: reserved name {name!r}", code="unsafe_path")
    if "\x00" in name:
        raise TrustIntegrityError(f"{what}: NUL byte in name", code="unsafe_path")
    if name.startswith("/") or name.startswith("\\"):
        raise TrustIntegrityError(f"{what}: absolute path {name!r}", code="unsafe_path")
    # Windows drive / UNC-style absolute forms.
    if len(name) >= 2 and name[1] == ":":
        raise TrustIntegrityError(f"{what}: absolute path {name!r}", code="unsafe_path")
    if "/" in name or "\\" in name:
        raise TrustIntegrityError(
            f"{what}: path separator in member name {name!r}",
            code="unsafe_path",
        )
    return name


def open_nofollow(path: Path | str, *, flags: int = os.O_RDONLY) -> int:
    """Open without following a final-component symlink when O_NOFOLLOW exists.

    BL-4: Prefer no-follow open semantics for package reads. When the platform
    lacks O_NOFOLLOW, callers must still run lstat-based hermetic checks first.
    """
    target = Path(path)
    open_flags = int(flags) | _O_NOFOLLOW | _O_CLOEXEC
    try:
        return os.open(target, open_flags)
    except OSError as exc:
        # Linux: ELOOP when final component is a symlink under O_NOFOLLOW.
        if _O_NOFOLLOW and exc.errno in (errno.ELOOP, getattr(errno, "EFTYPE", -1)):
            raise TrustIntegrityError(
                f"refusing symlink open: {target}",
                code="unsafe_symlink",
            ) from exc
        raise


def read_bytes_nofollow(path: Path | str) -> bytes:
    """Read entire file via no-follow open (BL-4)."""
    fd = open_nofollow(path)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def sha256_file_nofollow(path: Path | str) -> str:
    """SHA-256 of file contents without following a leaf symlink (BL-4)."""
    fd = open_nofollow(path)
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def assert_hermetic_package_member(
    package_root: Path,
    member_path: Path,
    *,
    what: str | None = None,
    seen_inodes: dict[tuple[int, int], str] | None = None,
) -> os.stat_result:
    """Ensure ``member_path`` is a hermetic regular file under ``package_root``.

    FAIL_CLOSED on symlinks, non-regular types, hardlinks (when ``st_nlink`` is
    trustworthy), path escape, and duplicate ``(st_dev, st_ino)`` targets.
    """
    label = what or Path(member_path).name
    root = Path(package_root)
    member = Path(member_path)
    assert_safe_package_member_name(member.name, what=label)

    try:
        st = member.lstat()
    except OSError as exc:
        raise TrustIntegrityError(
            f"{label} unreadable: {member}",
            code="missing_file",
        ) from exc

    if stat.S_ISLNK(st.st_mode):
        raise TrustIntegrityError(
            f"{label} must not be a symlink: {member}",
            code="unsafe_symlink",
        )
    if not stat.S_ISREG(st.st_mode):
        raise TrustIntegrityError(
            f"{label} must be a regular file (not dir/fifo/device): {member}",
            code="unsafe_path",
        )

    # Practical hardlink detection (POSIX). Some network filesystems always
    # report st_nlink=1 even when hardlinks exist — documented OS limitation.
    if _HARDLINK_NLINK_DETECTABLE and int(getattr(st, "st_nlink", 1) or 1) > 1:
        raise TrustIntegrityError(
            f"{label} must not be hardlinked (nlink={st.st_nlink}): {member}",
            code="unsafe_hardlink",
        )

    try:
        root_real = root.resolve(strict=True)
        parent_real = member.parent.resolve(strict=True)
    except OSError as exc:
        raise TrustIntegrityError(
            f"{label} path not resolvable under package root",
            code="unsafe_path",
        ) from exc

    # Flat export packages: member must be a direct child of the package root.
    # Parent resolve also catches symlink parents that jump outside the root.
    if parent_real != root_real:
        raise TrustIntegrityError(
            f"{label} escapes package root or is not a direct member: {member}",
            code="unsafe_path",
        )

    if seen_inodes is not None:
        key = (int(st.st_dev), int(st.st_ino))
        prior = seen_inodes.get(key)
        if prior is not None and prior != member.name:
            raise TrustIntegrityError(
                f"duplicate canonical target: {prior!r} and {member.name!r}",
                code="unsafe_hardlink",
            )
        seen_inodes[key] = member.name

    return st


def assert_hermetic_export_package(package_root: Path | str) -> set[str]:
    """Enumerate an export package fail-closed without following symlinks.

    BL-4: package self-containment / hermeticity boundary. Enumeration uses
    ``os.scandir`` + ``lstat`` semantics (``follow_symlinks=False``).
    """
    root = Path(package_root)
    # Do not follow a symlink package root via is_dir()/iterdir surprises.
    if root.is_symlink():
        raise TrustIntegrityError(
            f"export package root must not be a symlink: {root}",
            code="unsafe_symlink",
        )
    if not root.is_dir():
        raise TrustIntegrityError(f"export path missing: {root}", code="missing_export")

    names: set[str] = set()
    seen_inodes: dict[tuple[int, int], str] = {}
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise TrustIntegrityError(
            f"export package unreadable: {root}",
            code="missing_export",
        ) from exc

    for entry in entries:
        name = entry.name
        assert_safe_package_member_name(name)
        member = root / name
        if entry.is_symlink():
            raise TrustIntegrityError(
                f"package member must not be a symlink: {name}",
                code="unsafe_symlink",
            )
        if entry.is_dir(follow_symlinks=False):
            raise TrustIntegrityError(
                f"extra directory not allowed in export package: {name}",
                code="extra_file",
            )
        if not entry.is_file(follow_symlinks=False):
            raise TrustIntegrityError(
                f"unexpected non-regular package member: {name}",
                code="unsafe_path",
            )
        assert_hermetic_package_member(
            root,
            member,
            what=name,
            seen_inodes=seen_inodes,
        )
        names.add(name)
    return names
