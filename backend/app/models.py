"""Initial task and event persistence models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (Index("ix_review_tasks_project_status", "project_id", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="CREATED", index=True)
    current_round: Mapped[int] = mapped_column(Integer, default=0)
    max_rounds: Mapped[int] = mapped_column(Integer, default=3)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    events: Mapped[list[TaskEvent]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskEvent.id"
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    task: Mapped[ReviewTask] = relationship(back_populates="events")
