"""关键事件数据模型 - 用于分层摘要和长跨度伏笔追踪"""
from sqlalchemy import Column, String, Text, Integer, DateTime, ForeignKey, JSON
from sqlalchemy.sql import func
from app.database import Base
import uuid


class KeyEvent(Base):
    """关键事件表 - 记录全书重要事件时间轴"""
    __tablename__ = "key_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    chapter_number = Column(Integer, nullable=False, comment="发生章节")

    # 事件类型
    event_type = Column(
        String(50),
        nullable=False,
        comment="事件类型: foreshadow_plant(伏笔埋入), foreshadow_resolve(伏笔回收), "
                "major_turn(重大转折), character_intro(角色登场), character_death(角色死亡), "
                "item_acquire(重要物品获得), item_lose(重要物品失去), "
                "relationship_change(关系变化), location_change(重要地点变化), "
                "power_up(实力提升), secret_reveal(秘密揭示)"
    )

    # 事件内容
    title = Column(String(200), nullable=False, comment="事件标题")
    description = Column(Text, comment="事件详细描述")

    # 关联实体
    related_entities = Column(JSON, comment="关联实体: {characters: [], items: [], locations: []}")

    # 重要程度 (1-5)
    importance = Column(Integer, default=3, comment="重要程度: 1=次要, 3=普通, 5=核心")

    # 是否已回收（针对伏笔类事件）
    is_resolved = Column(Integer, default=0, comment="是否已回收: 0=未回收, 1=已回收")
    resolved_chapter = Column(Integer, comment="回收章节号")

    # 元数据
    extra_metadata = Column("metadata", JSON, comment="额外元数据")

    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return f"<KeyEvent(id={self.id}, type={self.event_type}, title={self.title}, chapter={self.chapter_number})>"
