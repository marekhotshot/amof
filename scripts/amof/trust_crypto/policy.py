"""Trust policy: allowed / revoked / preferred keys (FAIL_CLOSED)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..trust_layer import TrustIntegrityError
from .filesystem_keys import trust_authority_root


POLICY_SCHEMA = "amof.trust_policy/v1"
POLICY_FILENAME = "trust-policy.json"


@dataclass(frozen=True)
class TrustPolicy:
    allowed_key_ids: frozenset[str]
    revoked_key_ids: frozenset[str]
    preferred_key_id: str | None
    require_signatures: bool
    allow_unknown_keys: bool
    allow_unsigned: bool

    def assert_key_usable(self, key_id: str) -> None:
        kid = str(key_id or "").strip().lower()
        if not kid:
            raise TrustIntegrityError("missing public_key_id", code="missing_key")
        if kid in self.revoked_key_ids:
            raise TrustIntegrityError(
                f"revoked key: {kid}",
                code="revoked_key",
            )
        if kid not in self.allowed_key_ids:
            if self.allow_unknown_keys:
                return
            raise TrustIntegrityError(
                f"unknown key: {kid}",
                code="unknown_key",
            )


def default_policy_path() -> Path:
    return trust_authority_root() / POLICY_FILENAME


def empty_policy(*, require_signatures: bool = False) -> TrustPolicy:
    return TrustPolicy(
        allowed_key_ids=frozenset(),
        revoked_key_ids=frozenset(),
        preferred_key_id=None,
        require_signatures=require_signatures,
        allow_unknown_keys=False,
        allow_unsigned=not require_signatures,
    )


def policy_from_dict(payload: dict[str, Any]) -> TrustPolicy:
    if payload.get("schema") != POLICY_SCHEMA:
        raise TrustIntegrityError(
            f"unexpected trust policy schema: {payload.get('schema')!r}",
            code="invalid_policy",
        )
    allowed = {
        str(x).strip().lower()
        for x in (payload.get("allowed_key_ids") or [])
        if str(x).strip()
    }
    revoked = {
        str(x).strip().lower()
        for x in (payload.get("revoked_key_ids") or [])
        if str(x).strip()
    }
    preferred = str(payload.get("preferred_key_id") or "").strip().lower() or None
    if preferred and preferred not in allowed:
        raise TrustIntegrityError(
            "preferred_key_id must be listed in allowed_key_ids",
            code="invalid_policy",
        )
    overlap = allowed & revoked
    if overlap:
        raise TrustIntegrityError(
            f"key cannot be both allowed and revoked: {sorted(overlap)[0]}",
            code="invalid_policy",
        )
    require_signatures = bool(payload.get("require_signatures", False))
    allow_unknown = bool(payload.get("allow_unknown_keys", False))
    allow_unsigned = bool(payload.get("allow_unsigned", not require_signatures))
    if require_signatures:
        allow_unsigned = False
    return TrustPolicy(
        allowed_key_ids=frozenset(allowed),
        revoked_key_ids=frozenset(revoked),
        preferred_key_id=preferred,
        require_signatures=require_signatures,
        allow_unknown_keys=allow_unknown,
        allow_unsigned=allow_unsigned,
    )


def load_trust_policy(path: Path | None = None) -> TrustPolicy:
    policy_path = path if path is not None else default_policy_path()
    if not policy_path.is_file():
        # No policy file: unsigned legacy OK; signatures present still need known keys
        # unless allow_unknown. Default fail-closed for unknown keys.
        return empty_policy(require_signatures=False)
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustIntegrityError(
            f"trust-policy.json is not valid JSON: {policy_path}",
            code="invalid_policy",
        ) from exc
    if not isinstance(payload, dict):
        raise TrustIntegrityError("trust-policy.json must be an object", code="invalid_policy")
    return policy_from_dict(payload)


def write_trust_policy(policy: TrustPolicy, path: Path | None = None) -> Path:
    policy_path = path if path is not None else default_policy_path()
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": POLICY_SCHEMA,
        "allowed_key_ids": sorted(policy.allowed_key_ids),
        "revoked_key_ids": sorted(policy.revoked_key_ids),
        "preferred_key_id": policy.preferred_key_id,
        "require_signatures": policy.require_signatures,
        "allow_unknown_keys": policy.allow_unknown_keys,
        "allow_unsigned": policy.allow_unsigned,
    }
    policy_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return policy_path


def enroll_key(policy: TrustPolicy, key_id: str, *, preferred: bool = False) -> TrustPolicy:
    kid = str(key_id).strip().lower()
    allowed = set(policy.allowed_key_ids)
    revoked = set(policy.revoked_key_ids)
    revoked.discard(kid)
    allowed.add(kid)
    preferred_id = kid if preferred or policy.preferred_key_id is None else policy.preferred_key_id
    return TrustPolicy(
        allowed_key_ids=frozenset(allowed),
        revoked_key_ids=frozenset(revoked),
        preferred_key_id=preferred_id,
        require_signatures=policy.require_signatures,
        allow_unknown_keys=policy.allow_unknown_keys,
        allow_unsigned=policy.allow_unsigned,
    )


def revoke_key(policy: TrustPolicy, key_id: str) -> TrustPolicy:
    kid = str(key_id).strip().lower()
    allowed = set(policy.allowed_key_ids)
    revoked = set(policy.revoked_key_ids)
    allowed.discard(kid)
    revoked.add(kid)
    preferred = policy.preferred_key_id if policy.preferred_key_id != kid else None
    return TrustPolicy(
        allowed_key_ids=frozenset(allowed),
        revoked_key_ids=frozenset(revoked),
        preferred_key_id=preferred,
        require_signatures=policy.require_signatures,
        allow_unknown_keys=policy.allow_unknown_keys,
        allow_unsigned=policy.allow_unsigned,
    )
