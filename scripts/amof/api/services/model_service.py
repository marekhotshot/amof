"""Fetch and cache OpenRouter models list for the control plane model library."""
from __future__ import annotations

import os
import time
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_TTL_SECONDS = 3600

_cache: Optional[Dict[str, Any]] = None
_cache_time: float = 0


def get_models(api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return list of models from OpenRouter. Uses in-memory cache with TTL."""
    global _cache, _cache_time
    now = time.time()
    if _cache is not None and (now - _cache_time) < CACHE_TTL_SECONDS:
        return _cache.get("data", [])
    key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        logger.warning("OPENROUTER_API_KEY not set; cannot fetch models")
        return []
    try:
        resp = requests.get(OPENROUTER_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30)
        resp.raise_for_status()
        _cache = resp.json()
        _cache_time = now
        return _cache.get("data", [])
    except Exception as e:
        logger.warning("Failed to fetch OpenRouter models: %s", e)
        if _cache is not None:
            return _cache.get("data", [])
        return []
