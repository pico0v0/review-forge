"""HTTP request and response schemas for review tasks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import ModelRoleSettings
from app.projects import LimitSettings


class ReviewScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["diff", "paths", "module", "all"] = "diff"
    paths: list[str] = Field(default_factory=list, max_length=200)
    module_id: str | None = None
    base_ref: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> ReviewScope:
        if self.mode == "paths" and not self.paths:
            raise ValueError("paths mode requires at least one path")
        if self.mode == "module" and not self.module_id:
            raise ValueError("module mode requires module_id")
        if self.mode != "paths" and self.paths:
            raise ValueError("paths can only be provided in paths mode")
        if self.base_ref and (
            self.mode != "diff"
            or any(c.isspace() for c in self.base_ref)
            or self.base_ref.startswith("-")
            or ".." in self.base_ref
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_.-"
                for c in self.base_ref
            )
        ):
            raise ValueError("base_ref is only supported in diff mode")
        return self


class ReviewSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=128)
    source_agent: str = Field(default="generic", min_length=1, max_length=64)
    review_type: Literal["full", "security", "correctness"] = "full"
    scope: ReviewScope = Field(default_factory=ReviewScope)
    reviewer: ModelRoleSettings | None = None
    fixer: ModelRoleSettings | None = None
    limits: LimitSettings | None = None
    dry_run: bool = True


class ReviewTaskResponse(BaseModel):
    id: str
    project_id: str
    status: str
    source_agent: str
    review_type: str
    current_round: int
    max_rounds: int
    stop_reason: str | None
    base_commit: str | None
    review_base: str | None
    task_branch: str | None
    reviewer: dict[str, str] | None
    fixer: dict[str, str] | None
    verdict: str | None
    summary: str | None
    tokens_used: int
    cost_usd: float
    error_message: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
