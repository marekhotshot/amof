"""Settings API: read/update agent config for LLM ladder and preferences."""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from amof.api.services.settings_service import (
    get_agent_config,
    resolve_agent_dry_run,
    update_agent_config,
)
from amof.orchestrator.llm.profile_catalog import (
    RECOMMENDED_DEMO_SELECTION,
    get_profile_selection,
    list_profile_catalog,
    validate_profile_selection,
)

router = APIRouter(prefix="/settings", tags=["settings"])


class LLMLadderRoles(BaseModel):
    orchestrator: Optional[Dict[str, List[str]]] = None
    worker: Optional[Dict[str, List[str]]] = None


class AgentSettingsUpdate(BaseModel):
    default_max_cost: Optional[float] = None
    model_ladder: Optional[bool] = None
    default_provider: Optional[str] = None
    verbose: Optional[bool] = None
    dry_run: Optional[bool] = None
    thinking_budget: Optional[int] = None
    auto_index: Optional[bool] = None
    budget_warning_thresholds: Optional[List[float]] = None
    llm_ladder: Optional[Dict[str, Any]] = None
    llm_profile_selection: Optional[Dict[str, str]] = None

class AgentPromptCreate(BaseModel):
    name: str
    content: str

@router.post("/prompts")
def create_prompt(body: AgentPromptCreate):
    """Create a new agent prompt file."""
    from amof.api.command_builder import get_workspace_root
    prompts_dir = get_workspace_root() / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    
    # Sanitize name
    safe_name = "".join(c if c.isalnum() or c == "-" else "-" for c in body.name.lower())
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid agent name")
        
    path = prompts_dir / f"{safe_name}.md"
    path.write_text(body.content, encoding="utf-8")
    return {"status": "ok", "id": safe_name, "filename": f"{safe_name}.md"}


def _config_with_defaults(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure runtime-effective keys are explicit for UI/operator visibility."""
    out = dict(raw)
    dry_run, dry_run_source = resolve_agent_dry_run(raw)
    out["dry_run"] = dry_run
    out["dry_run_source"] = dry_run_source
    out["llm_profile_selection"] = get_profile_selection(raw)
    out["llm_profile_catalog"] = list_profile_catalog()
    out["llm_profile_recommended_demo_mapping"] = dict(RECOMMENDED_DEMO_SELECTION)
    return out


@router.get("/agent", response_model=Dict[str, Any])
def get_settings_agent():
    """Return current .amof/agent.yaml as JSON."""
    return _config_with_defaults(get_agent_config())


@router.put("/agent", response_model=Dict[str, Any])
def put_settings_agent(body: AgentSettingsUpdate):
    """Update .amof/agent.yaml with provided keys (merge)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return _config_with_defaults(get_agent_config())
    if "llm_profile_selection" in updates:
        updates["llm_profile_selection"] = validate_profile_selection(
            updates["llm_profile_selection"]
        )
    try:
        updated = update_agent_config(updates)
        return _config_with_defaults(updated)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
