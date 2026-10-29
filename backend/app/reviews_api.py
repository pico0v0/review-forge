"""Authenticated review submission, progress, and artifact endpoints."""

import json
from datetime import UTC
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import select

from app.auth import require_auth
from app.errors import HubError
from app.models import ModelCall, ReviewRound, ReviewTask, TaskEvent
from app.schemas import ReviewRetryRequest, ReviewSubmitRequest
from app.task_service import task_response

router = APIRouter(prefix="/api/reviews", dependencies=[Depends(require_auth)], tags=["reviews"])


def find_task(request: Request, task_id: str) -> ReviewTask:
    with request.app.state.session_factory() as session:
        task = session.get(ReviewTask, task_id)
        if task is None:
            raise HubError("task_not_found", "Review task was not found", 404)
        session.expunge(task)
        return task


@router.post("", status_code=202)
async def submit_review(payload: ReviewSubmitRequest, request: Request) -> dict:
    # Creation has no await: project exclusivity and the daily sequence are serialized by the Hub.
    task = request.app.state.task_service.create(payload)
    request.app.state.review_worker.schedule(task.id)
    return task_response(task)


@router.get("")
def list_reviews(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    project_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    query = select(ReviewTask).order_by(ReviewTask.created_at.desc()).limit(limit).offset(offset)
    if project_id:
        query = query.where(ReviewTask.project_id == project_id)
    if status:
        query = query.where(ReviewTask.status == status)
    with request.app.state.session_factory() as session:
        return [task_response(task) for task in session.scalars(query)]


@router.get("/{task_id}")
def get_review(task_id: str, request: Request) -> dict:
    return task_response(find_task(request, task_id))


@router.get("/{task_id}/events")
def events(task_id: str, request: Request) -> list[dict]:
    find_task(request, task_id)
    with request.app.state.session_factory() as session:
        return [
            {
                "id": event.id,
                "event_type": event.event_type,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "message": event.message,
                "created_at": (
                    event.created_at.replace(tzinfo=UTC)
                    if event.created_at.tzinfo is None
                    else event.created_at.astimezone(UTC)
                ).isoformat(),
            }
            for event in session.scalars(
                select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id)
            )
        ]


@router.get("/{task_id}/rounds")
def rounds(task_id: str, request: Request) -> list[dict]:
    find_task(request, task_id)
    with request.app.state.session_factory() as session:
        return [
            {
                "round": item.round_no,
                "verdict": item.verdict,
                "summary": item.summary,
                "findings": json.loads(item.findings_json),
                "prompt_tokens": item.prompt_tokens,
                "completion_tokens": item.completion_tokens,
            }
            for item in session.scalars(
                select(ReviewRound)
                .where(ReviewRound.task_id == task_id)
                .order_by(ReviewRound.round_no)
            )
        ]


@router.get("/{task_id}/model-calls")
def model_calls(task_id: str, request: Request) -> list[dict]:
    find_task(request, task_id)
    with request.app.state.session_factory() as session:
        return [
            {
                "id": call.id,
                "round": call.round_no,
                "role": call.role,
                "provider": call.provider,
                "model": call.model,
                "prompt_tokens": call.prompt_tokens,
                "completion_tokens": call.completion_tokens,
                "cost_usd": call.cost_usd,
                "elapsed_ms": call.elapsed_ms,
            }
            for call in session.scalars(
                select(ModelCall).where(ModelCall.task_id == task_id).order_by(ModelCall.id)
            )
        ]


def result_path(task: ReviewTask, name: str) -> Path:
    if not task.task_path:
        raise HubError("result_not_ready", "Review artifacts are not available yet", 409)
    path = Path(task.task_path) / "result" / name
    if not path.is_file():
        raise HubError("result_not_ready", "Review artifacts are not available yet", 409)
    return path


@router.get("/{task_id}/report", response_class=PlainTextResponse)
def report(task_id: str, request: Request) -> str:
    return result_path(find_task(request, task_id), "report.md").read_text(encoding="utf-8")


@router.get("/{task_id}/findings")
def findings(task_id: str, request: Request) -> list[dict]:
    return json.loads(
        result_path(find_task(request, task_id), "findings.json").read_text(encoding="utf-8")
    )


@router.get("/{task_id}/validation")
def validation(task_id: str, request: Request) -> dict:
    return json.loads(
        result_path(find_task(request, task_id), "validation.json").read_text(encoding="utf-8")
    )


@router.get("/{task_id}/diff", response_class=PlainTextResponse)
def proposed_diff(task_id: str, request: Request) -> str:
    task = find_task(request, task_id)
    if not task.task_path:
        return ""
    path = Path(task.task_path) / "result/changes.patch"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


@router.get("/{task_id}/artifacts/result")
def result_archive(task_id: str, request: Request) -> FileResponse:
    return FileResponse(
        result_path(find_task(request, task_id), "result.zip"),
        media_type="application/zip",
        filename=f"{task_id}-result.zip",
    )


@router.post("/{task_id}/cancel")
async def cancel(task_id: str, request: Request) -> dict:
    find_task(request, task_id)
    await request.app.state.review_worker.cancel(task_id)
    return task_response(find_task(request, task_id))


@router.post("/{task_id}/retry", status_code=202)
async def retry(task_id: str, payload: ReviewRetryRequest, request: Request) -> dict:
    task = request.app.state.task_service.retry(task_id, payload)
    request.app.state.review_worker.schedule(task.id)
    return task_response(task)


@router.post("/{task_id}/rollback")
def rollback(task_id: str, request: Request) -> dict:
    request.app.state.review_worker.rollback(task_id)
    return task_response(find_task(request, task_id))
