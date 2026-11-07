"""Add sha256 and its validators to files

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b0c1d2e3f4a5'
down_revision = 'a9b0c1d2e3f4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('files') as batch:
        batch.add_column(sa.Column('sha256', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('sha256_size', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('sha256_mtime', sa.Float(), nullable=True))
        batch.create_index('ix_files_sha256', ['sha256'])


def downgrade():
    with op.batch_alter_table('files') as batch:
        batch.drop_index('ix_files_sha256')
        batch.drop_column('sha256_mtime')
        batch.drop_column('sha256_size')
        batch.drop_column('sha256')
