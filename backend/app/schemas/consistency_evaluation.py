"""整书一致性评测 Schema。"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ConsistencyEvaluationCreateRequest(BaseModel):
    scope: str = Field(default="latest_10", description="评测范围 latest_10/project/chapter_range")
    start_chapter: Optional[int] = Field(default=None, description="起始章节")
    end_chapter: Optional[int] = Field(default=None, description="结束章节")
    snapshot_mode: str = Field(default="latest", description="快照模式 latest/published")
    provider: Optional[str] = Field(default=None, description="指定提供商")
    model: Optional[str] = Field(default=None, description="指定模型")
    trigger_type: str = Field(default="manual", description="触发方式 manual/auto")
    trigger_chapter_number: Optional[int] = Field(default=None, description="自动触发章节号")


class ConsistencyIssueResponse(BaseModel):
    id: str
    category: str
    subcategory: Optional[str] = None
    severity: str
    title: str
    description: Optional[str] = None
    exact_quote: Optional[str] = None
    location_text: Optional[str] = None
    chapter_number: Optional[int] = None
    evidence_json: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class ConsistencyEvaluationResponse(BaseModel):
    id: str
    project_id: str
    snapshot_id: str
    trigger_type: str
    trigger_chapter_number: Optional[int] = None
    status: str
    provider: Optional[str] = None
    model: Optional[str] = None
    benchmark_name: str
    benchmark_version: str
    overall_score: Optional[float] = None
    issue_count: int = 0
    summary_json: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class ConsistencyEvaluationDetailResponse(ConsistencyEvaluationResponse):
    issues: List[ConsistencyIssueResponse] = Field(default_factory=list)


class ConsistencyEvaluationListResponse(BaseModel):
    total: int
    items: List[ConsistencyEvaluationResponse]


class ConsistencyIssueListResponse(BaseModel):
    total: int
    items: List[ConsistencyIssueResponse]
