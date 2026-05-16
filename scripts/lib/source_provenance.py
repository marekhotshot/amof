"""Source-truth provenance enforcement for AMOF shell-image deploys.

This module is the single chokepoint for deciding whether a given on-disk
repository checkout is admissible as a source of an `amof-dashboard` or
`amof-controlplane` image that will become live on a managed AMOF cluster
(`platform.amof.dev` and equivalents).

The forensic record for the live shell regression on 2026-04-22 traced the
root cause to a dashboard image built from a noncanonical lane checkout
(`_lanes/amof-ui-dashboard-authoritative-dev`, branch `fix/director-state-scoping`)
and rolled to the live deployment via `kubectl set image`. To make that
class of regression impossible, this module enforces:

- the source root must be a canonical AMOF source path, not `_lanes/*`,
  `.amof-worktrees/*`, or any other parent;
- the checked-out branch must be the approved canonical branch (`main` by
  default) or match an approved release-ref pattern (`release/*`, `v*`);
- the working tree must be clean (no uncommitted or untracked changes);
- the HEAD commit must exist on a configured remote-tracking ref so that
  noncanonical-but-clean local branches still hard-fail.

This module is intentionally dependency-free (stdlib only), pure, and
testable: every check returns a structured `ProvenanceResult`, and every
failure reason is explicit. There is no silent fallback.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_ALLOWED_BRANCHES: tuple[str, ...] = ("main",)
DEFAULT_ALLOWED_REF_PATTERNS: tuple[str, ...] = ("release/*", "v*")
DEFAULT_REMOTES: tuple[str, ...] = ("origin",)

SHELL_COMPONENTS: dict[str, str] = {
    "dashboard": "amof-ui",
    "controlplane": "amof",
}


@dataclasses.dataclass(frozen=True)
class ProvenanceResult:
    """Structured outcome of a provenance check.

    `ok` is True only when every individual gate passed. `reasons` records
    every gate that failed, in the order they were evaluated, so callers
    can produce truthful rejection output instead of guessing which check
    fired first.

    When `requested_ref` is set, this result describes a ref-mode check:
    `branch` is the requested ref name (e.g. `main`, `release/1.5.x`, tag
    `v1.2.3`) and `commit` is the SHA that ref resolves to *inside the
    canonical repo*. The `clean` field is reported informationally only;
    explicit-ref builds copy a detached worktree at the resolved commit, so
    local checkout dirtiness does not affect the bytes that are built.
    """

    ok: bool
    component: str | None
    repo_dir: str
    workspace_root: str | None
    canonical_path: bool
    branch: str | None
    commit: str | None
    clean: bool
    head_on_remote: bool
    reasons: tuple[str, ...]
    requested_ref: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class ProvenanceError(RuntimeError):
    """Raised when prerequisites for a provenance check are missing."""


def _git(repo_dir: Path, *args: str) -> str:
    """Run a git command in `repo_dir` and return stripped stdout."""

    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ProvenanceError(
            f"git {' '.join(args)} failed in {repo_dir}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def _is_canonical_path(repo_dir: Path, workspace_root: Path, component_repo: str | None) -> bool:
    """Return True iff `repo_dir` is the canonical path for the component.

    AMOF-147 makes `apps/amof-ui` the canonical dashboard source. The legacy
    split-workspace `repos/<name>` shape remains admissible for non-migrated
    workspace evidence, but nested lane and ticket worktree checkouts remain
    rejected so shell-image deploys cannot inherit drift from those surfaces.
    """

    try:
        repo_resolved = repo_dir.resolve(strict=True)
    except FileNotFoundError:
        return False
    workspace_resolved = workspace_root.resolve(strict=False)
    canonical_paths: list[Path] = []
    if component_repo == "amof-ui":
        canonical_paths.append((workspace_resolved / "apps" / "amof-ui").resolve(strict=False))
        canonical_paths.append((workspace_resolved / "repos" / "amof" / "apps" / "amof-ui").resolve(strict=False))
    elif component_repo == "amof":
        canonical_paths.append(workspace_resolved.resolve(strict=False))

    legacy_root = (workspace_resolved / "repos").resolve(strict=False)
    if component_repo is None:
        canonical_paths.append(legacy_root)
    elif component_repo:
        canonical_paths.append((legacy_root / component_repo).resolve(strict=False))

    for canonical_path in canonical_paths:
        if repo_resolved == canonical_path:
            return True
    return False


def _branch_is_approved(
    branch: str | None,
    allowed_branches: Sequence[str],
    allowed_ref_patterns: Sequence[str],
) -> bool:
    if branch is None or branch == "":
        return False
    if branch == "HEAD":
        return False
    if branch in allowed_branches:
        return True
    for pattern in allowed_ref_patterns:
        if fnmatch.fnmatch(branch, pattern):
            return True
    return False


def _detect_workspace_root(repo_dir: Path) -> Path | None:
    """Find the AMOF workspace root that contains `repo_dir`, if any.

    Walks up parents until it sees a `repos/` directory next to a `.amof/`
    or `ecosystems/` directory. Returns None if no plausible root is found.
    """

    current = repo_dir.resolve(strict=False)
    for candidate in [current, *current.parents]:
        if (
            (candidate / "apps" / "amof-ui").is_dir()
            and (candidate / "scripts" / "amof").is_dir()
            and (candidate / "ecosystems").is_dir()
        ):
            return candidate
        if (candidate / "repos").is_dir() and (
            (candidate / ".amof").is_dir() or (candidate / "ecosystems").is_dir()
        ):
            return candidate
    return None


def verify_shell_source(
    repo_dir: Path | str,
    *,
    requested_ref: str | None = None,
    component: str | None = None,
    workspace_root: Path | str | None = None,
    allowed_branches: Iterable[str] | None = None,
    allowed_ref_patterns: Iterable[str] | None = None,
    remotes: Iterable[str] | None = None,
    require_remote_head: bool = True,
) -> ProvenanceResult:
    """Verify that `repo_dir` is admissible as a shell-image source.

    Parameters
    ----------
    repo_dir:
        Filesystem path to the candidate source checkout.
    requested_ref:
        Optional explicit git ref (branch name, tag, or remote-tracking ref)
        whose bytes will actually be built. When set, the gate switches to
        ref-mode: the canonical-path and ref-policy/remote-reachability gates
        apply to this ref, and HEAD-state checks (current branch,
        dirtiness, HEAD-on-remote) are skipped because explicit-ref builds
        copy a detached worktree at the resolved ref. When `None`, the
        gate runs in HEAD-mode and behaves as before.
    component:
        Optional shell component name (`dashboard` or `controlplane`). When
        set, the canonical repo name is enforced (`amof-ui` for dashboard,
        `amof` for controlplane).
    workspace_root:
        Optional override for the AMOF workspace root. When omitted it is
        auto-detected by walking up from `repo_dir`. When detection fails,
        the canonical-path check fails closed.
    allowed_branches, allowed_ref_patterns:
        Approved canonical branch names and shell-glob ref patterns. Defaults
        to `main` plus `release/*` / `v*`.
    remotes:
        Remote names to consult when verifying the HEAD is published.
    require_remote_head:
        When True (the default), HEAD (in HEAD-mode) or the resolved ref (in
        ref-mode) must be reachable from at least one approved remote ref so
        that an unpushed local commit/ref is rejected.

    Returns
    -------
    ProvenanceResult with `ok=True` only when every gate passes.
    """

    repo_path = Path(repo_dir)
    component_value = component
    component_repo: str | None = None
    if component_value is not None:
        if component_value not in SHELL_COMPONENTS:
            raise ProvenanceError(
                f"Unknown shell component '{component_value}'. Expected one of: "
                f"{', '.join(sorted(SHELL_COMPONENTS))}"
            )
        component_repo = SHELL_COMPONENTS[component_value]

    if workspace_root is None:
        detected = _detect_workspace_root(repo_path)
    else:
        detected = Path(workspace_root)

    if allowed_branches is None:
        branches = DEFAULT_ALLOWED_BRANCHES
    else:
        branches = tuple(allowed_branches)
    if allowed_ref_patterns is None:
        ref_patterns = DEFAULT_ALLOWED_REF_PATTERNS
    else:
        ref_patterns = tuple(allowed_ref_patterns)
    if remotes is None:
        remote_list = DEFAULT_REMOTES
    else:
        remote_list = tuple(remotes)

    reasons: list[str] = []

    if not repo_path.exists():
        reasons.append(f"repo path does not exist: {repo_path}")
        return ProvenanceResult(
            ok=False,
            component=component_value,
            repo_dir=str(repo_path),
            workspace_root=str(detected) if detected else None,
            canonical_path=False,
            branch=None,
            commit=None,
            clean=False,
            head_on_remote=False,
            reasons=tuple(reasons),
            requested_ref=requested_ref,
        )

    canonical_path = False
    if detected is None:
        reasons.append(
            f"could not detect AMOF workspace root from {repo_path}; canonical-path check failed closed"
        )
    else:
        canonical_path = _is_canonical_path(repo_path, detected, component_repo)
        if not canonical_path:
            if component_repo == "amof-ui":
                expected = str(detected / "apps" / "amof-ui")
            elif component_repo == "amof":
                expected = str(detected)
            else:
                expected = str(detected / "repos" / "<name>")
            reasons.append(
                f"source root {repo_path} is not the canonical source path "
                f"(expected {expected}); lane and worktree checkouts are rejected"
            )

    branch: str | None = None
    commit: str | None = None
    clean = False
    head_on_remote = False

    if requested_ref is not None:
        # Ref-mode: validate the bytes that will actually be built.
        #
        # The build script (`build-cloud-images.sh`) checks out
        # `requested_ref` into a detached temp worktree, so checkout HEAD
        # state, current branch, and local dirtiness do not affect the
        # resulting image. The risk surface is the *ref itself*: it must
        # name an approved canonical branch/tag and resolve to a commit
        # that exists on a remote-tracking ref this gate trusts.
        branch = requested_ref
        clean = True  # informational only; not a gate in ref-mode

        if not _branch_is_approved(requested_ref, branches, ref_patterns):
            reasons.append(
                f"requested ref '{requested_ref}' is not approved (expected one of "
                f"{sorted(branches)} or a ref matching {list(ref_patterns)}); "
                "lane and feature refs are rejected"
            )

        try:
            commit = _git(repo_path, "rev-parse", "--verify", f"{requested_ref}^{{commit}}")
        except ProvenanceError as exc:
            reasons.append(
                f"requested ref '{requested_ref}' does not resolve to a commit in "
                f"{repo_path}: {exc}"
            )
            commit = None

        if require_remote_head and commit is not None:
            head_on_remote = _commit_on_remote(
                repo_path, commit, remote_list, branches, ref_patterns
            )
            if not head_on_remote:
                reasons.append(
                    f"requested ref '{requested_ref}' resolves to {commit[:12]} which "
                    f"is not reachable from any approved remote ref "
                    f"({list(remote_list)} matching {list(branches) + list(ref_patterns)}); "
                    "local-only refs are rejected"
                )
        elif not require_remote_head:
            head_on_remote = True
    else:
        # HEAD-mode: validate the canonical checkout's current state.
        try:
            try:
                branch = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
            except ProvenanceError as exc:
                reasons.append(str(exc))
                branch = None

            if branch is not None and not _branch_is_approved(branch, branches, ref_patterns):
                reasons.append(
                    f"branch '{branch}' is not approved (expected one of {sorted(branches)} "
                    f"or a ref matching {list(ref_patterns)})"
                )

            try:
                commit = _git(repo_path, "rev-parse", "HEAD")
            except ProvenanceError as exc:
                reasons.append(str(exc))
                commit = None

            try:
                porcelain = _git(repo_path, "status", "--porcelain")
            except ProvenanceError as exc:
                reasons.append(str(exc))
            else:
                clean = porcelain == ""
                if not clean:
                    preview = ", ".join(
                        line.strip() for line in porcelain.splitlines()[:5] if line.strip()
                    )
                    reasons.append(
                        f"working tree is dirty: {preview}"
                        + (" (truncated)" if len(porcelain.splitlines()) > 5 else "")
                    )

            if require_remote_head and commit is not None:
                head_on_remote = _commit_on_remote(
                    repo_path, commit, remote_list, branches, ref_patterns
                )
                if not head_on_remote:
                    reasons.append(
                        f"HEAD commit {commit[:12]} is not reachable from any approved remote ref "
                        f"({remote_list} matching {list(branches) + list(ref_patterns)}); "
                        "unpushed branches are rejected"
                    )
            elif not require_remote_head:
                head_on_remote = True

        except ProvenanceError as exc:
            reasons.append(str(exc))

    ok = canonical_path and not reasons
    if requested_ref is None:
        # Preserve original semantics: HEAD-mode requires clean tree.
        ok = ok and clean
    return ProvenanceResult(
        ok=ok,
        component=component_value,
        repo_dir=str(repo_path),
        workspace_root=str(detected) if detected else None,
        canonical_path=canonical_path,
        branch=branch,
        commit=commit,
        clean=clean,
        head_on_remote=head_on_remote,
        reasons=tuple(reasons),
        requested_ref=requested_ref,
    )


def _commit_on_remote(
    repo_path: Path,
    commit: str,
    remotes: Sequence[str],
    branches: Sequence[str],
    ref_patterns: Sequence[str],
) -> bool:
    """Return True iff `commit` is reachable from at least one approved remote ref."""

    try:
        raw = _git(repo_path, "for-each-ref", "--format=%(refname)", "refs/remotes")
    except ProvenanceError:
        return False
    candidate_refs = [line for line in raw.splitlines() if line.strip()]
    approved_refs: list[str] = []
    for ref in candidate_refs:
        for remote in remotes:
            prefix = f"refs/remotes/{remote}/"
            if not ref.startswith(prefix):
                continue
            short = ref[len(prefix):]
            if short == "HEAD":
                continue
            if short in branches:
                approved_refs.append(ref)
            else:
                for pattern in ref_patterns:
                    if fnmatch.fnmatch(short, pattern):
                        approved_refs.append(ref)
                        break
    if not approved_refs:
        return False
    for ref in approved_refs:
        try:
            _git(repo_path, "merge-base", "--is-ancestor", commit, ref)
        except ProvenanceError:
            continue
        return True
    return False


def format_failure(result: ProvenanceResult) -> str:
    """Render a human-readable rejection block for a failed result."""

    if result.requested_ref is not None:
        mode = f"ref-mode requested_ref={result.requested_ref}"
    else:
        mode = f"HEAD-mode branch={result.branch or '?'}"
    header = (
        f"Source provenance check FAILED for component={result.component or '?'} "
        f"repo={result.repo_dir} {mode} "
        f"commit={(result.commit or '?')[:12]}"
    )
    lines = [header, "Rejected because:"]
    for reason in result.reasons:
        lines.append(f"  - {reason}")
    lines.append(
        "This chokepoint exists because a prior shell regression was caused "
        "by a dashboard build from a noncanonical lane checkout. Live shell "
        "deploys must come from a clean canonical repos/<name> checkout on "
        "an approved branch/ref. There is no override flag."
    )
    return "\n".join(lines)


def _cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="source_provenance",
        description="AMOF source-truth provenance check for shell-image deploys.",
    )
    parser.add_argument("--repo-dir", required=True, help="Path to the candidate source checkout.")
    parser.add_argument(
        "--component",
        choices=sorted(SHELL_COMPONENTS),
        default=None,
        help="Shell component being deployed (dashboard or controlplane).",
    )
    parser.add_argument(
        "--workspace-root",
        default=os.environ.get("AMOF_PROVENANCE_ROOT"),
        help="Override the auto-detected AMOF workspace root.",
    )
    parser.add_argument(
        "--allow-branch",
        action="append",
        default=None,
        help="Add an approved canonical branch (repeatable). Defaults to 'main'.",
    )
    parser.add_argument(
        "--allow-ref-pattern",
        action="append",
        default=None,
        help="Add an approved ref glob pattern (repeatable). Defaults to 'release/*' and 'v*'.",
    )
    parser.add_argument(
        "--requested-ref",
        default=None,
        help=(
            "Validate this explicit git ref (branch, tag, or remote ref) "
            "instead of the checkout HEAD. Used by build-cloud-images.sh "
            "when --amof-ref / --amof-ui-ref is supplied."
        ),
    )
    parser.add_argument(
        "--no-require-remote-head",
        action="store_true",
        help="Skip the remote-tracking gate (testing only).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured ProvenanceResult as JSON instead of human text.",
    )
    args = parser.parse_args(argv)

    result = verify_shell_source(
        args.repo_dir,
        requested_ref=args.requested_ref,
        component=args.component,
        workspace_root=args.workspace_root,
        allowed_branches=args.allow_branch,
        allowed_ref_patterns=args.allow_ref_pattern,
        require_remote_head=not args.no_require_remote_head,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        if result.ok:
            label = (
                f"requested_ref={result.requested_ref}"
                if result.requested_ref is not None
                else f"branch={result.branch}"
            )
            print(
                f"Source provenance OK: component={result.component or '?'} "
                f"repo={result.repo_dir} {label} "
                f"commit={(result.commit or '')[:12]}"
            )
        else:
            print(format_failure(result), file=sys.stderr)
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
