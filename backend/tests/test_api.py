from fastapi.testclient import TestClient

from app.config import AppSettings, PathsSettings
from app.main import create_app


def test_health_endpoint_reports_healthy_database(tmp_path) -> None:
    app = create_app(AppSettings(paths=PathsSettings(data_dir=tmp_path / "data")))
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "reviewforge",
        "version": "0.1.0",
    }


def test_session_requires_local_token_and_sets_strict_cookie(tmp_path) -> None:
    app = create_app(AppSettings(paths=PathsSettings(data_dir=tmp_path / "data")))
    with TestClient(app) as client:
        assert client.get("/api/session").status_code == 401
        response = client.get(
            "/api/session", headers={"Authorization": f"Bearer {app.state.http_token}"}
        )
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert client.get("/api/session").status_code == 200
        assert (
            client.get("/api/session", headers={"Origin": "https://untrusted.example"}).status_code
            == 403
        )
