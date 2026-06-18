"""Workspace commands - generate workspace file, open, push, and run materialization."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

from ..app_paths import ensure_canonical_repo_write_allowed
from ..app_config import get_registered_workspace, load_workspace_registry, register_workspace
from ..utils import (
    get_git_branch,
    get_git_commit,
    get_git_commit_full,
    is_git_dirty,
    list_worktrees,
    normalize_branch_prefix,
    run_command,
)
from ..state import (
    get_effective_repos,
    update_repo_commit,
    is_in_workspace,
    get_state,
    get_active_ticket,
    get_all_tickets,
)
from ..runtime_workspace import RuntimeWorkspaceError, materialize_run_workspace


def _load_dotenv(env_path: Path) -> None:
    """Load KEY=VALUE lines from env_path into os.environ (no quotes/export)."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            key, value = m.group(1), m.group(2)
            value = value.strip().strip("'\"").replace(r"\n", "\n")
            os.environ.setdefault(key, value)


def _git_push_with_token(path: Path, branch: str) -> tuple[int, str]:
    """Run git push, using GIT_TOKEN or GITHUB_TOKEN from env if set (HTTPS auth)."""
    token = os.environ.get("GIT_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return run_command(["git", "-C", str(path), "push", "-u", "origin", branch])
    # Credential helper reads token from env (avoids leaking in process list).
    helper = "!f() { echo username=git; echo password=$GIT_TOKEN; }; f"
    return run_command(
        ["git", "-C", str(path), "-c", "credential.helper=" + helper, "push", "-u", "origin", branch]
    )


def get_workspace_filename(ecosystem: Optional[str] = None) -> Path:
    """Get the workspace filename based on ecosystem."""
    if ecosystem:
        return Path(f"amof.{ecosystem}.code-workspace")

    state = get_state()
    if state and state.get("ecosystem"):
        return Path(f"amof.{state['ecosystem']}.code-workspace")

    return Path("amof.code-workspace")


def _infer_repo_name(repo_source: str) -> str:
    candidate = repo_source.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    candidate = candidate.strip()
    if not candidate:
        raise RuntimeWorkspaceError("Could not infer repo name from repo source.")
    return candidate


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeWorkspaceError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise RuntimeWorkspaceError(f"{field_name} is required.")
    return normalized


def _load_materialization_request_from_intake(
    intake_path: str | Path,
    target_base_dir_override: str | None = None,
) -> dict[str, str | None]:
    intake_file = Path(intake_path)
    try:
        payload = json.loads(intake_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeWorkspaceError(f"Intake file not found: {intake_file}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeWorkspaceError(f"Invalid intake JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeWorkspaceError("Intake envelope must be a JSON object.")
    if payload.get("result_kind") != "director_intake_execution_contract":
        raise RuntimeWorkspaceError(
            "Intake envelope result_kind must be director_intake_execution_contract."
        )

    execution_handoff = payload.get("execution_handoff")
    if not isinstance(execution_handoff, dict):
        raise RuntimeWorkspaceError("Intake envelope must include execution_handoff object.")
    if execution_handoff.get("handoff_kind") != "workspace_materialization_dry_run":
        raise RuntimeWorkspaceError(
            "execution_handoff.handoff_kind must be workspace_materialization_dry_run."
        )

    workspace_materialization = execution_handoff.get("workspace_materialization")
    if not isinstance(workspace_materialization, dict):
        raise RuntimeWorkspaceError(
            "execution_handoff.workspace_materialization must be an object."
        )

    repo_source = _require_string(
        workspace_materialization.get("repo"),
        "execution_handoff.workspace_materialization.repo",
    )
    expected_sha = _require_string(
        workspace_materialization.get("expected_sha"),
        "execution_handoff.workspace_materialization.expected_sha",
    )
    run_id = _require_string(
        workspace_materialization.get("run_id"),
        "execution_handoff.workspace_materialization.run_id",
    )

    target_base_dir = target_base_dir_override
    if target_base_dir is None:
        target_base_dir = _require_string(
            workspace_materialization.get("target_base_dir"),
            "execution_handoff.workspace_materialization.target_base_dir",
        )
    else:
        target_base_dir = _require_string(target_base_dir, "target_base_dir")

    branch_or_ref = workspace_materialization.get("branch_or_ref")
    if branch_or_ref is not None:
        branch_or_ref = _require_string(
            branch_or_ref,
            "execution_handoff.workspace_materialization.branch_or_ref",
        )

    candidate_sha = workspace_materialization.get("candidate_sha")
    if candidate_sha is not None:
        candidate_sha = _require_string(
            candidate_sha,
            "execution_handoff.workspace_materialization.candidate_sha",
        )

    return {
        "repo": repo_source,
        "expected_sha": expected_sha,
        "run_id": run_id,
        "target_base_dir": target_base_dir,
        "branch_or_ref": branch_or_ref,
        "candidate_sha": candidate_sha,
    }


def _write_materialization_handoff_result(
    *,
    intake_path: str,
    receipt: Any,
) -> Path:
    handoff_result_path = Path(receipt.receipt_path).with_name("execution-handoff-result.json")
    payload = {
        "result_kind": "workspace_materialization_handoff_result",
        "status": "ready",
        "intake_path": str(Path(intake_path)),
        "run_id": receipt.run_id,
        "repo_name": receipt.repo_name,
        "repo_url": receipt.repo_url,
        "expected_sha": receipt.expected_sha,
        "actual_sha": receipt.actual_sha,
        "dirty": receipt.dirty,
        "workspace_path": receipt.workspace_path,
        "receipt_path": receipt.receipt_path,
        "candidate_sha": receipt.candidate_sha,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    handoff_result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return handoff_result_path


def materialize_from_intake_envelope(
    *,
    intake_path: str,
    target_base_dir_override: str | None = None,
) -> tuple[Any, Path]:
    """Materialize a workspace from intake JSON and return receipt + handoff path."""

    request = _load_materialization_request_from_intake(
        intake_path=intake_path,
        target_base_dir_override=target_base_dir_override,
    )
    receipt = materialize_run_workspace(
        repo_name=_infer_repo_name(request["repo"] or ""),
        repo_url=request["repo"] or "",
        expected_sha=request["expected_sha"] or "",
        run_id=request["run_id"] or "",
        target_base_dir=request["target_base_dir"] or "",
        branch_or_ref=request["branch_or_ref"],
        candidate_sha=request["candidate_sha"],
    )
    handoff_result_path = _write_materialization_handoff_result(
        intake_path=intake_path,
        receipt=receipt,
    )
    return receipt, handoff_result_path


def cmd_workspace_materialize_run(args: Any) -> int:
    """Materialize one isolated per-run workspace and print the receipt path."""

    repo_source = str(getattr(args, "repo", "") or "").strip()
    expected_sha = str(getattr(args, "expected_sha", "") or "").strip()
    run_id = str(getattr(args, "run_id", "") or "").strip()
    target_base_dir = str(getattr(args, "target_base_dir", "") or "").strip()
    branch_or_ref = getattr(args, "branch_or_ref", None)
    candidate_sha = getattr(args, "candidate_sha", None)

    try:
        receipt = materialize_run_workspace(
            repo_name=_infer_repo_name(repo_source),
            repo_url=repo_source,
            expected_sha=expected_sha,
            run_id=run_id,
            target_base_dir=target_base_dir,
            branch_or_ref=branch_or_ref,
            candidate_sha=candidate_sha,
        )
    except RuntimeWorkspaceError as exc:
        sys.stderr.write(f"[workspace] {exc}\n")
        return 1

    print(receipt.receipt_path)
    return 0


def cmd_workspace_materialize_from_intake(args: Any) -> int:
    """Materialize one isolated per-run workspace from a Director Intake envelope."""

    intake_path = str(getattr(args, "intake", "") or "").strip()
    target_base_dir_override = getattr(args, "target_base_dir", None)
    target_base_dir_override = (
        str(target_base_dir_override).strip() if target_base_dir_override is not None else None
    )

    try:
        _, handoff_result_path = materialize_from_intake_envelope(
            intake_path=intake_path,
            target_base_dir_override=target_base_dir_override,
        )
    except RuntimeWorkspaceError as exc:
        sys.stderr.write(f"[workspace] {exc}\n")
        return 1

    print(handoff_result_path)
    return 0


def cmd_workspace(manifest: Dict[str, Any], ecosystem: Optional[str] = None) -> int:
    """Generate VSCode/Cursor workspace file for multi-repo git tracking."""
    repos = get_effective_repos(manifest)

    folders: List[Dict[str, str]] = [{"path": ".", "name": "AMOF Workspace"}]

    for repo in repos:
        name = repo.get("name")
        path = repo.get("path", f"repos/{name}")
        if Path(path).exists():
            folders.append({"path": path, "name": name})

    workspace_data = {
        "folders": folders,
        "settings": {
            "git.repositoryScanMaxDepth": 1,
            "files.exclude": {
                "**/__pycache__": True,
                "**/*.pyc": True,
                "**/node_modules": True,
            },
        },
    }

    workspace_path = get_workspace_filename(ecosystem)
    ensure_canonical_repo_write_allowed(
        operation="write workspace file",
        target_path=Path.cwd() / workspace_path,
        base=Path.cwd(),
    )
    with workspace_path.open("w", encoding="utf-8") as f:
        json.dump(workspace_data, f, indent=2)

    print(f"[workspace] Generated {workspace_path}")
    print(f"[workspace] Folders: {len(folders)}")
    print(f"\nOpen with: cursor {workspace_path}")
    return 0


def cmd_open(ecosystem: Optional[str] = None) -> int:
    """Open existing workspace in Cursor IDE.

    Resolves the workspace file from any location (main or worktree):
      1. If ecosystem given, look in its worktree directory first.
      2. Fall back to current directory.
      3. Fall back to any amof.*.code-workspace in current dir.
    """
    from ..utils import get_main_worktree_root, get_worktree_dir

    workspace_path: Optional[Path] = None

    # Strategy 1: Look in the ecosystem's worktree directory
    if ecosystem:
        main_root = get_main_worktree_root()
        if main_root:
            wt_dir = get_worktree_dir(ecosystem, main_root)
            candidate = wt_dir / f"amof.{ecosystem}.code-workspace"
            if candidate.exists():
                workspace_path = candidate

    # Strategy 2: Look in current directory
    if workspace_path is None:
        local = get_workspace_filename(ecosystem)
        if local.exists():
            workspace_path = local

    # Strategy 3: Any workspace file in current dir
    if workspace_path is None:
        workspace_files = sorted(Path(".").glob("amof.*.code-workspace"))
        if workspace_files:
            workspace_path = workspace_files[0]

    if workspace_path is None:
        sys.stderr.write("[open] No workspace file found.\n")
        if ecosystem:
            sys.stderr.write(f"[open] Run 'amof -e {ecosystem} install' to create the workspace,\n")
            sys.stderr.write(f"[open] or 'amof -e {ecosystem} workspace' to regenerate the file.\n")
        else:
            sys.stderr.write("[open] Run 'amof -e <ecosystem> install' to create a workspace.\n")
        return 1

    print(f"[open] Opening {workspace_path} in Cursor...")
    code, out = run_command(["cursor", str(workspace_path)])
    if code != 0:
        sys.stderr.write(f"[open] Failed to open Cursor: {out}\n")
        print(f"\n[open] Manual command: cursor {workspace_path}")
        return 1

    return 0


def cmd_workspace_list() -> int:
    """List all active workspace worktrees."""
    from ..utils import get_git_toplevel

    worktrees = list_worktrees()
    if not worktrees:
        print("[workspace] No git worktrees found.")
        return 0

    # Separate main worktree from linked workspace worktrees
    workspace_wts = []
    main_wt = None
    for wt in worktrees:
        branch = wt.get("branch", "")
        if branch.startswith("workspace/"):
            wt["ecosystem"] = branch.split("/", 1)[1]
            workspace_wts.append(wt)
        elif not main_wt:
            main_wt = wt

    # Detect which worktree we're currently in
    current_toplevel = get_git_toplevel()
    current_path = str(current_toplevel) if current_toplevel else None

    if main_wt:
        marker = " <-- you are here" if current_path == main_wt.get("path") else ""
        print(f"Main: {main_wt.get('path')} ({main_wt.get('branch', '?')}){marker}")
        print()

    if not workspace_wts:
        print("[workspace] No active workspace worktrees.")
        print("[workspace] Create one with: amof -e <ecosystem> install")
        return 0

    # Calculate column widths
    max_eco = max(len("ECOSYSTEM"), max(len(wt.get("ecosystem", "")) for wt in workspace_wts))
    max_path = max(len("PATH"), max(len(wt.get("path", "")) for wt in workspace_wts))

    header = f"  {'ECOSYSTEM':<{max_eco + 2}}{'PATH':<{max_path + 2}}OPEN"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for wt in workspace_wts:
        eco = wt.get("ecosystem", "?")
        path = wt.get("path", "?")
        is_current = current_path == path
        marker = "* " if is_current else "  "
        open_cmd = f"amof open {eco}"
        print(f"{marker}{eco:<{max_eco + 2}}{path:<{max_path + 2}}{open_cmd}")

    print(f"\nOpen workspace:  amof open <ecosystem>")
    print(f"Switch terminal: cd <path>")
    return 0


def cmd_workspace_registry_list(*, json_output: bool = False) -> int:
    """List registered workspaces stored in AMOF app config."""
    workspaces = load_workspace_registry()["workspaces"]
    if json_output:
        print(json.dumps({"workspaces": workspaces}, indent=2))
        return 0
    if not workspaces:
        print("[workspace] No registered workspaces.")
        return 0
    max_name = max(len("NAME"), *(len(name) for name in workspaces))
    max_path = max(len("PATH"), *(len(str(entry.get("path", ""))) for entry in workspaces.values()))
    print(f"{'NAME':<{max_name}}  {'PATH':<{max_path}}  DEFAULT_REF")
    print(f"{'-' * max_name}  {'-' * max_path}  -----------")
    for name in sorted(workspaces):
        entry = workspaces[name]
        print(f"{name:<{max_name}}  {str(entry.get('path', '')):<{max_path}}  {entry.get('default_ref', 'main')}")
    return 0


def cmd_workspace_register(args: Any) -> int:
    """Register one workspace alias in AMOF app config."""
    name = getattr(args, "workspace_name", None) or getattr(args, "name", None)
    path = getattr(args, "workspace_repo", None) or getattr(args, "path", None)
    try:
        entry = register_workspace(
            name,
            path,
            default_ref=getattr(args, "default_ref", None),
        )
    except ValueError as exc:
        sys.stderr.write(f"[workspace] {exc}\n")
        return 1
    if bool(getattr(args, "json", False)):
        print(json.dumps(entry, indent=2))
    else:
        print(f"[workspace] Registered {entry['name']} -> {entry['path']} @ {entry['default_ref']}")
    return 0


def cmd_workspace_show(args: Any) -> int:
    """Show one registered workspace entry."""
    try:
        entry = get_registered_workspace(getattr(args, "name", None))
    except KeyError as exc:
        sys.stderr.write(f"[workspace] {exc}\n")
        return 1
    if bool(getattr(args, "json", False)):
        print(json.dumps(entry, indent=2))
    else:
        print(yaml.safe_dump(entry, sort_keys=False).rstrip())
    return 0


def setup_shell_aliases() -> None:
    """Set up shell aliases for amof command."""
    amof_root = Path(__file__).parent.parent.parent.resolve()
    alias_line = f'source "{amof_root}/amof-aliases.sh"'

    for config in [Path.home() / ".bashrc", Path.home() / ".zshrc"]:
        if not config.exists():
            continue
        content = config.read_text(encoding="utf-8")
        if "amof-aliases.sh" in content:
            print(f"[install] Shell aliases already configured in {config.name}")
            return
        try:
            with config.open("a", encoding="utf-8") as f:
                f.write(f"\n# AMOF aliases\n{alias_line}\n")
            print(f"[install] Added shell aliases to {config.name}")
            return
        except Exception:
            pass


def cmd_push(manifest: Dict[str, Any], commit_message: Optional[str] = None, ecosystem: Optional[str] = None) -> int:
    """Push all branches (workspace + feature branches) to origin."""
    # Load .env so GIT_TOKEN/GITHUB_TOKEN is available for HTTPS push (avoids "could not read Username").
    for env_dir in (Path.cwd(), Path(__file__).resolve().parents[3]):
        _load_dotenv(env_dir / ".env")
        if os.environ.get("GIT_TOKEN") or os.environ.get("GITHUB_TOKEN"):
            break

    repos = get_effective_repos(manifest)
    track_commits = is_in_workspace()
    active_ticket = get_active_ticket()

    if not ecosystem:
        state = get_state()
        ecosystem = state.get("ecosystem")

    def commit_changes(path: Path, repo_name: str = "workspace") -> bool:
        if not is_git_dirty(path):
            return True

        code, out = run_command(["git", "-C", str(path), "add", "-A"])
        if code != 0:
            sys.stderr.write(f"[push] {repo_name}: failed to stage - {out}\n")
            return False

        if not commit_message:
            branch = get_git_branch(path)
            ticket_id = active_ticket or ""
            if not ticket_id and branch and "/" in branch:
                ticket_id = branch.split("/")[-1]
            msg = f"{ticket_id} Commit uncommitted changes" if ticket_id else "Commit uncommitted changes"
        else:
            msg = commit_message

        code, out = run_command(["git", "-C", str(path), "commit", "-m", msg])
        if code != 0:
            sys.stderr.write(f"[push] {repo_name}: failed to commit - {out}\n")
            return False

        print(f"[push] {repo_name}: committed changes")
        return True

    amof_branch = get_git_branch(Path("."))
    if not amof_branch:
        sys.stderr.write("[push] Could not determine current branch\n")
        return 1

    if is_git_dirty(Path(".")):
        if not commit_changes(Path("."), "workspace"):
            return 1

    print(f"[push] Pushing workspace branch: {amof_branch}")
    code, out = _git_push_with_token(Path("."), amof_branch)
    if code != 0:
        sys.stderr.write(f"[push] Failed to push workspace: {out}\n")
    else:
        print(f"[push] Pushed {amof_branch}")

    # Determine expected feature branch for ticket-tracked repos
    workspace_config = manifest.get("workspace", {})
    repo_branch_prefix = normalize_branch_prefix(
        workspace_config.get("repo_branch_prefix", "feature")
    )
    expected_feature_branch = f"{repo_branch_prefix}/{active_ticket}" if active_ticket else None

    # Build set of repos tracked by the active ticket
    ticket_repos: set = set()
    if active_ticket:
        tickets = get_all_tickets()
        ticket_info = tickets.get(active_ticket, {})
        ticket_repos = set(ticket_info.get("repos", {}).keys())

    # Pre-push branch check: warn about ticket-tracked repos on the wrong branch
    wrong_branch_repos = []
    if expected_feature_branch and ticket_repos:
        for repo in repos:
            name = repo.get("name")
            readonly = repo.get("readonly", False)
            repo_path = Path(repo.get("path", name))
            if not repo_path.exists() or readonly:
                continue
            if name not in ticket_repos:
                continue
            current_branch = get_git_branch(repo_path)
            if current_branch and current_branch != expected_feature_branch:
                wrong_branch_repos.append((name, current_branch))

    skip_repos = {name for name, _ in wrong_branch_repos}

    pushed_repos = []
    skipped_repos = []
    for repo in repos:
        name = repo.get("name")
        readonly = repo.get("readonly", False)
        repo_path = Path(repo.get("path", name))

        if not repo_path.exists() or readonly:
            continue

        current_branch = get_git_branch(repo_path)
        if not current_branch:
            continue

        # Skip ticket-tracked repos that are on the wrong branch
        if name in skip_repos:
            skipped_repos.append((name, current_branch))
            continue

        if is_git_dirty(repo_path):
            if not commit_changes(repo_path, name):
                continue

        print(f"[push] Pushing {name}: {current_branch}")
        code, out = _git_push_with_token(repo_path, current_branch)
        if code != 0:
            sys.stderr.write(f"[push] {name}: failed - {out}\n")
        else:
            commit = get_git_commit(repo_path)
            commit_full = get_git_commit_full(repo_path)
            print(f"[push] {name}: pushed @ {commit}")

            if track_commits and commit and commit_full:
                update_repo_commit(name, current_branch, commit, commit_full)
                pushed_repos.append(f"{name}: {current_branch} @ {commit}")

    if track_commits and pushed_repos:
        print(f"\n[push] Recorded commits in .amof/state.json")

    if skipped_repos:
        sys.stderr.write(f"\n[push] WARNING: Skipped {len(skipped_repos)} repo(s) on wrong branch (expected {expected_feature_branch}):\n")
        for name, branch in skipped_repos:
            sys.stderr.write(f"  {name}: on '{branch}'\n")
        sys.stderr.write(f"\nFix with: amof ticket switch {active_ticket}\n")

    print("\n[push] Done!")
    return 0
