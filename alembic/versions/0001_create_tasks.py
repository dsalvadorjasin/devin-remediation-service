"""create tasks table

Revision ID: 0001
Revises:
Create Date: 2026-09-21
"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("issue_number", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("issue_url", sa.String(), nullable=False, server_default=""),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("session_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("pr_url", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])


def downgrade() -> None:
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_table("tasks")
