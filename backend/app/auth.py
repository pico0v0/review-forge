"""Local bearer authentication and same-origin session cookies."""

import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request

from app.errors import HubError

COOKIE_NAME = "reviewforge_session"


def create_runtime_token(data_dir: Path) -> str:
    runtime = data_dir / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    token_path = runtime / "http_token"
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)
    return token


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HubError("origin_rejected", "Cross-site requests are not permitted", 403)
    if origin:
        supplied = urlsplit(origin)
        expected = urlsplit(str(request.base_url))
        if (supplied.scheme, supplied.netloc) != (expected.scheme, expected.netloc):
            raise HubError("origin_rejected", "Origin must match the Hub address", 403)


def require_auth(request: Request) -> None:
    check_origin(request)
    header = request.headers.get("authorization", "")
    candidate = header[7:] if header.startswith("Bearer ") else request.cookies.get(COOKIE_NAME)
    expected = request.app.state.http_token
    if not candidate or not secrets.compare_digest(candidate, expected):
        raise HubError("unauthorized", "A valid local Hub token is required", 401)
