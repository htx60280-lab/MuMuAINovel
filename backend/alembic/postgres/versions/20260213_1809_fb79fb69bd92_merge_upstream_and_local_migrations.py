"""merge_upstream_and_local_migrations

Revision ID: fb79fb69bd92
Revises: c3d4e5f6g7h8, d4d253e3f4c6
Create Date: 2026-02-13 18:09:16.486821

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fb79fb69bd92'
down_revision: Union[str, None] = ('c3d4e5f6g7h8', 'd4d253e3f4c6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass