"""审查功能的 Pydantic 模型"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class ReviewRequest(BaseModel):
    """审查请求"""
    dimensions: Optional[List[str]] = Field(
        default=None,
        description="要审查的维度列表，默认全部: ooc, pacing, high_point, consistency"
    )
    force_refresh: bool = Field(
        default=False,
        description="是否强制重新审查（忽略缓存）"
    )


class DimensionResultResponse(BaseModel):
    """单个维度的审查结果"""
    dimension: str = Field(..., description="维度名称")
    score: float = Field(..., description="评分 0-100")
    analysis: str = Field(..., description="分析内容")
    suggestions: List[str] = Field(default=[], description="改进建议")
    details: Dict[str, Any] = Field(default={}, description="详细数据")


class ReviewResponse(BaseModel):
    """审查响应"""
    chapter_id: str = Field(..., description="章节ID")
    overall_score: float = Field(..., description="综合评分 0-100")
    dimensions: Dict[str, DimensionResultResponse] = Field(
        default={},
        description="各维度审查结果"
    )
    reviewed_at: str = Field(..., description="审查时间")
    metadata: Dict[str, Any] = Field(default={}, description="元数据")

    class Config:
        json_schema_extra = {
            "example": {
                "chapter_id": "abc123",
                "overall_score": 78.5,
                "dimensions": {
                    "ooc": {
                        "dimension": "ooc",
                        "score": 85,
                        "analysis": "角色行为基本符合设定...",
                        "suggestions": ["建议强化角色特征"],
                        "details": {}
                    },
                    "pacing": {
                        "dimension": "pacing",
                        "score": 72,
                        "analysis": "节奏整体流畅...",
                        "suggestions": ["可适当增加对话"],
                        "details": {}
                    },
                    "high_point": {
                        "dimension": "high_point",
                        "score": 78,
                        "analysis": "爽点设置合理...",
                        "suggestions": ["可增加悬念设置"],
                        "details": {}
                    }
                },
                "reviewed_at": "2026-02-03T10:00:00",
                "metadata": {"chapter_number": 5, "word_count": 3500}
            }
        }
