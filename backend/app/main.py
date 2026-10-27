"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from filelock import FileLock, Timeout
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.auth import COOKIE_NAME, create_runtime_token, require_auth
from app.config import AppSettings, default_config_dir, load_settings
from app.db import database_url_for, make_engine, make_session_factory
from app.errors import HubError
from app.migrations import migrate
from app.projects import ProjectConfig, ProjectConfigError, ProjectRegistry
from app.providers import ModelGateway


def create_app(
    settings: AppSettings | None = None,
    *,
    config_dir: Path | None = None,
    database_url: str | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings(config_dir)
    resolved_config_dir = (config_dir or default_config_dir()).expanduser().resolve()
    resolved_engine = engine or make_engine(
        database_url or database_url_for(resolved_settings.paths.data_dir)
    )
    session_factory = make_session_factory(resolved_engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        runtime = resolved_settings.paths.data_dir / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        lock = FileLock(runtime / "hub.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError("A ReviewForge instance already owns this data directory") from exc
        try:
            migrate(resolved_engine)
            _app.state.http_token = create_runtime_token(resolved_settings.paths.data_dir)
            yield
        finally:
            resolved_engine.dispose()
            lock.release()

    app = FastAPI(
        title="ReviewForge",
        summary="Automated AI code review and repair for local Git projects",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.engine = resolved_engine
    app.state.session_factory = session_factory
    app.state.project_registry = ProjectRegistry(resolved_config_dir)
    app.state.model_gateway = ModelGateway(
        resolved_settings.models.providers,
        max_response_bytes=resolved_settings.security.max_response_bytes,
    )

    @app.exception_handler(HubError)
    async def hub_error_handler(_request: Request, exc: HubError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.get("/api/session", dependencies=[Depends(require_auth)], tags=["system"])
    def establish_session(response: Response) -> dict[str, bool]:
        response.set_cookie(
            COOKIE_NAME,
            app.state.http_token,
            httponly=True,
            samesite="strict",
            path="/api",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": True}

    @app.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        with resolved_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "service": "reviewforge", "version": app.version}

    @app.get("/api/projects", dependencies=[Depends(require_auth)], tags=["projects"])
    def list_projects() -> list[ProjectConfig]:
        try:
            return app.state.project_registry.list()
        except ProjectConfigError as exc:
            raise HubError("project_config_invalid", str(exc), 500) from exc

    @app.post(
        "/api/projects",
        status_code=201,
        dependencies=[Depends(require_auth)],
        tags=["projects"],
    )
    def register_project(project: ProjectConfig) -> ProjectConfig:
        try:
            return app.state.project_registry.register(project)
        except ProjectConfigError as exc:
            raise HubError("project_config_invalid", str(exc), 400) from exc

    @app.get(
        "/api/projects/{project_id}",
        dependencies=[Depends(require_auth)],
        tags=["projects"],
    )
    def get_project(project_id: str) -> ProjectConfig:
        try:
            project = app.state.project_registry.get(project_id)
        except ProjectConfigError as exc:
            raise HubError("project_config_invalid", str(exc), 500) from exc
        if project is None:
            raise HubError("project_not_found", "Project was not found", 404)
        return project

    @app.patch(
        "/api/projects/{project_id}",
        dependencies=[Depends(require_auth)],
        tags=["projects"],
    )
    def update_project(project_id: str, project: ProjectConfig) -> ProjectConfig:
        try:
            return app.state.project_registry.update(project_id, project)
        except ProjectConfigError as exc:
            if "was not found" in str(exc):
                raise HubError("project_not_found", str(exc), 404) from exc
            raise HubError("project_config_invalid", str(exc), 400) from exc

    @app.get("/api/models", dependencies=[Depends(require_auth)], tags=["models"])
    def list_models() -> list[dict[str, object]]:
        return app.state.model_gateway.list_models()

    @app.post("/api/models/test", dependencies=[Depends(require_auth)], tags=["models"])
    async def test_model(payload: dict[str, str]) -> dict[str, object]:
        provider = payload.get("provider")
        model = payload.get("model")
        if not provider or not model:
            raise HubError("invalid_request", "provider and model are required", 422)
        return await app.state.model_gateway.test(provider, model)

    return app


app = create_app()
