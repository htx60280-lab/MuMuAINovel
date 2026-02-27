"""调整 chapter_memories 向量维度至 2560

Revision ID: c1d2e3f4g5h6
Revises: fb79fb69bd92
Create Date: 2026-02-15 21:00:00.000000

说明：
- 将 chapter_memories.embedding 改为 vector(2560)
- 清空旧向量并按维度决定索引策略
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4g5h6'
down_revision: Union[str, None] = 'fb79fb69bd92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TARGET_DIMENSIONS = 2560
INDEX_DIMENSION_LIMIT = 2000


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chapter_memories_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_chapter_memories_embedding_ivfflat")
    op.execute("UPDATE chapter_memories SET embedding = NULL")
    op.execute(f"ALTER TABLE chapter_memories ALTER COLUMN embedding TYPE vector({TARGET_DIMENSIONS}) USING NULL")

    # HNSW/IVFFLAT 索引对维度有上限，超过限制时跳过索引创建
    if TARGET_DIMENSIONS <= INDEX_DIMENSION_LIMIT:
        op.execute("""
            CREATE INDEX ix_chapter_memories_embedding_ivfflat
            ON chapter_memories
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100)
        """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chapter_memories_embedding_ivfflat")
    op.execute("UPDATE chapter_memories SET embedding = NULL")
    op.execute("ALTER TABLE chapter_memories ALTER COLUMN embedding TYPE vector(1536) USING NULL")
    op.execute("""
        CREATE INDEX ix_chapter_memories_embedding_hnsw
        ON chapter_memories
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)
