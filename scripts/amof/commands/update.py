"""Update AMOF public CLI installs without leaving pipx metadata broken."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import __version__

DEFAULT_SOURCE_URL = "https://github.com/marekhotshot/amof.git"
PACKAGE_NAME = "amof"
_STABLE_TAG_RE = re.compile(r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")


@dataclass(frozen=True)
class InstallInfo:
    method: str
    executable: str
    prefix: str
    runtime_path: str
    detail: str


def _path_has_pipx_venv(path: str | Path, *, package_name: str = PACKAGE_NAME) -> bool:
    parts = Path(path).expanduser().parts
    lowered = [part.lower() for part in parts]
    if package_name not in lowered:
        return False
    return "pipx" in lowered and "venvs" in lowered


def _source_checkout_root(runtime_path: Path | None = None) -> Path | None:
    start = (runtime_path or Path(__file__)).resolve(strict=False)
    for candidate in (start, *start.parents):
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / ".git").exists()
            and (candidate / "scripts" / "amof").is_dir()
        ):
            return candidate
    return None


def detect_install_method(
    *,
    executable: str | None = None,
    prefix: str | None = None,
    runtime_path: str | Path | None = None,
) -> InstallInfo:
    exe = executable or sys.executable
    pref = prefix or sys.prefix
    runtime = Path(runtime_path or __file__).resolve(strict=False)
    if _path_has_pipx_venv(pref) or _path_has_pipx_venv(exe):
        return InstallInfo(
            method="pipx",
            executable=exe,
            prefix=pref,
            runtime_path=str(runtime),
            detail="AMOF appears to be managed by pipx.",
        )
    checkout_root = _source_checkout_root(runtime)
    if checkout_root is not None:
        return InstallInfo(
            method="source",
            executable=exe,
            prefix=pref,
            runtime_path=str(runtime),
            detail=f"AMOF is running from source checkout: {checkout_root}",
        )
    return InstallInfo(
        method="pip",
        executable=exe,
        prefix=pref,
        runtime_path=str(runtime),
        detail="AMOF appears to be installed by pip.",
    )


def _stable_tag_key(tag: str) -> tuple[int, int, int] | None:
    match = _STABLE_TAG_RE.match(tag)
    if not match:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def parse_latest_stable_tag(ref_lines: Iterable[str]) -> str | None:
    tags: dict[str, tuple[int, int, int]] = {}
    for raw_line in ref_lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/")
        if tag.endswith("^{}"):
            tag = tag[:-3]
        key = _stable_tag_key(tag)
        if key is not None:
            tags[tag] = key
    if not tags:
        return None
    return max(tags.items(), key=lambda item: item[1])[0]


def discover_latest_stable_tag(
    source_url: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str | None, str | None]:
    completed = runner(
        ["git", "ls-remote", "--tags", source_url, "refs/tags/v*"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout or "").strip()
        return None, output or "git ls-remote failed"
    latest = parse_latest_stable_tag((completed.stdout or "").splitlines())
    if latest is None:
        return None, "no stable AMOF release tags were found"
    return latest, None


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


def _display_current_and_target(target: str) -> None:
    print(f"[update] Current version: v{__version__}")
    print(f"[update] Target version:  {target}")


def _run_update_command(
    command: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    verbose: bool = False,
) -> int:
    completed = runner(command, capture_output=True, text=True, check=False)
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        if output.strip():
            sys.stderr.write(output.strip() + "\n")
        else:
            sys.stderr.write("[update] Update command failed.\n")
        return completed.returncode or 1
    if verbose and output.strip():
        print(output.strip())
    return 0


def cmd_update(
    args: Any,
    *,
    install_info: InstallInfo | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    source_url = str(getattr(args, "source_url", None) or DEFAULT_SOURCE_URL).strip()
    explicit_version = str(getattr(args, "target_version", None) or "").strip()
    check_only = bool(getattr(args, "check", False))
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    verbose = bool(getattr(args, "verbose", False))

    if explicit_version:
        target = explicit_version
    else:
        target, error = discover_latest_stable_tag(source_url, runner=runner)
        if error:
            sys.stderr.write(f"[update] Could not discover latest stable release: {error}\n")
            sys.stderr.write("[update] Try an explicit target, for example: amof update --version v2.1.0\n")
            return 1
        assert target is not None

    if not target.startswith("v"):
        target = f"v{target}"

    _display_current_and_target(target)
    if check_only:
        return 0
    if target == f"v{__version__}":
        print("[update] AMOF is already up to date.")
        return 0

    info = install_info or detect_install_method()
    print(f"[update] Install method: {info.method}")
    print(f"[update] {info.detail}")

    package_spec = f"git+{source_url}@{target}"
    if info.method == "source":
        sys.stderr.write(
            "[update] This is a source checkout install. "
            "Use git fetch/checkout or rerun ./scripts/install-amof.sh.\n"
        )
        return 1
    if info.method == "pipx":
        pipx = which("pipx")
        if not pipx:
            sys.stderr.write("[update] This AMOF install appears to be managed by pipx, but pipx is not on PATH.\n")
            sys.stderr.write(f"[update] Install pipx, then run: pipx install --force {package_spec!r}\n")
            return 1
        command = [pipx, "install", "--force", package_spec]
    else:
        command = [info.executable, "-m", "pip", "install", "--force-reinstall", package_spec]

    print("[update] Command:")
    print("  " + " ".join(command))
    if dry_run:
        print("[update] Dry run only; no changes made.")
        return 0
    if not yes and not _confirm("Proceed with AMOF update?"):
        print("[update] Cancelled.")
        return 1
    return _run_update_command(command, runner=runner, verbose=verbose)


__all__ = [
    "DEFAULT_SOURCE_URL",
    "InstallInfo",
    "cmd_update",
    "detect_install_method",
    "discover_latest_stable_tag",
    "parse_latest_stable_tag",
]
