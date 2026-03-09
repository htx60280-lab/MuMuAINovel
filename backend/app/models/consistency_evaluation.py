"""整书一致性评测相关模型。"""
from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, JSON, Float, Index
from sqlalchemy.sql import func

from app.database import Base
import uuid


class StorySnapshot(Base):
    """故事快照表：固化某次评测时的章节内容。"""

    __tablename__ = "story_snapshots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(100), nullable=False, index=True, comment="用户ID")
    chapter_start = Column(Integer, nullable=False, comment="起始章节")
    chapter_end = Column(Integer, nullable=False, comment="结束章节")
    chapter_count = Column(Integer, nullable=False, default=0, comment="章节数量")
    source_mode = Column(String(20), nullable=False, default="latest", comment="快照来源 latest/published")
    content = Column(Text, nullable=False, comment="拼接后的故事文本")
    content_hash = Column(String(64), nullable=False, index=True, comment="内容哈希")
    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")

    __table_args__ = (
        Index("idx_story_snapshot_project_created", "project_id", "created_at"),
    )


class ConsistencyEvaluation(Base):
    """整书一致性评测任务表。"""

    __tablename__ = "consistency_evaluations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_id = Column(String(36), ForeignKey("story_snapshots.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(100), nullable=False, index=True, comment="用户ID")
    trigger_type = Column(String(20), nullable=False, default="manual", comment="触发方式 manual/auto")
    trigger_chapter_number = Column(Integer, nullable=True, comment="自动触发时对应章节号")
    status = Column(String(20), nullable=False, default="pending", comment="任务状态 pending/running/succeeded/failed")
    provider = Column(String(50), nullable=True, comment="评测使用的提供商")
    model = Column(String(100), nullable=True, comment="评测使用的模型")
    benchmark_name = Column(String(50), nullable=False, default="constory-bench", comment="评测器名称")
    benchmark_version = Column(String(50), nullable=False, default="poc-v1", comment="评测器版本")
    overall_score = Column(Float, nullable=True, comment="总体评分")
    issue_count = Column(Integer, nullable=False, default=0, comment="问题总数")
    summary_json = Column(JSON, nullable=True, comment="聚合摘要")
    raw_result_json = Column(JSON, nullable=True, comment="原始结果")
    error_message = Column(Text, nullable=True, comment="错误信息")
    started_at = Column(DateTime, nullable=True, comment="开始时间")
    finished_at = Column(DateTime, nullable=True, comment="完成时间")
    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")

    __table_args__ = (
        Index("idx_consistency_eval_project_created", "project_id", "created_at"),
        Index("idx_consistency_eval_status", "status"),
    )


class ConsistencyIssue(Base):
    """一致性问题明细表。"""

    __tablename__ = "consistency_issues"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    evaluation_id = Column(String(36), ForeignKey("consistency_evaluations.id", ondelete="CASCADE"), nullable=False, index=True)
    category = Column(String(50), nullable=False, comment="问题大类")
    subcategory = Column(String(100), nullable=True, comment="问题子类")
    severity = Column(String(20), nullable=False, default="medium", comment="严重等级")
    title = Column(String(200), nullable=False, comment="问题标题")
    description = Column(Text, nullable=True, comment="问题描述")
    exact_quote = Column(Text, nullable=True, comment="原文证据")
    location_text = Column(String(200), nullable=True, comment="位置描述")
    chapter_number = Column(Integer, nullable=True, comment="映射章节号")
    evidence_json = Column(JSON, nullable=True, comment="扩展证据")
    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")

    __table_args__ = (
        Index("idx_consistency_issue_eval_category", "evaluation_id", "category"),
    )
