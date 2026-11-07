"""Add the Lantern change journal

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd2e3f4a5b6c7'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('lantern_meta',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_table('lantern_journal',
        sa.Column('position', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('platform', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('position'),
    )


def downgrade():
    op.drop_table('lantern_journal')
    op.drop_table('lantern_meta')
