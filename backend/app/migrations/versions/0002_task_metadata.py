"""Add task metadata, model call, and review round persistence."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_tasks", sa.Column("snapshot_json", sa.Text(), server_default="{}", nullable=False)
    )
    op.add_column(
        "review_tasks",
        sa.Column("source_agent", sa.String(64), server_default="generic", nullable=False),
    )
    op.add_column(
        "review_tasks",
        sa.Column("review_type", sa.String(32), server_default="full", nullable=False),
    )
    op.add_column(
        "review_tasks", sa.Column("scope_json", sa.Text(), server_default="{}", nullable=False)
    )
    for name, kind in (
        ("base_commit", sa.String(64)),
        ("review_base", sa.String(64)),
        ("task_branch", sa.String(128)),
        ("worktree_path", sa.Text()),
        ("task_path", sa.Text()),
        ("reviewer_provider", sa.String(128)),
        ("reviewer_model", sa.String(128)),
        ("fixer_provider", sa.String(128)),
        ("fixer_model", sa.String(128)),
        ("verdict", sa.String(32)),
        ("error_message", sa.Text()),
        ("started_at", sa.DateTime(timezone=True)),
        ("completed_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("review_tasks", sa.Column(name, kind, nullable=True))
    op.add_column(
        "review_tasks", sa.Column("tokens_used", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "review_tasks", sa.Column("cost_usd", sa.Float(), server_default="0", nullable=False)
    )
    op.add_column(
        "task_events", sa.Column("actor", sa.String(32), server_default="system", nullable=False)
    )
    op.create_table(
        "review_rounds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("review_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("findings_json", sa.Text(), nullable=False),
        sa.Column("request_path", sa.Text(), nullable=False),
        sa.Column("response_path", sa.Text(), nullable=False),
        sa.Column("patch_path", sa.Text()),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_review_rounds_task_id", "review_rounds", ["task_id"])
    op.create_index(
        "uq_review_round_task_no", "review_rounds", ["task_id", "round_no"], unique=True
    )
    op.create_table(
        "model_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("review_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Float(), server_default="0", nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("response_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_model_calls_task_id", "model_calls", ["task_id"])


def downgrade() -> None:
    op.drop_column("review_tasks", "snapshot_json")
    op.drop_table("model_calls")
    op.drop_table("review_rounds")
    op.drop_column("task_events", "actor")
    for name in (
        "cost_usd",
        "tokens_used",
        "completed_at",
        "started_at",
        "error_message",
        "verdict",
        "fixer_model",
        "fixer_provider",
        "reviewer_model",
        "reviewer_provider",
        "task_path",
        "worktree_path",
        "task_branch",
        "review_base",
        "base_commit",
        "scope_json",
        "review_type",
        "source_agent",
    ):
        op.drop_column("review_tasks", name)
