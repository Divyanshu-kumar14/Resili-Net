"""outbound mailbox context for direct email; wider notification kinds

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('outbound_emails') as batch_op:
        batch_op.add_column(sa.Column('mailbox_id', sa.String(length=200), nullable=True))
        batch_op.alter_column(
            'kind', existing_type=sa.String(length=40), type_=sa.String(length=120)
        )


def downgrade() -> None:
    with op.batch_alter_table('outbound_emails') as batch_op:
        batch_op.alter_column(
            'kind', existing_type=sa.String(length=120), type_=sa.String(length=40)
        )
        batch_op.drop_column('mailbox_id')
