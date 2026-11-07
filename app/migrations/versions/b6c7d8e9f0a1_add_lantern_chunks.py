"""Add Lantern chunk indexes

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b6c7d8e9f0a1'
down_revision = 'a5b6c7d8e9f0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('lantern_chunks',
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('ref_size', sa.Integer(), nullable=False),
        sa.Column('ref_sha256', sa.String(length=64), nullable=False),
        sa.Column('raw', sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint('sha256'),
    )


def downgrade():
    op.drop_table('lantern_chunks')
