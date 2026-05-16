"""Build CLI argv for control plane actions."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from ..manifest import resolve_workspace_root

# Workspace root: where ecosystems/ and scripts/ live
def get_workspace_root() -> Path:
    return resolve_workspace_root()


def get_code_root(workspace_root: Optional[Path] = None) -> Path:
    root = os.environ.get("AMOF_CODE_ROOT")
    if root:
        return Path(root).resolve()
    return workspace_root or get_workspace_root()


def _resolve_script_path(root: Path, *relative_candidates: str) -> Path:
    code_root = get_code_root(root)
    candidates = []
    for relative in relative_candidates:
        candidates.append(code_root / relative)
        if root != code_root:
            candidates.append(root / relative)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


_PUBLIC_RUNTIME_SURFACE_REMOVED_DETAIL = (
    "This runtime build/deploy surface was removed from public AMOF canonical "
    "main. Public AMOF keeps install/bootstrap/contracts only; replay the "
    "runtime/operator path from the private operating repo."
)


def build_command(
    root: Path,
    action: str,
    ecosystem: str,
    *extra: str,
) -> Tuple[List[str], Path]:
    """Return (argv, cwd) for subprocess. argv[0] is python, then scripts/amof.py, -e, ecosystem, action, ..."""
    script = _resolve_script_path(root, "scripts/amof.py", "scripts/amof/__main__.py")
    argv = [sys.executable, str(script), "-e", ecosystem, action, *extra]
    return argv, root


def build_install_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "install", ecosystem)


def build_spin_deploy_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "spin", ecosystem, "deploy")


def build_spin_destroy_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "spin", ecosystem, "destroy")


def build_validate_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "manifest", ecosystem, "validate")


def build_release_validate_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "release", ecosystem, "validate")


def build_sync_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "sync", ecosystem)


def build_director_gmd_dev_local_proof_command(
    root: Path,
    ecosystem: str,
    *,
    input_path: str,
    output_path: Optional[str] = None,
    local_port: Optional[int] = None,
) -> Tuple[List[str], Path]:
    args = ["gmd-dev-local-proof", "--input", input_path]
    if output_path:
        args.extend(["--output", output_path])
    if local_port is not None:
        args.extend(["--local-port", str(local_port)])
    return build_command(root, "director-action", ecosystem, *args)


def build_ticket_start_command(
    root: Path,
    ecosystem: str,
    ticket_id: str,
    repos: Optional[str] = None,
    stage_id: Optional[str] = None,
    environment_id: Optional[str] = None,
    repo_selections: Optional[str] = None,
) -> Tuple[List[str], Path]:
    args = [ticket_id]
    if repos:
        args.extend(["--repos", repos])
    if stage_id:
        args.extend(["--stage", stage_id])
    if environment_id:
        args.extend(["--environment", environment_id])
    if repo_selections:
        args.extend(["--repo-selections", repo_selections])
    return build_command(root, "ticket", ecosystem, "start", *args)


def build_ticket_list_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "ticket", ecosystem, "list")


def build_ticket_switch_command(root: Path, ecosystem: str, ticket_id: str) -> Tuple[List[str], Path]:
    return build_command(root, "ticket", ecosystem, "switch", ticket_id)


def build_ticket_end_command(root: Path, ecosystem: str, ticket_id: str, cleanup: bool = False, cleanup_local: bool = False) -> Tuple[List[str], Path]:
    args = [ticket_id]
    if cleanup:
        args.append("--cleanup")
    if cleanup_local:
        args.append("--cleanup-local")
    return build_command(root, "ticket", ecosystem, "end", *args)


def build_promote_main_command(
    root: Path,
    ecosystem: str,
    *,
    repo: str,
    ticket_id: str,
    candidate_branch: str,
    source_sha: str,
    gitops_commit_sha: str,
    expected_main_sha: str,
    promotion_reason: str,
    push: bool = False,
) -> Tuple[List[str], Path]:
    args = [
        "--repo",
        repo,
        "--ticket-id",
        ticket_id,
        "--candidate-branch",
        candidate_branch,
        "--source-sha",
        source_sha,
        "--gitops-commit-sha",
        gitops_commit_sha,
        "--expected-main-sha",
        expected_main_sha,
        "--promotion-reason",
        promotion_reason,
        "--push" if push else "--dry-run",
    ]
    return build_command(root, "promote-main", ecosystem, *args)


def build_promote_main_revert_command(
    root: Path,
    ecosystem: str,
    *,
    repo: str,
    synthetic_commit_sha: str,
) -> Tuple[List[str], Path]:
    args = [
        "--repo",
        repo,
        "--synthetic-commit-sha",
        synthetic_commit_sha,
    ]
    return build_command(root, "promote-main-revert", ecosystem, *args)


def build_status_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "status", ecosystem)


def build_push_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "push", ecosystem)


def build_archive_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "archive", ecosystem, "--force")


def build_discard_command(root: Path, ecosystem: str) -> Tuple[List[str], Path]:
    return build_command(root, "discard", ecosystem, "--force")


def _git_repo_args(repo_dir: Path, *args: str) -> List[str]:
    resolved = repo_dir.resolve()
    return ["git", "-c", f"safe.directory={resolved}", "-C", str(resolved), *args]


def _git_current_ref(repo_dir: Path) -> Dict[str, Optional[str]]:
    branch_proc = subprocess.run(
        _git_repo_args(repo_dir, "branch", "--show-current"),
        capture_output=True,
        text=True,
        check=True,
    )
    commit_proc = subprocess.run(
        _git_repo_args(repo_dir, "rev-parse", "HEAD"),
        capture_output=True,
        text=True,
        check=True,
    )
    branch = branch_proc.stdout.strip() or None
    commit = commit_proc.stdout.strip() or None
    return {"branch": branch, "commit": commit}


def resolve_lifecycle_build_sources(root: Path) -> Dict[str, Dict[str, Optional[str]]]:
    sources: Dict[str, Dict[str, Optional[str]]] = {}
    amof_repo_dir = root if (root / "scripts" / "amof").exists() else root / "repos" / "amof"
    assistant_repo_dir = root / "repos" / "amof-assistant"
    repo_paths = {
        "amof": amof_repo_dir,
        "amof_ui": root / "apps" / "amof-ui",
        "assistant": assistant_repo_dir,
    }
    for key, repo_dir in repo_paths.items():
        if not repo_dir.exists():
            sources[key] = {"branch": None, "commit": None}
            continue
        try:
            sources[key] = _git_current_ref(repo_dir)
        except subprocess.CalledProcessError:
            sources[key] = {"branch": None, "commit": None}
    return sources


def build_lifecycle_build_command(
    root: Path,
    image_tag: Optional[str] = None,
    no_push: bool = False,
    profile: Optional[str] = None,
    source_mode: str = "canonical",
    source_branch: Optional[str] = None,
    amof_ref: Optional[str] = None,
    amof_ui_ref: Optional[str] = None,
    assistant_ref: Optional[str] = None,
) -> Tuple[List[str], Path]:
    """Public canonical main does not expose lifecycle image builds."""
    raise RuntimeError(_PUBLIC_RUNTIME_SURFACE_REMOVED_DETAIL)


def build_cluster_lifecycle_build_command(
    root: Path,
    image_tag: Optional[str] = None,
    no_push: bool = False,
    profile: Optional[str] = None,
    source_branch: Optional[str] = None,
    amof_ref: Optional[str] = None,
    amof_ui_ref: Optional[str] = None,
    assistant_ref: Optional[str] = None,
) -> Tuple[List[str], Path]:
    raise RuntimeError(_PUBLIC_RUNTIME_SURFACE_REMOVED_DETAIL)


_GMD_APP_BUILD_SUPPORTED_SERVICES = (
    "frontend",
    "emailservice",
    "currencyservice",
    "shippingservice",
    "productcatalogservice",
    "checkoutservice",
    "recommendationservice",
    "paymentservice",
    "loadgenerator",
    # cartservice is the only entry whose script-side dispatch points at
    # a nested context (src/cartservice/src). The command_builder layer
    # itself stays uniform: it just passes --service cartservice and the
    # script's SERVICE_DOCKERFILE / SERVICE_CONTEXT map handles the path.
    "cartservice",
    # adservice closes out the chart-default first-party set: all 11
    # services that the gmd Helm chart deploys by default are now
    # buildable from source via this command. Only shoppingassistantservice
    # remains, and it is chart-default off.
    "adservice",
)


def build_gmd_app_lifecycle_build_command(
    root: Path,
    image_tag: Optional[str] = None,
    services: Optional[List[str]] = None,
    no_push: bool = False,
) -> Tuple[List[str], Path]:
    """Build (argv, cwd) for `scripts/build-gmd-app.sh`.

    PoC slices UP8-14 / UP8-15: the dispatcher does NOT yet route this
    command from `release_lifecycle_action()`. The function exists so
    the source-build path can be exercised and unit-tested end-to-end
    before the `gmd` ecosystem build contract flips from
    `mirror_upstream` to `source_build`.

    Supported services are intentionally explicit
    (`_GMD_APP_BUILD_SUPPORTED_SERVICES`) rather than a wildcard so
    every additional service is a deliberate, reviewable diff.
    """
    script = _resolve_script_path(root, "scripts/build-gmd-app.sh")
    if not script.exists():
        raise FileNotFoundError(f"gmd-app build script not found: {script}")
    selected_services = list(services) if services else ["frontend"]
    if not selected_services:
        raise ValueError("services must contain at least one entry")
    unsupported = [s for s in selected_services if s not in _GMD_APP_BUILD_SUPPORTED_SERVICES]
    if unsupported:
        raise ValueError(
            "gmd-app PoC supports only "
            f"{list(_GMD_APP_BUILD_SUPPORTED_SERVICES)}; rejected: {unsupported}"
        )
    if len(selected_services) != 1:
        raise ValueError(
            "gmd-app PoC supports exactly one service per invocation; got: "
            f"{selected_services}"
        )
    service = selected_services[0]
    tag = image_tag or f"gmd-dev-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"
    argv = [
        "/usr/bin/env",
        "bash",
        str(script),
        "--service",
        service,
        "--tag",
        tag,
    ]
    if no_push:
        argv.append("--no-push")
    return argv, root


def build_lifecycle_deploy_command(
    root: Path,
    profile: str = "cloud-dev",
    image_tag: Optional[str] = None,
    release_name: str = "amof",
    namespace: str = "amof-system",
) -> Tuple[List[str], Path]:
    """Public canonical main does not expose lifecycle deploy actions."""
    raise RuntimeError(_PUBLIC_RUNTIME_SURFACE_REMOVED_DETAIL)


# RETIRED (clean-start slice 7). This builder targets the
# demo-microsaas legacy lane, retired from current truth and
# preserved only for legacy recovery. New ecosystems use the
# universal Argo CD pattern at infrastructure/gitops/<ecosystem>/.
def build_demo_microsaas_deploy_command(
    root: Path,
    environment_id: str = "dev",
    namespace: str = "demo-microsaas",
    release_name: str = "demo-microsaas",
    image_tag: Optional[str] = None,
    image_digest: Optional[str] = None,
    image_repository: Optional[str] = None,
    public_base_url: Optional[str] = None,
    host: Optional[str] = None,
    server_ip: Optional[str] = None,
    skip_build: bool = True,
) -> Tuple[List[str], Path]:
    """Build (argv, cwd) for the bounded Argo CD demo-microsaas deploy runner."""
    script = _resolve_script_path(
        root,
        "repos/amof/scripts/amof/tools/argocd_demo_microsaas_deploy.py",
        "scripts/amof/tools/argocd_demo_microsaas_deploy.py",
    )
    if not script.exists():
        raise FileNotFoundError(f"Deploy script not found: {script}")
    argv = [
        sys.executable,
        str(script),
        "--environment-id",
        environment_id,
        "--namespace",
        namespace,
        "--release",
        release_name,
    ]
    if image_tag:
        argv.extend(["--image-tag", image_tag])
    if image_digest:
        argv.extend(["--image-digest", image_digest])
    if image_repository:
        argv.extend(["--image-repository", image_repository])
    if public_base_url:
        argv.extend(["--public-base-url", public_base_url])
    if host:
        argv.extend(["--host", host])
    return argv, root
