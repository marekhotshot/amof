"""Public promotion-proof command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..public_proof import PublicProofError, list_proofs, load_proof


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def cmd_proof(args: argparse.Namespace) -> int:
    action = str(getattr(args, "proof_cmd", "") or "list")
    root = _repo_root()
    try:
        if action == "list":
            proofs = list_proofs(root)
            if getattr(args, "json", False):
                print(json.dumps(proofs, indent=2, sort_keys=True))
            else:
                if not proofs:
                    print("No public promotion proofs found.")
                    return 0
                for proof in proofs:
                    authority = proof["authority"]
                    if isinstance(authority, list):
                        authority = ",".join(authority)
                    print(
                        f"{proof['ticket_id']}  {proof['promotion_sha'][:12]}  "
                        f"{authority}  {proof['acceptance']}"
                    )
            return 0
        proof = load_proof(str(args.ref), repo_root=root)
        if getattr(args, "json", False):
            print(json.dumps(proof, indent=2, sort_keys=True))
        else:
            print(f"Ticket: {proof['ticket_id']}")
            print(f"Promotion SHA: {proof['promotion_sha']}")
            print(f"Source SHA: {proof['source_sha']}")
            print(f"GitOps SHA: {proof['gitops_sha'] or '<none>'}")
            print(f"Authority: {proof['authority']}")
            print(f"Acceptance: {proof['acceptance']}")
            print(f"Verification: {proof['verification']}")
            print(f"Evidence digest: {proof['evidence_digest']}")
            print(f"Proof: {proof['proof']}")
            print(f"Reproduce: python3 scripts/amof.py demo migration --non-interactive")
        return 0
    except PublicProofError as exc:
        print(str(exc))
        return 2


__all__ = ["cmd_proof"]
