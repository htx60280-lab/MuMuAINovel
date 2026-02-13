"""升级 embedding 维度并添加状态容器字段

Revision ID: 9c4d5e6f7a8b
Revises: 8b3c4d5e6f7a
Create Date: 2026-02-03 15:00:00.000000

Embedding 维度自动识别：
- 根据 EMBEDDING_MODEL 环境变量自动匹配维度
- 也可通过 EMBEDDING_DIMENSIONS 环境变量手动覆盖
"""
from typing import Sequence, Union
import os

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c4d5e6f7a8b'
down_revision: Union[str, None] = '8b3c4d5e6f7a'
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
    if os.getenv("EMBEDDING_DIMENSIONS"):
        return int(os.getenv("EMBEDDING_DIMENSIONS"))
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    return MODEL_DIMENSIONS.get(model, 1536)


EMBEDDING_DIMENSIONS = get_embedding_dimensions()


def upgrade() -> None:
    # 1. 升级 chapter_memories.embedding 维度
    # 先删除旧的 HNSW 索引
    op.execute("DROP INDEX IF EXISTS ix_chapter_memories_embedding_hnsw")

    # 清空现有 embedding 数据（维度变更后旧数据无效）
    op.execute("UPDATE chapter_memories SET embedding = NULL")

    # 修改列类型为配置的维度
    op.execute(f"ALTER TABLE chapter_memories ALTER COLUMN embedding TYPE vector({EMBEDDING_DIMENSIONS}) USING NULL")

    # 重建 HNSW 索引
    op.execute("""
        CREATE INDEX ix_chapter_memories_embedding_hnsw
        ON chapter_memories
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # 2. 添加 world_state 到 projects 表
    op.add_column('projects', sa.Column(
        'world_state',
        sa.JSON(),
        nullable=True,
        comment='全局状态: {current_location, inventory, relationships, status_changes, last_time_reference}'
    ))

    # 3. 添加 state_change_log 到 chapters 表
    op.add_column('chapters', sa.Column(
        'state_change_log',
        sa.JSON(),
        nullable=True,
        comment='本章状态变更: {location_change, items_gained, items_lost, status_changes, relationships, time_passed, summary}'
    ))


def downgrade() -> None:
    # 删除 state_change_log 列
    op.drop_column('chapters', 'state_change_log')

    # 删除 world_state 列
    op.drop_column('projects', 'world_state')

    # 恢复 embedding 为 384 维
    op.execute("DROP INDEX IF EXISTS ix_chapter_memories_embedding_hnsw")
    op.execute("UPDATE chapter_memories SET embedding = NULL")
    op.execute("ALTER TABLE chapter_memories ALTER COLUMN embedding TYPE vector(384) USING NULL")
    op.execute("""
        CREATE INDEX ix_chapter_memories_embedding_hnsw
        ON chapter_memories
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)
