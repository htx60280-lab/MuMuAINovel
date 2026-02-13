"""添加 Embedding 配置字段到 settings 表

Revision ID: a1b2c3d4e5f6
Revises: 9c4d5e6f7a8b
Create Date: 2026-02-03 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '9c4d5e6f7a8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 添加 Embedding 配置字段到 settings 表
    op.add_column('settings', sa.Column(
        'embedding_api_key',
        sa.String(length=500),
        nullable=True,
        comment='Embedding API密钥（可选）'
    ))
    op.add_column('settings', sa.Column(
        'embedding_base_url',
        sa.String(length=500),
        nullable=True,
        comment='Embedding API地址（可选）'
    ))
    op.add_column('settings', sa.Column(
        'embedding_model',
        sa.String(length=100),
        nullable=True,
        server_default='text-embedding-3-small',
        comment='Embedding模型名称'
    ))


def downgrade() -> None:
    op.drop_column('settings', 'embedding_model')
    op.drop_column('settings', 'embedding_base_url')
    op.drop_column('settings', 'embedding_api_key')
