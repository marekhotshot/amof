"""Per-run workspace materialization for exact-SHA execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")


class RuntimeWorkspaceError(RuntimeError):
    """Raised when a per-run workspace cannot be materialized truthfully."""


@dataclass(frozen=True)
class WorkspaceReceipt:
    run_id: str
    repo_name: str
    repo_url: str
    expected_sha: str
    actual_sha: str
    branch_or_ref: str
    dirty: bool
    workspace_path: str
    receipt_path: str
    timestamp: str
    candidate_sha: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_safe_token(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RuntimeWorkspaceError(f"{field_name} is required.")
    if not _SAFE_TOKEN.fullmatch(normalized):
        raise RuntimeWorkspaceError(
            f"{field_name} must contain only letters, digits, dot, underscore, or dash."
        )
    return normalized


def _ensure_within_base(base_dir: Path, candidate: Path) -> Path:
    resolved_base = base_dir.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_base)
    except ValueError as exc:
        raise RuntimeWorkspaceError(
            f"Refusing to materialize workspace outside base directory: {resolved_candidate}"
        ) from exc
    return resolved_candidate


def _run_git(args: list[str], *, cwd: Path | None = None) -> str:
    cmd = ["git", *args]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise RuntimeWorkspaceError(f"{' '.join(cmd)} failed: {message}")
    return proc.stdout.strip()


def _detect_branch_or_ref(workspace_path: Path, requested_ref: str | None) -> str:
    if requested_ref:
        return requested_ref
    for args in (
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        ["branch", "--show-current"],
    ):
        try:
            value = _run_git(args, cwd=workspace_path).strip()
        except RuntimeWorkspaceError:
            continue
        if value:
            return value
    return "detached"


def _write_receipt(path: Path, receipt: WorkspaceReceipt) -> None:
    path.write_text(json.dumps(receipt.to_dict(), indent=2) + "\n", encoding="utf-8")


def materialize_run_workspace(
    *,
    repo_name: str,
    repo_url: str,
    expected_sha: str,
    run_id: str,
    target_base_dir: str | Path,
    branch_or_ref: str | None = None,
    candidate_sha: str | None = None,
) -> WorkspaceReceipt:
    """Clone a repo into an isolated run root and verify the requested SHA."""

    safe_repo_name = _require_safe_token(repo_name, "repo_name")
    safe_run_id = _require_safe_token(run_id, "run_id")
    expected_sha = expected_sha.strip()
    repo_url = repo_url.strip()
    if not repo_url:
        raise RuntimeWorkspaceError("repo_url is required.")
    if not expected_sha:
        raise RuntimeWorkspaceError("expected_sha is required.")

    base_dir = Path(target_base_dir).resolve(strict=False)
    base_dir.mkdir(parents=True, exist_ok=True)
    run_root = _ensure_within_base(base_dir, base_dir / safe_run_id)
    workspace_path = _ensure_within_base(run_root, run_root / safe_repo_name)
    receipt_path = _ensure_within_base(run_root, run_root / "workspace-receipt.json")

    if run_root.exists():
        raise RuntimeWorkspaceError(f"Run root already exists: {run_root}")

    try:
        run_root.mkdir(parents=True, exist_ok=False)
        _run_git(["clone", "--no-checkout", repo_url, str(workspace_path)])
        _run_git(["checkout", "--detach", expected_sha], cwd=workspace_path)

        actual_sha = _run_git(["rev-parse", "HEAD"], cwd=workspace_path)
        if actual_sha != expected_sha:
            raise RuntimeWorkspaceError(
                f"Materialized SHA mismatch: expected {expected_sha}, got {actual_sha}"
            )

        dirty = bool(_run_git(["status", "--short"], cwd=workspace_path))
        resolved_branch_or_ref = _detect_branch_or_ref(workspace_path, branch_or_ref)
        receipt = WorkspaceReceipt(
            run_id=safe_run_id,
            repo_name=safe_repo_name,
            repo_url=repo_url,
            expected_sha=expected_sha,
            actual_sha=actual_sha,
            branch_or_ref=resolved_branch_or_ref,
            dirty=dirty,
            workspace_path=str(workspace_path),
            receipt_path=str(receipt_path),
            timestamp=_now_iso(),
            candidate_sha=candidate_sha.strip() if candidate_sha and candidate_sha.strip() else None,
        )
        _write_receipt(receipt_path, receipt)
        return receipt
    except Exception:
        shutil.rmtree(run_root, ignore_errors=True)
        raise
