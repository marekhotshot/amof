"""Small backend-independent runtime formatting helpers."""

from __future__ import annotations

import re


def safe_run_id(value: str, *, fallback: str = "run") -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return normalized[:96] or fallback


def infer_validation_status(final_text: str) -> str:
    lowered = final_text.lower()
    failure_markers = (
        "failed (failures=", "failed (errors=", "traceback (most recent call last)",
        "assertionerror", "\nfail:", "\nerror:", "the test ran, but it did not",
        "resulting in a failure",
    )
    if any(marker in lowered for marker in failure_markers):
        return "failed"
    success_markers = ("ran 1 test", "\nok", "validation_ok")
    if any(marker in lowered for marker in success_markers):
        return "passed"
    return "not_run"
