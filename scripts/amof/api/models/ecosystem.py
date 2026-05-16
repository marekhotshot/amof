from typing import List, Optional, Union, Dict, Any
from pydantic import BaseModel, Field


class RepoDefinition(BaseModel):
    name: str
    url: Optional[str] = ""
    branch: Optional[str] = None
    path: Optional[str] = None
    readonly: Optional[bool] = False
    enabled: Optional[bool] = True


class RepoBranchStatusModel(BaseModel):
    repo: str
    branch: str
    commit: str
    mode: str
    status: str
    tracking_branch: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    fetch_state: Optional[str] = None
    fetched_at: Optional[str] = None
    fetch_message: Optional[str] = None


class RepoBranchInventoryModel(BaseModel):
    repo: str
    manifest_branch: Optional[str] = None
    default_branch: Optional[str] = None
    status: str
    readonly: bool = False
    branches: List[str] = Field(default_factory=list)
    raw_branches: List[str] = Field(default_factory=list)
    protected_branches: List[str] = Field(default_factory=list)


class EcosystemBranchSummaryModel(BaseModel):
    repo_count: int
    ahead_count: int
    behind_count: int
    dirty_count: int
    wrong_branch_count: int
    fetch_state: Optional[str] = None
    fetched_at: Optional[str] = None
    fetch_message: Optional[str] = None


class CreateEcosystemRequest(BaseModel):
    """Ecosystem create request; name must be slug-safe."""
    name: str = Field(..., min_length=1, description="Slug: alphanumeric, hyphen, underscore")
    repos: List[Union[str, RepoDefinition, Dict[str, Any]]] = Field(default_factory=list)
    description: Optional[str] = None

class EcosystemReadModel(BaseModel):
    """Ecosystem list/detail response."""
    name: str
    path: str
    repos: List[Union[str, RepoDefinition, Dict[str, Any]]]
    kb_files_count: int
    journal_files_count: int
    last_index_time: str
    active_tasks: int = 0
    branch_summary: Optional[EcosystemBranchSummaryModel] = None
    repo_statuses: List[RepoBranchStatusModel] = Field(default_factory=list)
    available_branches: List[str] = Field(default_factory=list)
    repo_branch_inventory: List[RepoBranchInventoryModel] = Field(default_factory=list)
