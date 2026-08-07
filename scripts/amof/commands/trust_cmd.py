"""Trust Layer CLI — verify bundles, manage local signing keys."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..app_paths import get_app_paths
from ..trust_crypto import (
    FilesystemKeyProvider,
    enroll_key,
    load_trust_policy,
    write_trust_policy,
)
from ..trust_layer import (
    TrustIntegrityError,
    evidence_bundle_dir,
    verify_evidence_consistency,
    verify_run_evidence,
)


def cmd_trust_verify(args: Any) -> int:
    run_id = str(getattr(args, "run_id", "") or "").strip()
    if not run_id:
        sys.stderr.write("Usage: amof trust verify RUN\n")
        return 1

    try:
        if Path(run_id).is_dir() and (Path(run_id) / "manifest.json").is_file():
            result = verify_evidence_consistency(run_id)
            bundle = str(Path(run_id).resolve())
        else:
            data_root = get_app_paths().data_root
            bundle = str(evidence_bundle_dir(data_root, run_id))
            result = verify_run_evidence(data_root, run_id)
    except TrustIntegrityError as exc:
        if bool(getattr(args, "json", False)):
            print(
                json.dumps(
                    {
                        "status": "FAIL",
                        "run_id": run_id,
                        "reason": str(exc),
                        "code": getattr(exc, "code", "integrity_error"),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print("FAIL")
            print(str(exc))
        return 1

    if bool(getattr(args, "json", False)):
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "run_id": result.get("run_id") or run_id,
                    "bundle_dir": result.get("bundle_dir") or bundle,
                    "signature": result.get("signature"),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("PASS")
    return 0


def cmd_trust_keygen(args: Any) -> int:
    """Generate a local Ed25519 operator keypair and enroll it in trust-policy."""
    try:
        provider = FilesystemKeyProvider()
        record = provider.generate_keypair()
        policy = load_trust_policy()
        preferred = bool(getattr(args, "preferred", True))
        policy = enroll_key(policy, record.key_id, preferred=preferred)
        # New signed runs should require signatures once a key exists.
        if bool(getattr(args, "require_signatures", False)):
            from ..trust_crypto.policy import TrustPolicy

            policy = TrustPolicy(
                allowed_key_ids=policy.allowed_key_ids,
                revoked_key_ids=policy.revoked_key_ids,
                preferred_key_id=policy.preferred_key_id,
                require_signatures=True,
                allow_unknown_keys=policy.allow_unknown_keys,
                allow_unsigned=False,
            )
        path = write_trust_policy(policy)
    except TrustIntegrityError as exc:
        sys.stderr.write(f"[trust] FAIL_CLOSED: {exc}\n")
        return 1

    payload = {
        "ok": True,
        "algorithm": record.algorithm,
        "public_key_id": record.key_id,
        "policy_path": str(path),
        "preferred_key_id": policy.preferred_key_id,
    }
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"KEYGEN_OK public_key_id={record.key_id}")
    return 0


def cmd_trust(args: Any) -> int:
    action = str(getattr(args, "trust_cmd", "") or "").strip()
    if action == "verify":
        return cmd_trust_verify(args)
    if action == "keygen":
        return cmd_trust_keygen(args)
    sys.stderr.write("Usage: amof trust <verify|keygen> ...\n")
    return 1


__all__ = ["cmd_trust", "cmd_trust_verify", "cmd_trust_keygen"]
