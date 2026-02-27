"""添加CriticAgent评分字段到plot_analysis

Revision ID: e5f6g7h8i9j0
Revises: c1d2e3f4g5h6
Create Date: 2026-02-24 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6g7h8i9j0'
down_revision: Union[str, None] = 'c1d2e3f4g5h6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('plot_analysis', sa.Column('ooc_score', sa.Float(), nullable=True, comment='角色一致性评分 0-100'))
    op.add_column('plot_analysis', sa.Column('consistency_score', sa.Float(), nullable=True, comment='设定一致性评分 0-100'))
    op.add_column('plot_analysis', sa.Column('three_line_rhythm_score', sa.Float(), nullable=True, comment='三线节奏评分 0-100'))
    op.add_column('plot_analysis', sa.Column('critic_details', sa.JSON(), nullable=True, comment='CriticAgent 详细审查结果'))


def downgrade() -> None:
    op.drop_column('plot_analysis', 'critic_details')
    op.drop_column('plot_analysis', 'three_line_rhythm_score')
    op.drop_column('plot_analysis', 'consistency_score')
    op.drop_column('plot_analysis', 'ooc_score')
