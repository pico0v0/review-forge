"""Task creation, state transitions, and the first single-round review worker."""

from __future__ import annotations

import asyncio
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
from app.schemas import ReviewSubmitRequest
from app.validation import run_check

logger = logging.getLogger(__name__)

ACTIVE_STATES = {"CREATED", "BUILDING_CONTEXT", "REVIEWING", "VALIDATING"}
TRANSITIONS = {
    "CREATED": {"BUILDING_CONTEXT", "NEEDS_ATTENTION", "CANCELLED"},
    "BUILDING_CONTEXT": {"REVIEWING", "NEEDS_ATTENTION", "CANCELLED"},
    "REVIEWING": {"VALIDATING", "NEEDS_ATTENTION", "CANCELLED"},
    "VALIDATING": {"COMPLETED", "NEEDS_ATTENTION", "CANCELLED"},
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
        if not request.dry_run:
            raise HubError(
                "feature_not_available",
                "This milestone supports read-only dry_run reviews; "
                "automatic repair is forthcoming",
                422,
            )
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
                        "dry_run": True,
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
        task, project = self._load(task_id)
        snapshot = json.loads(task.snapshot_json)
        role = ModelRoleSettings.model_validate(snapshot["reviewer"])
        prompt = self._prompt(project)
        context = build_context(
            project,
            Path(task.worktree_path),
            json.loads(task.scope_json),
            Path(task.task_path),
            task_id,
            1,
            task.review_base,
            max_bytes=min(self.settings.context.max_total_bytes, 400_000),
            max_file_bytes=self.settings.context.max_file_bytes,
            review_type=task.review_type,
        )
        provider = next(p for p in self.settings.models.providers if p.id == role.provider)
        model = next(m for m in provider.models if m.id == role.model)
        # Bytes provide a conservative ceiling for common byte-level tokenizers.
        input_bound = len((context.text + prompt).encode("utf-8")) + 4096
        limits = snapshot["limits"]
        available = min(model.context_window, limits["max_total_tokens"]) - input_bound
        output_limit = min(model.max_output_tokens, 16_000, available)
        if output_limit <= 0:
            raise HubError("budget_tokens", "Input and reserved output exceed the token budget")
        reserve_cost = (
            input_bound * model.input_usd_per_1m + output_limit * model.output_usd_per_1m
        ) / 1_000_000
        if reserve_cost > limits["max_cost_usd"]:
            raise HubError("budget_cost", "Estimated call cost exceeds the remaining budget")
        self._transition(task_id, "REVIEWING", message="Review context is ready")
        response, call = await self.gateway.review(
            role,
            task_id=task_id,
            round_no=1,
            system_prompt=prompt,
            context=f"Review type: {task.review_type}\n{context.text}",
            max_output_tokens=output_limit,
        )
        prompt_tokens = call.get("prompt_tokens")
        completion_tokens = call.get("completion_tokens")
        call["prompt_tokens"] = input_bound if prompt_tokens is None else int(prompt_tokens)
        call["completion_tokens"] = (
            len(response.model_dump_json().encode("utf-8"))
            if completion_tokens is None
            else int(completion_tokens)
        )
        # Persist paid usage before awaiting checks, which can be interrupted independently.
        self._record_review(task_id, response, call)
        actual_cost = (
            call["prompt_tokens"] * model.input_usd_per_1m
            + call["completion_tokens"] * model.output_usd_per_1m
        ) / 1_000_000
        within_budget = (
            call["prompt_tokens"] + call["completion_tokens"] <= limits["max_total_tokens"]
            and actual_cost <= limits["max_cost_usd"]
        )
        command = project.test_command
        acceptance = project.acceptance_command
        if json.loads(task.scope_json)["mode"] == "module":
            scope = json.loads(task.scope_json)
            module = next(m for m in project.modules if m.id == scope["module_id"])
            command = module.test_command or command
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
        blocking = any(f.severity in project.gate.blocking_severities for f in response.findings)
        self._transition(task_id, "VALIDATING", message="Validating review result")
        if within_budget and not blocking and response.verdict not in {"blocked", "needs_info"}:
            env_names = [p.api_key_env for p in self.settings.models.providers if p.api_key_env]
            if command:
                checks["tests"] = await run_check(
                    command, Path(task.worktree_path), 900, task_id, env_names
                )
            if acceptance and (not command or checks["tests"]["passed"]):
                checks["acceptance"] = await run_check(
                    acceptance, Path(task.worktree_path), 1200, task_id, env_names
                )
                checks["display_verified"] = checks["acceptance"]["passed"]
            try:
                require_clean(Path(task.worktree_path))
            except HubError:
                checks["worktree_clean"] = False
        final_status = self._save_result(task_id, response, checks)
        if final_status == "COMPLETED":
            try:
                git(project.root, "worktree", "remove", task.worktree_path)
            except HubError:
                logger.warning("Completed task %s retains its worktree for cleanup", task_id)

    def _prepare(self, task_id: str) -> None:
        task, project = self._load(task_id)
        self._transition(
            task_id, "BUILDING_CONTEXT", message="Checking project and creating worktree"
        )
        if self.settings.automation.require_clean_worktree:
            require_clean(project.root)
        if head(project.root) != task.base_commit:
            raise HubError("target_changed", "Project HEAD changed after task submission", 409)
        Path(task.task_path).mkdir(parents=True, exist_ok=False)
        create_worktree(project.root, Path(task.worktree_path), task.task_branch, task.base_commit)

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

    def _save_result(self, task_id: str, response, checks: dict) -> str:
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
        final_status = "NEEDS_ATTENTION" if not gate_passed else "COMPLETED"
        stop_reason = (
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
                "This is a read-only review. Proposed changes have not been applied.",
            ]
        )
        (result_dir / "report.md").write_text("\n".join(report_lines), encoding="utf-8")
        with zipfile.ZipFile(result_dir / "result.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for file in result_dir.iterdir():
                if file.is_file() and file.suffix != ".zip":
                    archive.write(file, file.name)
        self._transition(task_id, final_status, stop_reason=stop_reason)
        return final_status

    def _record_review(self, task_id: str, response, call: dict[str, object]) -> None:
        task, _project = self._load(task_id)
        response_path = Path(task.task_path) / "rounds" / "1" / "response.json"
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
            if stored is None or stored.status != "REVIEWING":
                raise HubError("invalid_state", "Task is no longer reviewing", 409)
            stored.current_round = 1
            stored.verdict = response.verdict
            stored.summary = response.summary
            stored.tokens_used = prompt_tokens + completion_tokens
            stored.cost_usd = cost
            stored.rounds.append(
                ReviewRound(
                    round_no=1,
                    verdict=response.verdict,
                    summary=response.summary,
                    findings_json=json.dumps(
                        [item.model_dump(mode="json") for item in response.findings],
                        ensure_ascii=False,
                    ),
                    request_path=str(
                        Path(task.task_path) / "rounds" / "1" / "context" / "request.zip"
                    ),
                    response_path=str(response_path),
                    patch_path=str(patch_path) if patch_path else None,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            )
            stored.model_calls.append(
                ModelCall(
                    round_no=1,
                    role="reviewer",
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
