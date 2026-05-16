"""Persist minimal AMOF install metadata in app-data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_paths import ensure_app_roots, ensure_parent_dir, get_app_paths

VALID_CHANNELS = {"stable", "dev", "pinned"}


def install_metadata_file() -> Path:
    return get_app_paths().config_root / "install-metadata.json"


def load_install_metadata() -> dict[str, Any] | None:
    path = install_metadata_file()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def save_install_metadata(
    *,
    channel: str,
    version: str | None,
    install_method: str,
    installed_at: str | None = None,
) -> Path:
    normalized_channel = str(channel or "").strip()
    if normalized_channel not in VALID_CHANNELS:
        raise ValueError(f"unsupported install channel: {normalized_channel}")
    normalized_method = str(install_method or "").strip()
    if not normalized_method:
        raise ValueError("install method is required")
    normalized_version = str(version or "").strip() or None
    payload = {
        "channel": normalized_channel,
        "version": normalized_version,
        "install_method": normalized_method,
        "installed_at": installed_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    ensure_app_roots()
    target = ensure_parent_dir(install_metadata_file())
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


__all__ = ["install_metadata_file", "load_install_metadata", "save_install_metadata", "VALID_CHANNELS"]
