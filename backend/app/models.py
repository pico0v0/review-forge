"""Initial task and event persistence models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (Index("ix_review_tasks_project_status", "project_id", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    source_agent: Mapped[str] = mapped_column(String(64), default="generic")
    review_type: Mapped[str] = mapped_column(String(32), default="full")
    scope_json: Mapped[str] = mapped_column(Text, default="{}")
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="CREATED", index=True)
    current_round: Mapped[int] = mapped_column(Integer, default=0)
    max_rounds: Mapped[int] = mapped_column(Integer, default=3)
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_base: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_branch: Mapped[str | None] = mapped_column(String(128), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewer_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fixer_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fixer_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    events: Mapped[list[TaskEvent]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskEvent.id"
    )
    rounds: Mapped[list[ReviewRound]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="ReviewRound.round_no"
    )
    model_calls: Mapped[list[ModelCall]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="ModelCall.id"
    )


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(32), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    task: Mapped[ReviewTask] = relationship(back_populates="events")


class ReviewRound(Base):
    __tablename__ = "review_rounds"
    __table_args__ = (Index("uq_review_round_task_no", "task_id", "round_no", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True
    )
    round_no: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    findings_json: Mapped[str] = mapped_column(Text)
    request_path: Mapped[str] = mapped_column(Text)
    response_path: Mapped[str] = mapped_column(Text)
    patch_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    task: Mapped[ReviewTask] = relationship(back_populates="rounds")


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True
    )
    round_no: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    response_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    task: Mapped[ReviewTask] = relationship(back_populates="model_calls")
