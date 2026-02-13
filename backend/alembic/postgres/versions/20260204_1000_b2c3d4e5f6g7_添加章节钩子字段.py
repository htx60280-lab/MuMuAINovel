"""添加章节钩子字段和审查结果缓存

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2026-02-04 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6g7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    """添加 end_hook 和 review_result 字段到 chapters 表"""
    op.add_column(
        'chapters',
        sa.Column(
            'end_hook',
            sa.JSON,
            nullable=True,
            comment='章节结尾钩子: {type, content, must_respond_next}'
        )
    )
    op.add_column(
        'chapters',
        sa.Column(
            'review_result',
            sa.JSON,
            nullable=True,
            comment='AI审查结果缓存: {overall_score, dimensions, reviewed_at}'
        )
    )


def downgrade():
    """移除 end_hook 和 review_result 字段"""
    op.drop_column('chapters', 'review_result')
    op.drop_column('chapters', 'end_hook')
