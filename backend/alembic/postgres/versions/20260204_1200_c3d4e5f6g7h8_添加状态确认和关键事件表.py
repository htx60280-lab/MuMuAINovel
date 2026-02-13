"""添加待确认状态变化字段和关键事件表

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-02-04 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6g7h8'
down_revision = 'b2c3d4e5f6g7'
branch_labels = None
depends_on = None


def upgrade():
    """
    1. 添加 pending_state_change 字段到 chapters 表（状态污染防护）
    2. 创建 key_events 表（分层摘要 + 关键事件时间轴）
    """
    # 1. 添加待确认状态变化字段
    op.add_column(
        'chapters',
        sa.Column(
            'pending_state_change',
            sa.JSON,
            nullable=True,
            comment='待确认的状态变化: {items_gained, items_lost, location_change, ...}'
        )
    )

    # 2. 创建关键事件表
    op.create_table(
        'key_events',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('chapter_number', sa.Integer, nullable=False, comment='发生章节'),
        sa.Column('event_type', sa.String(50), nullable=False, comment='事件类型'),
        sa.Column('title', sa.String(200), nullable=False, comment='事件标题'),
        sa.Column('description', sa.Text, comment='事件详细描述'),
        sa.Column('related_entities', sa.JSON, comment='关联实体'),
        sa.Column('importance', sa.Integer, default=3, comment='重要程度 1-5'),
        sa.Column('is_resolved', sa.Integer, default=0, comment='是否已回收'),
        sa.Column('resolved_chapter', sa.Integer, comment='回收章节号'),
        sa.Column('metadata', sa.JSON, comment='额外元数据'),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now())
    )

    # 创建索引
    op.create_index('ix_key_events_project_importance', 'key_events', ['project_id', 'importance'])
    op.create_index('ix_key_events_project_type', 'key_events', ['project_id', 'event_type'])


def downgrade():
    """回滚迁移"""
    op.drop_index('ix_key_events_project_type', 'key_events')
    op.drop_index('ix_key_events_project_importance', 'key_events')
    op.drop_table('key_events')
    op.drop_column('chapters', 'pending_state_change')
