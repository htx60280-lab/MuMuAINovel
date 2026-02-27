"""添加CriticAgent评分字段到plot_analysis

Revision ID: a2b3c4d5e6f7
Revises: d887fd1a30a6
Create Date: 2026-02-24 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'd887fd1a30a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('plot_analysis', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ooc_score', sa.Float(), nullable=True, comment='角色一致性评分 0-100'))
        batch_op.add_column(sa.Column('consistency_score', sa.Float(), nullable=True, comment='设定一致性评分 0-100'))
        batch_op.add_column(sa.Column('three_line_rhythm_score', sa.Float(), nullable=True, comment='三线节奏评分 0-100'))
        batch_op.add_column(sa.Column('critic_details', sa.JSON(), nullable=True, comment='CriticAgent 详细审查结果'))


def downgrade() -> None:
    with op.batch_alter_table('plot_analysis', schema=None) as batch_op:
        batch_op.drop_column('critic_details')
        batch_op.drop_column('three_line_rhythm_score')
        batch_op.drop_column('consistency_score')
        batch_op.drop_column('ooc_score')
