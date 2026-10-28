import asyncio
import json
import re
import subprocess
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
from app.projects import ProjectConfig
from app.providers import ModelGateway
from app.schemas import ReviewSubmitRequest


def make_review_app(tmp_path: Path, handler, **project_options):
    project = tmp_path / "project"
    project.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.invalid"],
    ):
        subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)
    (project / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(project), "commit", "-m", "initial"], check=True, capture_output=True
    )
    prompt = tmp_path / "reviewer.md"
    prompt.write_text("Review supplied code for bugs.", encoding="utf-8")
    settings = AppSettings(
        paths={"data_dir": tmp_path / "data"},
        security={"code_egress_acknowledged": True},
        models={
            "providers": [
                {"id": "fake", "base_url": "http://127.0.0.1:9999/v1", "models": [{"id": "review"}]}
            ],
            "roles": {"reviewer": {"provider": "fake", "model": "review"}},
        },
    )
    app = create_app(settings, config_dir=tmp_path / "config")
    options = {"test_command": "git diff --exit-code", **project_options}
    app.state.project_registry.register(
        ProjectConfig(
            id="demo",
            name="Demo",
            root=project,
            review_prompt=prompt.relative_to(tmp_path).as_posix(),
            **options,
        )
    )
    app.state.model_gateway = ModelGateway(
        settings.models.providers, transport=httpx.MockTransport(handler)
    )
    return app, project


def completion(
    request: httpx.Request, *, findings=None, verdict="approved", usage=None
) -> httpx.Response:
    payload = json.loads(request.content)
    content = payload["messages"][-1]["content"]
    task_id = re.search(r"task_id=(REV-[0-9-]+)", content)[1]
    body = {
        "schema_version": "2.0",
        "task_id": task_id,
        "round": 1,
        "verdict": verdict,
        "summary": "Review complete",
        "findings": findings or [],
    }
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(body)}}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50},
        },
    )


def wait_for_review(client: TestClient, task_id: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = client.get(f"/api/reviews/{task_id}").json()
        if result["status"] in {"COMPLETED", "NEEDS_ATTENTION", "CANCELLED"}:
            return result
        time.sleep(0.05)
    pytest.fail("Review did not finish within the test deadline")


def test_review_runs_in_worktree_and_archives_result(tmp_path) -> None:
    async def handler(request):
        return completion(request)

    app, root = make_review_app(tmp_path, handler)
    before = (root / "example.py").read_bytes()
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        accepted = client.post(
            "/api/reviews", json={"project_id": "demo", "scope": {"mode": "all"}}
        )
        assert accepted.status_code == 202, accepted.text
        task_id = accepted.json()["id"]
        result = wait_for_review(client, task_id)
        assert result["status"] == "COMPLETED", result
        assert result["stop_reason"] == "gate_passed"
        assert result["tokens_used"] == 150
        assert client.get(f"/api/reviews/{task_id}/rounds").json()[0]["verdict"] == "approved"
        assert client.get(f"/api/reviews/{task_id}/validation").json()["tests"]["passed"]
        assert client.get(f"/api/reviews/{task_id}/model-calls").json()[0]["provider"] == "fake"
        archive = client.get(f"/api/reviews/{task_id}/artifacts/result")
        assert archive.status_code == 200
        assert "Review complete" in client.get(f"/api/reviews/{task_id}/report").text
    assert (root / "example.py").read_bytes() == before
    with zipfile.ZipFile(tmp_path / "data/tasks" / task_id / "result/result.zip") as zipped:
        assert "review.json" in zipped.namelist()


def test_blocking_findings_stop_without_modifying_project(tmp_path) -> None:
    async def handler(request):
        return completion(
            request,
            verdict="changes_required",
            findings=[
                {
                    "id": "F1",
                    "severity": "P1",
                    "file": "example.py",
                    "line": 1,
                    "title": "Example defect",
                }
            ],
        )

    app, root = make_review_app(tmp_path, handler)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        accepted = client.post(
            "/api/reviews", json={"project_id": "demo", "scope": {"mode": "all"}}
        )
        assert accepted.status_code == 202, accepted.text
        task_id = accepted.json()["id"]
        result = wait_for_review(client, task_id)
        assert result["status"] == "NEEDS_ATTENTION", result
        assert result["stop_reason"] == "blocking_findings"
        assert len(client.get(f"/api/reviews/{task_id}/findings").json()) == 1
    assert (root / "example.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_cancel_inflight_review_and_reject_duplicate(tmp_path) -> None:
    async def handler(request):
        await asyncio.sleep(30)
        return completion(request)

    app, _root = make_review_app(tmp_path, handler)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        payload = {"project_id": "demo", "scope": {"mode": "all"}}
        accepted = client.post("/api/reviews", json=payload)
        assert accepted.status_code == 202, accepted.text
        task_id = accepted.json()["id"]
        duplicate = client.post("/api/reviews", json=payload)
        assert duplicate.status_code == 409
        result = client.post(f"/api/reviews/{task_id}/cancel")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "CANCELLED"


def test_cancel_during_validation_keeps_model_usage(tmp_path, monkeypatch) -> None:
    import app.task_service

    async def handler(request):
        return completion(request)

    async def blocking_check(*_args, **_kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(app.task_service, "run_check", blocking_check)
    app, _root = make_review_app(tmp_path, handler)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        accepted = client.post(
            "/api/reviews", json={"project_id": "demo", "scope": {"mode": "all"}}
        )
        task_id = accepted.json()["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = client.get(f"/api/reviews/{task_id}").json()
            if state["status"] == "VALIDATING":
                break
            time.sleep(0.02)
        else:
            pytest.fail("Review did not reach validation")

        cancelled = client.post(f"/api/reviews/{task_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"
        assert cancelled.json()["tokens_used"] == 150
        assert client.get(f"/api/reviews/{task_id}/model-calls").json()[0]["prompt_tokens"] == 100


def test_egress_requires_explicit_acknowledgement(tmp_path) -> None:
    async def handler(request):
        pytest.fail("No model call should occur")

    app, _root = make_review_app(tmp_path, handler)
    app.state.settings.security.code_egress_acknowledged = False
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        response = client.post(
            "/api/reviews", json={"project_id": "demo", "scope": {"mode": "all"}}
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    ("test_command", "expected"),
    [(None, "test_command_missing"), ("exit 1", "tests_failed")],
)
def test_missing_or_failed_tests_cannot_pass_gate(tmp_path, test_command, expected) -> None:
    async def handler(request):
        return completion(request)

    app, _root = make_review_app(tmp_path, handler, test_command=test_command)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        accepted = client.post(
            "/api/reviews", json={"project_id": "demo", "scope": {"mode": "all"}}
        )
        assert accepted.status_code == 202, accepted.text
        result = wait_for_review(client, accepted.json()["id"])
        assert result["status"] == "NEEDS_ATTENTION", result
        assert result["stop_reason"] == expected


def test_budget_stops_before_sending_model_request(tmp_path) -> None:
    async def handler(request):
        pytest.fail("Budget exhaustion must prevent the model request")

    app, _root = make_review_app(tmp_path, handler)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        accepted = client.post(
            "/api/reviews",
            json={
                "project_id": "demo",
                "scope": {"mode": "all"},
                "limits": {"max_total_tokens": 1},
            },
        )
        assert accepted.status_code == 202, accepted.text
        result = wait_for_review(client, accepted.json()["id"])
        assert result["stop_reason"] == "budget_tokens", result


def test_partial_provider_usage_is_marked_estimated(tmp_path) -> None:
    async def handler(request):
        return completion(request, usage={"prompt_tokens": 100})

    app, _root = make_review_app(tmp_path, handler)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        accepted = client.post(
            "/api/reviews", json={"project_id": "demo", "scope": {"mode": "all"}}
        )
        assert accepted.status_code == 202, accepted.text
        task_id = accepted.json()["id"]
        result = wait_for_review(client, task_id)
        assert result["status"] == "COMPLETED"
        assert result["tokens_used"] > 100
        assert client.get(f"/api/reviews/{task_id}/validation").json()["usage_estimated"]


def test_global_token_limit_cannot_be_overridden_and_skips_checks(tmp_path, monkeypatch) -> None:
    import app.task_service

    async def handler(request):
        return completion(request, usage={"prompt_tokens": 49_000, "completion_tokens": 2_000})

    async def unexpected_check(*_args, **_kwargs):
        pytest.fail("Checks must not run after the reported usage exceeds the global budget")

    monkeypatch.setattr(app.task_service, "run_check", unexpected_check)
    app, _root = make_review_app(tmp_path, handler)
    app.state.settings.limits.max_total_tokens = 50_000
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        accepted = client.post(
            "/api/reviews",
            json={
                "project_id": "demo",
                "scope": {"mode": "all"},
                "limits": {"max_total_tokens": 500_000},
            },
        )
        assert accepted.status_code == 202, accepted.text
        result = wait_for_review(client, accepted.json()["id"])
        assert result["stop_reason"] == "budget_tokens"
        assert result["tokens_used"] == 51_000


def test_invalid_model_output_stops_without_modifying_project(tmp_path) -> None:
    async def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "not JSON"}}]})

    app, root = make_review_app(tmp_path, handler)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        accepted = client.post(
            "/api/reviews", json={"project_id": "demo", "scope": {"mode": "all"}}
        )
        assert accepted.status_code == 202, accepted.text
        result = wait_for_review(client, accepted.json()["id"])
        assert result["stop_reason"] == "invalid_model_output", result
        assert result["status"] == "NEEDS_ATTENTION"
    assert (root / "example.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_restart_marks_unfinished_task_without_repeating_model_call(tmp_path) -> None:
    async def handler(request):
        pytest.fail("Recovery must not repeat a potentially paid model request")

    app, _root = make_review_app(tmp_path, handler)
    with TestClient(app):
        task = app.state.task_service.create(
            ReviewSubmitRequest(project_id="demo", scope={"mode": "all"})
        )
        first_token = app.state.http_token

    with TestClient(app) as client:
        assert first_token != app.state.http_token
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        result = client.get(f"/api/reviews/{task.id}").json()
        assert result["status"] == "NEEDS_ATTENTION"
        assert result["stop_reason"] == "interrupted"
        assert result["completed_at"].endswith("+00:00")
        events = client.get(f"/api/reviews/{task.id}/events").json()
        assert events[-1]["to_status"] == "NEEDS_ATTENTION"
        assert events[-1]["created_at"].endswith("+00:00")


def test_project_roundtrip_preserves_global_limit_inheritance(tmp_path) -> None:
    async def handler(request):
        pytest.fail("The inherited global budget must prevent the model call")

    app, _root = make_review_app(tmp_path, handler, limits={"max_rounds": 2})
    app.state.settings.limits.max_total_tokens = 1
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {app.state.http_token}"
        accepted = client.post(
            "/api/reviews", json={"project_id": "demo", "scope": {"mode": "all"}}
        )
        assert accepted.status_code == 202, accepted.text
        result = wait_for_review(client, accepted.json()["id"])
        assert result["max_rounds"] == 2
        assert result["stop_reason"] == "budget_tokens"
