"""Plan or execute coherent candidate-bundle promotions into ``main``.

The planner validates a candidate bundle, computes explicit code/env deltas,
materializes the synthetic result tree in a temporary worktree, and records an
audit artifact. The push path is intentionally thin: it reuses the dry-run
planner, recreates the same synthetic tree in a temporary worktree, creates one
synthetic commit, and pushes it fast-forward to ``origin/main``.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from ..app_paths import evidence_dir, locks_dir
from ..intake.build_write import ENV_ONLY_COMMIT_MESSAGE_RE, ENV_PATH_PREFIX, infer_ticket_id
from ..manifest import get_ecosystem_root, resolve_workspace_root, simple_parse_yaml
from ..utils import ensure_dir
from .workspace import _load_dotenv

ALLOWED_GITOPS_PREFIXES = (ENV_PATH_PREFIX,)
FORBIDDEN_CODE_DELTA_SUFFIXES = (".tgz",)
LOCK_DIR = locks_dir()
AUDIT_SUBDIR = "audit"
PROMOTION_ID_BYTES = 8
MAIN_PUSH_BYPASS_ENV = "AMOF_ALLOW_MAIN_PUSH"
PROMOTION_SUBJECT_PREFIX = "chore(promote-main): promote "
NO_GITOPS_SHA = "<none>"
PRIVATE_PROMOTION_POLICY_RELATIVE_PATH = Path(".amof-local") / "promotion-targets.yaml"
COMPAT_LOCK_RELATIVE_PATH = Path("compat") / "public-private.lock.yaml"
GITHUB_AUTH_SCOPE_HINT = (
    "Set GITHUB_TOKEN (classic: repo; fine-grained: Contents read/write) "
    "or configure a non-interactive git credential.helper."
)
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class PromoteMainInput:
    repo: str
    ticket_id: str
    candidate_branch: str
    source_sha: str
    gitops_commit_sha: str | None
    expected_main_sha: str
    promotion_reason: str
    dry_run: bool
    require_run_summary: str | None = None
    require_promotion_readiness_result: str | None = None


@dataclass(frozen=True)
class PromoteMainPlan:
    ok: bool
    status: str
    mode: str
    repo: str
    repo_path: str
    ticket_id: str
    candidate_branch: str
    source_sha: str
    gitops_commit_sha: str | None
    expected_main_sha: str
    current_origin_main_sha: str | None
    promotion_id: str
    bundle_id: str
    code_delta_files: list[str]
    env_delta_files: list[str]
    synthetic_commit_message: str
    synthetic_tree_sha: str | None
    audit_record_path: str
    lock_path: str
    lock_status: str
    lock_final_status: str
    validation_checks: dict[str, bool]
    rejection_reason: str | None = None
    synthetic_commit_sha: str | None = None
    result_main_sha: str | None = None
    already_promoted_commit_sha: str | None = None
    push_attempted: bool = False
    push_succeeded: bool = False
    failure_stage: str | None = None
    failure_reason: str | None = None
    failure_classification: str | None = None
    legacy_numeric_fallback_used: bool = False
    promotion_target_policy_path: str | None = None
    compat_lock_reconciliation: dict[str, Any] | None = None
    merge_base_sha: str | None = None
    candidate_delta_paths: list[str] | None = None
    current_main_advanced_paths: list[str] | None = None
    overlap_paths: list[str] | None = None
    stale_base: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromoteMainRevertInput:
    repo: str
    synthetic_commit_sha: str


@dataclass(frozen=True)
class PromoteMainRevertResult:
    ok: bool
    status: str
    repo: str
    repo_path: str
    synthetic_commit_sha: str
    current_origin_main_sha: str | None
    revert_commit_sha: str | None
    lock_path: str
    lock_status: str
    lock_final_status: str
    failure_reason: str | None = None


@dataclass(frozen=True)
class PromotionTarget:
    repo_name: str
    repo_path: Path
    expected_remote: str | None = None
    policy_path: Path | None = None


class PromotionLockError(RuntimeError):
    """Raised when the local promotion lock cannot be acquired."""


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class PromotionLock:
    """Simple exclusive lock file for local promotion planning."""

    def __init__(self, path: Path, payload: dict[str, Any]) -> None:
        self.path = path
        self.payload = payload
        self.acquired = False

    def acquire(self) -> None:
        ensure_dir(self.path.parent)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing = ""
            if self.path.exists():
                try:
                    existing = self.path.read_text(encoding="utf-8").strip()
                except OSError:
                    existing = ""
            detail = f"promotion lock already exists at {self.path}"
            if existing:
                detail += f": {existing}"
            raise PromotionLockError(detail) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(self.payload, indent=2) + "\n")
        except Exception:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            raise
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.acquired = False

    def __enter__(self) -> "PromotionLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _normalize_ticket_id(value: str) -> str:
    text = str(value or "").strip().upper()
    if not text:
        raise ValueError("ticket_id is required")
    return text


def _is_legacy_numeric_ticket_id(ticket_id: str) -> bool:
    return bool(re.fullmatch(r"AMOF-\d+", str(ticket_id or "").strip().upper()))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "bundle"


def _short_sha(value: str) -> str:
    return str(value or "")[:12]


def _sha_matches(recorded_sha: str, resolved_sha: str) -> bool:
    recorded = str(recorded_sha or "").strip().lower()
    resolved = str(resolved_sha or "").strip().lower()
    if not recorded or not resolved:
        return False
    return resolved.startswith(recorded) or recorded.startswith(resolved)


def _git(
    repo_path: Path,
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo_path.resolve()}", *args],
        cwd=str(repo_path),
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
    )


def _git_ok(repo_path: Path, *args: str, input_text: str | None = None, env: dict[str, str] | None = None) -> str:
    completed = _git(repo_path, *args, input_text=input_text, env=env)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "").strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _resolve_commit(repo_path: Path, revision: str) -> str:
    return _git_ok(repo_path, "rev-parse", f"{revision}^{{commit}}")


def _parse_timestamp(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"{field_name} is required")
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{field_name} must be a valid ISO-8601 timestamp") from exc
    return text


def _require_non_empty_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{field_name} must be an object")
    return value


def _require_bool(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{field_name} must be a boolean")
    return value


def _require_int(value: Any, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"{field_name} must be an integer")
    return value


def _require_string_list(value: Any, *, field_name: str, min_items: int = 1) -> list[str]:
    if not isinstance(value, list):
        raise RuntimeError(f"{field_name} must be an array of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError(f"{field_name} must contain only non-empty strings")
        items.append(item.strip())
    if len(items) < min_items:
        raise RuntimeError(f"{field_name} must contain at least {min_items} item(s)")
    return items


def _require_full_sha(value: Any, *, field_name: str) -> str:
    text = _require_non_empty_string(value, field_name=field_name).lower()
    if not FULL_SHA_RE.fullmatch(text):
        raise RuntimeError(f"{field_name} must be a 40-character lowercase git SHA")
    return text


def _resolve_evidence_path(raw_path: str, *, repo_path: Path, workspace_root: Path) -> Path:
    raw = _require_non_empty_string(raw_path, field_name="evidence_path")
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
    else:
        repo_candidate = (repo_path / candidate).resolve(strict=False)
        workspace_candidate = (workspace_root / candidate).resolve(strict=False)
        if repo_candidate.exists():
            resolved = repo_candidate
        elif workspace_candidate.exists():
            resolved = workspace_candidate
        elif raw.startswith(".amof-local/") or raw.startswith("./.amof-local/"):
            resolved = workspace_candidate
        else:
            resolved = repo_candidate
    if not resolved.exists():
        raise RuntimeError(f"required evidence path does not exist: {resolved}")
    if not resolved.is_file():
        raise RuntimeError(f"required evidence path is not a file: {resolved}")
    return resolved


def _is_within_path(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _display_path(path: Path, *, workspace_root: Path) -> str:
    resolved = path.resolve(strict=False)
    base = workspace_root.resolve(strict=False)
    if _is_within_path(resolved, base):
        return str(resolved.relative_to(base))
    return str(resolved)


def _private_promotion_policy_path(workspace_root: Path) -> Path:
    return (workspace_root / PRIVATE_PROMOTION_POLICY_RELATIVE_PATH).resolve(strict=False)


def _load_private_promotion_target(workspace_root: Path) -> PromotionTarget:
    policy_path = _private_promotion_policy_path(workspace_root)
    if not policy_path.exists():
        raise RuntimeError(f"required private promotion policy missing: {policy_path}")
    if not policy_path.is_file():
        raise RuntimeError(f"private promotion policy is not a file: {policy_path}")

    try:
        payload = simple_parse_yaml(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"failed to read private promotion policy {policy_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"private promotion policy must be an object: {policy_path}")

    version = payload.get("version")
    if version != 1:
        raise RuntimeError(f"private promotion policy {policy_path} must declare version: 1")

    targets = _require_mapping(payload.get("targets"), field_name="promotion_target_policy.targets")
    unexpected_targets = sorted(str(name).strip() for name in targets if str(name).strip() != "amof-private")
    if unexpected_targets:
        raise RuntimeError(
            "private promotion policy may authorize only amof-private; "
            f"unexpected targets: {', '.join(unexpected_targets)}"
        )

    entry = _require_mapping(
        targets.get("amof-private"),
        field_name="promotion_target_policy.targets.amof-private",
    )
    path_text = _require_non_empty_string(
        entry.get("path"),
        field_name="promotion_target_policy.targets.amof-private.path",
    )
    relative_path = Path(path_text)
    if relative_path.is_absolute():
        raise RuntimeError("private promotion target path must be workspace-relative")

    resolved_workspace_root = workspace_root.resolve(strict=False)
    resolved_repo_path = (resolved_workspace_root / relative_path).resolve(strict=False)
    if not _is_within_path(resolved_repo_path, resolved_workspace_root):
        raise RuntimeError(
            "private promotion target path escapes the resolved workspace root: "
            f"{resolved_repo_path}"
        )

    expected_remote = _require_non_empty_string(
        entry.get("remote"),
        field_name="promotion_target_policy.targets.amof-private.remote",
    )
    target_branch = _require_non_empty_string(
        entry.get("branch"),
        field_name="promotion_target_policy.targets.amof-private.branch",
    )
    if target_branch != "main":
        raise RuntimeError("private promotion target branch must be exactly 'main'")

    return PromotionTarget(
        repo_name="amof-private",
        repo_path=resolved_repo_path,
        expected_remote=expected_remote,
        policy_path=policy_path,
    )


def _resolve_promote_main_target(
    manifest: dict[str, Any],
    workspace_root: Path,
    repo_name: str,
) -> PromotionTarget:
    normalized_repo_name = str(repo_name or "").strip()
    if normalized_repo_name == "amof":
        return PromotionTarget(
            repo_name="amof",
            repo_path=_resolve_repo_path(manifest, workspace_root, normalized_repo_name),
        )
    if normalized_repo_name == "amof-private":
        return _load_private_promotion_target(workspace_root)
    raise RuntimeError(f"repo {normalized_repo_name} is not an allowed promote-main target")


def _validate_target_remote(target: PromotionTarget, *, workspace_root: Path) -> None:
    if not target.expected_remote:
        return
    env = _git_env_with_credentials(workspace_root)
    actual_remote = _origin_remote_url(target.repo_path, env)
    if not actual_remote:
        raise RuntimeError(f"{target.repo_name} must define an origin remote")
    if actual_remote != target.expected_remote:
        raise RuntimeError(
            f"{target.repo_name} origin remote mismatch: expected {target.expected_remote}; "
            f"got {actual_remote}"
        )


def _ensure_evidence_path_allowed(path: Path, *, repo_path: Path, workspace_root: Path) -> None:
    allowed_roots = (
        (repo_path / "docs" / "audit").resolve(strict=False),
        (workspace_root / ".amof-local" / "evidence").resolve(strict=False),
        evidence_dir().resolve(strict=False),
    )
    if any(_is_within_path(path, root) for root in allowed_roots):
        return
    roots_text = ", ".join(str(root) for root in allowed_roots)
    raise RuntimeError(
        f"evidence path {path} is outside allowed auditable roots: {roots_text}"
    )


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"failed to parse JSON from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a top-level JSON object")
    return payload


def _validate_json_schema_if_available(payload: dict[str, Any], schema_path: Path) -> None:
    if importlib.util.find_spec("jsonschema") is None or not schema_path.exists():
        return
    import jsonschema

    schema_payload = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(instance=payload, schema=schema_payload)
    except jsonschema.ValidationError as exc:
        raise RuntimeError(f"schema validation failed for {schema_path.name}: {exc.message}") from exc


def _resolve_contract_schema_path(*, repo_path: Path, workspace_root: Path, schema_name: str) -> Path:
    repo_candidate = repo_path / "contracts" / schema_name
    if repo_candidate.exists():
        return repo_candidate
    workspace_candidate = workspace_root / "contracts" / schema_name
    return workspace_candidate


def _normalize_repo_identity(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    identity = Path(text).name.strip().lower()
    if identity.endswith(".git"):
        identity = identity[:-4]
    if not identity:
        return None
    return identity


def _repo_identity_status(run_summary: dict[str, Any], *, target_repo: str) -> str:
    target = target_repo.strip().lower()
    receipts = run_summary.get("receipts")
    if not isinstance(receipts, dict):
        return "unknown"
    candidates = [_normalize_repo_identity(run_summary.get("repo"))]
    for receipt_name in ("workspace_receipt", "execution_handoff_result"):
        receipt = receipts.get(receipt_name)
        if isinstance(receipt, dict):
            candidates.append(_normalize_repo_identity(receipt.get("repo_name")))
            candidates.append(_normalize_repo_identity(receipt.get("repo_url")))
    known_candidates = [candidate for candidate in candidates if candidate]
    if any(candidate == target for candidate in known_candidates):
        return "match"
    ambiguous_prefixes = ("source", "clone", "checkout", "workspace")
    if not known_candidates or all(
        candidate.startswith(ambiguous_prefixes) or candidate == "main" for candidate in known_candidates
    ):
        return "unknown"
    return "mismatch"


def _validate_promotion_readiness_result_evidence(
    evidence_path: str,
    *,
    source_sha: str,
    repo_path: Path,
    workspace_root: Path,
) -> None:
    resolved_path = _resolve_evidence_path(evidence_path, repo_path=repo_path, workspace_root=workspace_root)
    _ensure_evidence_path_allowed(resolved_path, repo_path=repo_path, workspace_root=workspace_root)
    payload = _load_json_file(resolved_path)
    _validate_json_schema_if_available(
        payload,
        _resolve_contract_schema_path(
            repo_path=repo_path,
            workspace_root=workspace_root,
            schema_name="promotion-readiness-result.schema.json",
        ),
    )
    if (
        _require_non_empty_string(
            payload.get("result_kind"),
            field_name="promotion_readiness_result.result_kind",
        )
        != "promotion_readiness_result"
    ):
        raise RuntimeError("promotion_readiness_result.result_kind must be 'promotion_readiness_result'")
    if (
        _require_non_empty_string(
            payload.get("classification"),
            field_name="promotion_readiness_result.classification",
        )
        != "ready_for_promotion"
    ):
        raise RuntimeError(
            "promotion_readiness_result.classification must be 'ready_for_promotion'"
        )
    expected_sha = _require_full_sha(
        payload.get("expected_sha"),
        field_name="promotion_readiness_result.expected_sha",
    )
    actual_sha = _require_full_sha(
        payload.get("actual_sha"),
        field_name="promotion_readiness_result.actual_sha",
    )
    if expected_sha != source_sha or actual_sha != source_sha:
        raise RuntimeError("promotion readiness evidence SHA does not match the promoted source SHA")
    _require_non_empty_string(payload.get("run_id"), field_name="promotion_readiness_result.run_id")
    _parse_timestamp(payload.get("timestamp"), field_name="promotion_readiness_result.timestamp")
    _require_non_empty_string(
        payload.get("run_summary_path"),
        field_name="promotion_readiness_result.run_summary_path",
    )
    _require_string_list(payload.get("reasons"), field_name="promotion_readiness_result.reasons")


def _validate_run_summary_evidence(
    evidence_path: str,
    *,
    source_sha: str,
    repo_name: str,
    repo_path: Path,
    workspace_root: Path,
) -> None:
    resolved_path = _resolve_evidence_path(evidence_path, repo_path=repo_path, workspace_root=workspace_root)
    _ensure_evidence_path_allowed(resolved_path, repo_path=repo_path, workspace_root=workspace_root)
    payload = _load_json_file(resolved_path)
    _validate_json_schema_if_available(
        payload,
        _resolve_contract_schema_path(
            repo_path=repo_path,
            workspace_root=workspace_root,
            schema_name="run-summary.schema.json",
        ),
    )
    if (
        _require_non_empty_string(payload.get("final_status"), field_name="run_summary.final_status")
        != "executed"
    ):
        raise RuntimeError("run_summary.final_status must be 'executed'")
    if (
        _require_non_empty_string(
            payload.get("lifecycle_state"),
            field_name="run_summary.lifecycle_state",
        )
        != "ready_for_promotion"
    ):
        raise RuntimeError("run_summary.lifecycle_state must be 'ready_for_promotion'")
    expected_sha = _require_full_sha(payload.get("expected_sha"), field_name="run_summary.expected_sha")
    actual_sha = _require_full_sha(payload.get("actual_sha"), field_name="run_summary.actual_sha")
    if expected_sha != source_sha or actual_sha != source_sha:
        raise RuntimeError("run summary evidence SHA does not match the promoted source SHA")
    if payload.get("failure_message") is not None:
        raise RuntimeError("run_summary.failure_message must be null")
    _require_non_empty_string(payload.get("run_id"), field_name="run_summary.run_id")
    _parse_timestamp(payload.get("timestamp"), field_name="run_summary.timestamp")

    validation_summary = _require_mapping(
        payload.get("validation_summary"),
        field_name="run_summary.validation_summary",
    )
    if not _require_bool(
        validation_summary.get("actual_sha_matches_expected"),
        field_name="run_summary.validation_summary.actual_sha_matches_expected",
    ):
        raise RuntimeError("run_summary.validation_summary.actual_sha_matches_expected must be true")
    if not _require_bool(
        validation_summary.get("workspace_clean_after_materialization"),
        field_name="run_summary.validation_summary.workspace_clean_after_materialization",
    ):
        raise RuntimeError("run_summary.validation_summary.workspace_clean_after_materialization must be true")
    if (
        _require_int(
            validation_summary.get("execution_exit_code"),
            field_name="run_summary.validation_summary.execution_exit_code",
        )
        != 0
    ):
        raise RuntimeError("run_summary.validation_summary.execution_exit_code must be 0")
    if _require_bool(
        validation_summary.get("source_repo_mutated"),
        field_name="run_summary.validation_summary.source_repo_mutated",
    ):
        raise RuntimeError("run_summary.validation_summary.source_repo_mutated must be false")

    receipts = _require_mapping(payload.get("receipts"), field_name="run_summary.receipts")
    receipt_paths = _require_mapping(payload.get("receipt_paths"), field_name="run_summary.receipt_paths")
    _require_non_empty_string(
        receipt_paths.get("execution_step_receipt_path"),
        field_name="run_summary.receipt_paths.execution_step_receipt_path",
    )
    execution_step_receipt = _require_mapping(
        receipts.get("execution_step_receipt"),
        field_name="run_summary.receipts.execution_step_receipt",
    )
    _require_string_list(
        execution_step_receipt.get("command_argv"),
        field_name="run_summary.receipts.execution_step_receipt.command_argv",
    )
    if _require_bool(
        execution_step_receipt.get("dirty_status_after"),
        field_name="run_summary.receipts.execution_step_receipt.dirty_status_after",
    ):
        raise RuntimeError("run_summary.receipts.execution_step_receipt.dirty_status_after must be false")
    if _require_bool(
        execution_step_receipt.get("source_repo_mutated"),
        field_name="run_summary.receipts.execution_step_receipt.source_repo_mutated",
    ):
        raise RuntimeError(
            "run_summary.receipts.execution_step_receipt.source_repo_mutated must be false"
        )

    repo_identity = _repo_identity_status(payload, target_repo=repo_name)
    if repo_identity == "mismatch":
        raise RuntimeError("run summary evidence repo identity does not match the promotion target repo")


def _validate_optional_promotion_evidence(
    bundle: PromoteMainInput,
    *,
    source_sha: str,
    repo_path: Path,
    workspace_root: Path,
) -> None:
    if bundle.require_run_summary and bundle.require_promotion_readiness_result:
        raise RuntimeError(
            "Use only one of --require-run-summary or --require-promotion-readiness-result."
        )
    if bundle.require_promotion_readiness_result:
        _validate_promotion_readiness_result_evidence(
            bundle.require_promotion_readiness_result,
            source_sha=source_sha,
            repo_path=repo_path,
            workspace_root=workspace_root,
        )
    elif bundle.require_run_summary:
        _validate_run_summary_evidence(
            bundle.require_run_summary,
            source_sha=source_sha,
            repo_name=bundle.repo,
            repo_path=repo_path,
            workspace_root=workspace_root,
        )


def _branch_exists(repo_path: Path, branch: str) -> bool:
    return _git(repo_path, "rev-parse", "--verify", f"{branch}^{{commit}}").returncode == 0


def _is_ancestor(repo_path: Path, older: str, newer: str) -> bool:
    return _git(repo_path, "merge-base", "--is-ancestor", older, newer).returncode == 0


def _name_status_diff(
    repo_path: Path,
    base: str,
    head: str,
    *,
    pathspecs: list[str] | None = None,
) -> list[tuple[str, str]]:
    args = ["diff", "--name-status", "--no-renames", f"{base}..{head}"]
    if pathspecs:
        args.extend(["--", *pathspecs])
    out = _git_ok(repo_path, *args)
    entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        text = line.strip()
        if not text:
            continue
        parts = text.split("\t", 1)
        if len(parts) != 2:
            raise RuntimeError(f"unexpected git diff --name-status output: {text}")
        status, path = parts
        normalized_status = status.strip()[:1].upper()
        normalized_path = path.strip()
        if normalized_status not in {"A", "M", "D"}:
            raise RuntimeError(f"unexpected git diff status {normalized_status!r} for path {normalized_path}")
        if not normalized_path:
            raise RuntimeError(f"unexpected empty path in git diff output: {text}")
        entries.append((normalized_status, normalized_path))
    return entries


def _changed_files(repo_path: Path, base: str, head: str) -> list[str]:
    return [path for _, path in _name_status_diff(repo_path, base, head)]


def _stale_base_overlap_details(
    repo_path: Path,
    *,
    source_sha: str,
    current_origin_main_sha: str,
) -> tuple[str, list[str]]:
    merge_base = _git_ok(repo_path, "merge-base", source_sha, current_origin_main_sha)
    if _is_ancestor(repo_path, current_origin_main_sha, source_sha):
        return merge_base, []
    main_changed_files = set(_changed_files(repo_path, merge_base, current_origin_main_sha))
    candidate_changed_files = set(_changed_files(repo_path, merge_base, source_sha))
    return merge_base, sorted(main_changed_files & candidate_changed_files)


def _patch_text(repo_path: Path, base: str, head: str, *, pathspecs: list[str] | None = None) -> str:
    args = ["diff", "--binary", "--full-index", f"{base}..{head}"]
    if pathspecs:
        args.extend(["--", *pathspecs])
    completed = _git(repo_path, *args)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "").strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def _apply_patch(repo_path: Path, patch_text: str) -> None:
    if not patch_text.strip():
        return
    completed = _git(
        repo_path,
        "apply",
        "--index",
        "--allow-binary-replacement",
        "-",
        input_text=patch_text,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "").strip() or "git apply failed")


def _commit_subject(repo_path: Path, commit_sha: str) -> str:
    return _git_ok(repo_path, "show", "-s", "--format=%s", commit_sha)


def _commit_changed_files(repo_path: Path, commit_sha: str) -> list[str]:
    out = _git_ok(repo_path, "show", "--pretty=", "--name-only", commit_sha)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _show_file(repo_path: Path, commit_sha: str, path: str) -> str:
    return _git_ok(repo_path, "show", f"{commit_sha}:{path}")


def _is_allowed_gitops_path(path: str) -> bool:
    return any(str(path).startswith(prefix) for prefix in ALLOWED_GITOPS_PREFIXES)


def _is_forbidden_code_delta_path(path: str) -> bool:
    normalized = str(path or "").strip()
    if not normalized:
        return False
    if _is_allowed_gitops_path(normalized):
        return True
    if normalized.endswith(FORBIDDEN_CODE_DELTA_SUFFIXES):
        return True

    parts = Path(normalized).parts
    if len(parts) >= 2 and parts[:2] == (".amof", "sessions"):
        return True
    if len(parts) >= 3 and parts[0] == "ecosystems" and parts[2] in {"audit", "journal"}:
        return True
    return False


def _forbidden_code_delta_files(entries: list[tuple[str, str]]) -> list[str]:
    return [
        path
        for status, path in entries
        if status != "D" and _is_forbidden_code_delta_path(path)
    ]


def _parse_ticket_env(content: str) -> dict[str, Any]:
    payload = simple_parse_yaml(content)
    ticket = payload.get("ticket") if isinstance(payload, dict) else {}
    images = payload.get("images") if isinstance(payload, dict) else {}
    ticket = ticket if isinstance(ticket, dict) else {}
    images = images if isinstance(images, dict) else {}
    image_tags: dict[str, str] = {}
    for key, value in images.items():
        if isinstance(value, dict) and value.get("tag") is not None:
            image_tags[str(key)] = str(value.get("tag"))
    return {
        "ticket_id": str(ticket.get("id") or "").strip(),
        "ticket_branch": str(ticket.get("branch") or "").strip(),
        "commit_sha": str(ticket.get("commitSha") or "").strip(),
        "image_tags": image_tags,
    }


def _env_files_match_bundle(
    repo_path: Path,
    env_commit_sha: str,
    env_delta_files: list[str],
    *,
    ticket_id: str,
    source_sha: str,
) -> tuple[bool, str | None]:
    for path in env_delta_files:
        if not _is_allowed_gitops_path(path):
            return False, f"{path} is outside the allowed GitOps scope"
    for path in env_delta_files:
        try:
            parsed = _parse_ticket_env(_show_file(repo_path, env_commit_sha, path))
        except Exception as exc:
            return False, f"failed to read {path} from env commit: {exc}"
        env_ticket_id = str(parsed.get("ticket_id") or "").upper()
        env_source_sha = str(parsed.get("commit_sha") or "").strip()
        image_tags = [tag for tag in (parsed.get("image_tags") or {}).values() if str(tag).strip()]
        if env_ticket_id != ticket_id:
            return False, f"{path} carries ticket id {env_ticket_id or '<missing>'}, expected {ticket_id}"
        if not _sha_matches(env_source_sha, source_sha):
            return False, f"{path} references commitSha {env_source_sha or '<missing>'}, expected {source_sha}"
        if any(not _sha_matches(tag, source_sha) for tag in image_tags):
            return False, f"{path} image tags do not all match source_sha {source_sha}"
    return True, None


def _bundle_id(repo: str, ticket_id: str, source_sha: str, gitops_commit_sha: str | None, expected_main_sha: str) -> str:
    digest = hashlib.sha256(
        f"{repo}:{ticket_id}:{source_sha}:{gitops_commit_sha or NO_GITOPS_SHA}:{expected_main_sha}".encode("utf-8")
    ).hexdigest()
    return digest


def _promotion_id(bundle_id: str) -> str:
    return f"promote-{bundle_id[: PROMOTION_ID_BYTES * 2]}"


def _synthetic_commit_message(bundle: PromoteMainInput, promotion_id: str, bundle_id: str) -> str:
    return (
        f"chore(promote-main): promote {bundle.ticket_id} candidate bundle\n\n"
        f"Ticket: {bundle.ticket_id}\n"
        f"Candidate-Branch: {bundle.candidate_branch}\n"
        f"Source-SHA: {bundle.source_sha}\n"
        f"GitOps-SHA: {bundle.gitops_commit_sha or NO_GITOPS_SHA}\n"
        f"Bundle-ID: {bundle_id}\n"
        f"Promotion-ID: {promotion_id}\n"
        f"Reason: {bundle.promotion_reason}\n"
    )


def _fetch_origin_main(repo_path: Path, workspace_root: Path) -> tuple[bool, str]:
    completed = _git_with_credentials(repo_path, workspace_root, "fetch", "origin", "main", "--quiet")
    output = (completed.stderr or completed.stdout or "").strip()
    return completed.returncode == 0, output


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _operator_root_candidates(workspace_root: Path, repo_path: Path) -> list[Path]:
    return _unique_paths(
        [
            workspace_root,
            *workspace_root.parents,
            repo_path,
            *repo_path.parents,
        ]
    )


def _find_operator_compat_lock(workspace_root: Path, repo_path: Path) -> Path | None:
    for root in _operator_root_candidates(workspace_root, repo_path):
        candidate = root / COMPAT_LOCK_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    return None


def _find_private_repo_for_compat_lock(
    lock_path: Path,
    *,
    workspace_root: Path,
    repo_path: Path,
) -> Path | None:
    roots = _unique_paths(
        [
            lock_path.parents[1],
            *_operator_root_candidates(workspace_root, repo_path),
        ]
    )
    for root in roots:
        candidate = root / "repos" / "amof-private"
        if (candidate / ".git").exists():
            return candidate.resolve(strict=False)
    return None


def _refresh_origin_main_sha(repo_path: Path, workspace_root: Path) -> tuple[str | None, str | None]:
    fetch_ok, fetch_out = _fetch_origin_main(repo_path, workspace_root)
    if not fetch_ok:
        return None, _classify_git_failure(fetch_out) or "fetch_failed"
    try:
        return _resolve_commit(repo_path, "origin/main"), None
    except RuntimeError:
        return None, "origin_main_unresolved"


def _quote_yaml_scalar_like(existing_value: str, replacement: str) -> str:
    text = str(existing_value or "").strip()
    if text.startswith("'"):
        return "'" + replacement.replace("'", "''") + "'"
    if text.startswith('"'):
        return '"' + replacement.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return '"' + replacement + '"'


def _replace_yaml_section_scalar(
    text: str,
    *,
    section: str,
    key: str,
    replacement: str,
) -> str:
    lines = text.splitlines()
    section_index: int | None = None
    next_section_index = len(lines)
    section_re = re.compile(rf"^{re.escape(section)}\s*:\s*(?:#.*)?$")
    top_level_re = re.compile(r"^[A-Za-z0-9_-]+\s*:")
    key_re = re.compile(rf"^(\s+{re.escape(key)}\s*:\s*)(.*?)(\s*(?:#.*)?)$")
    for index, line in enumerate(lines):
        if section_index is None:
            if section_re.match(line):
                section_index = index
            continue
        if index > section_index and top_level_re.match(line):
            next_section_index = index
            break
        match = key_re.match(line)
        if match:
            prefix, old_value, suffix = match.groups()
            lines[index] = f"{prefix}{_quote_yaml_scalar_like(old_value, replacement)}{suffix}"
            return "\n".join(lines) + "\n"
    if section_index is None:
        lines.extend([f"{section}:", f"  {key}: \"{replacement}\""])
    else:
        lines.insert(next_section_index, f"  {key}: \"{replacement}\"")
    return "\n".join(lines) + "\n"


def _run_post_reconciliation_doctor(repo_path: Path, workspace_root: Path) -> dict[str, Any]:
    scripts_root = Path(__file__).resolve().parents[2]
    entrypoint = scripts_root / "amof.py"
    command = (
        [sys.executable, str(entrypoint), "doctor", "--json"]
        if entrypoint.exists()
        else [sys.executable, "-m", "amof", "doctor", "--json"]
    )
    env = os.environ.copy()
    env["AMOF_WORKSPACE_ROOT"] = str(workspace_root)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo_path),
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except Exception:
        return {"status": "failed", "exit_code": None}
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
    }


def _reconcile_operator_compat_lock_after_promotion(
    *,
    workspace_root: Path,
    public_repo_path: Path,
) -> dict[str, Any]:
    lock_path = _find_operator_compat_lock(workspace_root, public_repo_path)
    if lock_path is None:
        return {
            "attempted": False,
            "status": "skipped",
            "public_origin_main": None,
            "private_origin_main": None,
            "lock_path": None,
            "backup_path": None,
            "doctor_status": "not_run",
            "failure_reason": "compat_lock_not_found",
        }
    private_repo_path = _find_private_repo_for_compat_lock(
        lock_path,
        workspace_root=workspace_root,
        repo_path=public_repo_path,
    )
    if private_repo_path is None:
        return {
            "attempted": True,
            "status": "warning",
            "public_origin_main": None,
            "private_origin_main": None,
            "lock_path": str(lock_path),
            "backup_path": None,
            "doctor_status": "not_run",
            "failure_reason": "private_repo_not_found",
        }

    public_origin_main_sha, public_error = _refresh_origin_main_sha(
        public_repo_path,
        workspace_root,
    )
    private_origin_main_sha, private_error = _refresh_origin_main_sha(
        private_repo_path,
        workspace_root,
    )
    if public_origin_main_sha is None or private_origin_main_sha is None:
        return {
            "attempted": True,
            "status": "warning",
            "public_origin_main": public_origin_main_sha,
            "private_origin_main": private_origin_main_sha,
            "lock_path": str(lock_path),
            "backup_path": None,
            "doctor_status": "not_run",
            "failure_reason": public_error or private_error or "origin_main_refresh_failed",
        }

    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup_path = lock_path.with_name(f"{lock_path.name}.bak.{timestamp}")
    try:
        original = lock_path.read_text(encoding="utf-8")
        shutil.copy2(lock_path, backup_path)
        updated = _replace_yaml_section_scalar(
            original,
            section="public",
            key="main_sha",
            replacement=public_origin_main_sha,
        )
        updated = _replace_yaml_section_scalar(
            updated,
            section="private",
            key="current_main_sha",
            replacement=private_origin_main_sha,
        )
        lock_path.write_text(updated, encoding="utf-8")
    except Exception:
        return {
            "attempted": True,
            "status": "warning",
            "public_origin_main": public_origin_main_sha,
            "private_origin_main": private_origin_main_sha,
            "lock_path": str(lock_path),
            "backup_path": str(backup_path),
            "doctor_status": "not_run",
            "failure_reason": "lock_update_failed",
        }

    doctor = _run_post_reconciliation_doctor(public_repo_path, workspace_root)
    doctor_status = "ok" if doctor.get("status") == "passed" else "warning"
    return {
        "attempted": True,
        "status": "ok" if doctor_status == "ok" else "warning",
        "public_origin_main": public_origin_main_sha,
        "private_origin_main": private_origin_main_sha,
        "lock_path": str(lock_path),
        "backup_path": str(backup_path),
        "doctor_status": doctor_status,
        "failure_reason": None if doctor_status == "ok" else "doctor_failed",
    }


def _git_env_with_credentials(workspace_root: Path) -> dict[str, str]:
    for env_dir in (workspace_root, workspace_root.parent):
        _load_dotenv(env_dir / ".env")
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "Never")
    return env


def _git_has_configured_credential_helper(repo_path: Path, env: dict[str, str]) -> bool:
    for key in ("credential.helper", "credential.https://github.com.helper"):
        completed = _git(repo_path, "config", "--get-all", key, env=env)
        if completed.returncode == 0 and any(line.strip() for line in completed.stdout.splitlines()):
            return True
    return False


def _origin_remote_url(repo_path: Path, env: dict[str, str]) -> str:
    completed = _git(repo_path, "remote", "get-url", "origin", env=env)
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def _origin_requires_github_credentials(repo_path: Path, env: dict[str, str]) -> bool:
    remote_url = _origin_remote_url(repo_path, env).lower()
    return remote_url.startswith("https://github.com/")


def _credential_helper_for_env_token(env: dict[str, str]) -> str | None:
    if env.get("GIT_TOKEN") or env.get("GITHUB_TOKEN"):
        return "!f() { echo username=git; echo password=${GIT_TOKEN:-$GITHUB_TOKEN}; }; f"
    return None


def _missing_noninteractive_auth_message() -> str:
    return (
        "auth_error: no non-interactive GitHub auth available for promote-main. "
        + GITHUB_AUTH_SCOPE_HINT
    )


def _classify_git_failure(output: str) -> str | None:
    text = str(output or "").lower()
    if not text:
        return None
    auth_markers = (
        "auth_error:",
        "authentication failed",
        "invalid username or token",
        "could not read username",
        "could not read password",
        "terminal prompts disabled",
        "permission to ",
        "403",
    )
    network_markers = (
        "could not resolve host",
        "temporary failure in name resolution",
        "failed to connect",
        "connection timed out",
        "connection reset",
        "network is unreachable",
        "tls",
        "proxy error",
    )
    if any(marker in text for marker in auth_markers):
        return "auth_error"
    if any(marker in text for marker in network_markers):
        return "network_error"
    return None


def _git_with_credentials(
    repo_path: Path,
    workspace_root: Path,
    *args: str,
    allow_main_push: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = _git_env_with_credentials(workspace_root)
    if allow_main_push:
        env[MAIN_PUSH_BYPASS_ENV] = "1"
    helper = _credential_helper_for_env_token(env)
    git_args = list(args)
    if helper:
        git_args = ["-c", "credential.helper=" + helper, *git_args]
    elif _origin_requires_github_credentials(repo_path, env) and not _git_has_configured_credential_helper(repo_path, env):
        return subprocess.CompletedProcess(
            args=["git", *git_args],
            returncode=1,
            stdout="",
            stderr=_missing_noninteractive_auth_message(),
        )
    return _git(repo_path, *git_args, env=env)


def _materialize_synthetic_tree(
    repo_path: Path,
    *,
    main_base: str,
    source_sha: str,
    gitops_commit_sha: str | None,
    env_delta_files: list[str],
    delta_base: str | None = None,
) -> tuple[str | None, str | None]:
    with tempfile.TemporaryDirectory(prefix="amof-promote-main-") as temp_dir:
        temp_path = Path(temp_dir)
        add_completed = _git(repo_path, "worktree", "add", "--detach", str(temp_path), main_base)
        if add_completed.returncode != 0:
            return None, (add_completed.stderr or add_completed.stdout or "").strip() or "git worktree add failed"
        try:
            code_patch = _patch_text(repo_path, delta_base if delta_base is not None else main_base, source_sha)
            _apply_patch(temp_path, code_patch)
            if gitops_commit_sha and env_delta_files:
                env_patch = _patch_text(repo_path, source_sha, gitops_commit_sha, pathspecs=env_delta_files)
                _apply_patch(temp_path, env_patch)
            tree_sha = _git_ok(temp_path, "write-tree")
            return tree_sha, None
        except Exception as exc:
            return None, str(exc)
        finally:
            _git(repo_path, "worktree", "remove", "--force", str(temp_path))


def _create_synthetic_commit(
    repo_path: Path,
    *,
    workspace_root: Path,
    main_base: str,
    source_sha: str,
    gitops_commit_sha: str | None,
    env_delta_files: list[str],
    commit_message: str,
    delta_base: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    with tempfile.TemporaryDirectory(prefix="amof-promote-main-") as temp_dir:
        temp_path = Path(temp_dir)
        add_completed = _git(repo_path, "worktree", "add", "--detach", str(temp_path), main_base)
        if add_completed.returncode != 0:
            return None, None, (add_completed.stderr or add_completed.stdout or "").strip() or "git worktree add failed"
        try:
            code_patch = _patch_text(repo_path, delta_base if delta_base is not None else main_base, source_sha)
            _apply_patch(temp_path, code_patch)
            if gitops_commit_sha and env_delta_files:
                env_patch = _patch_text(repo_path, source_sha, gitops_commit_sha, pathspecs=env_delta_files)
                _apply_patch(temp_path, env_patch)
            tree_sha = _git_ok(temp_path, "write-tree")

            env = _git_env_with_credentials(workspace_root)
            env.setdefault("GIT_AUTHOR_NAME", "AMOF Promote")
            env.setdefault("GIT_AUTHOR_EMAIL", "operator@amof.dev")
            env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
            env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
            commit_sha = _git_ok(
                temp_path,
                "commit-tree",
                tree_sha,
                "-p",
                main_base,
                input_text=commit_message,
                env=env,
            )
            return tree_sha, commit_sha, None
        except Exception as exc:
            return None, None, str(exc)
        finally:
            _git(repo_path, "worktree", "remove", "--force", str(temp_path))


def _promotion_receipts_root(workspace_root: Path, ticket_id: str) -> Path:
    ticket_dir = str(ticket_id).strip()
    if len(workspace_root.parts) >= 3 and tuple(workspace_root.parts[-3:]) == ("receipts", "promote-main", ticket_dir):
        return workspace_root
    return workspace_root / "receipts" / "promote-main" / ticket_dir


def _promotion_audit_dir(
    workspace_root: Path,
    *,
    ecosystem: str | None,
    ticket_id: str,
) -> Path:
    if ecosystem:
        return get_ecosystem_root(ecosystem, str(workspace_root)) / AUDIT_SUBDIR
    return _promotion_receipts_root(workspace_root, ticket_id) / AUDIT_SUBDIR


def _write_audit_record(
    workspace_root: Path,
    ecosystem: str | None,
    plan: PromoteMainPlan,
) -> Path:
    audit_dir = _promotion_audit_dir(workspace_root, ecosystem=ecosystem, ticket_id=plan.ticket_id)
    ensure_dir(audit_dir)
    timestamp = time.strftime("%Y-%m-%d-%H%M%S")
    filename = f"{timestamp}-promote-main-{_slugify(plan.ticket_id)}.json"
    path = audit_dir / filename
    path.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def _rewrite_audit_record(workspace_root: Path, plan: PromoteMainPlan) -> None:
    audit_path = workspace_root / plan.audit_record_path
    audit_path.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")


def _finalize_plan(
    plan: PromoteMainPlan,
    *,
    workspace_root: Path,
    ecosystem: str | None,
    lock_final_status: str,
) -> PromoteMainPlan:
    audit_dir = _promotion_audit_dir(workspace_root, ecosystem=ecosystem, ticket_id=plan.ticket_id)
    ensure_dir(audit_dir)
    timestamp = time.strftime("%Y-%m-%d-%H%M%S")
    filename = f"{timestamp}-promote-main-{_slugify(plan.ticket_id)}.json"
    audit_path = audit_dir / filename
    finalized = PromoteMainPlan(
        **{
            **plan.to_dict(),
            "audit_record_path": _display_path(audit_path, workspace_root=workspace_root),
            "lock_final_status": lock_final_status,
        }
    )
    audit_path.write_text(json.dumps(finalized.to_dict(), indent=2) + "\n", encoding="utf-8")
    return finalized


def _lock_payload_for(bundle: PromoteMainInput, *, promotion_id: str, expected_main_sha: str) -> dict[str, Any]:
    return {
        "promotion_id": promotion_id,
        "ticket_id": bundle.ticket_id,
        "candidate_branch": bundle.candidate_branch,
        "source_sha": bundle.source_sha,
        "gitops_commit_sha": bundle.gitops_commit_sha,
        "expected_main_sha": expected_main_sha,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _find_existing_promotion(
    repo_path: Path,
    *,
    ref: str,
    bundle_id: str,
    source_sha: str,
    gitops_commit_sha: str | None,
) -> str | None:
    gitops_marker = gitops_commit_sha or NO_GITOPS_SHA
    log_output = _git_ok(repo_path, "log", ref, "--format=%H%x1f%B%x1e")
    for entry in log_output.split("\x1e"):
        if not entry.strip():
            continue
        sha, _, message = entry.partition("\x1f")
        if not sha.strip():
            continue
        if f"Bundle-ID: {bundle_id}" in message:
            return sha.strip()
        if (
            f"Source-SHA: {source_sha}" in message
            and f"GitOps-SHA: {gitops_marker}" in message
        ):
            return sha.strip()
    return None


def _is_synthetic_promotion_commit(repo_path: Path, commit_sha: str) -> bool:
    message = _git_ok(repo_path, "show", "-s", "--format=%B", commit_sha)
    first_line = message.splitlines()[0] if message.splitlines() else ""
    return (
        first_line.startswith(PROMOTION_SUBJECT_PREFIX)
        and "Promotion-ID:" in message
        and "Bundle-ID:" in message
    )


def _push_synthetic_commit(
    repo_path: Path,
    *,
    workspace_root: Path,
    synthetic_commit_sha: str,
) -> tuple[bool, str]:
    completed = _git_with_credentials(
        repo_path,
        workspace_root,
        "push",
        "origin",
        f"{synthetic_commit_sha}:refs/heads/main",
        allow_main_push=True,
    )
    output = (completed.stderr or completed.stdout or "").strip()
    return completed.returncode == 0, output


def _revert_synthetic_commit(
    repo_path: Path,
    *,
    workspace_root: Path,
    synthetic_commit_sha: str,
    current_origin_main_sha: str,
) -> tuple[str | None, str | None]:
    with tempfile.TemporaryDirectory(prefix="amof-promote-main-revert-") as temp_dir:
        temp_path = Path(temp_dir)
        add_completed = _git(repo_path, "worktree", "add", "--detach", str(temp_path), current_origin_main_sha)
        if add_completed.returncode != 0:
            return None, (add_completed.stderr or add_completed.stdout or "").strip() or "git worktree add failed"
        try:
            env = _git_env_with_credentials(workspace_root)
            env.setdefault("GIT_AUTHOR_NAME", "AMOF Revert")
            env.setdefault("GIT_AUTHOR_EMAIL", "operator@amof.dev")
            env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
            env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
            completed = _git(temp_path, "revert", "--no-edit", synthetic_commit_sha, env=env)
            if completed.returncode != 0:
                return None, (completed.stderr or completed.stdout or "").strip() or "git revert failed"
            return _git_ok(temp_path, "rev-parse", "HEAD"), None
        finally:
            _git(repo_path, "worktree", "remove", "--force", str(temp_path))


def _workspace_repo_path_candidates(workspace_root: Path, repo_name: str) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in (workspace_root, *workspace_root.parents):
        if repo_name == "amof" and (candidate / ".git").exists() and (candidate / "scripts" / "amof").is_dir():
            repo_path = candidate.resolve()
            if repo_path not in seen:
                seen.add(repo_path)
                candidates.append(repo_path)
        repo_path = (candidate / "repos" / repo_name).resolve()
        if repo_path in seen:
            continue
        seen.add(repo_path)
        candidates.append(repo_path)
    return candidates


def _resolve_repo_path(manifest: dict[str, Any], workspace_root: Path, repo_name: str) -> Path:
    for repo in manifest.get("repos", []):
        if repo.get("name") == repo_name:
            configured_path = (workspace_root / str(repo.get("path") or f"repos/{repo_name}")).resolve()
            if configured_path.exists():
                return configured_path
            return configured_path
    for repo_path in _workspace_repo_path_candidates(workspace_root, repo_name):
        if repo_path.exists():
            return repo_path
    raise RuntimeError(
        f"repo {repo_name} could not be resolved from manifest or workspace layout starting at {workspace_root}"
    )


def plan_promote_main_dry_run(
    manifest: dict[str, Any],
    bundle: PromoteMainInput,
    *,
    ecosystem: str | None,
    workspace_root: Path | None = None,
) -> PromoteMainPlan:
    workspace_root = (workspace_root or resolve_workspace_root()).resolve()
    target = _resolve_promote_main_target(manifest, workspace_root, bundle.repo)
    repo_path = target.repo_path
    if not repo_path.exists():
        raise RuntimeError(f"repo path does not exist: {repo_path}")

    ticket_id = _normalize_ticket_id(bundle.ticket_id)
    branch_ticket_id = (infer_ticket_id(bundle.candidate_branch) or "").upper()
    legacy_numeric_fallback_used = _is_legacy_numeric_ticket_id(ticket_id)

    source_sha = _resolve_commit(repo_path, bundle.source_sha)
    gitops_input = str(bundle.gitops_commit_sha or "").strip()
    gitops_commit_sha = _resolve_commit(repo_path, gitops_input) if gitops_input else None
    expected_main_sha = _resolve_commit(repo_path, bundle.expected_main_sha)
    # Candidate-delta base: promotion applies only diff(merge_base..source) so a
    # stale candidate never reverts main's advancement. For a non-stale candidate
    # based directly on expected_main_sha the merge-base equals expected_main_sha,
    # preserving the existing single-candidate behavior exactly.
    _merge_base_completed = _git(repo_path, "merge-base", source_sha, expected_main_sha)
    delta_base = (
        _merge_base_completed.stdout.strip()
        if _merge_base_completed.returncode == 0 and _merge_base_completed.stdout.strip()
        else expected_main_sha
    )
    bundle_id = _bundle_id(bundle.repo, ticket_id, source_sha, gitops_commit_sha, expected_main_sha)
    promotion_id = _promotion_id(bundle_id)
    lock_path = workspace_root / LOCK_DIR / f"promote-main-{_slugify(bundle.repo)}.lock"
    lock_payload = _lock_payload_for(
        PromoteMainInput(
            repo=bundle.repo,
            ticket_id=ticket_id,
            candidate_branch=bundle.candidate_branch,
            source_sha=source_sha,
            gitops_commit_sha=gitops_commit_sha,
            expected_main_sha=expected_main_sha,
            promotion_reason=bundle.promotion_reason,
            dry_run=bundle.dry_run,
        ),
        promotion_id=promotion_id,
        expected_main_sha=expected_main_sha,
    )

    validation_checks: dict[str, bool] = {
        "candidate_branch_exists": False,
        "source_sha_reachable_from_candidate_branch": False,
        "gitops_commit_sha_reachable_from_candidate_branch": False,
        "source_sha_included_before_env_commit": False,
        "env_commit_is_amof_origin": False,
        "code_delta_has_code_truth_only": False,
        "env_delta_scope_valid": False,
        "env_commit_matches_source_sha": False,
        "ticket_linkage_consistent": False,
        "origin_main_matches_expected_main_sha": False,
        "stale_base_overlap_free": False,
    }
    if bundle.require_run_summary or bundle.require_promotion_readiness_result:
        validation_checks["promotion_readiness_evidence_path_allowed"] = False
        validation_checks["promotion_readiness_evidence_valid"] = False

    rejection_reason: str | None = None
    failure_classification: str | None = None
    current_origin_main_sha: str | None = None
    code_delta_files: list[str] = []
    env_delta_files: list[str] = []
    synthetic_tree_sha: str | None = None
    current_main_advanced_paths: list[str] = []
    overlap_paths: list[str] = []
    stale_base: bool = False
    promotion_target_policy_path = (
        str(target.policy_path.resolve(strict=False)) if target.policy_path is not None else None
    )
    synthetic_commit_message = _synthetic_commit_message(
        PromoteMainInput(
            repo=bundle.repo,
            ticket_id=ticket_id,
            candidate_branch=bundle.candidate_branch,
            source_sha=source_sha,
            gitops_commit_sha=gitops_commit_sha,
            expected_main_sha=expected_main_sha,
            promotion_reason=bundle.promotion_reason,
            dry_run=bundle.dry_run,
            require_run_summary=bundle.require_run_summary,
            require_promotion_readiness_result=bundle.require_promotion_readiness_result,
        ),
        promotion_id,
        bundle_id,
    )

    if rejection_reason is None and (bundle.require_run_summary or bundle.require_promotion_readiness_result):
        try:
            _validate_optional_promotion_evidence(
                bundle,
                source_sha=source_sha,
                repo_path=repo_path,
                workspace_root=workspace_root,
            )
        except RuntimeError as exc:
            rejection_reason = str(exc)
        else:
            validation_checks["promotion_readiness_evidence_path_allowed"] = True
            validation_checks["promotion_readiness_evidence_valid"] = True

    try:
        with PromotionLock(lock_path, lock_payload):
            if rejection_reason is None:
                try:
                    _validate_target_remote(target, workspace_root=workspace_root)
                except RuntimeError as exc:
                    rejection_reason = str(exc)

            if not _branch_exists(repo_path, bundle.candidate_branch):
                rejection_reason = f"candidate branch {bundle.candidate_branch} does not exist"
            else:
                validation_checks["candidate_branch_exists"] = True

            if rejection_reason is None and _is_ancestor(repo_path, source_sha, bundle.candidate_branch):
                validation_checks["source_sha_reachable_from_candidate_branch"] = True
            elif rejection_reason is None:
                rejection_reason = "source_sha is not reachable from candidate_branch"

            if gitops_commit_sha is None:
                validation_checks["gitops_commit_sha_reachable_from_candidate_branch"] = True
                validation_checks["source_sha_included_before_env_commit"] = True
            elif rejection_reason is None and _is_ancestor(repo_path, gitops_commit_sha, bundle.candidate_branch):
                validation_checks["gitops_commit_sha_reachable_from_candidate_branch"] = True
            elif rejection_reason is None:
                rejection_reason = "gitops_commit_sha is not reachable from candidate_branch"

            if (
                gitops_commit_sha is not None
                and rejection_reason is None
                and _is_ancestor(repo_path, source_sha, gitops_commit_sha)
            ):
                validation_checks["source_sha_included_before_env_commit"] = True
            elif gitops_commit_sha is not None and rejection_reason is None:
                rejection_reason = "source_sha is not included before gitops_commit_sha"

            code_delta_entries = _name_status_diff(repo_path, delta_base, source_sha)
            code_delta_files = [path for _, path in code_delta_entries]
            forbidden_code_delta = _forbidden_code_delta_files(code_delta_entries)
            if rejection_reason is None and code_delta_files and not forbidden_code_delta:
                validation_checks["code_delta_has_code_truth_only"] = True
            elif rejection_reason is None and not code_delta_files:
                rejection_reason = "candidate_code_delta is empty"
            elif rejection_reason is None:
                listed = ", ".join(forbidden_code_delta[:5])
                if len(forbidden_code_delta) > 5:
                    listed += ", ..."
                rejection_reason = (
                    "candidate_code_delta contains forbidden deployment-state artifacts"
                    f": {listed}"
                )

            if gitops_commit_sha is None:
                validation_checks["env_delta_scope_valid"] = True
                validation_checks["env_commit_is_amof_origin"] = True
                validation_checks["env_commit_matches_source_sha"] = True
                validation_checks["ticket_linkage_consistent"] = ticket_id == branch_ticket_id
                if rejection_reason is None and not validation_checks["ticket_linkage_consistent"]:
                    rejection_reason = (
                        "ticket linkage is inconsistent across branch and input bundle "
                        f"(branch_ticket_id={branch_ticket_id or '<missing>'}, input_ticket_id={ticket_id})"
                    )
            else:
                env_delta_entries = _name_status_diff(repo_path, source_sha, gitops_commit_sha)
                env_delta_files = [path for _, path in env_delta_entries]
                if rejection_reason is None and env_delta_files and all(_is_allowed_gitops_path(path) for path in env_delta_files):
                    validation_checks["env_delta_scope_valid"] = True
                elif rejection_reason is None:
                    rejection_reason = "candidate_env_delta is empty or changes files outside allowed GitOps scope"

                gitops_subject = _commit_subject(repo_path, gitops_commit_sha)
                gitops_changed_files = _commit_changed_files(repo_path, gitops_commit_sha)
                if rejection_reason is None and ENV_ONLY_COMMIT_MESSAGE_RE.match(gitops_subject) and all(
                    _is_allowed_gitops_path(path) for path in gitops_changed_files
                ):
                    validation_checks["env_commit_is_amof_origin"] = True
                elif rejection_reason is None:
                    rejection_reason = "gitops_commit_sha is not an AMOF-origin env-only commit"

                if rejection_reason is None:
                    env_match_ok, env_match_reason = _env_files_match_bundle(
                        repo_path,
                        gitops_commit_sha,
                        env_delta_files,
                        ticket_id=ticket_id,
                        source_sha=source_sha,
                    )
                    if env_match_ok:
                        validation_checks["env_commit_matches_source_sha"] = True
                    else:
                        rejection_reason = env_match_reason or "env commit does not resolve to the same source_sha"

                if rejection_reason is None:
                    message_ticket_ok = ticket_id.lower().replace("-", "-") in gitops_subject.lower()
                    validation_checks["ticket_linkage_consistent"] = (
                        ticket_id == branch_ticket_id and message_ticket_ok
                    )
                    if not validation_checks["ticket_linkage_consistent"]:
                        rejection_reason = (
                            "ticket linkage is inconsistent across branch, env commit, and input bundle "
                            f"(branch_ticket_id={branch_ticket_id or '<missing>'}, input_ticket_id={ticket_id}, "
                            f"env_commit_subject_matches={'yes' if message_ticket_ok else 'no'})"
                        )

            if rejection_reason is None:
                fetch_ok, fetch_out = _fetch_origin_main(repo_path, workspace_root)
                if not fetch_ok:
                    rejection_reason = f"failed to refresh origin/main: {fetch_out or 'git fetch failed'}"
                    failure_classification = _classify_git_failure(fetch_out)
                else:
                    current_origin_main_sha = _resolve_commit(repo_path, "origin/main")
                    if current_origin_main_sha == expected_main_sha:
                        validation_checks["origin_main_matches_expected_main_sha"] = True
                    else:
                        rejection_reason = (
                            f"origin/main drifted to {current_origin_main_sha}; expected {expected_main_sha}"
                        )
                        failure_classification = "remote_diverged"

            if rejection_reason is None and current_origin_main_sha is not None:
                merge_base, overlapping_files = _stale_base_overlap_details(
                    repo_path,
                    source_sha=source_sha,
                    current_origin_main_sha=current_origin_main_sha,
                )
                # Authoritative post-fetch merge-base; equals delta_base whenever the
                # promotion proceeds (origin_main_matches_expected_main_sha is enforced).
                delta_base = merge_base
                current_main_advanced_paths = _changed_files(repo_path, merge_base, current_origin_main_sha)
                overlap_paths = list(overlapping_files)
                stale_base = not _is_ancestor(repo_path, current_origin_main_sha, source_sha)
                if overlapping_files:
                    rejection_reason = (
                        "stale-base overlap detected: "
                        f"candidate merge-base {merge_base}; "
                        f"expected_main_sha={expected_main_sha}; "
                        f"current_origin_main_sha={current_origin_main_sha}; "
                        "overlapping files: "
                        f"{', '.join(overlapping_files)}. "
                        "Replay or rebase the candidate on current origin/main and regenerate the promotion bundle."
                    )
                else:
                    validation_checks["stale_base_overlap_free"] = True

            if rejection_reason is None:
                synthetic_tree_sha, materialize_error = _materialize_synthetic_tree(
                    repo_path,
                    main_base=expected_main_sha,
                    source_sha=source_sha,
                    gitops_commit_sha=gitops_commit_sha,
                    env_delta_files=env_delta_files,
                    delta_base=delta_base,
                )
                if materialize_error:
                    rejection_reason = f"failed to materialize synthetic result tree: {materialize_error}"

            status = "ready" if rejection_reason is None else "rejected"
            plan = PromoteMainPlan(
                ok=rejection_reason is None,
                status=status,
                mode="dry-run",
                repo=bundle.repo,
                repo_path=str(repo_path),
                ticket_id=ticket_id,
                candidate_branch=bundle.candidate_branch,
                source_sha=source_sha,
                gitops_commit_sha=gitops_commit_sha,
                expected_main_sha=expected_main_sha,
                current_origin_main_sha=current_origin_main_sha,
                promotion_id=promotion_id,
                bundle_id=bundle_id,
                code_delta_files=code_delta_files,
                env_delta_files=env_delta_files,
                synthetic_commit_message=synthetic_commit_message,
                synthetic_tree_sha=synthetic_tree_sha,
                audit_record_path="",
                lock_path=_display_path(lock_path, workspace_root=workspace_root),
                lock_status="acquired",
                lock_final_status="in_progress",
                validation_checks=validation_checks,
                rejection_reason=rejection_reason,
                failure_classification=failure_classification,
                legacy_numeric_fallback_used=legacy_numeric_fallback_used,
                promotion_target_policy_path=promotion_target_policy_path,
                merge_base_sha=delta_base,
                candidate_delta_paths=list(code_delta_files),
                current_main_advanced_paths=list(current_main_advanced_paths),
                overlap_paths=list(overlap_paths),
                stale_base=stale_base,
            )
    except PromotionLockError as exc:
        plan = PromoteMainPlan(
            ok=False,
            status="rejected",
            mode="dry-run",
            repo=bundle.repo,
            repo_path=str(repo_path),
            ticket_id=ticket_id,
            candidate_branch=bundle.candidate_branch,
            source_sha=source_sha,
            gitops_commit_sha=gitops_commit_sha,
            expected_main_sha=expected_main_sha,
            current_origin_main_sha=None,
            promotion_id=promotion_id,
            bundle_id=bundle_id,
            code_delta_files=[],
            env_delta_files=[],
            synthetic_commit_message=synthetic_commit_message,
            synthetic_tree_sha=None,
            audit_record_path="",
            lock_path=_display_path(lock_path, workspace_root=workspace_root),
            lock_status="unavailable",
            lock_final_status="not_acquired",
            validation_checks=validation_checks,
            rejection_reason=str(exc),
            failure_classification=None,
            legacy_numeric_fallback_used=legacy_numeric_fallback_used,
            promotion_target_policy_path=promotion_target_policy_path,
        )

    lock_final_status = "released" if plan.lock_status == "acquired" else plan.lock_final_status
    return _finalize_plan(
        plan,
        workspace_root=workspace_root,
        ecosystem=ecosystem,
        lock_final_status=lock_final_status,
    )


def _print_plan(plan: PromoteMainPlan) -> None:
    print("[promote-main] Promotion result")
    print(f"  Status: {plan.status}")
    print(f"  Mode: {plan.mode}")
    print(f"  Repo: {plan.repo}")
    print(f"  Ticket: {plan.ticket_id}")
    if plan.legacy_numeric_fallback_used:
        print("  LEGACY_NUMERIC_FALLBACK_USED")
    print(f"  Candidate branch: {plan.candidate_branch}")
    print(f"  Source SHA: {plan.source_sha}")
    print(f"  GitOps SHA: {plan.gitops_commit_sha or NO_GITOPS_SHA}")
    print(f"  Expected main SHA: {plan.expected_main_sha}")
    print(f"  Current origin/main SHA: {plan.current_origin_main_sha or '<unresolved>'}")
    print(f"  Promotion ID: {plan.promotion_id}")
    print(f"  Bundle ID: {plan.bundle_id[:16]}")
    print(f"  Lock: {plan.lock_path} ({plan.lock_status} -> {plan.lock_final_status})")
    print(f"  Audit record: {plan.audit_record_path}")
    if plan.promotion_target_policy_path:
        print(f"  Promotion target policy: {plan.promotion_target_policy_path}")
    if plan.already_promoted_commit_sha:
        print(f"  Already promoted commit: {plan.already_promoted_commit_sha}")
    if plan.rejection_reason:
        print(f"  Reject reason: {plan.rejection_reason}")
    if plan.failure_stage:
        print(f"  Failure stage: {plan.failure_stage}")
    if plan.failure_reason:
        print(f"  Failure reason: {plan.failure_reason}")
    if plan.failure_classification:
        print(f"  Failure classification: {plan.failure_classification}")
    if plan.compat_lock_reconciliation:
        reconciliation = plan.compat_lock_reconciliation
        print("  Compat lock reconciliation:")
        print(f"    status: {reconciliation.get('status', '<unknown>')}")
        if reconciliation.get("lock_path"):
            print(f"    lock: {reconciliation['lock_path']}")
        if reconciliation.get("backup_path"):
            print(f"    backup: {reconciliation['backup_path']}")
        if reconciliation.get("doctor_status"):
            print(f"    doctor: {reconciliation['doctor_status']}")
    print("  Code delta files:")
    for path in plan.code_delta_files or ["<none>"]:
        print(f"    - {path}")
    print("  Env delta files:")
    for path in plan.env_delta_files or ["<none>"]:
        print(f"    - {path}")
    print("  Validation checks:")
    for key, passed in plan.validation_checks.items():
        print(f"    - {key}: {'pass' if passed else 'fail'}")
    print("  Synthetic commit message:")
    for line in plan.synthetic_commit_message.rstrip().splitlines():
        print(f"    {line}")
    print(f"  Synthetic tree SHA: {plan.synthetic_tree_sha or '<not materialized>'}")
    if plan.synthetic_commit_sha:
        print(f"  Synthetic commit SHA: {plan.synthetic_commit_sha}")
    if plan.result_main_sha:
        print(f"  Result main SHA: {plan.result_main_sha}")
    print(f"  Push attempted: {'yes' if plan.push_attempted else 'no'}")
    print(f"  Push succeeded: {'yes' if plan.push_succeeded else 'no'}")


def _print_revert_result(result: PromoteMainRevertResult) -> None:
    print("[promote-main-revert] Revert result")
    print(f"  Status: {result.status}")
    print(f"  Repo: {result.repo}")
    print(f"  Synthetic commit SHA: {result.synthetic_commit_sha}")
    print(f"  Current origin/main SHA: {result.current_origin_main_sha or '<unresolved>'}")
    print(f"  Lock: {result.lock_path} ({result.lock_status} -> {result.lock_final_status})")
    if result.revert_commit_sha:
        print(f"  Revert commit SHA: {result.revert_commit_sha}")
    if result.failure_reason:
        print(f"  Failure reason: {result.failure_reason}")


def execute_promote_main_push(
    manifest: dict[str, Any],
    bundle: PromoteMainInput,
    *,
    ecosystem: str | None,
    workspace_root: Path | None = None,
) -> PromoteMainPlan:
    workspace_root = (workspace_root or resolve_workspace_root()).resolve()
    plan = plan_promote_main_dry_run(manifest, bundle, ecosystem=ecosystem, workspace_root=workspace_root)
    plan = replace(plan, mode="push")
    _rewrite_audit_record(workspace_root, plan)

    repo_path = Path(plan.repo_path)
    existing_promotion_sha = None
    if plan.current_origin_main_sha:
        existing_promotion_sha = _find_existing_promotion(
            repo_path,
            ref=plan.current_origin_main_sha,
            bundle_id=plan.bundle_id,
            source_sha=plan.source_sha,
            gitops_commit_sha=plan.gitops_commit_sha,
        )

    if existing_promotion_sha:
        finalized = replace(
            plan,
            ok=False,
            status="rejected",
            already_promoted_commit_sha=existing_promotion_sha,
            failure_stage="already_promoted",
            failure_reason=f"bundle already promoted on main at {existing_promotion_sha}",
            rejection_reason=f"bundle already promoted on main at {existing_promotion_sha}",
        )
        _rewrite_audit_record(workspace_root, finalized)
        return finalized

    if not plan.ok:
        finalized = replace(
            plan,
            failure_stage="pre_write_validation",
            failure_reason=plan.rejection_reason,
        )
        _rewrite_audit_record(workspace_root, finalized)
        return finalized

    lock_path = workspace_root / plan.lock_path
    lock_payload = _lock_payload_for(
        bundle,
        promotion_id=plan.promotion_id,
        expected_main_sha=plan.expected_main_sha,
    )

    try:
        with PromotionLock(lock_path, lock_payload):
            fetch_ok, fetch_out = _fetch_origin_main(repo_path, workspace_root)
            if not fetch_ok:
                finalized = replace(
                    plan,
                    ok=False,
                    status="failed",
                    lock_status="acquired",
                    lock_final_status="in_progress",
                    failure_stage="refresh_origin_main",
                    failure_reason=f"failed to refresh origin/main: {fetch_out or 'git fetch failed'}",
                    failure_classification=_classify_git_failure(fetch_out),
                )
                _rewrite_audit_record(workspace_root, finalized)
                return finalized

            current_origin_main_sha = _resolve_commit(repo_path, "origin/main")
            if current_origin_main_sha != plan.expected_main_sha:
                existing_promotion_sha = _find_existing_promotion(
                    repo_path,
                    ref="origin/main",
                    bundle_id=plan.bundle_id,
                    source_sha=plan.source_sha,
                    gitops_commit_sha=plan.gitops_commit_sha,
                )
                if existing_promotion_sha:
                    finalized = replace(
                        plan,
                        ok=False,
                        status="rejected",
                        current_origin_main_sha=current_origin_main_sha,
                        lock_status="acquired",
                        lock_final_status="in_progress",
                        already_promoted_commit_sha=existing_promotion_sha,
                        failure_stage="already_promoted",
                        failure_reason=f"bundle already promoted on main at {existing_promotion_sha}",
                        rejection_reason=f"bundle already promoted on main at {existing_promotion_sha}",
                    )
                else:
                    finalized = replace(
                        plan,
                        ok=False,
                        status="rejected",
                        current_origin_main_sha=current_origin_main_sha,
                        lock_status="acquired",
                        lock_final_status="in_progress",
                        failure_stage="recheck_origin_main",
                        failure_reason=f"origin/main drifted to {current_origin_main_sha}; expected {plan.expected_main_sha}",
                        rejection_reason=f"origin/main drifted to {current_origin_main_sha}; expected {plan.expected_main_sha}",
                        failure_classification="remote_diverged",
                    )
                _rewrite_audit_record(workspace_root, finalized)
                return finalized

            existing_promotion_sha = _find_existing_promotion(
                repo_path,
                ref="origin/main",
                bundle_id=plan.bundle_id,
                source_sha=plan.source_sha,
                gitops_commit_sha=plan.gitops_commit_sha,
            )
            if existing_promotion_sha:
                finalized = replace(
                    plan,
                    ok=False,
                    status="rejected",
                    lock_status="acquired",
                    lock_final_status="in_progress",
                    already_promoted_commit_sha=existing_promotion_sha,
                    failure_stage="already_promoted",
                    failure_reason=f"bundle already promoted on main at {existing_promotion_sha}",
                    rejection_reason=f"bundle already promoted on main at {existing_promotion_sha}",
                )
                _rewrite_audit_record(workspace_root, finalized)
                return finalized

            synthetic_tree_sha, synthetic_commit_sha, materialize_error = _create_synthetic_commit(
                repo_path,
                workspace_root=workspace_root,
                main_base=plan.expected_main_sha,
                source_sha=plan.source_sha,
                gitops_commit_sha=plan.gitops_commit_sha,
                env_delta_files=plan.env_delta_files,
                commit_message=plan.synthetic_commit_message,
                delta_base=plan.merge_base_sha,
            )
            if materialize_error:
                finalized = replace(
                    plan,
                    ok=False,
                    status="failed",
                    lock_status="acquired",
                    lock_final_status="in_progress",
                    failure_stage="materialize_synthetic_commit",
                    failure_reason=materialize_error,
                )
                _rewrite_audit_record(workspace_root, finalized)
                return finalized

            if plan.synthetic_tree_sha and synthetic_tree_sha != plan.synthetic_tree_sha:
                finalized = replace(
                    plan,
                    ok=False,
                    status="failed",
                    lock_status="acquired",
                    lock_final_status="in_progress",
                    synthetic_tree_sha=synthetic_tree_sha,
                    failure_stage="synthetic_tree_mismatch",
                    failure_reason=(
                        "synthetic tree changed between validation and execution: "
                        f"{synthetic_tree_sha} != {plan.synthetic_tree_sha}"
                    ),
                )
                _rewrite_audit_record(workspace_root, finalized)
                return finalized

            push_ok, push_output = _push_synthetic_commit(
                repo_path,
                workspace_root=workspace_root,
                synthetic_commit_sha=synthetic_commit_sha or "",
            )
            if not push_ok:
                finalized = replace(
                    plan,
                    ok=False,
                    status="failed",
                    lock_status="acquired",
                    lock_final_status="in_progress",
                    synthetic_tree_sha=synthetic_tree_sha,
                    synthetic_commit_sha=synthetic_commit_sha,
                    push_attempted=True,
                    push_succeeded=False,
                    failure_stage="push_rejected",
                    failure_reason=push_output or "git push failed",
                    failure_classification=_classify_git_failure(push_output),
                )
                _rewrite_audit_record(workspace_root, finalized)
                return finalized

            finalized = replace(
                plan,
                ok=True,
                status="promoted",
                lock_status="acquired",
                lock_final_status="in_progress",
                synthetic_tree_sha=synthetic_tree_sha,
                synthetic_commit_sha=synthetic_commit_sha,
                result_main_sha=synthetic_commit_sha,
                push_attempted=True,
                push_succeeded=True,
            )
            if bundle.repo == "amof":
                finalized = replace(
                    finalized,
                    compat_lock_reconciliation=_reconcile_operator_compat_lock_after_promotion(
                        workspace_root=workspace_root,
                        public_repo_path=repo_path,
                    ),
                )
            _rewrite_audit_record(workspace_root, finalized)
    except PromotionLockError as exc:
        finalized = replace(
            plan,
            ok=False,
            status="rejected",
            lock_status="unavailable",
            lock_final_status="not_acquired",
            failure_stage="execution_lock_unavailable",
            failure_reason=str(exc),
            rejection_reason=str(exc),
        )
        _rewrite_audit_record(workspace_root, finalized)
        return finalized

    finalized = replace(finalized, lock_final_status="released")
    _rewrite_audit_record(workspace_root, finalized)
    return finalized


def execute_promote_main_revert(
    manifest: dict[str, Any],
    request: PromoteMainRevertInput,
    *,
    ecosystem: str,
    workspace_root: Path | None = None,
) -> PromoteMainRevertResult:
    workspace_root = (workspace_root or resolve_workspace_root()).resolve()
    repo_path = _resolve_repo_path(manifest, workspace_root, request.repo)
    synthetic_commit_sha = _resolve_commit(repo_path, request.synthetic_commit_sha)
    lock_path = workspace_root / LOCK_DIR / f"promote-main-{_slugify(request.repo)}.lock"
    lock_payload = {
        "action": "promote-main-revert",
        "synthetic_commit_sha": synthetic_commit_sha,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    try:
        with PromotionLock(lock_path, lock_payload):
            fetch_ok, fetch_out = _fetch_origin_main(repo_path, workspace_root)
            if not fetch_ok:
                return PromoteMainRevertResult(
                    ok=False,
                    status="failed",
                    repo=request.repo,
                    repo_path=str(repo_path),
                    synthetic_commit_sha=synthetic_commit_sha,
                    current_origin_main_sha=None,
                    revert_commit_sha=None,
                    lock_path=_display_path(lock_path, workspace_root=workspace_root),
                    lock_status="acquired",
                    lock_final_status="released",
                    failure_reason=f"failed to refresh origin/main: {fetch_out or 'git fetch failed'}",
                )

            current_origin_main_sha = _resolve_commit(repo_path, "origin/main")
            if not _is_ancestor(repo_path, synthetic_commit_sha, current_origin_main_sha):
                return PromoteMainRevertResult(
                    ok=False,
                    status="rejected",
                    repo=request.repo,
                    repo_path=str(repo_path),
                    synthetic_commit_sha=synthetic_commit_sha,
                    current_origin_main_sha=current_origin_main_sha,
                    revert_commit_sha=None,
                    lock_path=_display_path(lock_path, workspace_root=workspace_root),
                    lock_status="acquired",
                    lock_final_status="released",
                    failure_reason=f"{synthetic_commit_sha} is not reachable from origin/main",
                )
            if not _is_synthetic_promotion_commit(repo_path, synthetic_commit_sha):
                return PromoteMainRevertResult(
                    ok=False,
                    status="rejected",
                    repo=request.repo,
                    repo_path=str(repo_path),
                    synthetic_commit_sha=synthetic_commit_sha,
                    current_origin_main_sha=current_origin_main_sha,
                    revert_commit_sha=None,
                    lock_path=_display_path(lock_path, workspace_root=workspace_root),
                    lock_status="acquired",
                    lock_final_status="released",
                    failure_reason=f"{synthetic_commit_sha} is not an AMOF synthetic promotion commit",
                )

            revert_commit_sha, revert_error = _revert_synthetic_commit(
                repo_path,
                workspace_root=workspace_root,
                synthetic_commit_sha=synthetic_commit_sha,
                current_origin_main_sha=current_origin_main_sha,
            )
            if revert_error:
                return PromoteMainRevertResult(
                    ok=False,
                    status="failed",
                    repo=request.repo,
                    repo_path=str(repo_path),
                    synthetic_commit_sha=synthetic_commit_sha,
                    current_origin_main_sha=current_origin_main_sha,
                    revert_commit_sha=None,
                    lock_path=_display_path(lock_path, workspace_root=workspace_root),
                    lock_status="acquired",
                    lock_final_status="released",
                    failure_reason=revert_error,
                )

            push_completed = _git_with_credentials(
                repo_path,
                workspace_root,
                "push",
                "origin",
                f"{revert_commit_sha}:refs/heads/main",
                allow_main_push=True,
            )
            if push_completed.returncode != 0:
                return PromoteMainRevertResult(
                    ok=False,
                    status="failed",
                    repo=request.repo,
                    repo_path=str(repo_path),
                    synthetic_commit_sha=synthetic_commit_sha,
                    current_origin_main_sha=current_origin_main_sha,
                    revert_commit_sha=revert_commit_sha,
                    lock_path=_display_path(lock_path, workspace_root=workspace_root),
                    lock_status="acquired",
                    lock_final_status="released",
                    failure_reason=(push_completed.stderr or push_completed.stdout or "").strip() or "git push failed",
                )

            return PromoteMainRevertResult(
                ok=True,
                status="reverted",
                repo=request.repo,
                repo_path=str(repo_path),
                synthetic_commit_sha=synthetic_commit_sha,
                current_origin_main_sha=current_origin_main_sha,
                revert_commit_sha=revert_commit_sha,
                lock_path=_display_path(lock_path, workspace_root=workspace_root),
                lock_status="acquired",
                lock_final_status="released",
            )
    except PromotionLockError as exc:
        return PromoteMainRevertResult(
            ok=False,
            status="rejected",
            repo=request.repo,
            repo_path=str(repo_path),
            synthetic_commit_sha=synthetic_commit_sha,
            current_origin_main_sha=None,
            revert_commit_sha=None,
            lock_path=_display_path(lock_path, workspace_root=workspace_root),
            lock_status="unavailable",
            lock_final_status="not_acquired",
            failure_reason=str(exc),
        )


def cmd_promote_main(manifest: dict[str, Any], args: Any, ecosystem: str | None = None) -> int:
    """Validate or execute a promote-to-main candidate bundle."""
    dry_run = bool(getattr(args, "dry_run", False))
    push = bool(getattr(args, "push", False))
    require_run_summary = (
        str(getattr(args, "require_run_summary", "") or "").strip() or None
    )
    require_promotion_readiness_result = (
        str(getattr(args, "require_promotion_readiness_result", "") or "").strip() or None
    )
    if dry_run == push:
        sys.stderr.write("[promote-main] Choose exactly one of --dry-run or --push.\n")
        return 1
    if require_run_summary and require_promotion_readiness_result:
        sys.stderr.write(
            "[promote-main] Use only one of --require-run-summary or --require-promotion-readiness-result.\n"
        )
        return 1
    bundle = PromoteMainInput(
        repo=str(args.repo).strip(),
        ticket_id=_normalize_ticket_id(args.ticket_id),
        candidate_branch=str(args.candidate_branch).strip(),
        source_sha=str(args.source_sha).strip(),
        gitops_commit_sha=(str(args.gitops_commit_sha).strip() if getattr(args, "gitops_commit_sha", None) else None),
        expected_main_sha=str(args.expected_main_sha).strip(),
        promotion_reason=str(args.promotion_reason).strip(),
        dry_run=dry_run,
        require_run_summary=require_run_summary,
        require_promotion_readiness_result=require_promotion_readiness_result,
    )
    plan = (
        plan_promote_main_dry_run(manifest, bundle, ecosystem=ecosystem)
        if dry_run
        else execute_promote_main_push(manifest, bundle, ecosystem=ecosystem)
    )
    _print_plan(plan)
    return 0 if plan.ok else 1


def cmd_promote_main_revert(manifest: dict[str, Any], args: Any, ecosystem: str | None = None) -> int:
    """Revert one AMOF synthetic promotion commit on main."""
    if str(getattr(args, "repo", "") or "").strip() == "amof-private":
        sys.stderr.write("[promote-main-revert] amof-private is not supported by this revert path.\n")
        return 1
    if not ecosystem:
        sys.stderr.write("[promote-main-revert] Ecosystem could not be resolved.\n")
        return 1

    result = execute_promote_main_revert(
        manifest,
        PromoteMainRevertInput(
            repo=str(args.repo).strip(),
            synthetic_commit_sha=str(args.synthetic_commit_sha).strip(),
        ),
        ecosystem=ecosystem,
    )
    _print_revert_result(result)
    return 0 if result.ok else 1
