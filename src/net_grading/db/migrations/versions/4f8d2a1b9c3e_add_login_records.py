"""add login_records table

Revision ID: 4f8d2a1b9c3e
Revises: 772a27ebf0b1
Create Date: 2026-04-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4f8d2a1b9c3e'
down_revision: Union[str, Sequence[str], None] = '772a27ebf0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'login_records',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=False),
        sa.Column('student_id', sa.String(length=32), nullable=False),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('login_records', schema=None) as batch_op:
        batch_op.create_index('ix_login_records_ip_created', ['ip', 'created_at'], unique=False)
        batch_op.create_index('ix_login_records_student_id', ['student_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('login_records', schema=None) as batch_op:
        batch_op.drop_index('ix_login_records_student_id')
        batch_op.drop_index('ix_login_records_ip_created')
    op.drop_table('login_records')
