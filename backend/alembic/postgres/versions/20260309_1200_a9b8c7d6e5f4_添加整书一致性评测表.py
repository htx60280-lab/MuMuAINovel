"""添加整书一致性评测表

Revision ID: a9b8c7d6e5f4
Revises: e5f6g7h8i9j0
Create Date: 2026-03-09 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'a9b8c7d6e5f4'
down_revision = 'e5f6g7h8i9j0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'story_snapshots',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=100), nullable=False),
        sa.Column('chapter_start', sa.Integer(), nullable=False),
        sa.Column('chapter_end', sa.Integer(), nullable=False),
        sa.Column('chapter_count', sa.Integer(), nullable=False),
        sa.Column('source_mode', sa.String(length=20), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_story_snapshots_project_id', 'story_snapshots', ['project_id'], unique=False)
    op.create_index('ix_story_snapshots_user_id', 'story_snapshots', ['user_id'], unique=False)
    op.create_index('ix_story_snapshots_content_hash', 'story_snapshots', ['content_hash'], unique=False)
    op.create_index('idx_story_snapshot_project_created', 'story_snapshots', ['project_id', 'created_at'], unique=False)

    op.create_table(
        'consistency_evaluations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('snapshot_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=100), nullable=False),
        sa.Column('trigger_type', sa.String(length=20), nullable=False),
        sa.Column('trigger_chapter_number', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=True),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('benchmark_name', sa.String(length=50), nullable=False),
        sa.Column('benchmark_version', sa.String(length=50), nullable=False),
        sa.Column('overall_score', sa.Float(), nullable=True),
        sa.Column('issue_count', sa.Integer(), nullable=False),
        sa.Column('summary_json', sa.JSON(), nullable=True),
        sa.Column('raw_result_json', sa.JSON(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['snapshot_id'], ['story_snapshots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_consistency_evaluations_project_id', 'consistency_evaluations', ['project_id'], unique=False)
    op.create_index('ix_consistency_evaluations_snapshot_id', 'consistency_evaluations', ['snapshot_id'], unique=False)
    op.create_index('ix_consistency_evaluations_user_id', 'consistency_evaluations', ['user_id'], unique=False)
    op.create_index('idx_consistency_eval_project_created', 'consistency_evaluations', ['project_id', 'created_at'], unique=False)
    op.create_index('idx_consistency_eval_status', 'consistency_evaluations', ['status'], unique=False)

    op.create_table(
        'consistency_issues',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('evaluation_id', sa.String(length=36), nullable=False),
        sa.Column('category', sa.String(length=50), nullable=False),
        sa.Column('subcategory', sa.String(length=100), nullable=True),
        sa.Column('severity', sa.String(length=20), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('exact_quote', sa.Text(), nullable=True),
        sa.Column('location_text', sa.String(length=200), nullable=True),
        sa.Column('chapter_number', sa.Integer(), nullable=True),
        sa.Column('evidence_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
        sa.ForeignKeyConstraint(['evaluation_id'], ['consistency_evaluations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_consistency_issues_evaluation_id', 'consistency_issues', ['evaluation_id'], unique=False)
    op.create_index('idx_consistency_issue_eval_category', 'consistency_issues', ['evaluation_id', 'category'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_consistency_issue_eval_category', table_name='consistency_issues')
    op.drop_index('ix_consistency_issues_evaluation_id', table_name='consistency_issues')
    op.drop_table('consistency_issues')
    op.drop_index('idx_consistency_eval_status', table_name='consistency_evaluations')
    op.drop_index('idx_consistency_eval_project_created', table_name='consistency_evaluations')
    op.drop_index('ix_consistency_evaluations_user_id', table_name='consistency_evaluations')
    op.drop_index('ix_consistency_evaluations_snapshot_id', table_name='consistency_evaluations')
    op.drop_index('ix_consistency_evaluations_project_id', table_name='consistency_evaluations')
    op.drop_table('consistency_evaluations')
    op.drop_index('idx_story_snapshot_project_created', table_name='story_snapshots')
    op.drop_index('ix_story_snapshots_content_hash', table_name='story_snapshots')
    op.drop_index('ix_story_snapshots_user_id', table_name='story_snapshots')
    op.drop_index('ix_story_snapshots_project_id', table_name='story_snapshots')
    op.drop_table('story_snapshots')
