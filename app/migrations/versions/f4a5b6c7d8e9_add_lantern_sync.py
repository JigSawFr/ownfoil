"""Add Lantern sync data

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f4a5b6c7d8e9'
down_revision = 'e3f4a5b6c7d8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('lantern_sync_accounts',
        sa.Column('user', sa.String(), nullable=False),
        sa.Column('generation', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('user'),
    )
    op.create_table('lantern_sync_collections',
        sa.Column('user', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('rev', sa.Integer(), nullable=False),
        sa.Column('floor', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('user', 'name'),
    )
    op.create_table('lantern_sync_items',
        sa.Column('user', sa.String(), nullable=False),
        sa.Column('collection', sa.String(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('updated', sa.String(), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('deleted', sa.Integer(), nullable=False),
        sa.Column('rev', sa.Integer(), nullable=False),
        sa.Column('kept', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('user', 'collection', 'key'),
    )


def downgrade():
    op.drop_table('lantern_sync_items')
    op.drop_table('lantern_sync_collections')
    op.drop_table('lantern_sync_accounts')
