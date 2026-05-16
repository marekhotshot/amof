"""Release command -- automate version bumps, changelog, docs, commit, tag, push.

Workflow:
  amof release status               # show version info
  amof release patch --alpha        # v1.0.3-alpha.1
  amof release patch --beta         # v1.0.3-beta.1
  amof release patch                # v1.0.3
  amof release minor --alpha        # v1.1.0-alpha.1
  amof release major                # v2.0.0
  amof release promote --beta       # current alpha -> beta.1
  amof release promote              # current pre-release -> stable
  amof release log                  # show release history

Automations:
  1. Parse last git tag to determine next version
  2. Update __version__ in version files
  3. Update CHANGELOG.md header
  4. Update README.md "Current status" line
  5. Write release record to releases/<tag>.json
  6. Commit with "chore(release): vX.Y.Z..."
  7. Tag with "vX.Y.Z..."
  8. Push (with guardrail confirmation since git push is sensitive)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── SemVer parsing ────────────────────────────────────────────

# Matches: v1.2.3, v1.2.3-alpha, v1.2.3-alpha.2, v1.2.3-beta.1, v1.2.3-rc.3
_TAG_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[a-zA-Z]+)(?:\.(?P<pre_num>\d+))?)?$"
)

_PRE_STAGES = ["alpha", "beta", "rc"]

_VERSION_FILES = [
    "scripts/amof/__init__.py",
    "scripts/amof/orchestrator/__init__.py",
]


@dataclass
class Version:
    """Parsed semantic version with optional pre-release suffix."""

    major: int
    minor: int
    patch: int
    pre: Optional[str] = None    # "alpha", "beta", "rc", or None (stable)
    pre_num: int = 1             # e.g. alpha.2 -> pre_num=2

    @classmethod
    def parse(cls, tag: str) -> Optional["Version"]:
        """Parse a version string (with or without 'v' prefix)."""
        m = _TAG_RE.match(tag.strip())
        if not m:
            return None
        return cls(
            major=int(m.group("major")),
            minor=int(m.group("minor")),
            patch=int(m.group("patch")),
            pre=m.group("pre"),
            pre_num=int(m.group("pre_num")) if m.group("pre_num") else 1,
        )

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre:
            return f"{base}-{self.pre}.{self.pre_num}"
        return base

    @property
    def tag(self) -> str:
        return f"v{self}"

    @property
    def is_stable(self) -> bool:
        return self.pre is None

    def bump(self, part: str, pre: Optional[str] = None) -> "Version":
        """Create a new bumped version.

        Args:
            part: "major", "minor", or "patch"
            pre: Pre-release stage ("alpha", "beta", "rc") or None for stable
        """
        if part == "major":
            v = Version(self.major + 1, 0, 0)
        elif part == "minor":
            v = Version(self.major, self.minor + 1, 0)
        elif part == "patch":
            if self.pre and pre:
                v = Version(self.major, self.minor, self.patch)
            else:
                v = Version(self.major, self.minor, self.patch + 1)
        else:
            raise ValueError(f"Unknown bump part: {part}")

        if pre:
            v.pre = pre
            v.pre_num = 1
        return v

    def promote(self, target: Optional[str] = None) -> "Version":
        """Promote pre-release to next stage or stable.

        None -> stable
        "beta" -> beta.1 (from alpha)
        "rc" -> rc.1 (from alpha/beta)
        """
        if not self.pre:
            raise ValueError("Cannot promote a stable version")

        if target is None:
            return Version(self.major, self.minor, self.patch)

        if target not in _PRE_STAGES:
            raise ValueError(f"Unknown pre-release stage: {target}")

        current_idx = _PRE_STAGES.index(self.pre) if self.pre in _PRE_STAGES else -1
        target_idx = _PRE_STAGES.index(target)

        if target_idx < current_idx:
            raise ValueError(
                f"Cannot promote {self.pre} to {target} "
                f"(must be a later stage: {' -> '.join(_PRE_STAGES)} -> stable)"
            )

        if target_idx == current_idx:
            return Version(self.major, self.minor, self.patch, self.pre, self.pre_num + 1)

        return Version(self.major, self.minor, self.patch, target, 1)

    def next_pre(self) -> "Version":
        """Increment pre-release number (alpha.1 -> alpha.2)."""
        if not self.pre:
            raise ValueError("Not a pre-release version")
        return Version(self.major, self.minor, self.patch, self.pre, self.pre_num + 1)


# ── Git helpers ───────────────────────────────────────────────


def _run(cmd: List[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a git command."""
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=30, check=check)


def _get_latest_tag() -> Optional[str]:
    """Get the latest semver tag from git."""
    try:
        result = _run(["git", "tag", "--sort=-v:refname"])
        for line in result.stdout.strip().splitlines():
            tag = line.strip()
            if _TAG_RE.match(tag.lstrip("v")):
                return tag
        return None
    except Exception:
        return None


def _get_commits_since_tag(tag: str) -> List[str]:
    """Get commit subjects since a tag."""
    try:
        result = _run(["git", "log", f"{tag}..HEAD", "--pretty=format:%s"])
        return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
    except Exception:
        return []


def _get_current_branch() -> str:
    """Get current git branch name."""
    result = _run(["git", "branch", "--show-current"])
    return result.stdout.strip()


def _has_uncommitted_changes() -> bool:
    """Check for uncommitted changes."""
    result = _run(["git", "status", "--porcelain"])
    return bool(result.stdout.strip())


def _get_git_sha() -> str:
    """Get current HEAD commit SHA."""
    try:
        result = _run(["git", "rev-parse", "HEAD"])
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _get_git_author() -> str:
    """Get git author name."""
    try:
        result = _run(["git", "config", "user.name"])
        return result.stdout.strip()
    except Exception:
        return "unknown"


# ── File updaters ─────────────────────────────────────────────


def _update_version_files(new_version: str, workspace_root: Path) -> bool:
    """Update __version__ in all version files."""
    any_updated = False
    for rel_path in _VERSION_FILES:
        full_path = workspace_root / rel_path
        if not full_path.exists():
            continue
        text = full_path.read_text(encoding="utf-8")
        new_text = re.sub(
            r'__version__\s*=\s*"[^"]*"',
            f'__version__ = "{new_version}"',
            text,
        )
        if new_text != text:
            full_path.write_text(new_text, encoding="utf-8")
            any_updated = True
    return any_updated


def _update_changelog_header(new_version: str, workspace_root: Path) -> bool:
    """Insert a new version header above the existing one in CHANGELOG.md."""
    changelog_path = workspace_root / "CHANGELOG.md"
    if not changelog_path.exists():
        return False

    text = changelog_path.read_text(encoding="utf-8")
    today = time.strftime("%Y-%m-%d")
    new_header = f"## [{new_version}] - {today}"

    header_re = re.compile(r"^## \[[^\]]+\] - \d{4}-\d{2}-\d{2}", re.MULTILINE)
    match = header_re.search(text)

    if match:
        new_text = text[:match.start()] + new_header + "\n\n" + text[match.start():]
    else:
        new_text = text.replace(
            "# Changelog\n",
            f"# Changelog\n\n{new_header}\n",
        )

    if new_text == text:
        return False

    changelog_path.write_text(new_text, encoding="utf-8")
    return True


def _update_readme_status(new_version: str, workspace_root: Path) -> bool:
    """Update the 'Current status' line in README.md."""
    readme_path = workspace_root / "README.md"
    if not readme_path.exists():
        return False

    text = readme_path.read_text(encoding="utf-8")
    status_re = re.compile(r"\*\*Current status:\*\*\s*v[\d][^\n]*")
    match = status_re.search(text)

    if not match:
        return False

    old_line = match.group()
    desc_match = re.search(r"v[\d\w.\-]+\.\s*(.*)", old_line)
    description = desc_match.group(1) if desc_match else ""

    new_line = f"**Current status:** v{new_version}. {description}".rstrip()
    new_text = text[:match.start()] + new_line + text[match.end():]

    if new_text == text:
        return False

    readme_path.write_text(new_text, encoding="utf-8")
    return True


def _stage_release_files(files: List[str], workspace_root: Path) -> None:
    """Stage only the specific files modified by the release command."""
    for f in files:
        p = workspace_root / f
        if p.exists():
            _run(["git", "add", str(p)])


# ── Validation ────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Results from pre-release validation checks."""

    passed: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def print_report(self) -> None:
        for msg in self.passed:
            print(f"    \u2713 {msg}")
        for msg in self.warnings:
            print(f"    \u26a0 {msg}")
        for msg in self.errors:
            print(f"    \u2717 {msg}")


def _validate_release(
    current: Version,
    next_ver: Version,
    workspace_root: Path,
    strict: bool = False,
) -> ValidationResult:
    """Run validation gates appropriate for the target version stage."""
    result = ValidationResult()
    stage = next_ver.pre or "stable"

    # 1. Uncommitted changes
    if _has_uncommitted_changes():
        msg = "Uncommitted changes in working tree"
        if stage == "alpha" and not strict:
            result.warnings.append(msg)
        else:
            result.errors.append(msg)
    else:
        result.passed.append("Working tree clean")

    # 2. Branch guard (stable only from main/release/*)
    branch = _get_current_branch()
    if stage == "stable":
        if branch not in ("main", "master") and not branch.startswith("release/"):
            msg = f"Stable release from '{branch}' -- expected main or release/*"
            if strict:
                result.errors.append(msg)
            else:
                result.warnings.append(msg)
        else:
            result.passed.append(f"Branch '{branch}' is appropriate for stable")
    else:
        result.passed.append(f"Branch: {branch}")

    # 3. Version files in sync
    init_versions = {}
    for rel_path in _VERSION_FILES:
        full_path = workspace_root / rel_path
        if full_path.exists():
            text = full_path.read_text(encoding="utf-8")
            m = re.search(r'__version__\s*=\s*"([^"]*)"', text)
            if m:
                init_versions[rel_path] = m.group(1)

    unique_versions = set(init_versions.values())
    if len(unique_versions) > 1:
        result.warnings.append(
            f"Version drift: {', '.join(f'{k}={v}' for k, v in init_versions.items())}"
        )
    elif unique_versions:
        result.passed.append(f"Version files in sync ({unique_versions.pop()})")

    # 4. CHANGELOG content
    changelog = workspace_root / "CHANGELOG.md"
    if changelog.exists():
        result.passed.append("CHANGELOG.md exists")
    elif stage == "stable":
        result.errors.append("CHANGELOG.md not found (required for stable)")
    else:
        result.warnings.append("CHANGELOG.md not found")

    if strict:
        result.errors.extend(result.warnings)
        result.warnings = []

    return result


# ── Audit trail ───────────────────────────────────────────────


def _get_tag_commit_timestamp_iso(tag: str) -> Optional[str]:
    """Return the commit timestamp of a tag as ISO-8601 UTC, or None."""
    if not tag:
        return None
    result = _run(["git", "log", "-1", "--format=%cI", tag], check=False)
    if result.returncode != 0:
        return None
    stamp = (result.stdout or "").strip()
    return stamp or None


def _collect_runpod_activity_for_release(
    previous_tag: str, workspace_root: Path
) -> Optional[Dict[str, Any]]:
    """Best-effort RunPod activity collector, scoped to this release window.

    T6: surfaces pod lifecycle events (create/stop/delete/health_check/...)
    recorded in the workspace JSONL audit file between the previous tag
    commit and now. Fails quiet when RunPod is not configured or the
    audit file is missing; the release still records cleanly.
    """

    try:
        # Import lazily so `amof release` does not require requests for
        # release flows that never touch RunPod.
        from amof.api.services.runpod import (
            collect_runpod_activity,
            load_runpod_settings,
        )
    except Exception:  # noqa: BLE001
        return None
    since = _get_tag_commit_timestamp_iso(previous_tag)
    try:
        settings = load_runpod_settings()
    except Exception:  # noqa: BLE001
        settings = None
    audit_path: Optional[Path] = None
    if settings is None:
        audit_path = workspace_root / ".amof" / "audit" / "runpod-pods.jsonl"
    try:
        return collect_runpod_activity(
            since_iso=since, audit_path=audit_path, settings=settings
        )
    except Exception:  # noqa: BLE001
        return None


def _build_release_record(
    current: Version,
    next_ver: Version,
    bump: Optional[str],
    commits: List[str],
    updated_files: List[str],
    origin: str = "cli",
    dry_run: bool = False,
    pushed: bool = False,
    workspace_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build a release audit record."""
    action = "promote" if bump == "promote" else "bump"
    record: Dict[str, Any] = {
        "schema_version": 1,
        "version": str(next_ver),
        "tag": next_ver.tag,
        "previous_tag": current.tag,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "bump_part": bump if action == "bump" else None,
        "stage": next_ver.pre,
        "stage_num": next_ver.pre_num if next_ver.pre else None,
        "branch": _get_current_branch(),
        "commit": _get_git_sha(),
        "author": _get_git_author(),
        "commits_since_previous": len(commits),
        "commit_subjects": commits[:50],
        "files_updated": updated_files,
        "origin": origin,
        "dry_run": dry_run,
        "pushed": pushed,
    }
    if workspace_root is not None:
        runpod_activity = _collect_runpod_activity_for_release(
            current.tag, workspace_root
        )
        if runpod_activity is not None:
            record["runpod_activity"] = runpod_activity
    return record


def _write_release_record(record: Dict[str, Any], workspace_root: Path) -> str:
    """Write release record to releases/<tag>.json. Returns relative path."""
    releases_dir = workspace_root / "releases"
    releases_dir.mkdir(exist_ok=True)
    filename = f"{record['tag']}.json"
    filepath = releases_dir / filename
    filepath.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return f"releases/{filename}"


def _build_tag_annotation(
    current: Version,
    next_ver: Version,
    commits: List[str],
    message: Optional[str] = None,
) -> str:
    """Build enriched git tag annotation."""
    lines = [message or f"v{next_ver}", ""]
    lines.append(f"Previous: {current.tag}")
    lines.append(f"Branch: {_get_current_branch()}")
    lines.append(f"Commits: {len(commits)} since {current.tag}")
    if commits:
        lines.append("")
        lines.append("Changes:")
        for c in commits[:20]:
            lines.append(f"- {c}")
        if len(commits) > 20:
            lines.append(f"... and {len(commits) - 20} more")
    return "\n".join(lines)


# ── Commands ──────────────────────────────────────────────────


def cmd_release_status() -> int:
    """Show current release status and next version options."""
    workspace_root = Path.cwd()

    latest_tag = _get_latest_tag()
    if not latest_tag:
        print("\n  No semver tags found. Initialize with:")
        print("    git tag -a v0.1.0 -m 'initial'\n")
        return 1

    current = Version.parse(latest_tag)
    if not current:
        print(f"\n  Cannot parse tag '{latest_tag}' as semver\n")
        return 1

    branch = _get_current_branch()
    commits = _get_commits_since_tag(latest_tag)
    dirty = _has_uncommitted_changes()

    init_versions = {}
    for rel_path in _VERSION_FILES:
        full_path = workspace_root / rel_path
        if full_path.exists():
            text = full_path.read_text(encoding="utf-8")
            m = re.search(r'__version__\s*=\s*"([^"]*)"', text)
            if m:
                init_versions[rel_path] = m.group(1)

    print(f"\n  AMOF Release Status")
    print(f"  {'─' * 40}")
    print()
    print(f"  Current version:  {current.tag}")

    for path, ver in init_versions.items():
        mark = "\u2713 matches" if ver == str(current) else f"\u2717 DRIFT (got {ver})"
        short = path.split("/")[-2] + "/" + path.split("/")[-1]
        print(f"  {short:<32} {mark}")

    print(f"  Branch:           {branch}")
    stage = current.pre or "stable"
    print(f"  Stage:            {stage}" + (" (pre-release)" if current.pre else ""))

    print()
    print(f"  Since {current.tag}:")
    print(f"    Commits:        {len(commits)}")
    if commits:
        print()
        print(f"  Recent commits:")
        for c in commits[:8]:
            print(f"    - {c}")
        if len(commits) > 8:
            print(f"    ... and {len(commits) - 8} more")

    print()
    print(f"  Next versions:")
    if current.pre:
        next_pre = current.next_pre()
        print(f"    {next_pre.tag:<20} amof release promote --{current.pre}")
        idx = _PRE_STAGES.index(current.pre) if current.pre in _PRE_STAGES else -1
        for i in range(idx + 1, len(_PRE_STAGES)):
            ns = _PRE_STAGES[i]
            promoted = current.promote(ns)
            print(f"    {promoted.tag:<20} amof release promote --{ns}")
        stable = current.promote()
        print(f"    {stable.tag:<20} amof release promote")
    else:
        pa = current.bump("patch", "alpha")
        print(f"    {pa.tag:<20} amof release patch --alpha")
        ps = current.bump("patch")
        print(f"    {ps.tag:<20} amof release patch")
        mi = current.bump("minor", "alpha")
        print(f"    {mi.tag:<20} amof release minor --alpha")

    print()
    print(f"  Readiness:")
    if dirty:
        print(f"    \u26a0  Uncommitted changes in working tree")
    else:
        print(f"    \u2713  No uncommitted changes")

    unique_versions = set(init_versions.values())
    if len(unique_versions) > 1:
        print(f"    \u2717  Version drift detected across version files")
    elif unique_versions and str(current) not in unique_versions:
        print(f"    \u26a0  __version__ ({unique_versions.pop()}) does not match tag ({current.tag})")
    elif unique_versions:
        print(f"    \u2713  Version files match latest tag")

    if len(commits) > 0:
        print(f"    \u26a0  {len(commits)} commit(s) since last release")
    else:
        print(f"    \u2713  No unreleased commits")
    print()

    return 0


def cmd_release_log() -> int:
    """Show release history from releases/*.json files."""
    workspace_root = Path.cwd()
    releases_dir = workspace_root / "releases"

    if not releases_dir.exists() or not any(releases_dir.glob("*.json")):
        # Fall back to git tags
        print("\n  No release records found in releases/.")
        print("  Showing git tags instead:\n")
        try:
            result = _run(["git", "tag", "--sort=-v:refname", "-n1"])
            tags = result.stdout.strip()
            if tags:
                print(f"  {'TAG':<24} {'MESSAGE'}")
                print(f"  {'─' * 50}")
                for line in tags.splitlines()[:20]:
                    parts = line.strip().split(None, 1)
                    tag = parts[0] if parts else ""
                    msg = parts[1] if len(parts) > 1 else ""
                    if _TAG_RE.match(tag.lstrip("v")):
                        print(f"  {tag:<24} {msg}")
            else:
                print("  No semver tags found.")
        except Exception:
            print("  Could not read git tags.")
        print()
        return 0

    records = []
    for f in sorted(releases_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            records.append(data)
        except (json.JSONDecodeError, OSError):
            continue

    if not records:
        print("\n  No valid release records found.\n")
        return 0

    print(f"\n  AMOF Release History")
    print(f"  {'─' * 72}")
    print(f"  {'TAG':<24} {'DATE':<12} {'ACTION':<20} {'COMMITS':>7}  {'ORIGIN'}")
    print(f"  {'─' * 72}")

    for r in records[:30]:
        tag = r.get("tag", "?")
        ts = r.get("timestamp", "")[:10]
        action = r.get("action", "?")
        stage = r.get("stage")
        if action == "promote" and stage:
            action = f"promote \u2192 {stage}"
        elif action == "promote":
            action = "promote \u2192 stable"
        elif action == "bump":
            part = r.get("bump_part", "")
            pre = f" ({stage})" if stage else ""
            action = f"bump {part}{pre}"
        commits = r.get("commits_since_previous", 0)
        origin = r.get("origin", "?")
        print(f"  {tag:<24} {ts:<12} {action:<20} {commits:>7}  {origin}")

    print()
    return 0


def cmd_release(
    bump: Optional[str] = None,
    pre: Optional[str] = None,
    promote_target: Optional[str] = None,
    message: Optional[str] = None,
    push: bool = True,
    dry_run: bool = False,
    yes: bool = False,
    skip_validation: bool = False,
    strict: bool = False,
) -> int:
    """Execute the release workflow.

    Args:
        bump: Version part to bump: "major", "minor", "patch", or None for promote.
        pre: Pre-release stage: "alpha", "beta", "rc", or None for stable.
        promote_target: Target stage for promote (None = stable, "beta", "rc").
        message: Optional tag annotation message.
        push: Whether to push after tagging.
        dry_run: Show what would happen without making changes.
        yes: Skip confirmation prompt.
        skip_validation: Bypass all validation checks.
        strict: Treat warnings as errors.

    Returns:
        Exit code (0 = success).
    """
    workspace_root = Path.cwd()

    # 1. Get current version from latest tag
    latest_tag = _get_latest_tag()
    if not latest_tag:
        sys.stderr.write("[release] No semver tags found. Create an initial tag first:\n")
        sys.stderr.write("  git tag -a v0.1.0 -m 'initial'\n")
        return 1

    current = Version.parse(latest_tag)
    if not current:
        sys.stderr.write(f"[release] Cannot parse tag '{latest_tag}' as semver\n")
        return 1

    # 2. Calculate next version
    if bump == "promote":
        if promote_target == "alpha":
            sys.stderr.write("[release] Cannot promote to alpha (it's the first pre-release stage).\n")
            sys.stderr.write("  Use 'amof release patch --alpha' to start a new alpha cycle.\n")
            return 1
        try:
            next_ver = current.promote(promote_target)
        except ValueError as e:
            sys.stderr.write(f"[release] {e}\n")
            return 1
    elif bump in ("major", "minor", "patch"):
        if pre and current.pre == pre and bump == "patch":
            next_ver = current.next_pre()
        else:
            next_ver = current.bump(bump, pre)
    else:
        sys.stderr.write("[release] Specify bump part: major, minor, patch, or promote\n")
        return 1

    # 3. Gather commit log since last tag
    commits = _get_commits_since_tag(latest_tag)

    # 4. Validate
    if not skip_validation:
        vresult = _validate_release(current, next_ver, workspace_root, strict=strict)
        if not vresult.ok or vresult.warnings:
            print("\n  Pre-release validation:")
            vresult.print_report()
            print()
        if not vresult.ok:
            sys.stderr.write("  Validation failed. Use --skip-validation to override.\n\n")
            return 1

    # 5. Display plan
    print(f"\n  Current:  {current.tag}")
    print(f"  Next:     {next_ver.tag}")
    print(f"  Branch:   {_get_current_branch()}")
    if commits:
        print(f"  Commits:  {len(commits)} since {latest_tag}")
        for c in commits[:10]:
            print(f"    - {c}")
        if len(commits) > 10:
            print(f"    ... and {len(commits) - 10} more")
    else:
        print(f"  Commits:  0 since {latest_tag} (tag-only release)")

    print()
    print("  Will update:")
    for vf in _VERSION_FILES:
        if (workspace_root / vf).exists():
            print(f"    {vf:<40} \u2192  __version__ = \"{next_ver}\"")
    print(f"    {'CHANGELOG.md':<40} \u2192  ## [{next_ver}] - {time.strftime('%Y-%m-%d')}")
    print(f"    {'README.md':<40} \u2192  Current status: v{next_ver}")
    print(f"    {'releases/':<40} \u2192  {next_ver.tag}.json")
    print(f"    {'git commit + tag':<40} \u2192  {next_ver.tag}")
    if push:
        print(f"    {'git push + tags':<40} \u2192  origin")
    print()

    if dry_run:
        print("  [dry-run] No changes made.")
        return 0

    # 6. Confirm
    if not yes:
        try:
            choice = input("  Proceed? [Y/n] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if choice not in ("", "y", "yes"):
            print("  Aborted.")
            return 0

    # 7. Update files
    updated: List[str] = []
    if _update_version_files(str(next_ver), workspace_root):
        updated.extend(f for f in _VERSION_FILES if (workspace_root / f).exists())
    if _update_changelog_header(str(next_ver), workspace_root):
        updated.append("CHANGELOG.md")
    if _update_readme_status(str(next_ver), workspace_root):
        updated.append("README.md")

    if updated:
        print(f"  Updated: {', '.join(updated)}")
    else:
        print("  No file changes needed (version strings already correct)")

    # 8. Write release record
    record = _build_release_record(
        current, next_ver, bump, commits, updated, origin="cli", pushed=push,
        workspace_root=workspace_root,
    )
    record_path = _write_release_record(record, workspace_root)
    updated.append(record_path)
    print(f"  Recorded: {record_path}")

    # 9. Commit
    tag_msg = _build_tag_annotation(current, next_ver, commits, message)
    _stage_release_files(updated, workspace_root)

    if _has_uncommitted_changes():
        commit_msg = f"chore(release): {next_ver.tag}"
        result = _run(["git", "commit", "-m", commit_msg], check=False)
        if result.returncode != 0:
            sys.stderr.write(f"  Commit failed: {result.stderr}\n")
            return 1
        print(f"  Committed: {commit_msg}")
    else:
        print("  No changes to commit (tag-only release)")

    # 10. Tag
    result = _run(["git", "tag", "-a", next_ver.tag, "-m", tag_msg], check=False)
    if result.returncode != 0:
        sys.stderr.write(f"  Tag failed: {result.stderr}\n")
        return 1
    print(f"  Tagged:    {next_ver.tag}")

    # 11. Push
    if push:
        branch = _get_current_branch()
        result = _run(["git", "push", "origin", branch, "--tags"], check=False)
        if result.returncode != 0:
            sys.stderr.write(f"  Push failed: {result.stderr}\n")
            sys.stderr.write("  Tag was created locally. Push manually: git push origin --tags\n")
            return 1
        print(f"  Pushed:    {branch} + tags to origin")

    print(f"\n  Release {next_ver.tag} complete.\n")
    return 0


def release_from_agent(
    workspace_root: Path,
    bump: str = "patch",
    pre: str = "alpha",
) -> Optional[str]:
    """Non-interactive release for agent post-run menu.

    Returns the new tag string, or None on failure.
    """
    latest_tag = _get_latest_tag()
    if not latest_tag:
        return None

    current = Version.parse(latest_tag)
    if not current:
        return None

    if pre and current.pre == pre and bump == "patch":
        next_ver = current.next_pre()
    else:
        next_ver = current.bump(bump, pre)

    commits = _get_commits_since_tag(latest_tag)

    updated: List[str] = []
    if _update_version_files(str(next_ver), workspace_root):
        updated.extend(f for f in _VERSION_FILES if (workspace_root / f).exists())
    if _update_changelog_header(str(next_ver), workspace_root):
        updated.append("CHANGELOG.md")
    if _update_readme_status(str(next_ver), workspace_root):
        updated.append("README.md")

    record = _build_release_record(
        current, next_ver, bump, commits, updated, origin="agent",
        workspace_root=workspace_root,
    )
    record_path = _write_release_record(record, workspace_root)
    updated.append(record_path)

    _stage_release_files(updated, workspace_root)

    if _has_uncommitted_changes():
        _run(["git", "commit", "-m", f"chore(release): {next_ver.tag}"], check=False)

    tag_msg = _build_tag_annotation(current, next_ver, commits)
    _run(["git", "tag", "-a", next_ver.tag, "-m", tag_msg], check=False)

    return next_ver.tag
