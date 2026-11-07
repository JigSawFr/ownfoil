"""Add Lantern cheats

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a5b6c7d8e9f0'
down_revision = 'f4a5b6c7d8e9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('lantern_cheats',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('platform', sa.String(), nullable=False),
        sa.Column('title_id', sa.String(), nullable=False),
        sa.Column('build_id', sa.String(), nullable=True),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('updated', sa.String(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_lantern_cheats_title_id', 'lantern_cheats', ['title_id'])


def downgrade():
    op.drop_index('ix_lantern_cheats_title_id', table_name='lantern_cheats')
    op.drop_table('lantern_cheats')
