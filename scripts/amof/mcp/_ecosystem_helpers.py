"""Shared helpers for ecosystem resolution used by multiple MCP tool modules."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from amof.manifest import ECOSYSTEMS_DIR, list_available_ecosystems


def get_available_ecosystems() -> List[str]:
    """Return names of ecosystems that have an ecosystem.yaml."""
    if not ECOSYSTEMS_DIR.exists():
        return []
    return list_available_ecosystems()


def validate_ecosystem_exists(name: str) -> Optional[str]:
    """Return an error string if the ecosystem doesn't exist, else None."""
    available = get_available_ecosystems()
    if name in available:
        return None
    if not available:
        return f"Ecosystem '{name}' not found. No ecosystems exist yet."
    return (
        f"Ecosystem '{name}' not found. "
        f"Available: {', '.join(available)}"
    )


def load_ecosystem_manifest(name: str) -> Dict[str, Any]:
    """Load and parse an ecosystem's manifest without sys.exit on error."""
    path = ECOSYSTEMS_DIR / name / "ecosystem.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")

    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except Exception:
        from amof.manifest import simple_parse_yaml
        return simple_parse_yaml(text) or {}
