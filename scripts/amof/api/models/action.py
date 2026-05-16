from typing import Optional, List, Literal
from pydantic import BaseModel

class ActionEcosystemRequest(BaseModel):
    """General action request if it requires a body, else can be empty."""
    pass

class TicketRepoSelectionRequest(BaseModel):
    repo: str
    mode: Literal["shared", "ticket_local"]
    source_branch: str
    target_branch: str

class TicketStartRequest(BaseModel):
    """Request body for starting a ticket."""
    ticket_id: str
    repos: Optional[List[str]] = None
    stage_id: Optional[str] = None
    environment_id: Optional[str] = None
    repo_selections: Optional[List[TicketRepoSelectionRequest]] = None


class TicketSwitchRequest(BaseModel):
    """Request body for switching ticket."""
    ticket_id: str
    request_id: Optional[str] = None


class TicketEndRequest(BaseModel):
    """Request body for ending a ticket."""
    ticket_id: str
    cleanup: Optional[bool] = False
    cleanup_local: Optional[bool] = False


class TicketSaveRequest(BaseModel):
    """Request body for capturing a ticket metadata snapshot."""
    ticket_id: str
    option_id: str
    expected_current_tag: Optional[str] = None

class RunRequest(BaseModel):
    prompt: str
    mode: Optional[str] = "execute"
    session_id: Optional[str] = None  # conversation session to resume (runs are children of sessions)
    agent_id: Optional[str] = None  # which agent prompt to use (master, planner, devops, etc.)
    runtime_profile: Optional[str] = None  # optional resolved-runtime profile, e.g. local_qwen


class DirectorGmdDevLocalProofRequest(BaseModel):
    ticket_id: Optional[str] = None
    environment_id: Optional[str] = "dev"
    input_path: Optional[str] = None
    output_path: Optional[str] = None
    local_port: Optional[int] = None
    request_id: Optional[str] = None


class ActionResponse(BaseModel):
    task_id: str
    run_id: str
    session_id: Optional[str] = None  # conversation session this run belongs to
    status: str
    request_id: Optional[str] = None


class RunStatusResponse(BaseModel):
    run_id: str
    ecosystem: str
    action: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    terminal: bool
    last_event_at: Optional[str] = None
    last_event_type: Optional[str] = None
    last_message: Optional[str] = None
