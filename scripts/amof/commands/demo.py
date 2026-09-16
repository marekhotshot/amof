"""Public Runtime Authority demo command."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from ..demo_runtime import (
    MENU_ORDER,
    SCENARIOS,
    format_report,
    live_cluster_tools_available,
    run_scenario,
    validate_demo_receipt,
)


def cmd_demo(args: argparse.Namespace) -> int:
    scenario = str(getattr(args, "scenario", "") or "").strip().lower()
    non_interactive = bool(getattr(args, "non_interactive", False))
    live = bool(getattr(args, "live", False))
    show_receipt = bool(getattr(args, "show_receipt", False))
    emit_json = bool(getattr(args, "json", False))

    if not scenario:
        if non_interactive:
            scenario = "migration"
        else:
            scenario = _prompt_scenario()
    if scenario not in SCENARIOS:
        print(f"unknown scenario: {scenario}")
        print("choose: " + ", ".join(MENU_ORDER))
        return 2

    if scenario == "kubernetes" and live and not live_cluster_tools_available():
        print("Live Kubernetes requested but k3d+kubectl were not found.")
        return 2
    if (
        scenario == "kubernetes"
        and not live
        and not non_interactive
        and live_cluster_tools_available()
    ):
        answer = input("Use live disposable Kubernetes cluster? [y/N] ").strip().lower()
        live = answer in {"y", "yes"}

    home = os.environ.get("AMOF_HOME") or tempfile.mkdtemp(prefix="amof-demo-")
    os.environ["AMOF_HOME"] = home
    try:
        result = run_scenario(scenario, home=home, live=live)
    except RuntimeError as exc:
        print(str(exc))
        return 2

    if result.receipt:
        try:
            validate_demo_receipt(result.receipt)
        except RuntimeError as exc:
            print(format_report(result))
            print(f"receipt contract failed: {exc}")
            return 1

    if emit_json:
        print(
            json.dumps(
                {
                    "scenario": result.scenario,
                    "title": result.title,
                    "mode": result.mode,
                    "ok": result.ok,
                    "receipt_path": result.receipt_path,
                    "evidence_path": result.evidence_path,
                    "acceptance": (result.receipt or {}).get("acceptance_state")
                    or (
                        "PASS"
                        if (result.receipt or {}).get("compliance") == "within_scope"
                        else (result.receipt or {}).get("compliance")
                    ),
                    "steps": [
                        {
                            "name": step.name,
                            "ok": step.ok,
                            "detail": step.detail,
                            "invoked_transport": step.invoked_transport,
                        }
                        for step in result.steps
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("AMOF Runtime Authority — executable proof")
        print(f"AMOF_HOME={home}")
        print()
        print(format_report(result))
        if show_receipt and result.receipt_path:
            print(Path(result.receipt_path).read_text(encoding="utf-8"))
    return 0 if result.ok else 1


def _prompt_scenario() -> str:
    print("AMOF Runtime Authority — executable proof")
    print()
    print("Choose a scenario:")
    print()
    for index, key in enumerate(MENU_ORDER, start=1):
        title = SCENARIOS[key][0]
        suffix = "  (recommended)" if key == "migration" else ""
        print(f"  {index}. {title}{suffix}")
    print()
    raw = input("Scenario [1]: ").strip() or "1"
    if raw.isdigit():
        position = int(raw)
        if 1 <= position <= len(MENU_ORDER):
            return MENU_ORDER[position - 1]
    if raw.lower() in SCENARIOS:
        return raw.lower()
    print("unknown selection; using migration")
    return "migration"


__all__ = ["cmd_demo"]
