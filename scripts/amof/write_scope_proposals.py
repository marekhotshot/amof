"""Durable WriteScopeProposal identity store (Wave 1).

Workers propose; operators approve; runtime enforces.
A persisted proposal is evidence only — never mutation authority.

ID / hash semantics
-------------------
body_hash:
  sha256 of canonical JSON over these body fields only (sorted object keys;
  arrays keep normalized emission order):
    target_id, base_sha, allowed_roots, denied_roots,
    expected_checks, docs_only, source_mutation
  ``reason`` is stored on the frozen body but is intentionally excluded from
  the hash: it is human rationale and non-authoritative for path identity.

proposal_id:
  ``wsp-`` + sha256(f\"{run_id}:{body_hash}\")[:24]
  Same (run_id, body_hash) always yields the same proposal_id (idempotent).

Persistence never mutates a stored body. Load re-verifies body_hash; mismatch
fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_paths import ensure_parent_dir, write_scope_proposals_dir

PROPOSAL_KIND = "write_scope_proposal"
PROPOSAL_SCHEMA_VERSION = 1
PROPOSAL_STATUS_PROPOSED = "proposed"
PROPOSAL_ID_PREFIX = "wsp-"

# Frozen body fields persisted on disk.
WRITE_SCOPE_BODY_FIELDS = (
    "target_id",
    "base_sha",
    "allowed_roots",
    "denied_roots",
    "reason",
    "expected_checks",
    "docs_only",
    "source_mutation",
)

# Authority-relevant fields included in body_hash (reason excluded).
BODY_HASH_FIELDS = (
    "target_id",
    "base_sha",
    "allowed_roots",
    "denied_roots",
    "expected_checks",
    "docs_only",
    "source_mutation",
)

_HOSTILE_APPROVAL_KEYS = frozenset(
    {
        "approved_write_scope",
        "write_scope_approval",
        "write_scope_approval_id",
        "approval_id",
        "approved_by",
        "approved_at",
    }
)

_STORE_LOCK = threading.RLock()


class WriteScopeProposalError(ValueError):
    """Raised when a proposal cannot be persisted or loaded truthfully."""


@dataclass(frozen=True)
class PersistOutcome:
    persisted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    skipped_prose_only: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "persisted": list(self.persisted),
            "rejected": list(self.rejected),
            "skipped_prose_only": self.skipped_prose_only,
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Canonical repository-relative path classes for write-scope / grant authority.
# Missing/empty and repository-root scope are distinct; never coerce "" → ".".
PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE = "FILE_OR_DIRECTORY_RELATIVE_PATH"
PATH_CLASS_EXPLICIT_REPOSITORY_ROOT = "EXPLICIT_REPOSITORY_ROOT_SCOPE"
PATH_CLASS_MISSING_OR_INVALID = "MISSING_OR_INVALID_PATH"


@dataclass(frozen=True)
class RepositoryRelativePathClassification:
    path_class: str
    normalized: str | None
    detail: str
    raw: Any


def classify_repository_relative_scope_path(
    value: Any,
    *,
    missing: bool = False,
) -> RepositoryRelativePathClassification:
    """Classify a candidate repository-relative scope/tool path.

    Distinguishes:
    - FILE_OR_DIRECTORY_RELATIVE_PATH — normal repo-relative file/dir
    - EXPLICIT_REPOSITORY_ROOT_SCOPE — intentional root markers (``.``, ``./``)
    - MISSING_OR_INVALID_PATH — absent, empty, absolute, traversal, wildcards

    Empty string is never treated as repository-root scope.
    """
    if missing or value is None:
        return RepositoryRelativePathClassification(
            path_class=PATH_CLASS_MISSING_OR_INVALID,
            normalized=None,
            detail="missing_path",
            raw=value,
        )
    if not isinstance(value, str):
        return RepositoryRelativePathClassification(
            path_class=PATH_CLASS_MISSING_OR_INVALID,
            normalized=None,
            detail="non_string_path",
            raw=value,
        )
    path = value.strip()
    if not path:
        return RepositoryRelativePathClassification(
            path_class=PATH_CLASS_MISSING_OR_INVALID,
            normalized=None,
            detail="empty_path",
            raw=value,
        )
    # Explicit repository-root markers only — not accidental empties.
    if path in {".", "./"}:
        return RepositoryRelativePathClassification(
            path_class=PATH_CLASS_EXPLICIT_REPOSITORY_ROOT,
            normalized=None,
            detail="explicit_repository_root_marker",
            raw=value,
        )
    if (
        "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or any(char in path for char in "*?{}")
    ):
        detail = "absolute_path" if path.startswith("/") else "invalid_path_characters"
        return RepositoryRelativePathClassification(
            path_class=PATH_CLASS_MISSING_OR_INVALID,
            normalized=None,
            detail=detail,
            raw=value,
        )
    directory_root = path.endswith("/")
    parts = path.rstrip("/").split("/")
    if any(not part for part in parts):
        return RepositoryRelativePathClassification(
            path_class=PATH_CLASS_MISSING_OR_INVALID,
            normalized=None,
            detail="empty_path_segment",
            raw=value,
        )
    if any(part == ".." for part in parts):
        return RepositoryRelativePathClassification(
            path_class=PATH_CLASS_MISSING_OR_INVALID,
            normalized=None,
            detail="traversal_segment",
            raw=value,
        )
    if any(part == "." for part in parts):
        # Embedded "." segments are invalid relative scope, not root scope.
        return RepositoryRelativePathClassification(
            path_class=PATH_CLASS_MISSING_OR_INVALID,
            normalized=None,
            detail="invalid_dot_segment",
            raw=value,
        )
    normalized = "/".join(parts)
    if directory_root:
        normalized = f"{normalized}/"
    return RepositoryRelativePathClassification(
        path_class=PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE,
        normalized=normalized,
        detail="ok",
        raw=value,
    )


def _normalize_repository_relative_scope_path(value: Any) -> str | None:
    classified = classify_repository_relative_scope_path(value)
    if classified.path_class != PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE:
        return None
    return classified.normalized


def _contains_hostile_approval_claim(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            if key_text in _HOSTILE_APPROVAL_KEYS or key_text == "approved_write_scope":
                return True
            if _contains_hostile_approval_claim(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_hostile_approval_claim(item) for item in value)
    return False


def normalize_write_scope_body(value: Any) -> dict[str, Any] | None:
    """Normalize a candidate WriteScopeBody. Returns None if invalid/untrusted."""
    if not isinstance(value, dict):
        return None
    if _contains_hostile_approval_claim(value):
        return None
    required = set(WRITE_SCOPE_BODY_FIELDS)
    if not required.issubset(value):
        return None
    # Reject unknown keys that look like authority claims; strip other extras
    # by projecting onto the canonical field set only.
    target_id = str(value.get("target_id") or "").strip()
    base_sha = str(value.get("base_sha") or "").strip().lower()
    reason = str(value.get("reason") or "").strip()
    if not target_id or not re.fullmatch(r"[0-9a-f]{40}", base_sha) or not reason:
        return None

    def _string_list(name: str) -> list[str] | None:
        raw = value.get(name)
        if not isinstance(raw, list):
            return None
        if any(not isinstance(item, str) for item in raw):
            return None
        values = [item.strip() for item in raw]
        if any(not item for item in values):
            return None
        return values

    raw_allowed = _string_list("allowed_roots")
    raw_denied = _string_list("denied_roots")
    expected_checks = _string_list("expected_checks")
    if raw_allowed is None or not raw_allowed or raw_denied is None or expected_checks is None:
        return None
    allowed_roots = [_normalize_repository_relative_scope_path(item) for item in raw_allowed]
    denied_roots = [_normalize_repository_relative_scope_path(item) for item in raw_denied]
    if any(item is None for item in allowed_roots) or any(item is None for item in denied_roots):
        return None
    docs_only = value.get("docs_only")
    source_mutation = value.get("source_mutation")
    if not isinstance(docs_only, bool) or not isinstance(source_mutation, bool):
        return None
    return {
        "target_id": target_id,
        "base_sha": base_sha,
        "allowed_roots": [str(item) for item in allowed_roots],
        "denied_roots": [str(item) for item in denied_roots],
        "reason": reason,
        "expected_checks": expected_checks,
        "docs_only": docs_only,
        "source_mutation": source_mutation,
    }


def compute_body_hash(body: dict[str, Any]) -> str:
    """Deterministic hash over authority-relevant body fields (excludes reason)."""
    payload = {field: body[field] for field in BODY_HASH_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def compute_proposal_id(*, run_id: str, body_hash: str) -> str:
    material = f"{run_id}:{body_hash}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"{PROPOSAL_ID_PREFIX}{digest}"


def build_proposal_record(
    *,
    run_id: str,
    body: dict[str, Any],
    created_at: str | None = None,
) -> dict[str, Any]:
    frozen = normalize_write_scope_body(body)
    if frozen is None:
        raise WriteScopeProposalError("invalid write scope body")
    parent = str(run_id or "").strip()
    if not parent:
        raise WriteScopeProposalError("missing parent run_id")
    body_hash = compute_body_hash(frozen)
    proposal_id = compute_proposal_id(run_id=parent, body_hash=body_hash)
    return {
        "kind": PROPOSAL_KIND,
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "run_id": parent,
        "body": frozen,
        "body_hash": body_hash,
        "created_at": created_at or utc_now_iso(),
        "status": PROPOSAL_STATUS_PROPOSED,
    }


def proposal_path(proposal_id: str, *, base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else write_scope_proposals_dir()
    return root / f"{proposal_id}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent_dir(path)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(path)
        os.chmod(path, 0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def verify_proposal_record(record: dict[str, Any]) -> dict[str, Any]:
    """Fail closed if stored body no longer matches body_hash / proposal_id."""
    if not isinstance(record, dict):
        raise WriteScopeProposalError("proposal record is not an object")
    if record.get("kind") != PROPOSAL_KIND:
        raise WriteScopeProposalError("proposal kind mismatch")
    if record.get("schema_version") != PROPOSAL_SCHEMA_VERSION:
        raise WriteScopeProposalError("unsupported proposal schema_version")
    if record.get("status") != PROPOSAL_STATUS_PROPOSED:
        raise WriteScopeProposalError(f"unsupported proposal status: {record.get('status')}")
    run_id = str(record.get("run_id") or "").strip()
    proposal_id = str(record.get("proposal_id") or "").strip()
    stored_hash = str(record.get("body_hash") or "").strip()
    body = record.get("body")
    frozen = normalize_write_scope_body(body)
    if frozen is None:
        raise WriteScopeProposalError("stored proposal body is invalid")
    expected_hash = compute_body_hash(frozen)
    if stored_hash != expected_hash:
        raise WriteScopeProposalError(
            f"body_hash mismatch for {proposal_id}: stored body was mutated"
        )
    expected_id = compute_proposal_id(run_id=run_id, body_hash=expected_hash)
    if proposal_id != expected_id:
        raise WriteScopeProposalError(
            f"proposal_id mismatch: expected {expected_id}, got {proposal_id}"
        )
    # Return a defensive copy with frozen body (never mutate caller's dict in place
    # beyond verification projection).
    return {
        "kind": PROPOSAL_KIND,
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "run_id": run_id,
        "body": frozen,
        "body_hash": expected_hash,
        "created_at": str(record.get("created_at") or ""),
        "status": PROPOSAL_STATUS_PROPOSED,
    }


def save_proposal(record: dict[str, Any], *, base_dir: Path | None = None) -> dict[str, Any]:
    verified = verify_proposal_record(record)
    path = proposal_path(verified["proposal_id"], base_dir=base_dir)
    with _STORE_LOCK:
        if path.exists():
            existing = load_proposal(verified["proposal_id"], base_dir=base_dir)
            # Idempotent: same identity returns the original durable record.
            if (
                existing["body_hash"] == verified["body_hash"]
                and existing["run_id"] == verified["run_id"]
            ):
                return existing
            raise WriteScopeProposalError(
                f"proposal_id collision with different content: {verified['proposal_id']}"
            )
        _atomic_write_json(path, verified)
    return verified


def load_proposal(proposal_id: str, *, base_dir: Path | None = None) -> dict[str, Any]:
    ref = str(proposal_id or "").strip()
    if not ref:
        raise WriteScopeProposalError("proposal_id is required")
    path = proposal_path(ref, base_dir=base_dir)
    if not path.is_file():
        raise WriteScopeProposalError(f"proposal not found: {ref}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WriteScopeProposalError(f"corrupt proposal record: {ref}") from exc
    return verify_proposal_record(raw)


def list_proposals(
    *,
    run_id: str | None = None,
    status: str | None = None,
    base_dir: Path | None = None,
) -> list[dict[str, Any]]:
    root = base_dir if base_dir is not None else write_scope_proposals_dir()
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.glob("wsp-*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            record = verify_proposal_record(raw)
        except (OSError, json.JSONDecodeError, WriteScopeProposalError) as exc:
            stem = path.stem
            items.append(
                {
                    "kind": "write_scope_proposal",
                    "proposal_id": stem,
                    "run_id": "",
                    "status": "corrupt",
                    "integrity_error": str(exc),
                    "created_at": "",
                    "body": {},
                    "body_hash": "",
                }
            )
            continue
        if run_id is not None and record["run_id"] != run_id:
            continue
        if status is not None and record["status"] != status:
            continue
        items.append(record)
    items.sort(key=lambda item: (item.get("created_at") or "", item["proposal_id"]))
    return items


def collect_candidate_bodies(result: dict[str, Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Collect proposal candidates from a result dict.

    Prefers write_scope_proposals[]; falls back to singular write_scope_proposal.
    Returns (candidates, immediate_rejections).
    """
    rejected: list[dict[str, Any]] = []
    if not isinstance(result, dict):
        return [], [{"reason": "result_not_object"}]
    if _contains_hostile_approval_claim(result.get("approved_write_scope")) or (
        "approved_write_scope" in result
    ):
        rejected.append(
            {
                "reason": "untrusted_approved_write_scope_claim",
                "detail": "worker approved_write_scope claims are invalid for the proposal store",
            }
        )
    # Also reject if any nested hostile claim rides on the result envelope
    # outside proposal bodies (bodies are checked in normalize).
    for key in _HOSTILE_APPROVAL_KEYS:
        if key in result and key != "approved_write_scope":
            rejected.append(
                {
                    "reason": "untrusted_approval_claim",
                    "detail": f"result field {key!r} is not accepted as proposal authority",
                    "field": key,
                }
            )

    plural = result.get("write_scope_proposals")
    singular = result.get("write_scope_proposal")
    candidates: list[Any] = []
    if isinstance(plural, list) and plural:
        candidates.extend(plural)
    elif singular is not None:
        candidates.append(singular)
    return candidates, rejected


def persist_write_scope_proposals_from_result(
    result: dict[str, Any] | None,
    *,
    run_id: str | None = None,
    base_dir: Path | None = None,
    created_at: str | None = None,
) -> PersistOutcome:
    """Persist validated proposals from an AgentRunResult-shaped dict.

    - Prose-only / empty candidates → nothing stored (not authority).
    - Malformed / hostile approval-looking bodies → rejected, not stored.
    - Missing parent run_id → rejected.
    - Duplicate (same body_hash + run_id) → idempotent same proposal_id.
    """
    if not isinstance(result, dict):
        return PersistOutcome(persisted=[], rejected=[{"reason": "result_not_object"}], skipped_prose_only=True)

    parent = str(run_id or result.get("session_id") or result.get("run_id") or "").strip()
    candidates, rejected = collect_candidate_bodies(result)

    if not parent:
        if candidates:
            rejected.append(
                {
                    "reason": "missing_parent_run_id",
                    "detail": "proposals require a parent run_id / session_id",
                }
            )
        return PersistOutcome(
            persisted=[],
            rejected=rejected,
            skipped_prose_only=not candidates,
        )

    if not candidates:
        return PersistOutcome(
            persisted=[],
            rejected=rejected,
            skipped_prose_only=True,
        )

    persisted: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, candidate in enumerate(candidates):
        if _contains_hostile_approval_claim(candidate):
            rejected.append(
                {
                    "reason": "untrusted_approved_write_scope_claim",
                    "index": index,
                    "detail": "proposal body contains approval-looking worker claims",
                }
            )
            continue
        frozen = normalize_write_scope_body(candidate)
        if frozen is None:
            rejected.append(
                {
                    "reason": "malformed_proposal",
                    "index": index,
                    "detail": "proposal failed WriteScopeBody normalization",
                }
            )
            continue
        try:
            record = build_proposal_record(
                run_id=parent,
                body=frozen,
                created_at=created_at,
            )
            saved = save_proposal(record, base_dir=base_dir)
        except WriteScopeProposalError as exc:
            rejected.append(
                {
                    "reason": "persist_failed",
                    "index": index,
                    "detail": str(exc),
                }
            )
            continue
        if saved["proposal_id"] in seen_ids:
            continue
        seen_ids.add(saved["proposal_id"])
        persisted.append(saved)

    return PersistOutcome(
        persisted=persisted,
        rejected=rejected,
        skipped_prose_only=False,
    )


__all__ = [
    "BODY_HASH_FIELDS",
    "PROPOSAL_KIND",
    "PROPOSAL_SCHEMA_VERSION",
    "PROPOSAL_STATUS_PROPOSED",
    "PersistOutcome",
    "WRITE_SCOPE_BODY_FIELDS",
    "WriteScopeProposalError",
    "build_proposal_record",
    "collect_candidate_bodies",
    "PATH_CLASS_EXPLICIT_REPOSITORY_ROOT",
    "PATH_CLASS_FILE_OR_DIRECTORY_RELATIVE",
    "PATH_CLASS_MISSING_OR_INVALID",
    "RepositoryRelativePathClassification",
    "classify_repository_relative_scope_path",
    "compute_body_hash",
    "compute_proposal_id",
    "list_proposals",
    "load_proposal",
    "normalize_write_scope_body",
    "persist_write_scope_proposals_from_result",
    "proposal_path",
    "save_proposal",
    "utc_now_iso",
    "verify_proposal_record",
]
