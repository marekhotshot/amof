from typing import List, Optional, Any
from pydantic import BaseModel

class RunEvent(BaseModel):
    timestamp: float
    type: str
    message: str

class RunResponse(BaseModel):
    id: str
    ecosystem: str
    action: str
    status: str
    created_at: float
    updated_at: float
    exit_code: Optional[int] = None
    events: List[RunEvent] = []

class RunsListResponse(BaseModel):
    runs: List[Any]  # Use Any or a specific type, depending on how RunManager serializes it. Usually dict or object.
