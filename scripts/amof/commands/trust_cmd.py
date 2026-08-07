"""Trust Layer CLI — verify, export, offline verify-export, keygen."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..app_paths import get_app_paths
from ..trust_crypto import (
    FilesystemKeyProvider,
    enroll_key,
    export_trust_package,
    format_mode_report,
    init_transparency_log,
    load_trust_policy,
    verify_export_package,
    write_trust_policy,
)
from ..trust_layer import (
    TrustIntegrityError,
    evidence_bundle_dir,
    verify_evidence_consistency,
    verify_run_evidence,
)


def _print_modes(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(format_mode_report(result))


def cmd_trust_verify(args: Any) -> int:
    run_id = str(getattr(args, "run_id", "") or "").strip()
    if not run_id:
        sys.stderr.write("Usage: amof trust verify RUN\n")
        return 1

    try:
        if Path(run_id).is_dir() and (Path(run_id) / "manifest.json").is_file():
            # If this looks like an export package, use export verifier.
            if (Path(run_id) / "verification_metadata.json").is_file():
                result = verify_export_package(run_id)
                _print_modes(result, as_json=bool(getattr(args, "json", False)))
                return 0 if result.get("ok") else 1
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

    # Runtime verify: report Wave 004 mode breakdown (export package owns EXTERNAL_ANCHOR).
    signed = bool((result.get("signature") or {}).get("signed"))
    modes = {
        "LOCAL_INTEGRITY": {"status": "PASS"},
        "SIGNATURE_TRUST": {"status": "PASS" if signed else "SKIPPED"},
        "EXTERNAL_ANCHOR": {
            "status": "SKIPPED",
            "reason": "use amof trust export + verify-export for external anchor",
        },
        "TRUST_NOW": {
            "status": "SKIPPED",
            "reason": "runtime verify uses live policy inside SIGNATURE_TRUST",
        },
    }
    payload = {
        "status": "PASS",
        "run_id": result.get("run_id") or run_id,
        "bundle_dir": result.get("bundle_dir") or bundle,
        "signature": result.get("signature"),
        "modes": modes,
    }
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_mode_report(payload))
    return 0


def cmd_trust_export(args: Any) -> int:
    run_id = str(getattr(args, "run_id", "") or "").strip()
    if not run_id:
        sys.stderr.write("Usage: amof trust export RUN [--output DIR]\n")
        return 1
    output = getattr(args, "output", None)
    try:
        result = export_trust_package(
            run_id,
            output_dir=output,
            include_external_anchor=not bool(getattr(args, "no_external_anchor", False)),
        )
    except TrustIntegrityError as exc:
        sys.stderr.write(f"[trust] FAIL_CLOSED: {exc}\n")
        if bool(getattr(args, "json", False)):
            print(
                json.dumps(
                    {
                        "status": "FAIL",
                        "reason": str(exc),
                        "code": getattr(exc, "code", "integrity_error"),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        return 1
    if bool(getattr(args, "json", False)):
        print(json.dumps({"status": "PASS", **result}, indent=2, sort_keys=True))
    else:
        print(f"EXPORT_OK {result['export_dir']}")
    return 0


def cmd_trust_verify_export(args: Any) -> int:
    path = str(getattr(args, "path", "") or "").strip()
    if not path:
        sys.stderr.write("Usage: amof trust verify-export PATH\n")
        return 1
    try:
        result = verify_export_package(
            path,
            evaluate_trust_now_policy=not bool(getattr(args, "offline_only", False)),
        )
    except TrustIntegrityError as exc:
        # Attempt partial mode report when possible.
        payload = {
            "status": "FAIL",
            "export_dir": path,
            "reason": str(exc),
            "code": getattr(exc, "code", "integrity_error"),
        }
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("FAIL")
            print(str(exc))
        return 1
    _print_modes(result, as_json=bool(getattr(args, "json", False)))
    return 0 if result.get("ok") else 1


def cmd_trust_tlog_init(args: Any) -> int:
    """Explicitly create local transparency-log checkpoint signing authority."""
    try:
        result = init_transparency_log()
    except TrustIntegrityError as exc:
        sys.stderr.write(f"[trust] FAIL_CLOSED: {exc}\n")
        return 1
    if bool(getattr(args, "json", False)):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"TLOG_INIT_OK log_key_id={result['log_key_id']}")
    return 0


def cmd_trust_keygen(args: Any) -> int:
    """Generate a local Ed25519 operator keypair and enroll it in trust-policy."""
    try:
        provider = FilesystemKeyProvider()
        record = provider.generate_keypair()
        policy = load_trust_policy()
        preferred = bool(getattr(args, "preferred", True))
        policy = enroll_key(policy, record.key_id, preferred=preferred)
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
    if action == "export":
        return cmd_trust_export(args)
    if action == "verify-export":
        return cmd_trust_verify_export(args)
    if action == "keygen":
        return cmd_trust_keygen(args)
    if action == "tlog-init":
        return cmd_trust_tlog_init(args)
    sys.stderr.write(
        "Usage: amof trust <verify|export|verify-export|keygen|tlog-init> ...\n"
    )
    return 1


__all__ = [
    "cmd_trust",
    "cmd_trust_verify",
    "cmd_trust_export",
    "cmd_trust_verify_export",
    "cmd_trust_keygen",
    "cmd_trust_tlog_init",
]
