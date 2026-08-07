"""Trust Layer CLI — verify canonical evidence bundles."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..app_paths import get_app_paths
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
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("PASS")
    return 0


def cmd_trust(args: Any) -> int:
    action = str(getattr(args, "trust_cmd", "") or "").strip()
    if action == "verify":
        return cmd_trust_verify(args)
    sys.stderr.write("Usage: amof trust <verify> RUN\n")
    return 1


__all__ = ["cmd_trust", "cmd_trust_verify"]
