"""Create the initial task and event tables."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_round", sa.Integer(), nullable=False),
        sa.Column("max_rounds", sa.Integer(), nullable=False),
        sa.Column("stop_reason", sa.String(64)),
        sa.Column("summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_review_tasks_project_id", "review_tasks", ["project_id"])
    op.create_index("ix_review_tasks_status", "review_tasks", ["status"])
    op.create_index("ix_review_tasks_project_status", "review_tasks", ["project_id", "status"])
    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("review_tasks.id", ondelete="CASCADE")),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32)),
        sa.Column("message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])


def downgrade() -> None:
    op.drop_table("task_events")
    op.drop_table("review_tasks")
