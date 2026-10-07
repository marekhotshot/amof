"""Backend identity resolution shared by runner dispatch and adapters."""

from __future__ import annotations

from typing import Any


def runner_backend_type(record: dict[str, Any]) -> str:
    explicit = str(record.get("backend") or record.get("backend_type") or "").strip()
    if explicit:
        return explicit
    if str(record.get("driver") or "").strip().lower() == "hermes":
        return "hermes_opensandbox"
    return "planning_only"
