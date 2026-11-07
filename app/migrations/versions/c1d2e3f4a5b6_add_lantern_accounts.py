"""Add Lantern accounts, devices and invitations

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c1d2e3f4a5b6'
down_revision = 'b0c1d2e3f4a5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('lantern_accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('scopes', sa.String(), nullable=False, server_default=''),
        sa.Column('state', sa.String(), nullable=False, server_default='active'),
        sa.Column('created', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('name'),
        sa.UniqueConstraint('user_id'),
    )
    op.create_table('lantern_devices',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('scopes', sa.String(), nullable=False, server_default=''),
        sa.Column('key', sa.LargeBinary(), nullable=True),
        sa.Column('rating_cap', sa.String(), nullable=True),
        sa.Column('invitation', sa.String(), nullable=True),
        sa.Column('created', sa.String(), nullable=False),
        sa.Column('expires', sa.String(), nullable=True),
        sa.Column('last_used', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['account_id'], ['lantern_accounts.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_lantern_devices_account_id', 'lantern_devices', ['account_id'])
    op.create_table('lantern_invitations',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(), nullable=False),
        sa.Column('code_key', sa.String(), nullable=False),
        sa.Column('scopes', sa.String(), nullable=False, server_default=''),
        sa.Column('expires', sa.String(), nullable=False),
        sa.Column('devices', sa.Integer(), nullable=False),
        sa.Column('used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('rating_cap', sa.String(), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('created', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['account_id'], ['lantern_accounts.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('code_key'),
    )
    op.create_index('ix_lantern_invitations_account_id', 'lantern_invitations', ['account_id'])


def downgrade():
    op.drop_index('ix_lantern_invitations_account_id', table_name='lantern_invitations')
    op.drop_table('lantern_invitations')
    op.drop_index('ix_lantern_devices_account_id', table_name='lantern_devices')
    op.drop_table('lantern_devices')
    op.drop_table('lantern_accounts')
