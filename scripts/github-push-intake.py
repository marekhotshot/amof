#!/usr/bin/env python3
"""Thin local runner for GitHub push intake decisions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amof.intake.github_push import decide_ticket_build_write, load_payload, parse_github_push_event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Consume a GitHub push payload and print the AMOF ticket build-write decision."
    )
    parser.add_argument("--payload", required=True, help="Path to a GitHub push event JSON payload")
    parser.add_argument(
        "--proof",
        action="store_true",
        help="Emit a proof-mode dry-run decision instead of a real build-write decision",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = load_payload(Path(args.payload))
    event = parse_github_push_event(payload)
    decision = decide_ticket_build_write(event, proof_mode=args.proof)
    print(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
