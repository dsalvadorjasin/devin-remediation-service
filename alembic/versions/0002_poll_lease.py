"""add poll lease columns to tasks

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("poll_token", sa.String(length=32), nullable=True))
    op.add_column(
        "tasks", sa.Column("poll_lease_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tasks", "poll_lease_until")
    op.drop_column("tasks", "poll_token")
