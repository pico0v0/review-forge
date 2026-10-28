"""Project configuration loading and safe atomic updates."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

import yaml
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import ModelRoleSettings


class ProjectConfigError(ValueError):
    """Raised when a project entry is invalid or unsafe."""


def _is_unsafe_relative_path(value: str) -> bool:
    posix = PurePosixPath(value.replace("\\", "/"))
    windows = PureWindowsPath(value)
    return (
        not value
        or posix.is_absolute()
        or bool(windows.drive)
        or ".." in posix.parts
        or ":" in value
    )


class GateSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocking_severities: list[Literal["P0", "P1", "P2", "P3"]] = ["P0", "P1"]
    require_verdict: bool = False
    require_tests: bool = True
    require_acceptance: bool = True


class LimitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rounds: int = Field(default=3, ge=1, le=100)
    max_fix_attempts_per_round: int = Field(default=2, ge=0, le=20)
    max_total_tokens: int = Field(default=1_000_000, ge=0)
    max_cost_usd: float = Field(default=5, ge=0)
    max_wall_time_min: int = Field(default=120, ge=1, le=7 * 24 * 60)
    no_progress_rounds: int = Field(default=2, ge=1, le=20)


class ModuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    paths: list[str] = Field(min_length=1)
    test_command: str | None = None
    acceptance_command: str | None = None

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            if _is_unsafe_relative_path(path):
                raise ValueError("Module paths must be non-empty project-relative patterns")
        return value


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    source: Literal["qoder", "antigravity", "generic"] = "generic"
    root: Path
    review_prompt: str = "prompts/reviewer.md"
    default_base_ref: str | None = None
    reviewer: ModelRoleSettings | None = None
    fixer: ModelRoleSettings | None = None
    test_command: str | None = None
    acceptance_command: str | None = None
    finalize_mode: Literal["apply_to_project", "dry_run", "branch"] = "apply_to_project"
    egress_allowed: bool = True
    max_patch_files: int = Field(default=50, ge=1, le=1000)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    target_paths: list[str] = Field(default_factory=list)
    gate: GateSettings = Field(default_factory=GateSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    modules: list[ModuleConfig] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("root", mode="before")
    @classmethod
    def normalize_root(cls, value: str | Path) -> Path:
        root = Path(value).expanduser()
        if not root.is_absolute():
            raise ValueError("Project root must be absolute or use ~")
        return root.resolve()

    @field_validator("target_paths")
    @classmethod
    def validate_target_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            if _is_unsafe_relative_path(path):
                raise ValueError("Target paths must be non-empty project-relative paths")
        return value

    @model_validator(mode="after")
    def validate_modules(self) -> ProjectConfig:
        module_ids = [module.id for module in self.modules]
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("Module IDs must be unique within a project")
        return self


class ProjectRegistry:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir.expanduser().resolve()
        self.hub_root = self.config_dir.parent
        self.path = self.config_dir / "projects.yaml"

    def list(self) -> list[ProjectConfig]:
        return self._read()

    def get(self, project_id: str) -> ProjectConfig | None:
        return next((project for project in self._read() if project.id == project_id), None)

    def register(self, project: ProjectConfig) -> ProjectConfig:
        self._validate_project(project)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock"):
            projects = self._read()
            if any(existing.id == project.id for existing in projects):
                raise ProjectConfigError(f"Project {project.id!r} already exists")
            self._check_overlap(project, projects)
            projects.append(project)
            self._write(projects)
        return project

    def update(self, project_id: str, project: ProjectConfig) -> ProjectConfig:
        if project.id != project_id:
            raise ProjectConfigError("Project ID cannot be changed")
        self._validate_project(project)
        with FileLock(str(self.path) + ".lock"):
            projects = self._read()
            for index, current in enumerate(projects):
                if current.id == project_id:
                    self._check_overlap(project, projects[:index] + projects[index + 1 :])
                    projects[index] = project
                    self._write(projects)
                    return project
        raise ProjectConfigError(f"Project {project_id!r} was not found")

    def _check_overlap(self, project: ProjectConfig, projects: list[ProjectConfig]) -> None:
        for existing in projects:
            if project.root.is_relative_to(existing.root) or existing.root.is_relative_to(
                project.root
            ):
                raise ProjectConfigError("Project roots must not overlap")

    def _read(self) -> list[ProjectConfig]:
        if not self.path.exists():
            return []
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ProjectConfigError(f"Could not read {self.path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("projects", []), list):
            raise ProjectConfigError("projects.yaml must contain a projects list")
        if set(raw) - {"projects"}:
            raise ProjectConfigError("projects.yaml only supports the projects key")
        try:
            projects = [ProjectConfig.model_validate(item) for item in raw.get("projects", [])]
            if len({p.id for p in projects}) != len(projects):
                raise ProjectConfigError("Project IDs must be unique")
            for index, project in enumerate(projects):
                self._check_overlap(project, projects[:index])
            return projects
        except (TypeError, ValueError) as exc:
            raise ProjectConfigError(f"Invalid project configuration: {exc}") from exc

    def _write(self, projects: list[ProjectConfig]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {"projects": [self._dump(project) for project in projects]}
        content = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        handle, temporary = tempfile.mkstemp(
            prefix="projects.", suffix=".yaml", dir=self.config_dir
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _dump(self, project: ProjectConfig) -> dict[str, object]:
        # Preserve inheritance: writing defaults would turn them into project overrides on reload.
        dumped = project.model_dump(mode="json", exclude_unset=True)
        root = Path(dumped["root"])
        dumped["root"] = str(root)
        return dumped

    def _validate_project(self, project: ProjectConfig) -> None:
        if not project.root.is_dir():
            raise ProjectConfigError(f"Project root does not exist: {project.root}")
        try:
            result = subprocess.run(
                ["git", "-C", str(project.root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectConfigError(f"Could not inspect project Git repository: {exc}") from exc
        if result.returncode != 0:
            raise ProjectConfigError("Project root must be a Git repository")
        if Path(result.stdout.strip()).resolve() != project.root:
            raise ProjectConfigError("Project root must match the Git repository root")

        prompt = self.resolve_review_prompt(project)
        if not prompt.is_file():
            raise ProjectConfigError(f"Review prompt does not exist: {prompt}")

    def resolve_review_prompt(self, project: ProjectConfig) -> Path:
        prompt = Path(project.review_prompt).expanduser()
        if prompt.is_absolute():
            raise ProjectConfigError("Review prompt must be relative to the Hub root")
        resolved = (self.hub_root / prompt).resolve()
        if not resolved.is_relative_to(self.hub_root):
            raise ProjectConfigError("Review prompt must stay inside the Hub root")
        return resolved
