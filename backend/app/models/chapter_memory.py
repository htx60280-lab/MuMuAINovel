"""章节记忆向量存储模型 - 基于 pgvector"""
import uuid
from sqlalchemy import Column, String, Integer, Float, Text, DateTime, ForeignKey, JSON
from sqlalchemy.sql import func

from app.database import Base
from app.config import EMBEDDING_DIMENSIONS

try:
    from pgvector.sqlalchemy import Vector
    HAS_PGVECTOR = True
except ImportError:
    HAS_PGVECTOR = False
    Vector = None


class ChapterMemory(Base):
    """章节记忆切片表 - 用于 Context Agent 的向量检索"""
    __tablename__ = "chapter_memories"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    chapter_id = Column(String(36), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False, comment="切片序号")
    content = Column(Text, nullable=False, comment="文本切片(300-500字)")
    # 1536 维 embedding，匹配 text-embedding-3-small
    embedding = Column(Vector(EMBEDDING_DIMENSIONS), comment="pgvector 向量") if HAS_PGVECTOR else Column(Text, comment="向量(JSON序列化)")
    memory_type = Column(String(50), default="narrative", comment="类型: narrative/dialogue/description")
    characters_mentioned = Column(JSON, comment="涉及角色列表")
    locations_mentioned = Column(JSON, comment="涉及地点列表")
    timeline_state = Column(JSON, comment="当前时间线状态")
    importance_score = Column(Float, default=0.5, comment="重要性评分 0.0-1.0")
    story_timeline = Column(Integer, comment="章节序号(冗余存储)")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return f"<ChapterMemory(id={self.id[:8]}, chapter_id={self.chapter_id[:8]}, chunk={self.chunk_index})>"
