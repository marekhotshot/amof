"""Public promotion proof — portable projection of promote-main identity.

A stranger with only the public repository can bind a promoted SHA to
acceptance and verification without reading operator app-data.

This is not a second promotion authority. Fields come from governed
promote-main commit identity plus public-safe verification labels.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA = "amof.public_promotion_proof/v0"
PROOFS_DIRNAME = "docs/proofs"
REQUIRED_FIELDS = (
    "schema",
    "promotion_sha",
    "ticket_id",
    "candidate_branch",
    "source_sha",
    "gitops_sha",
    "bundle_id",
    "promotion_id",
    "authority",
    "acceptance",
    "verification",
    "verification_method",
    "evidence_digest",
    "proof",
    "environment",
)
DIGEST_FIELDS = (
    "schema",
    "promotion_sha",
    "ticket_id",
    "candidate_branch",
    "source_sha",
    "gitops_sha",
    "bundle_id",
    "promotion_id",
    "authority",
    "acceptance",
    "verification",
    "verification_method",
    "proof",
    "environment",
)
SHA_RE = __import__("re").compile(r"^[0-9a-f]{40}$")


class PublicProofError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_proof"):
        super().__init__(message)
        self.code = code


def _sha256_canonical(value: Any) -> str:
    import hashlib

    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def compute_evidence_digest(proof: dict[str, Any]) -> str:
    payload = {field: proof.get(field) for field in DIGEST_FIELDS}
    return _sha256_canonical(payload)


def normalize_public_proof(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PublicProofError("proof must be an object")
    if value.get("schema") != SCHEMA:
        raise PublicProofError(f"unsupported schema {value.get('schema')!r}")
    missing = [field for field in REQUIRED_FIELDS if field not in value]
    if missing:
        raise PublicProofError(f"missing fields: {missing}")
    for key in ("promotion_sha", "source_sha"):
        if SHA_RE.fullmatch(str(value.get(key) or "")) is None:
            raise PublicProofError(f"{key} must be a full SHA")
    if value.get("gitops_sha") not in {None, "<none>"}:
        gitops = str(value.get("gitops_sha") or "")
        if SHA_RE.fullmatch(gitops) is None:
            raise PublicProofError("gitops_sha must be null, <none>, or a full SHA")
    if value.get("acceptance") != "PASS":
        raise PublicProofError("public proof only records accepted promotions")
    if value.get("verification") != "PASS":
        raise PublicProofError("public proof only records verified promotions")
    environment = value.get("environment")
    if not isinstance(environment, dict) or "kind" not in environment:
        raise PublicProofError("environment.kind is required")
    forbidden = ("AMOF_HOME", "/home/", "kubeconfig", "token", "secret")
    blob = json.dumps(value, sort_keys=True)
    if any(marker.lower() in blob.lower() for marker in forbidden):
        raise PublicProofError("proof contains non-public or sensitive material", code="sensitive")
    expected = compute_evidence_digest(value)
    if value.get("evidence_digest") != expected:
        raise PublicProofError("evidence_digest mismatch", code="evidence_digest_mismatch")
    return dict(value)


def proofs_dir(repo_root: Path | None = None) -> Path:
    root = repo_root or Path.cwd()
    return root / PROOFS_DIRNAME


def list_proofs(repo_root: Path | None = None) -> list[dict[str, Any]]:
    directory = proofs_dir(repo_root)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("AMOF-*.json")):
        out.append(normalize_public_proof(json.loads(path.read_text(encoding="utf-8"))))
    return out


def load_proof(ticket_or_sha: str, *, repo_root: Path | None = None) -> dict[str, Any]:
    ref = str(ticket_or_sha or "").strip()
    if not ref:
        raise PublicProofError("ticket or SHA is required")
    directory = proofs_dir(repo_root)
    direct = directory / f"{ref}.json"
    if direct.is_file():
        return normalize_public_proof(json.loads(direct.read_text(encoding="utf-8")))
    for proof in list_proofs(repo_root):
        if ref in {proof["ticket_id"], proof["promotion_sha"], proof["source_sha"], proof["promotion_id"]}:
            return proof
        if len(ref) >= 7 and (
            proof["promotion_sha"].startswith(ref) or proof["source_sha"].startswith(ref)
        ):
            return proof
    raise PublicProofError(f"no public proof for {ref!r}", code="not_found")


def parse_promote_message(message: str) -> dict[str, str]:
    fields = {}
    for line in str(message or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in {
            "Ticket",
            "Candidate-Branch",
            "Source-SHA",
            "GitOps-SHA",
            "Bundle-ID",
            "Promotion-ID",
        }:
            fields[key] = value.strip()
    return fields
