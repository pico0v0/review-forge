"""Task creation, state transitions, and the first single-round review worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import AppSettings, ModelRoleSettings
from app.context import build_context
from app.errors import HubError
from app.git import create_worktree, git, head, require_clean
from app.models import ModelCall, ReviewRound, ReviewTask, TaskEvent, utc_now
from app.projects import ProjectConfig, ProjectConfigError, ProjectRegistry
from app.providers import ModelGateway
from app.schemas import ReviewRetryRequest, ReviewSubmitRequest
from app.validation import run_check

logger = logging.getLogger(__name__)

ACTIVE_STATES = {
    "CREATED",
    "BUILDING_CONTEXT",
    "REVIEWING",
    "FIXING",
    "VALIDATING",
    "FINALIZING",
}
TRANSITIONS = {
    "CREATED": {"BUILDING_CONTEXT", "NEEDS_ATTENTION", "CANCELLED"},
    "NEEDS_ATTENTION": {"BUILDING_CONTEXT", "CANCELLED"},
    "BUILDING_CONTEXT": {"REVIEWING", "NEEDS_ATTENTION", "CANCELLED"},
    "REVIEWING": {"VALIDATING", "NEEDS_ATTENTION", "CANCELLED"},
    "FIXING": {"REVIEWING", "VALIDATING", "NEEDS_ATTENTION", "CANCELLED"},
    "VALIDATING": {
        "FIXING",
        "REVIEWING",
        "FINALIZING",
        "COMPLETED",
        "NEEDS_ATTENTION",
        "CANCELLED",
    },
    "FINALIZING": {"COMPLETED", "NEEDS_ATTENTION", "CANCELLED"},
}
TASK_ID_PATTERN = re.compile(r"^REV-\d{8}-\d{3,}$")


def task_response(task: ReviewTask) -> dict[str, object]:
    def iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        ).isoformat()

    return {
        "id": task.id,
        "project_id": task.project_id,
        "status": task.status,
        "source_agent": task.source_agent,
        "review_type": task.review_type,
        "current_round": task.current_round,
        "max_rounds": task.max_rounds,
        "stop_reason": task.stop_reason,
        "base_commit": task.base_commit,
        "review_base": task.review_base,
        "task_branch": task.task_branch,
        "reviewer": (
            {"provider": task.reviewer_provider, "model": task.reviewer_model}
            if task.reviewer_provider and task.reviewer_model
            else None
        ),
        "fixer": (
            {"provider": task.fixer_provider, "model": task.fixer_model}
            if task.fixer_provider and task.fixer_model
            else None
        ),
        "verdict": task.verdict,
        "summary": task.summary,
        "tokens_used": task.tokens_used,
        "cost_usd": task.cost_usd,
        "error_message": task.error_message,
        "created_at": iso(task.created_at),
        "updated_at": iso(task.updated_at),
        "completed_at": iso(task.completed_at),
    }


class TaskService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: AppSettings,
        projects: ProjectRegistry,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.projects = projects

    def create(self, request: ReviewSubmitRequest) -> ReviewTask:
        if not self.settings.security.code_egress_acknowledged:
            raise HubError(
                "code_egress_unacknowledged",
                "Set security.code_egress_acknowledged=true before submitting code to a model",
                403,
            )
        project = self.projects.get(request.project_id)
        if project is None or not project.enabled:
            raise HubError("project_not_found", "Project is missing or disabled", 404)
        if not project.egress_allowed:
            raise HubError(
                "egress_disabled", "This project is not allowed to send code to models", 403
            )
        data_dir = self.settings.paths.data_dir.resolve()
        if data_dir.is_relative_to(project.root) or project.root.is_relative_to(data_dir):
            raise HubError("path_overlap", "Hub data must be separate from reviewed projects", 422)
        scope = request.scope.model_dump(mode="json", exclude_none=True)
        if scope["mode"] == "module" and not any(
            module.id == request.scope.module_id for module in project.modules
        ):
            raise HubError("module_not_found", "Requested module was not found", 404)
        for path in request.scope.paths:
            self._validate_scope_path(path)

        reviewer = request.reviewer or project.reviewer or self.settings.models.roles.reviewer
        fixer = request.fixer or project.fixer or self.settings.models.roles.fixer
        if reviewer is None:
            raise HubError("reviewer_not_configured", "Configure a reviewer model first", 422)
        self._validate_role(reviewer)
        if fixer is not None:
            self._validate_role(fixer)

        effective_limits = self.settings.limits.model_dump(mode="json")
        effective_limits.update(project.limits.model_dump(mode="json", exclude_unset=True))
        if request.limits is not None:
            effective_limits.update(request.limits.model_dump(mode="json", exclude_unset=True))
        global_limits = self.settings.limits.model_dump(mode="json")
        effective_limits = {
            name: min(value, global_limits[name]) if name in global_limits else value
            for name, value in effective_limits.items()
        }
        try:
            if self.settings.automation.require_clean_worktree:
                require_clean(project.root)
            base_commit = head(project.root)
            review_base = base_commit
            if request.scope.mode == "diff":
                ref = request.scope.base_ref or project.default_base_ref or "HEAD"
                review_base = (
                    git(project.root, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
                )
                git(project.root, "merge-base", "--is-ancestor", review_base, base_commit)
        except HubError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise HubError("git_failed", "Could not inspect project Git state", 400) from exc

        with self.session_factory() as session:
            active = session.scalar(
                select(ReviewTask.id).where(
                    ReviewTask.project_id == project.id,
                    ReviewTask.status.in_(ACTIVE_STATES),
                )
            )
            if active:
                raise HubError(
                    "project_review_active", "A review is already running for this project", 409
                )
            day = datetime.now(UTC).strftime("%Y%m%d")
            prefix = f"REV-{day}-"
            count = (
                session.scalar(
                    select(func.count())
                    .select_from(ReviewTask)
                    .where(ReviewTask.id.like(f"{prefix}%"))
                )
                or 0
            )
            task_id = f"{prefix}{count + 1:03d}"
            if not TASK_ID_PATTERN.fullmatch(task_id):
                raise HubError("task_id_invalid", "Could not create a valid task identifier", 500)
            task_path = self.settings.paths.data_dir / "tasks" / task_id
            worktree_path = self.settings.paths.data_dir / "worktrees" / task_id
            task = ReviewTask(
                id=task_id,
                project_id=project.id,
                source_agent=request.source_agent,
                review_type=request.review_type,
                scope_json=json.dumps(scope),
                snapshot_json=json.dumps(
                    {
                        "project": project.model_dump(mode="json", exclude_unset=True),
                        "limits": effective_limits,
                        "reviewer": reviewer.model_dump(mode="json"),
                        "fixer": fixer.model_dump(mode="json") if fixer else None,
                        "dry_run": request.dry_run,
                        "finalize_mode": "dry_run" if request.dry_run else project.finalize_mode,
                    }
                ),
                status="CREATED",
                current_round=0,
                max_rounds=effective_limits["max_rounds"],
                base_commit=base_commit,
                review_base=review_base,
                task_branch=f"ai-review/{task_id}",
                worktree_path=str(worktree_path),
                task_path=str(task_path),
                reviewer_provider=reviewer.provider,
                reviewer_model=reviewer.model,
                fixer_provider=fixer.provider if fixer else None,
                fixer_model=fixer.model if fixer else None,
            )
            task.events.append(
                TaskEvent(
                    event_type="created",
                    to_status="CREATED",
                    message="Review task accepted",
                    actor=request.source_agent,
                )
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    def _validate_role(self, role: ModelRoleSettings) -> None:
        provider = next(
            (item for item in self.settings.models.providers if item.id == role.provider), None
        )
        if provider is None or role.model not in {model.id for model in provider.models}:
            raise HubError("model_not_configured", "Selected model is not configured", 422)

    def retry(self, task_id: str, request: ReviewRetryRequest) -> ReviewTask:
        with self.session_factory() as session:
            task = session.get(ReviewTask, task_id)
            if task is None:
                raise HubError("task_not_found", "Review task was not found", 404)
            if task.status != "NEEDS_ATTENTION":
                raise HubError("invalid_state", "Only stopped reviews can be retried", 409)
            snapshot = json.loads(task.snapshot_json)
            if request.reviewer is not None:
                self._validate_role(request.reviewer)
                snapshot["reviewer"] = request.reviewer.model_dump(mode="json")
                task.reviewer_provider = request.reviewer.provider
                task.reviewer_model = request.reviewer.model
            if request.fixer is not None:
                self._validate_role(request.fixer)
                snapshot["fixer"] = request.fixer.model_dump(mode="json")
                task.fixer_provider = request.fixer.provider
                task.fixer_model = request.fixer.model
            if request.limits is not None:
                values = request.limits.model_dump(mode="json", exclude_unset=True)
                global_limits = self.settings.limits.model_dump(mode="json")
                snapshot["limits"].update(
                    {
                        key: min(value, global_limits[key])
                        for key, value in values.items()
                        if key in global_limits
                    }
                )
                task.max_rounds = snapshot["limits"]["max_rounds"]
            if request.dry_run is not None:
                snapshot["dry_run"] = request.dry_run
                snapshot["finalize_mode"] = (
                    "dry_run"
                    if request.dry_run
                    else snapshot.get("project", {}).get("finalize_mode", "apply_to_project")
                )
            task.snapshot_json = json.dumps(snapshot)
            task.stop_reason = None
            task.error_message = None
            task.completed_at = None
            task.status = "BUILDING_CONTEXT"
            task.events.append(
                TaskEvent(
                    event_type="retry_requested",
                    from_status="NEEDS_ATTENTION",
                    to_status="BUILDING_CONTEXT",
                    message="Review retry accepted",
                )
            )
            session.commit()
            session.refresh(task)
            return task

    @staticmethod
    def _validate_scope_path(path: str) -> None:
        from app.paths import relative_path

        relative_path(path)

    def transition(
        self,
        task_id: str,
        new_status: str,
        *,
        stop_reason: str | None = None,
        message: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self.session_factory() as session:
            task = session.get(ReviewTask, task_id)
            if task is None:
                return
            if new_status not in TRANSITIONS.get(task.status, set()):
                raise HubError(
                    "invalid_state", f"Cannot move task from {task.status} to {new_status}", 409
                )
            old_status = task.status
            task.status = new_status
            task.stop_reason = stop_reason
            task.error_message = error_message
            if new_status == "BUILDING_CONTEXT":
                task.started_at = utc_now()
            if new_status in {"COMPLETED", "NEEDS_ATTENTION", "CANCELLED"}:
                task.completed_at = utc_now()
            task.events.append(
                TaskEvent(
                    event_type="status_changed",
                    from_status=old_status,
                    to_status=new_status,
                    message=message or stop_reason,
                )
            )
            session.commit()


class ReviewWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: AppSettings,
        projects: ProjectRegistry,
        gateway: ModelGateway,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.projects = projects
        self.gateway = gateway
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.semaphore = asyncio.Semaphore(settings.automation.max_parallel_tasks)
        self.user_cancelled: set[str] = set()

    def schedule(self, task_id: str) -> None:
        running = asyncio.create_task(self.run(task_id), name=f"reviewforge-{task_id}")
        self.tasks[task_id] = running
        running.add_done_callback(lambda _task: self.tasks.pop(task_id, None))

    async def shutdown(self) -> None:
        running = list(self.tasks.values())
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    async def cancel(self, task_id: str) -> None:
        with self.session_factory() as session:
            task = session.get(ReviewTask, task_id)
            if task is None:
                raise HubError("task_not_found", "Review task was not found", 404)
            if task.status == "CANCELLED":
                return
            if task.status not in ACTIVE_STATES:
                raise HubError("invalid_state", "Only active reviews can be cancelled", 409)
        self.user_cancelled.add(task_id)
        try:
            running = self.tasks.get(task_id)
            if running is not None:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            with self.session_factory() as session:
                task = session.get(ReviewTask, task_id)
                if task.status == "CANCELLED":
                    return
            self._transition(
                task_id, "CANCELLED", stop_reason="cancelled", message="Cancelled by user"
            )
        finally:
            self.user_cancelled.discard(task_id)

    def recover(self) -> None:
        with self.session_factory() as session:
            ids = list(
                session.scalars(select(ReviewTask.id).where(ReviewTask.status.in_(ACTIVE_STATES)))
            )
        for task_id in ids:
            self._stop(
                task_id, "interrupted", "Hub restarted; review was not automatically repeated"
            )

    async def run(self, task_id: str) -> None:
        try:
            async with self.semaphore:
                task, project = self._load(task_id)
                limits = json.loads(task.snapshot_json)["limits"]
                async with asyncio.timeout(limits["max_wall_time_min"] * 60):
                    await self._review(task_id)
        except TimeoutError:
            self._stop(task_id, "timeout", "Review exceeded its total time budget")
        except asyncio.CancelledError:
            if task_id in self.user_cancelled:
                self._transition(task_id, "CANCELLED", stop_reason="cancelled")
            else:
                self._stop(task_id, "interrupted", "Review worker was interrupted")
            raise
        except HubError as exc:
            logger.info("Task %s stopped: %s", task_id, exc.code)
            self._stop(task_id, exc.code, exc.message)
        except Exception:
            logger.exception("Unexpected review task failure for %s", task_id)
            self._stop(task_id, "internal_error", "Review failed; inspect the local Hub log")
        finally:
            self.user_cancelled.discard(task_id)

    async def _review(self, task_id: str) -> None:
        self._prepare(task_id)
        task, _project = self._load(task_id)
        round_no = max(1, task.current_round + 1)
        seen_reviews: dict[str, int] = {}
        while True:
            task, project = self._load(task_id)
            snapshot = json.loads(task.snapshot_json)
            response, checks, within_budget = await self._review_round(task, project, round_no)
            signature = json.dumps(
                {
                    "verdict": response.verdict,
                    "findings": [
                        (finding.id, finding.severity, finding.file, finding.line)
                        for finding in response.findings
                    ],
                },
                sort_keys=True,
            )
            seen_reviews[signature] = seen_reviews.get(signature, 0) + 1
            if not within_budget:
                self._save_result(task_id, response, checks)
                break
            if (
                seen_reviews[signature] >= snapshot["limits"].get("no_progress_rounds", 2)
                and round_no > 1
            ):
                self._save_result(task_id, response, checks, stop_reason_override="no_progress")
                break
            blocking = any(
                f.severity in project.gate.blocking_severities for f in response.findings
            )
            tests_failed = bool(checks["tests"] and not checks["tests"]["passed"])
            needs_fix = (
                blocking
                or response.verdict in {"blocked", "needs_info", "changes_required"}
                or tests_failed
            )
            gate_passed = self._candidate_passes(project, response, checks)
            if gate_passed:
                mode = snapshot.get("finalize_mode", "dry_run")
                if mode == "apply_to_project":
                    self._save_result(
                        task_id,
                        response,
                        checks,
                        final_status_override="FINALIZING",
                    )
                    await self._finalize(task_id)
                else:
                    self._save_result(task_id, response, checks)
                break
            fixer_data = snapshot.get("fixer")
            limits = snapshot["limits"]
            if not needs_fix or not fixer_data or round_no >= task.max_rounds:
                self._save_result(task_id, response, checks)
                break
            fixed = False
            for attempt in range(1, limits["max_fix_attempts_per_round"] + 1):
                good_commit = head(Path(task.worktree_path))
                try:
                    fixer_response = await self._fix(
                        task, project, response, checks, round_no, attempt
                    )
                    if not self._apply_changes(task, project, fixer_response):
                        continue
                    env_names = [
                        p.api_key_env for p in self.settings.models.providers if p.api_key_env
                    ]
                    if not checks["tests_configured"]:
                        from app.git import reset_to

                        reset_to(Path(task.worktree_path), good_commit)
                        break
                    test_result = await run_check(
                        self._test_command(project, task),
                        Path(task.worktree_path),
                        900,
                        task.id,
                        env_names,
                    )
                    if test_result["passed"]:
                        from app.git import commit_files

                        status = git(
                            Path(task.worktree_path),
                            "status",
                            "--porcelain=v1",
                            "--untracked-files=all",
                        )
                        if status:
                            commit_files(
                                Path(task.worktree_path),
                                ["."],
                                f"{task_id} round {round_no}: "
                                f"fix {len(response.findings)} findings",
                            )
                        fixed = True
                        break
                    from app.git import reset_to

                    reset_to(Path(task.worktree_path), good_commit)
                    checks["tests"] = test_result
                except HubError as exc:
                    logger.info("Fix attempt %s for %s rejected: %s", attempt, task_id, exc.code)
            if not fixed:
                self._save_result(
                    task_id, response, checks, stop_reason_override="fix_attempts_exhausted"
                )
                break
            round_no += 1
            if round_no > task.max_rounds:
                self._save_result(task_id, response, checks)
                break
        task, project = self._load(task_id)
        mode = json.loads(task.snapshot_json).get("finalize_mode", "dry_run")
        if task.status == "COMPLETED" and mode != "branch":
            try:
                git(project.root, "worktree", "remove", task.worktree_path)
            except HubError:
                logger.warning("Completed task %s retains its worktree for cleanup", task_id)

    async def _review_round(self, task, project, round_no):
        snapshot = json.loads(task.snapshot_json)
        role = ModelRoleSettings.model_validate(snapshot["reviewer"])
        prompt = self._prompt(project)
        context = build_context(
            project,
            Path(task.worktree_path),
            json.loads(task.scope_json),
            Path(task.task_path),
            task.id,
            round_no,
            task.base_commit,
            max_bytes=min(self.settings.context.max_total_bytes, 400_000),
            max_file_bytes=self.settings.context.max_file_bytes,
            review_type=task.review_type,
        )
        self._transition(task.id, "REVIEWING", message=f"Reviewer started round {round_no}")
        response, call, within_budget = await self._call_model(
            task, role, prompt, context.text, round_no, "reviewer"
        )
        self._record_review(task.id, response, call, round_no, "reviewer")
        command = self._test_command(project, task)
        acceptance = project.acceptance_command
        if json.loads(task.scope_json)["mode"] == "module":
            scope = json.loads(task.scope_json)
            module = next(m for m in project.modules if m.id == scope["module_id"])
            acceptance = module.acceptance_command or acceptance
        checks = {
            "tests": None,
            "acceptance": None,
            "tests_configured": bool(command),
            "acceptance_configured": bool(acceptance),
            "display_verified": False,
            "worktree_clean": True,
            "usage_estimated": not bool(call.get("usage_reported")),
            "omitted": context.omitted,
        }
        self._transition(task.id, "VALIDATING", message="Validating review result")
        if (
            within_budget
            and not any(f.severity in project.gate.blocking_severities for f in response.findings)
            and response.verdict not in {"blocked", "needs_info", "changes_required"}
        ):
            env_names = [p.api_key_env for p in self.settings.models.providers if p.api_key_env]
            if command:
                checks["tests"] = await run_check(
                    command, Path(task.worktree_path), 900, task.id, env_names
                )
            if acceptance and (not command or checks["tests"]["passed"]):
                checks["acceptance"] = await run_check(
                    acceptance, Path(task.worktree_path), 1200, task.id, env_names
                )
                checks["display_verified"] = checks["acceptance"]["passed"]
            try:
                require_clean(Path(task.worktree_path))
            except HubError:
                checks["worktree_clean"] = False
        return response, checks, within_budget

    def _candidate_passes(self, project, response, checks):
        tests_passed = checks["tests"] is not None and checks["tests"]["passed"]
        acceptance_passed = not checks["acceptance_configured"] or (
            checks["acceptance"] is not None and checks["acceptance"]["passed"]
        )
        blocking = any(f.severity in project.gate.blocking_severities for f in response.findings)
        return (
            not blocking
            and response.verdict not in {"blocked", "needs_info", "changes_required"}
            and (not project.gate.require_verdict or response.verdict == "approved")
            and (not project.gate.require_tests or tests_passed)
            and (not project.gate.require_acceptance or acceptance_passed)
            and checks["worktree_clean"]
        )

    async def _finalize(self, task_id: str) -> None:
        from app.paths import resolve_inside

        task, project = self._load(task_id)
        root = Path(project.root)
        worktree = Path(task.worktree_path)
        task_dir = Path(task.task_path)
        apply_record = None
        try:
            if head(root) != task.base_commit:
                raise HubError("target_changed", "Project HEAD changed before finalization", 409)
            require_clean(root)
            require_clean(worktree)
            changed = (
                git(
                    worktree,
                    "diff",
                    "--name-only",
                    "-z",
                    "--no-renames",
                    task.base_commit,
                    "HEAD",
                    "--",
                )
                .decode("utf-8")
                .split("\0")
            )
            paths = [path for path in changed if path]
            for path in paths:
                resolve_inside(root, path)
                if project.target_paths and not any(
                    path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
                    for prefix in project.target_paths
                ):
                    raise HubError(
                        "apply_failed", f"Changed path is outside target_paths: {path}", 409
                    )
            if not paths:
                self._transition(task_id, "COMPLETED", stop_reason="gate_passed")
                return
            backup_dir = task_dir / "backup" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            backup_dir.mkdir(parents=True, exist_ok=False)
            records = []
            for path in paths:
                target = Path(root) / path
                if target.exists() and not target.is_file():
                    raise HubError("apply_failed", f"Target is not a regular file: {path}", 409)
                before = target.read_bytes() if target.exists() else None
                snapshot_path = backup_dir / path
                if before is not None:
                    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                    snapshot_path.write_bytes(before)
                records.append(
                    {
                        "path": path,
                        "before": hashlib.sha256(before).hexdigest()
                        if before is not None
                        else None,
                        "after": None,
                        "existed": before is not None,
                    }
                )
            patch = git(
                worktree, "diff", "--binary", "--no-ext-diff", task.base_commit, "HEAD", "--"
            )
            git(root, "apply", "--check", "--whitespace=nowarn", input_bytes=patch)
            git(root, "apply", "--whitespace=nowarn", input_bytes=patch)
            for record in records:
                target = root / record["path"]
                after = target.read_bytes() if target.exists() else None
                record["after"] = hashlib.sha256(after).hexdigest() if after is not None else None
            apply_record = {
                "status": "applied",
                "base_commit": task.base_commit,
                "before_head": task.base_commit,
                "after_head": head(root),
                "backup_dir": str(backup_dir),
                "files": records,
            }
            (task_dir / "apply.json").write_text(
                json.dumps(apply_record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._refresh_result_archive(task_dir)
            acceptance = project.acceptance_command
            if acceptance:
                env_names = [p.api_key_env for p in self.settings.models.providers if p.api_key_env]
                result = await run_check(acceptance, root, 1200, task_id, env_names)
                if not result["passed"]:
                    self._restore_apply(root, apply_record)
                    apply_record["status"] = "rolled_back"
                    apply_record["verify"] = result
                    (task_dir / "apply.json").write_text(
                        json.dumps(apply_record, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    self._refresh_result_archive(task_dir)
                    self._transition(task_id, "NEEDS_ATTENTION", stop_reason="verify_failed")
                    return
            if self.settings.automation.finalize.auto_commit:
                status = git(
                    root,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ).decode("utf-8", errors="replace")
                changed_paths = {line[3:] for line in status.splitlines()}
                if changed_paths != set(paths):
                    raise HubError(
                        "apply_failed",
                        "Project changed during acceptance; automatic commit was refused",
                        409,
                    )
                from app.git import commit_files

                commit_files(root, paths, f"ReviewForge: apply {task_id}")
                apply_record["after_head"] = head(root)
                apply_record["auto_committed"] = True
                (task_dir / "apply.json").write_text(
                    json.dumps(apply_record, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                self._refresh_result_archive(task_dir)
            self._transition(task_id, "COMPLETED", stop_reason="gate_passed")
        except HubError:
            if apply_record is not None and apply_record.get("status") == "applied":
                if head(root) == apply_record["before_head"]:
                    self._restore_apply(root, apply_record)
                    apply_record["status"] = "rolled_back"
                    (task_dir / "apply.json").write_text(
                        json.dumps(apply_record, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    self._refresh_result_archive(task_dir)
            raise
        except (OSError, UnicodeError) as exc:
            raise HubError(
                "apply_failed", "Could not finalize changes in the project", 409
            ) from exc

    @staticmethod
    def _restore_apply(root: Path, record: dict) -> None:
        for item in record["files"]:
            target = root / item["path"]
            snapshot = Path(record["backup_dir"]) / item["path"]
            if item["existed"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(snapshot.read_bytes())
            elif target.exists():
                target.unlink()

    @staticmethod
    def _refresh_result_archive(task_dir: Path) -> None:
        result_dir = task_dir / "result"
        archive_path = result_dir / "result.zip"
        if not result_dir.is_dir():
            return
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in result_dir.iterdir():
                if file.is_file() and file != archive_path:
                    archive.write(file, file.name)
            apply_path = task_dir / "apply.json"
            if apply_path.is_file():
                archive.write(apply_path, "apply.json")

    def rollback(self, task_id: str) -> ReviewTask:
        task, project = self._load(task_id)
        apply_path = Path(task.task_path) / "apply.json"
        if not apply_path.is_file():
            raise HubError("rollback_unavailable", "No successful finalization is available", 409)
        record = json.loads(apply_path.read_text(encoding="utf-8"))
        if record.get("status") != "applied":
            raise HubError(
                "rollback_unavailable", "The latest finalization is already rolled back", 409
            )
        root = Path(project.root)
        if record.get("auto_committed") or head(root) != record.get("after_head"):
            raise HubError(
                "rollback_conflict",
                "Project history contains a commit after finalization; "
                "automatic rollback would conflict with Git history",
                409,
            )
        for item in record["files"]:
            target = root / item["path"]
            current = target.read_bytes() if target.exists() else None
            current_hash = hashlib.sha256(current).hexdigest() if current is not None else None
            if current_hash != item["after"]:
                raise HubError(
                    "rollback_conflict",
                    f"Project file changed after finalization: {item['path']}",
                    409,
                )
        self._restore_apply(root, record)
        record["status"] = "rolled_back"
        apply_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        self._refresh_result_archive(Path(task.task_path))
        with self.session_factory() as session:
            stored = session.get(ReviewTask, task_id)
            if stored:
                stored.stop_reason = "rolled_back"
                session.commit()
                session.expunge(stored)
                return stored
        raise HubError("task_not_found", "Review task was not found", 404)

    @staticmethod
    def _test_command(project, task):
        command = project.test_command
        scope = json.loads(task.scope_json)
        if scope["mode"] == "module":
            module = next(m for m in project.modules if m.id == scope["module_id"])
            command = module.test_command or command
        return command

    async def _call_model(self, task, role, prompt, context, round_no, call_role):
        task, _project = self._load(task.id)
        provider = next(p for p in self.settings.models.providers if p.id == role.provider)
        model = next(m for m in provider.models if m.id == role.model)
        limits = json.loads(task.snapshot_json)["limits"]
        input_bound = len((context + prompt).encode("utf-8")) + 4096
        available = (
            min(model.context_window, limits["max_total_tokens"] - task.tokens_used) - input_bound
        )
        output_limit = min(model.max_output_tokens, 16_000, available)
        if output_limit <= 0:
            raise HubError("budget_tokens", "Input and reserved output exceed the token budget")
        response, call = await self.gateway.review(
            role,
            task_id=task.id,
            round_no=round_no,
            system_prompt=prompt,
            context=context,
            max_output_tokens=output_limit,
        )
        call["prompt_tokens"] = (
            input_bound if call.get("prompt_tokens") is None else int(call["prompt_tokens"])
        )
        call["completion_tokens"] = (
            len(response.model_dump_json().encode())
            if call.get("completion_tokens") is None
            else int(call["completion_tokens"])
        )
        call["role"] = call_role
        actual_cost = (
            call["prompt_tokens"] * model.input_usd_per_1m
            + call["completion_tokens"] * model.output_usd_per_1m
        ) / 1_000_000
        return (
            response,
            call,
            task.tokens_used + call["prompt_tokens"] + call["completion_tokens"]
            <= limits["max_total_tokens"]
            and task.cost_usd + actual_cost <= limits["max_cost_usd"],
        )

    async def _fix(self, task, project, review, checks, round_no, attempt):
        role = ModelRoleSettings.model_validate(json.loads(task.snapshot_json)["fixer"])
        prompt_path = self.projects.hub_root / "prompts" / "fixer.md"
        if not prompt_path.is_file():
            prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "fixer.md"
        try:
            prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HubError(
                "config_error", "Could not read the configured fixer prompt", 500
            ) from exc
        context = (
            json.dumps(
                {
                    "findings": [f.model_dump(mode="json") for f in review.findings],
                    "validation": checks,
                    "attempt": attempt,
                },
                ensure_ascii=False,
            )
            + "\n"
            + git(Path(task.worktree_path), "diff", "--no-ext-diff").decode(
                "utf-8", errors="replace"
            )
        )
        self._transition(
            task.id, "FIXING", message=f"Applying fix attempt {attempt} in round {round_no}"
        )
        response, call, within_budget = await self._call_model(
            task, role, prompt, context, round_no, "fixer"
        )
        self._record_review(task.id, response, call, round_no, "fixer", create_round=False)
        if not within_budget:
            raise HubError("budget_tokens", "Fixer call exceeded the remaining task budget")
        return response

    def _apply_changes(self, task, project, response):
        from app.context import in_scope
        from app.paths import allowed_source, resolve_inside

        root = Path(task.worktree_path)
        scope = json.loads(task.scope_json)
        patterns = scope.get("paths", [])
        if scope["mode"] == "module":
            patterns = next(m.paths for m in project.modules if m.id == scope["module_id"])
        if response.patch:
            if len(response.patch.encode()) > 2_000_000:
                raise HubError("patch_too_large", "Fix patch exceeds the size limit")
            import re

            paths = re.findall(r"^(?:\+\+\+|---) [ab]/(.+)$", response.patch, re.M)
            names = {path for path in paths if path != "/dev/null"}
            if (
                not names
                or len(names) > project.max_patch_files
                or any(not allowed_source(p) or not in_scope(p, patterns, project) for p in names)
            ):
                raise HubError(
                    "patch_scope_violation", "Patch contains paths outside the allowed source scope"
                )
            git(
                root, "apply", "--check", "--whitespace=nowarn", input_bytes=response.patch.encode()
            )
            git(root, "apply", "--whitespace=nowarn", input_bytes=response.patch.encode())
            return True
        if response.edits:
            if len(response.edits) > project.max_patch_files:
                raise HubError("patch_too_large", "Too many files in fixer response")
            for edit in response.edits:
                if (
                    not allowed_source(edit.path)
                    or not in_scope(edit.path, patterns, project)
                    or len(edit.content.encode()) > self.settings.context.max_file_bytes
                ):
                    raise HubError(
                        "patch_scope_violation", "File edit is outside allowed paths or size"
                    )
                target = resolve_inside(root, edit.path)
                if target.exists() and not target.is_file():
                    raise HubError("path_unsafe", "Edits can only target regular files")
            for edit in response.edits:
                target = resolve_inside(root, edit.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(edit.content, encoding="utf-8")
            return True
        return False

    def _prepare(self, task_id: str) -> None:
        task, project = self._load(task_id)
        if task.status == "CREATED":
            self._transition(
                task_id, "BUILDING_CONTEXT", message="Checking project and creating worktree"
            )
        elif task.status != "BUILDING_CONTEXT":
            raise HubError("invalid_state", "Task is not ready to run", 409)
        if self.settings.automation.require_clean_worktree:
            require_clean(project.root)
        if head(project.root) != task.base_commit:
            raise HubError("target_changed", "Project HEAD changed after task submission", 409)
        task_path = Path(task.task_path)
        worktree_path = Path(task.worktree_path)
        if not task_path.exists():
            task_path.mkdir(parents=True, exist_ok=False)
        if not worktree_path.exists():
            create_worktree(project.root, worktree_path, task.task_branch, task.base_commit)

    def _load(self, task_id: str) -> tuple[ReviewTask, ProjectConfig]:
        with self.session_factory() as session:
            task = session.get(ReviewTask, task_id)
            if task is None:
                raise HubError("task_not_found", "Review task was not found", 404)
            snapshot = json.loads(task.snapshot_json)
            project = ProjectConfig.model_validate(snapshot["project"])
            session.expunge(task)
            return task, project

    def _prompt(self, project: ProjectConfig) -> str:
        try:
            path = self.projects.resolve_review_prompt(project)
            prompt = path.read_text(encoding="utf-8")
        except (OSError, ProjectConfigError) as exc:
            raise HubError(
                "config_error", "Could not read the configured reviewer prompt", 500
            ) from exc
        from app.protocol import ReviewResponse

        return (
            prompt + "\n\nRequired JSON schema:\n" + json.dumps(ReviewResponse.model_json_schema())
        )

    def _transition(
        self,
        task_id: str,
        status: str,
        *,
        message: str | None = None,
        stop_reason: str | None = None,
    ) -> None:
        service = TaskService(self.session_factory, self.settings, self.projects)
        service.transition(task_id, status, message=message, stop_reason=stop_reason)

    def _stop(self, task_id: str, reason: str, message: str) -> None:
        with self.session_factory() as session:
            task = session.get(ReviewTask, task_id)
            if task is None or task.status not in ACTIVE_STATES:
                return
        try:
            self._transition(
                task_id,
                "NEEDS_ATTENTION",
                stop_reason=reason[:64],
                message=message,
            )
            with self.session_factory() as session:
                task = session.get(ReviewTask, task_id)
                if task:
                    task.error_message = message[:1000]
                    session.commit()
        except HubError:
            logger.exception("Failed to record stop reason for %s", task_id)

    def _save_result(
        self,
        task_id: str,
        response,
        checks: dict,
        stop_reason_override: str | None = None,
        final_status_override: str | None = None,
    ) -> str:
        task, project = self._load(task_id)
        task_dir = Path(task.task_path)
        result_dir = task_dir / "result"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "validation.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
        response_path = result_dir / "review.json"
        response_data = response.model_dump(mode="json")
        response_path.write_text(
            json.dumps(response_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        findings_path = result_dir / "findings.json"
        findings_path.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in response.findings],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        report_lines = [
            f"# Review {task_id}",
            "",
            f"Verdict: `{response.verdict}`",
            "",
            response.summary,
            "",
        ]
        for finding in response.findings:
            report_lines.extend(
                [
                    f"## [{finding.severity}] {finding.title}",
                    "",
                    f"`{finding.file}`" + (f":{finding.line}" if finding.line else ""),
                    "",
                    finding.description,
                    "",
                    f"Impact: {finding.impact}" if finding.impact else "",
                    f"Recommendation: {finding.recommendation}" if finding.recommendation else "",
                    "",
                ]
            )
        (result_dir / "report.md").write_text("\n".join(report_lines), encoding="utf-8")
        if response.patch:
            patch_path = result_dir / "changes.patch"
            patch_path.write_text(response.patch, encoding="utf-8")
        elif response.edits:
            (result_dir / "edits.json").write_text(
                json.dumps(
                    [item.model_dump(mode="json") for item in response.edits],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        if not response.patch:
            try:
                cumulative = git(
                    Path(task.worktree_path),
                    "diff",
                    "--no-ext-diff",
                    task.base_commit,
                    "HEAD",
                )
            except HubError:
                cumulative = b""
            if cumulative:
                (result_dir / "changes.patch").write_bytes(cumulative)

        blocking = any(f.severity in project.gate.blocking_severities for f in response.findings)
        tests_passed = checks["tests"] is not None and checks["tests"]["passed"]
        acceptance_passed = not checks["acceptance_configured"] or (
            checks["acceptance"] is not None and checks["acceptance"]["passed"]
        )
        limits = json.loads(task.snapshot_json)["limits"]
        budget_reason = (
            "budget_tokens"
            if task.tokens_used > limits["max_total_tokens"]
            else "budget_cost"
            if task.cost_usd > limits["max_cost_usd"]
            else None
        )
        gate_passed = (
            not blocking
            and response.verdict not in {"blocked", "needs_info"}
            and (not project.gate.require_verdict or response.verdict == "approved")
            and (not project.gate.require_tests or tests_passed)
            and (not project.gate.require_acceptance or acceptance_passed)
            and checks["worktree_clean"]
            and budget_reason is None
        )
        final_status = final_status_override or (
            "NEEDS_ATTENTION" if not gate_passed else "COMPLETED"
        )
        stop_reason = stop_reason_override or (
            budget_reason
            if budget_reason
            else "validation_modified_files"
            if not checks["worktree_clean"]
            else "blocking_findings"
            if blocking
            else response.verdict
            if response.verdict in {"blocked", "needs_info"}
            else "gate_passed"
            if gate_passed
            else "validation_required"
        )
        if (
            not gate_passed
            and budget_reason is None
            and checks["worktree_clean"]
            and not blocking
            and response.verdict not in {"blocked", "needs_info"}
        ):
            if project.gate.require_tests and not tests_passed:
                stop_reason = "tests_failed" if checks["tests"] else "test_command_missing"
            elif project.gate.require_acceptance and not acceptance_passed:
                stop_reason = "acceptance_failed"
        report_lines.extend(
            [
                "## Validation",
                "",
                f"Stop reason: `{stop_reason}`",
                "",
                f"Tests configured: {checks['tests_configured']}; passed: {tests_passed}",
                f"Acceptance configured: {checks['acceptance_configured']}",
                "Display verification: "
                + ("passed" if checks["display_verified"] else "not verified"),
                f"Usage estimated: {checks['usage_estimated']}",
                "",
                "Changes were validated in the isolated task worktree; "
                "applying tasks may additionally record a project backup.",
            ]
        )
        (result_dir / "report.md").write_text("\n".join(report_lines), encoding="utf-8")
        with zipfile.ZipFile(result_dir / "result.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for file in result_dir.iterdir():
                if file.is_file() and file.suffix != ".zip":
                    archive.write(file, file.name)
        self._transition(task_id, final_status, stop_reason=stop_reason)
        return final_status

    def _record_review(
        self,
        task_id: str,
        response,
        call: dict[str, object],
        round_no: int = 1,
        role: str = "reviewer",
        create_round: bool = True,
    ) -> None:
        task, _project = self._load(task_id)
        response_path = Path(task.task_path) / "rounds" / str(round_no) / f"{role}-response.json"
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(response.model_dump_json(indent=2), encoding="utf-8")
        patch_path = None
        if response.patch:
            patch_path = response_path.parent / "changes.patch"
            patch_path.write_text(response.patch, encoding="utf-8")
        prompt_tokens = int(call["prompt_tokens"])
        completion_tokens = int(call["completion_tokens"])
        provider = next(p for p in self.settings.models.providers if p.id == call["provider"])
        model = next(m for m in provider.models if m.id == call["model"])
        cost = (
            prompt_tokens * model.input_usd_per_1m + completion_tokens * model.output_usd_per_1m
        ) / 1_000_000
        with self.session_factory() as session:
            stored = session.get(ReviewTask, task_id)
            if stored is None or stored.status not in {"REVIEWING", "FIXING"}:
                raise HubError("invalid_state", "Task is no longer reviewing", 409)
            stored.current_round = max(stored.current_round, round_no)
            if role == "reviewer":
                stored.verdict = response.verdict
                stored.summary = response.summary
            stored.tokens_used += prompt_tokens + completion_tokens
            stored.cost_usd += cost
            if create_round:
                stored.rounds.append(
                    ReviewRound(
                        round_no=round_no,
                        verdict=response.verdict,
                        summary=response.summary,
                        findings_json=json.dumps(
                            [item.model_dump(mode="json") for item in response.findings],
                            ensure_ascii=False,
                        ),
                        request_path=str(
                            Path(task.task_path)
                            / "rounds"
                            / str(round_no)
                            / "context"
                            / "request.zip"
                        ),
                        response_path=str(response_path),
                        patch_path=str(patch_path) if patch_path else None,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                )
            stored.model_calls.append(
                ModelCall(
                    round_no=round_no,
                    role=role,
                    provider=str(call["provider"]),
                    model=str(call["model"]),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost,
                    elapsed_ms=int(call["elapsed_ms"]),
                    response_path=str(response_path),
                )
            )
            session.commit()
