"""添加 pgvector 章节记忆表

Revision ID: 8b3c4d5e6f7a
Revises: 421237957b27
Create Date: 2026-02-03 10:00:00.000000

Embedding 维度自动识别：
- 根据 EMBEDDING_MODEL 环境变量自动匹配维度
- 也可通过 EMBEDDING_DIMENSIONS 环境变量手动覆盖
- 默认 1536 维（匹配 text-embedding-3-small）
"""
from typing import Sequence, Union
import os

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b3c4d5e6f7a'
down_revision: Union[str, None] = '421237957b27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 常见 Embedding 模型的维度映射
MODEL_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "BAAI/bge-small-zh-v1.5": 512,
    "BAAI/bge-base-zh-v1.5": 768,
    "BAAI/bge-large-zh-v1.5": 1024,
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
}


def get_embedding_dimensions() -> int:
    """根据环境变量自动识别 embedding 维度"""
    # 手动配置优先
    if os.getenv("EMBEDDING_DIMENSIONS"):
        return int(os.getenv("EMBEDDING_DIMENSIONS"))

    # 根据模型自动识别
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    return MODEL_DIMENSIONS.get(model, 1536)


EMBEDDING_DIMENSIONS = get_embedding_dimensions()


def upgrade() -> None:
    # 创建 vector 扩展（如果不存在）
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 创建 chapter_memories 表
    op.create_table('chapter_memories',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('chapter_id', sa.String(length=36), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False, comment='切片序号'),
        sa.Column('content', sa.Text(), nullable=False, comment='文本切片(300-500字)'),
        sa.Column('embedding', sa.LargeBinary(), nullable=True, comment='pgvector 向量'),
        sa.Column('memory_type', sa.String(length=50), nullable=True, comment='类型: narrative/dialogue/description'),
        sa.Column('characters_mentioned', sa.JSON(), nullable=True, comment='涉及角色列表'),
        sa.Column('locations_mentioned', sa.JSON(), nullable=True, comment='涉及地点列表'),
        sa.Column('timeline_state', sa.JSON(), nullable=True, comment='当前时间线状态'),
        sa.Column('importance_score', sa.Float(), nullable=True, comment='重要性评分 0.0-1.0'),
        sa.Column('story_timeline', sa.Integer(), nullable=True, comment='章节序号(冗余存储)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True, comment='更新时间'),
        sa.ForeignKeyConstraint(['chapter_id'], ['chapters.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # 修改 embedding 列为配置的维度
    op.execute(f"ALTER TABLE chapter_memories ALTER COLUMN embedding TYPE vector({EMBEDDING_DIMENSIONS}) USING NULL")

    # 创建索引
    op.create_index(op.f('ix_chapter_memories_project_id'), 'chapter_memories', ['project_id'], unique=False)
    op.create_index(op.f('ix_chapter_memories_chapter_id'), 'chapter_memories', ['chapter_id'], unique=False)

    # 创建 HNSW 向量索引（用于高效的余弦相似度搜索）
    op.execute("""
        CREATE INDEX ix_chapter_memories_embedding_hnsw
        ON chapter_memories
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    # 删除 HNSW 索引
    op.execute("DROP INDEX IF EXISTS ix_chapter_memories_embedding_hnsw")

    # 删除普通索引
    op.drop_index(op.f('ix_chapter_memories_chapter_id'), table_name='chapter_memories')
    op.drop_index(op.f('ix_chapter_memories_project_id'), table_name='chapter_memories')

    # 删除表
    op.drop_table('chapter_memories')

    # 注意：不删除 vector 扩展，因为可能被其他表使用
