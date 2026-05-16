"""Models library: list LLM models from OpenRouter for control plane settings."""

from typing import List, Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from amof.api.services.model_service import get_models

router = APIRouter(prefix="/models", tags=["models"])


class ModelPricing(BaseModel):
    prompt: str = "0"
    completion: str = "0"
    request: str = "0"
    image: str = "0"
    input_cache_read: str = "0"
    input_cache_write: str = "0"


class ModelRead(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    context_length: int = 0
    pricing: Dict[str, Any] = {}
    architecture: Dict[str, Any] = {}
    supported_parameters: List[str] = []

    class Config:
        extra = "allow"


@router.get("", response_model=List[ModelRead])
def list_models():
    """Return list of available models from OpenRouter (cached)."""
    raw = get_models()
    result = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        result.append(ModelRead(
            id=m.get("id", ""),
            name=m.get("name", m.get("id", "")),
            description=m.get("description", ""),
            context_length=m.get("context_length", 0) or (m.get("top_provider") or {}).get("context_length", 0),
            pricing=m.get("pricing") or {},
            architecture=m.get("architecture") or {},
            supported_parameters=m.get("supported_parameters") or [],
        ))
    return result
