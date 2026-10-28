"""Validated model response protocol shared by reviewer and fixer."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.paths import relative_path


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    severity: Literal["P0", "P1", "P2", "P3", "info"]
    category: Literal[
        "security",
        "correctness",
        "concurrency",
        "error_handling",
        "boundary",
        "compatibility",
        "performance",
        "maintainability",
        "testing",
        "other",
    ] = "other"
    file: str
    line: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1)
    description: str = ""
    impact: str = ""
    recommendation: str = ""
    confidence: Literal["high", "medium", "low"] = "high"
    status: Literal["confirmed", "needs_verification"] = "confirmed"

    _validate_file = field_validator("file")(relative_path)


class FileEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str

    _validate_path = field_validator("path")(relative_path)


class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["2.0"]
    task_id: str
    round: int = Field(ge=1)
    verdict: Literal["approved", "changes_required", "needs_info", "blocked"]
    summary: str
    findings: list[Finding]
    patch: str | None = None
    edits: list[FileEdit] | None = None
    notes: str = ""

    @model_validator(mode="after")
    def validate_changes(self):
        if self.patch and self.edits:
            raise ValueError("patch and edits are mutually exclusive")
        if len({finding.id for finding in self.findings}) != len(self.findings):
            raise ValueError("Finding IDs must be unique")
        if self.edits and len({edit.path.casefold() for edit in self.edits}) != len(self.edits):
            raise ValueError("File edit paths must be unique")
        return self


def parse_response(content: str, task_id: str, round_no: int) -> ReviewResponse:
    if not isinstance(content, str):
        raise ValueError("Model response content must be a string")
    text = content.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    response = ReviewResponse.model_validate(json.loads(text))
    if response.task_id != task_id or response.round != round_no:
        raise ValueError("Model response does not match the requested task and round")
    return response
