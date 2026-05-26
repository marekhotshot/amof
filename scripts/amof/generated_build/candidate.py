"""Public generated-build candidate contract.

The public distribution keeps candidate APIs importable and callable while
failing closed. Candidate records that already exist locally are returned
through a sanitized envelope so policy details are not republished.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .store import store_root


ACTION_PROMOTE_CANDIDATE = "generated_build.promote_candidate.v1"
RESULT_CANDIDATE_CREATED = "candidate_created"
RESULT_REFUSED = "refused"
ADMISSION_CANDIDATE_ONLY = "candidate_only"


def promote_candidate(request: Dict[str, Any], *, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a fail-closed candidate promotion result without writing state."""
    _ = context
    created_at = _now()
    artifact_ref = request.get("artifact_ref") if isinstance(request.get("artifact_ref"), dict) else {}
    return {
        "action": ACTION_PROMOTE_CANDIDATE,
        "result": RESULT_REFUSED,
        "artifact_ref": {
            "artifact_path": str(artifact_ref.get("artifact_path") or ""),
            "repo_path": str(artifact_ref.get("repo_path") or ""),
            "service": str(artifact_ref.get("service") or ""),
            "image_digest": _request_image_digest(request, artifact_ref),
        },
        "target_ecosystem": str(request.get("target_ecosystem") or ""),
        "target_service": str(request.get("target_service") or ""),
        "admission_status": "refused",
        "artifact_proof_status": "",
        "reasons": ["public_contract_only"],
        "missing_prerequisites": ["candidate_admission_result_from_authorized_workflow"],
        "refusal_conditions": ["candidate_promotion_not_available_in_public_distribution"],
        "audit_receipt_path": "",
        "created_at": created_at,
    }


def list_candidates() -> Dict[str, Any]:
    """List locally persisted generated-build candidate records safely."""
    records_dir = store_root() / "candidates" / "records"
    if not records_dir.exists():
        return {"version": 1, "items": []}
    items = []
    for path in sorted(records_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items.append(_candidate_summary(record, path))
    return {"version": 1, "items": items}


def load_candidate(candidate_id: str) -> Dict[str, Any]:
    """Load one locally persisted candidate record through the public envelope."""
    path = _candidate_record_path(candidate_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    return _candidate_detail(record, path)


def _candidate_detail(record: Dict[str, Any], path: Path) -> Dict[str, Any]:
    summary = _candidate_summary(record, path)
    artifact_ref = record.get("artifact_ref") if isinstance(record.get("artifact_ref"), dict) else {}
    summary["artifact_ref"] = {
        "artifact_path": str(artifact_ref.get("artifact_path") or ""),
        "repo_path": str(artifact_ref.get("repo_path") or ""),
        "service": str(artifact_ref.get("service") or ""),
        "image_digest": str(record.get("image_digest") or artifact_ref.get("image_digest") or ""),
    }
    summary["admission_status"] = str(record.get("status") or "")
    summary["public_contract_only"] = True
    return summary


def _candidate_summary(record: Dict[str, Any], path: Path) -> Dict[str, Any]:
    return {
        "candidate_id": record.get("candidate_id"),
        "status": record.get("status"),
        "target_ecosystem": record.get("target_ecosystem"),
        "target_service": record.get("target_service"),
        "image_digest": record.get("image_digest"),
        "created_at": record.get("created_at"),
        "candidate_record_path": str(path),
        "audit_receipt_path": record.get("audit_receipt_path"),
        "public_contract_only": True,
    }


def _request_image_digest(request: Dict[str, Any], artifact_ref: Dict[str, Any]) -> str:
    confirmation = request.get("operator_confirmation") if isinstance(request.get("operator_confirmation"), dict) else {}
    return str(artifact_ref.get("image_digest") or confirmation.get("confirmed_image_digest") or "")


def _candidate_record_path(candidate_id: str) -> Path:
    return store_root() / "candidates" / "records" / f"{_slug(candidate_id)}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-") or "value"
