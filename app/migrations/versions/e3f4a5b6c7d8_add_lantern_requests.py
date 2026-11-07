"""Add Lantern requests

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e3f4a5b6c7d8'
down_revision = 'd2e3f4a5b6c7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('lantern_requests',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user', sa.String(), nullable=False),
        sa.Column('platform', sa.String(), nullable=False),
        sa.Column('title_id', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('updated', sa.String(), nullable=True),
        sa.Column('reason', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_lantern_requests_user', 'lantern_requests', ['user'])


def downgrade():
    op.drop_index('ix_lantern_requests_user', table_name='lantern_requests')
    op.drop_table('lantern_requests')
