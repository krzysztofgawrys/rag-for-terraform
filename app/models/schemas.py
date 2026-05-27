from pydantic import BaseModel, Field
from typing import Optional
from uuid import UUID
from datetime import datetime


# -- Module --------------------------------------------------------------------

class ModuleBase(BaseModel):
    repo: str
    module_name: str
    module_path: str
    tags: list[str] = []
    variables: dict = {}
    outputs: dict = {}
    resources: list[str] = []
    description: Optional[str] = None


class ModuleCreate(ModuleBase):
    raw_code: str
    commit_sha: Optional[str] = None


class ModuleResponse(ModuleBase):
    id: UUID
    version: str = "latest"
    indexed_at: datetime
    commit_sha: Optional[str] = None

    class Config:
        from_attributes = True


# -- Index Job -----------------------------------------------------------------

class IndexJobCreate(BaseModel):
    repo_url: str
    branch: str = "main"
    commit_sha: Optional[str] = None
    triggered_by: str = "manual"
    discover_tags: bool = True
    force: bool = False  # re-generate descriptions + embeddings for all modules


class IndexJobResponse(BaseModel):
    id: UUID
    repo: str
    repo_url: Optional[str] = None
    branch: Optional[str]
    commit_sha: Optional[str]
    status: str
    triggered_by: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    error: Optional[str]
    stats: Optional[dict] = None

    class Config:
        from_attributes = True


# -- Query ---------------------------------------------------------------------

class QueryRequest(BaseModel):
    query: str
    query_type: str = Field(
        default="generate",
        description="generate | compose | optimize | audit | search"
    )
    repo_filter: Optional[list[str] | str] = None
    tag_filter: Optional[list[str]] = None
    version_filter: Optional[list[str] | str] = None  # None=latest, "*"=all, ["v1.0.0","v2.0.0"]=specific
    top_k: int = Field(default=5, ge=1, le=20)


class QueryResult(BaseModel):
    module_name: str
    repo: str
    module_path: str
    version: str = "latest"
    tags: list[str]
    similarity: float
    description: Optional[str]


class QueryResponse(BaseModel):
    answer: str
    sources: list[QueryResult]
    latency_ms: int


# -- Webhook payloads ----------------------------------------------------------

class GitHubPushPayload(BaseModel):
    ref: str
    after: str                      # commit SHA
    repository: dict
    commits: list[dict] = []


class GitLabPushPayload(BaseModel):
    ref: str
    after: str
    project: dict
    commits: list[dict] = []


# -- Consumer Indexing --------------------------------------------------------

# -- Knowledge Snippets ------------------------------------------------------

class SnippetResponse(BaseModel):
    id: UUID
    kind: str
    summary: str
    evidence_count: int = 1
    source_locator: Optional[str] = None
    related_refs: Optional[list[str]] = None
    scope: Optional[str] = None
    consumer_repo: Optional[str] = None
    updated_at: Optional[datetime] = None


class ModuleRefSnippetSummary(BaseModel):
    module_ref: str
    usage_count: int = 0
    convention_count: int = 0
    kinds: list[str] = []


class SnippetModuleDetail(BaseModel):
    module_ref: str
    conventions: dict[str, SnippetResponse] = {}
    usages: list[SnippetResponse] = []


# -- Consumer Indexing --------------------------------------------------------

class ConsumerIndexJobCreate(BaseModel):
    repo_url: str
    branch: str = "main"
    commit_sha: Optional[str] = None
    triggered_by: str = "manual"
    force_clone: bool = False
    run_distillation: bool = True


class PaginatedIndexJobs(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[IndexJobResponse]


class ConsumerIndexJobResponse(BaseModel):
    id: UUID
    repo: str
    repo_url: Optional[str] = None
    branch: Optional[str]
    commit_sha: Optional[str]
    status: str
    triggered_by: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    error: Optional[str]
    stats: Optional[dict] = None

    class Config:
        from_attributes = True


class PaginatedConsumerJobs(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ConsumerIndexJobResponse]
