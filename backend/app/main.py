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
from app.config import AppSettings, load_settings
from app.db import database_url_for, make_engine, make_session_factory
from app.errors import HubError
from app.migrations import migrate


def create_app(
    settings: AppSettings | None = None,
    *,
    config_dir: Path | None = None,
    database_url: str | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings(config_dir)
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

    return app


app = create_app()
